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
from production_cost import PROCESSES, estimate_production_cost

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
    def capture_screenshot(self, output_path: str | None = None) -> str:
        """Capture a screenshot of the current part's viewport and save it
        to disk, returning the saved file's path.

        If `output_path` is None, a path in the system temp directory is
        chosen automatically. Used by report/generate_report.py to embed
        a picture of the part in the manufacturing readiness report.
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

    def compare_materials(
        self, material_names: list[str], quantity: int, manufacturing_process: str
    ) -> list[dict]:
        """Compare cost and carbon footprint for the current part's REAL
        geometry as if it were made from each material in `material_names`
        -- a "what if I used a different material" comparison. Never
        touches the actual CAD file: only reads geometry
        (volume/face_count/bend_count/bounding box) via the existing
        get_mass_properties()/get_face_count()/get_bend_count()/
        get_bounding_box_mm() methods, then substitutes each hypothetical
        material's real density/cost/carbon figures from materials.csv on
        top of that unchanged geometry.

        Concrete (not abstract) and implemented once here, not per
        adapter, since it's built entirely out of methods every
        CadAdapter subclass already provides -- same reasoning as
        estimate_carbon() above.

        Each hypothetical material's mass is volume_m3 * that material's
        own density_kg_m3 -- explicitly NOT the mass of the part's
        currently-assigned real material, since a different material at
        the same volume weighs a different amount. Production cost (see
        production_cost.py) depends only on geometry, not material choice,
        so it comes out identical across every material in the
        comparison -- only material_cost and carbon differ. This is
        expected behavior, not a bug, and is called out explicitly by the
        MCP tool wrapping this (see mcp_server.py's compare_materials).

        Raises ValueError if `manufacturing_process` isn't one of
        production_cost.PROCESSES. Never guesses a material's data: any
        material not found in materials.csv, or found but missing a
        density/cost/carbon cell, gets found=False (or a specific
        *_message field) instead of a silently wrong number.
        """
        if manufacturing_process not in PROCESSES:
            raise ValueError(
                f"manufacturing_process must be one of {list(PROCESSES)}"
            )

        materials = _get_materials()
        if materials is None:
            return [
                {
                    "material_name_queried": name,
                    "found": False,
                    "message": (
                        "materials.csv is unavailable"
                        + (f" ({_materials_load_error})" if _materials_load_error else "")
                    ),
                }
                for name in material_names
            ]

        mass_properties = self.get_mass_properties()
        volume_m3 = mass_properties.get("volume_m3")
        if volume_m3 is None:
            return [
                {
                    "material_name_queried": name,
                    "found": False,
                    "message": (
                        "Could not read volume for the current part (no "
                        "solid geometry?) -- comparison cannot be computed."
                    ),
                }
                for name in material_names
            ]

        face_count = self.get_face_count()
        bend_count = self.get_bend_count()
        bounding_box_mm = self.get_bounding_box_mm()

        results = []
        for name in material_names:
            lookup = get_material_cost_and_carbon(name, materials)
            if not lookup["found"]:
                results.append(
                    {"material_name_queried": name, "found": False, "message": lookup["message"]}
                )
                continue

            density = lookup["density_kg_m3"]
            if density is None:
                results.append(
                    {
                        "material_name_queried": name,
                        "found": False,
                        "material_matched": lookup["matched_from"],
                        "is_fuzzy_match": lookup["is_fuzzy_match"],
                        "message": (
                            f"'{lookup['matched_from']}' was matched in "
                            "materials.csv but its density_kg_m3 cell is "
                            "blank -- cannot compute a hypothetical mass."
                        ),
                    }
                )
                continue

            hypothetical_mass_kg = volume_m3 * density
            entry = {
                "material_name_queried": name,
                "found": True,
                "material_matched": lookup["matched_from"],
                "is_fuzzy_match": lookup["is_fuzzy_match"],
                "density_kg_m3": density,
                "hypothetical_mass_kg": round(hypothetical_mass_kg, 4),
            }

            cost_per_kg = lookup["cost_per_kg"]
            if cost_per_kg is None:
                entry["material_cost_inr"] = None
                entry["production_cost_inr"] = None
                entry["total_cost_per_unit_inr"] = None
                entry["total_cost_for_quantity_inr"] = None
                entry["cost_message"] = (
                    f"'{lookup['matched_from']}' has no cost_per_kg in "
                    "materials.csv -- cost cannot be estimated."
                )
            else:
                material_cost_inr = hypothetical_mass_kg * cost_per_kg
                production_result = estimate_production_cost(
                    manufacturing_process,
                    feature_count=face_count,
                    volume_m3=volume_m3,
                    order_quantity=quantity,
                    bounding_box_mm=bounding_box_mm,
                    bend_count=bend_count,
                )
                total_per_unit = material_cost_inr + production_result.production_cost_inr
                entry["material_cost_inr"] = round(material_cost_inr, 2)
                entry["production_cost_inr"] = production_result.production_cost_inr
                entry["total_cost_per_unit_inr"] = round(total_per_unit, 2)
                entry["total_cost_for_quantity_inr"] = round(total_per_unit * quantity, 2)

            carbon_factor = lookup["carbon_factor_kg_co2_per_kg"]
            if carbon_factor is None:
                entry["carbon_factor_kg_co2_per_kg"] = None
                entry["estimated_kg_co2e"] = None
                entry["carbon_message"] = (
                    f"'{lookup['matched_from']}' has no "
                    "carbon_factor_kg_co2_per_kg in materials.csv -- carbon "
                    "cannot be estimated."
                )
            else:
                entry["carbon_factor_kg_co2_per_kg"] = carbon_factor
                entry["estimated_kg_co2e"] = round(hypothetical_mass_kg * carbon_factor, 3)

            results.append(entry)

        return results
