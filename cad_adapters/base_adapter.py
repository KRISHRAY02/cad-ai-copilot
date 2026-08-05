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

_materials_cache = None
_materials_load_error = None
_materials_loaded = False


def _get_materials():
    """Lazily load and cache materials.csv (via materials_db.load_materials).

    Deferred until first use, same reasoning as why SolidWorksAdapter
    doesn't connect() at import time: a missing/broken materials.csv
    shouldn't prevent importing this module, only estimate_carbon()
    calls that actually need it.
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

    @abstractmethod
    def run_dfm_check(self) -> list[dict]:
        """Run the four geometry-driven DFM checks: hole geometry, wall
        thickness, draft angle, and dimension tolerance -- see
        cad_adapters/dfm_checks.py for thresholds and the real
        manufacturing problem each check protects against.

        Returns a combined list of finding dicts (see
        dfm_checks.make_finding), each either a flagged issue, a pass, or
        "not_applicable" when there's no real feature/dimension data to
        check against. Never returns an estimated or guessed value where
        real data isn't available.
        """
        raise NotImplementedError

    @abstractmethod
    def get_face_count(self) -> int:
        """Return the total number of faces across the part's solid bodies.

        Used as a geometric complexity feature for the ML cost model (see
        cost_model/) -- more faces generally means more machining
        operations.
        """
        raise NotImplementedError

    @abstractmethod
    def get_bend_count(self) -> int:
        """Return the number of Sheet Metal bend features in the current
        part's feature tree.

        Used by the Sheet Metal production cost formula (see
        production_cost.py) -- each bend is a separate press-brake
        operation. Returns 0 for parts with no Sheet Metal bend features
        (including parts made with any other manufacturing process), not
        an error -- a part simply having zero bends is a valid real
        answer, distinct from bend data being unavailable.
        """
        raise NotImplementedError

    @abstractmethod
    def get_bounding_box_mm(self) -> tuple[float, float, float]:
        """Return the current part's axis-aligned bounding box dimensions
        (x, y, z), in millimeters.

        Used by the Sheet Metal production cost formula (see
        production_cost.py) to approximate cutting length from the
        part's footprint perimeter -- a documented approximation, not a
        true flat-pattern measurement.
        """
        raise NotImplementedError

    # estimate_carbon() is concrete, not abstract: it's built entirely out
    # of get_mass()/get_material() plus materials.csv, so every subclass
    # gets it for free rather than having to reimplement the same
    # materials_db lookup logic. (estimate_cost() used to live here as a
    # similarly simple formula, but has been replaced by the Random
    # Forest model in cost_model/ -- see mcp_server.py's estimate_cost
    # tool, which now calls cost_model.predict.predict_cost() directly.)

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
