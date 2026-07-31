"""Abstract base class defining the common contract for all CAD adapters.

Every CAD platform integration (SolidWorks, Fusion 360, mock/test data,
etc.) must subclass CadAdapter and implement its methods. The AI
orchestrator, MCP server, and UI layers are written entirely against this
interface and never import a concrete adapter directly, so a new CAD
platform can be added later by writing one new adapter class with no
changes required anywhere else in the project.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from materials_db import get_material_cost_and_carbon, load_materials

# Flat per-unit machining/setup fee, in INR. Same rough, illustrative
# figure used throughout this project -- not a real manufacturing quote.
_MACHINING_BASE_FEE_INR = 2150.0

# Batch-size discount tiers for estimate_cost()'s quantity multiplier:
# (minimum quantity, multiplier applied to material+process cost per unit).
# Modeled loosely on how per-unit setup/overhead cost amortizes better
# across a larger batch -- a real quoting simplification, not a precise
# manufacturing cost model.
_QUANTITY_DISCOUNT_TIERS = [
    (100, 0.80),
    (10, 0.90),
    (1, 1.00),
]

_materials_cache = None
_materials_load_error = None
_materials_loaded = False


def _get_materials():
    """Lazily load and cache materials.csv (via materials_db.load_materials).

    Deferred until first use, same reasoning as why SolidWorksAdapter
    doesn't connect() at import time: a missing/broken materials.csv
    shouldn't prevent importing this module, only estimate_cost()/
    estimate_carbon() calls that actually need it.
    """
    global _materials_cache, _materials_load_error, _materials_loaded
    if not _materials_loaded:
        try:
            _materials_cache = load_materials()
        except (FileNotFoundError, ValueError) as e:
            _materials_cache = None
            _materials_load_error = str(e)
        _materials_loaded = True
    return _materials_cache


def _quantity_multiplier(quantity: int) -> float:
    for threshold, multiplier in _QUANTITY_DISCOUNT_TIERS:
        if quantity >= threshold:
            return multiplier
    return 1.0


@dataclass
class PartInfo:
    """Basic identifying information about the currently open part/model."""

    name: str
    file_path: str
    part_type: str  # e.g. "part", "assembly", "drawing"
    units: str  # e.g. "mm", "in"


@dataclass
class MaterialInfo:
    """Material assigned to the current part."""

    name: str
    density_kg_m3: float
    category: str = ""  # e.g. "Steel", "Aluminum", "Plastic"


@dataclass
class Feature:
    """A single feature in the model's feature tree (e.g. Extrude, Fillet)."""

    name: str
    feature_type: str
    suppressed: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)


class CadAdapter(ABC):
    """Abstract interface that all CAD platform adapters must implement.

    Concrete subclasses (SolidWorksAdapter, MockAdapter, ...) provide the
    platform-specific logic behind these methods. Callers should depend
    only on this interface, never on a specific subclass, so the
    application can be extended to new CAD platforms without touching
    the AI/MCP/UI layers.
    """

    @abstractmethod
    def connect(self) -> bool:
        """Establish a connection to the CAD application.

        Returns True if the connection was established successfully,
        False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def get_current_part_info(self) -> PartInfo:
        """Return basic identifying information about the active part."""
        raise NotImplementedError

    @abstractmethod
    def get_mass(self) -> float:
        """Return the mass of the current part, in kilograms."""
        raise NotImplementedError

    @abstractmethod
    def get_mass_properties(self) -> dict:
        """Return mass, volume, and surface area of the current part.

        Keys: "mass_kg", "volume_m3", "surface_area_m2". Any property
        that can't be computed (e.g. an empty part) is returned as None
        rather than raising.
        """
        raise NotImplementedError

    @abstractmethod
    def get_material(self) -> MaterialInfo:
        """Return the material assigned to the current part."""
        raise NotImplementedError

    @abstractmethod
    def get_features(self) -> list[Feature]:
        """Return the list of features in the current part's feature tree."""
        raise NotImplementedError

    # estimate_cost() and estimate_carbon() are concrete, not abstract:
    # they're built entirely out of get_mass()/get_material() plus
    # materials.csv, so every subclass gets them for free rather than
    # having to reimplement the same materials_db lookup logic.

    def estimate_cost(self, quantity: int = 1) -> dict:
        """Estimate the material + machining cost to produce `quantity`
        units of the current part, in INR.

        Looks up the current material's cost_per_kg in materials.csv (via
        materials_db.get_material_cost_and_carbon(), which tries an exact
        name match first and falls back to a fuzzy match). Returns
        found=False with an explanatory message instead of guessing if
        materials.csv is unavailable, the material isn't in it, or its
        cost_per_kg cell is blank.
        """
        if quantity < 1:
            raise ValueError("quantity must be at least 1")

        materials = _get_materials()
        if materials is None:
            return {
                "found": False,
                "message": (
                    "materials.csv is unavailable"
                    + (f" ({_materials_load_error})" if _materials_load_error else "")
                    + " -- cost cannot be estimated."
                ),
            }

        material = self.get_material()
        lookup = get_material_cost_and_carbon(material.name, materials)
        if not lookup["found"]:
            return {"found": False, "message": lookup["message"]}

        cost_per_kg = lookup["cost_per_kg"]
        if cost_per_kg is None:
            return {
                "found": False,
                "message": (
                    f"'{lookup['matched_from']}' was matched in materials.csv "
                    f"but its cost_per_kg cell is blank -- please fill it in."
                ),
            }

        # Cost formula, deliberately simple and explainable:
        #   material_cost_per_unit = mass (kg) * cost_per_kg (materials.csv)
        #   + a flat per-unit machining/process fee
        #   x a quantity multiplier that discounts per-unit cost at larger
        #     batch sizes (setup/overhead amortizes better), see
        #     _QUANTITY_DISCOUNT_TIERS above.
        # Not a real manufacturing quote -- a rough, explainable estimate.
        mass_kg = self.get_mass()
        material_cost_per_unit = mass_kg * cost_per_kg
        multiplier = _quantity_multiplier(quantity)
        cost_per_unit = (material_cost_per_unit + _MACHINING_BASE_FEE_INR) * multiplier
        total_cost = cost_per_unit * quantity

        return {
            "found": True,
            "quantity": quantity,
            "cost_per_unit_inr": round(cost_per_unit, 2),
            "estimated_total_cost_inr": round(total_cost, 2),
            "material_matched": lookup["matched_from"],
            "is_fuzzy_match": lookup["is_fuzzy_match"],
            "assumptions": (
                f"Rs {cost_per_kg}/kg for '{lookup['matched_from']}'"
                + (" (fuzzy match)" if lookup["is_fuzzy_match"] else "")
                + f", + Rs {_MACHINING_BASE_FEE_INR} flat machining fee/unit, "
                f"x{multiplier} quantity multiplier for a batch of {quantity}"
            ),
        }

    def estimate_carbon(self) -> dict:
        """Estimate the embodied carbon (cradle-to-gate) of the current
        part, in kg CO2e.

        Looks up the current material's carbon_factor_kg_co2_per_kg in
        materials.csv the same way estimate_cost() looks up cost_per_kg.
        Returns found=False with an explanatory message instead of
        guessing if materials.csv is unavailable, the material isn't in
        it, or its carbon_factor cell is blank.
        """
        materials = _get_materials()
        if materials is None:
            return {
                "found": False,
                "message": (
                    "materials.csv is unavailable"
                    + (f" ({_materials_load_error})" if _materials_load_error else "")
                    + " -- carbon footprint cannot be estimated."
                ),
            }

        material = self.get_material()
        lookup = get_material_cost_and_carbon(material.name, materials)
        if not lookup["found"]:
            return {"found": False, "message": lookup["message"]}

        carbon_factor = lookup["carbon_factor_kg_co2_per_kg"]
        if carbon_factor is None:
            return {
                "found": False,
                "message": (
                    f"'{lookup['matched_from']}' was matched in materials.csv "
                    f"but its carbon_factor_kg_co2_per_kg cell is blank -- "
                    f"please fill it in."
                ),
            }

        mass_kg = self.get_mass()
        total_co2e = mass_kg * carbon_factor

        return {
            "found": True,
            "estimated_kg_co2e": round(total_co2e, 3),
            "carbon_factor_kg_co2_per_kg": carbon_factor,
            "material_matched": lookup["matched_from"],
            "is_fuzzy_match": lookup["is_fuzzy_match"],
            "assumptions": (
                f"{carbon_factor} kg CO2e/kg for '{lookup['matched_from']}'"
                + (" (fuzzy match)" if lookup["is_fuzzy_match"] else "")
                + ", cradle-to-gate only (excludes machining energy and "
                "transport)"
            ),
        }
