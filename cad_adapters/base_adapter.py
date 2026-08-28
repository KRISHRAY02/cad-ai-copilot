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

from hardware_db import classify_component, load_hardware
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


@dataclass
class AssemblyComponent:
    """One row of an assembly's flattened, rolled-up component list --
    one entry per unique (part file, referenced configuration) combination
    found anywhere in the assembly tree, aggregated across the WHOLE tree
    (a part used both at the top level and inside a sub-assembly counts as
    one row with a combined quantity). This is what get_assembly_bom()
    needs for accurate cost/mass totals: a real assembly BOM rollup counts
    total quantity of a part needed to build one assembly, regardless of
    how deeply it's nested.

    `parent_assembly`/`level` describe where this component was *first*
    encountered during the tree walk -- kept for the indented BOM export
    (see bom_export.py), which groups rows by immediate parent for
    display. If the same part+configuration is reused under more than one
    parent, the indented view shows it once, under its first occurrence,
    while `quantity` here still correctly totals every instance across the
    whole tree. This is a deliberate simplification (a fully faithful
    indented BOM would repeat a shared part under every parent it appears
    under) rather than an oversight.

    Suppressed components and reference-only components (envelopes) are
    never turned into an AssemblyComponent at all -- skipped during the
    tree walk, not filtered out afterward.
    """

    part_name: str
    file_path: str
    configuration: str
    quantity: int
    parent_assembly: str | None
    level: int
    mass_kg: float | None
    volume_m3: float | None
    material: MaterialInfo | None
    face_count: int | None
    bend_count: int | None
    bounding_box_mm: tuple[float, float, float] | None


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
    def get_hole_features(self) -> list[dict]:
        """Return one dict per Hole Wizard hole in the current part's
        feature tree: {"feature_name": str, "diameter_mm": float | None,
        "depth_mm": float | None}.

        Same underlying hole data run_dfm_check()'s hole check reads (see
        _check_holes in solidworks_adapter.py) -- diameter/depth are real
        feature parameters, not estimates. depth_mm is None for a
        "through all" hole (its real depth depends on part thickness, not
        a fixed feature parameter) or when the Hole Wizard type doesn't
        expose a single depth/diameter value; callers must not guess a
        value for these. Used by production_cost.py's CNC Machining
        formula for per-hole machining time attribution. Returns an empty
        list for a part with no Hole Wizard holes -- a real, valid answer,
        not an error.
        """
        raise NotImplementedError

    @abstractmethod
    def highlight_feature(self, feature_id: str) -> dict:
        """Select/highlight the named feature so it visually highlights in
        the CAD viewport, the same way clicking it in the feature tree
        would.

        `feature_id` is a feature name as returned by get_features() or
        get_hole_features() (e.g. "Hole3"). Returns {"success": bool,
        "message": str}. Used by highlight_cost_driver() to visually point
        out the specific feature identified as the top cost driver.
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

    @abstractmethod
    def is_assembly(self) -> bool:
        """True if the currently active/open document is an assembly
        (possibly containing nested sub-assemblies), not a single part.

        Used by generate_manufacturing_report() to decide whether to
        generate an assembly-level report (screenshot, BOM, cost/mass
        rollup, cost-driver ranking) or a single-part report.
        """
        raise NotImplementedError

    @abstractmethod
    def get_assembly_components(self) -> list[AssemblyComponent]:
        """Walk the currently open assembly's full component tree
        (including nested sub-assemblies) and return one AssemblyComponent
        per unique (part file, referenced configuration) combination found
        anywhere in it, with quantity aggregated across the whole tree.

        Suppressed components and reference-only/envelope components are
        skipped entirely -- never returned as a row. A part referenced
        under two different configurations (e.g. the same bracket in
        "Default" and "Heavy Duty") is returned as two separate rows,
        since different configurations of a part can have different
        mass/material.

        Each returned AssemblyComponent's mass/material/face_count/
        bend_count/bounding_box_mm are read from that specific part file
        + configuration the same way get_mass()/get_material()/
        get_face_count()/get_bend_count()/get_bounding_box_mm() read them
        for a standalone open part -- any of these that can't be read
        (e.g. an unresolved/missing reference) is None rather than a
        guessed value.

        Raises if the currently open document is not an assembly -- call
        is_assembly() first.
        """
        raise NotImplementedError

    def get_assembly_bom(self, manufacturing_process: str, quantity: int) -> dict:
        """Roll up the currently open assembly's components into a
        structured Bill of Materials: one row per unique (part,
        configuration), each priced either as "Make" (material +
        production cost, same formulas as estimate_cost()) or "Buy"
        (a real purchased unit price from standard_hardware.csv) -- see
        hardware_db.classify_component() for the Make/Buy rule -- plus
        assembly-level totals (unique part count, total instance count,
        total mass, total cost for one assembly and for `quantity`
        assemblies) and a list of components with missing pricing data.

        Concrete (not abstract): built entirely out of
        get_assembly_components() plus the same materials_db/
        production_cost/hardware_db machinery estimate_cost() and
        compare_materials() already use, so every subclass gets it for
        free -- same reasoning as estimate_carbon() and compare_materials()
        above.

        Raises ValueError if `manufacturing_process` isn't one of
        production_cost.PROCESSES. Never guesses: a Make component with an
        unresolved material, or a Buy component with no
        standard_hardware.csv price, gets None cost/mass fields and is
        listed in the returned "missing_data" list instead of a wrong
        number silently feeding into the totals.
        """
        if manufacturing_process not in PROCESSES:
            raise ValueError(
                f"manufacturing_process must be one of {list(PROCESSES)}"
            )

        materials = _get_materials()
        try:
            hardware = load_hardware()
        except (FileNotFoundError, ValueError):
            hardware = {}

        components = self.get_assembly_components()

        rows = []
        missing_data = []
        total_mass_kg = 0.0
        total_cost_one_assembly_inr = 0.0
        total_instance_count = 0

        for component in components:
            total_instance_count += component.quantity

            classification_result = classify_component(
                component.part_name, component.file_path, hardware
            )
            classification = classification_result["classification"]

            unit_mass_kg = component.mass_kg
            unit_cost_inr = None
            material_name = component.material.name if component.material else None
            row_missing_reason = None

            if classification == "Buy":
                unit_cost_inr = classification_result["unit_cost_inr"]

            elif classification == "Buy - price not available":
                row_missing_reason = (
                    f"'{component.part_name}' looks like purchased hardware "
                    "(SOLIDWORKS Toolbox path) but has no entry in "
                    "standard_hardware.csv -- add a row for it."
                )

            else:  # "Make"
                if materials is None:
                    row_missing_reason = (
                        "materials.csv is unavailable"
                        + (f" ({_materials_load_error})" if _materials_load_error else "")
                    )
                elif component.material is None:
                    row_missing_reason = (
                        f"Could not read material for '{component.part_name}' "
                        f"(configuration '{component.configuration}')."
                    )
                else:
                    material_lookup = get_material_cost_and_carbon(
                        component.material.name, materials
                    )
                    if not material_lookup["found"] or material_lookup["cost_per_kg"] is None:
                        row_missing_reason = (
                            f"Material '{component.material.name}' "
                            "(used by "
                            f"'{component.part_name}') has no cost_per_kg in "
                            "materials.csv."
                        )
                        material_name = component.material.name
                    elif unit_mass_kg is None or component.volume_m3 is None:
                        row_missing_reason = (
                            f"Could not read geometry for '{component.part_name}' "
                            f"(configuration '{component.configuration}')."
                        )
                        material_name = material_lookup["matched_from"]
                    else:
                        material_name = material_lookup["matched_from"]
                        material_cost_per_unit = unit_mass_kg * material_lookup["cost_per_kg"]
                        production_result = estimate_production_cost(
                            manufacturing_process,
                            feature_count=component.face_count or 0,
                            volume_m3=component.volume_m3,
                            order_quantity=quantity,
                            bounding_box_mm=component.bounding_box_mm or (0.0, 0.0, 0.0),
                            bend_count=component.bend_count or 0,
                        )
                        unit_cost_inr = round(
                            material_cost_per_unit + production_result.production_cost_inr, 2
                        )

            total_mass_for_row = (
                round(unit_mass_kg * component.quantity, 4) if unit_mass_kg is not None else None
            )
            total_cost_for_row = (
                round(unit_cost_inr * component.quantity, 2) if unit_cost_inr is not None else None
            )

            if total_mass_for_row is not None:
                total_mass_kg += total_mass_for_row
            if total_cost_for_row is not None:
                total_cost_one_assembly_inr += total_cost_for_row

            row = {
                "part_name": component.part_name,
                "configuration": component.configuration,
                "classification": classification,
                "material": material_name,
                "quantity_per_assembly": component.quantity,
                "unit_mass_kg": unit_mass_kg,
                "total_mass_kg": total_mass_for_row,
                "unit_cost_inr": unit_cost_inr,
                "total_cost_inr": total_cost_for_row,
                "parent_assembly": component.parent_assembly,
                "level": component.level,
            }
            rows.append(row)

            if row_missing_reason:
                missing_data.append(
                    {
                        "part_name": component.part_name,
                        "configuration": component.configuration,
                        "reason": row_missing_reason,
                    }
                )

        return {
            "manufacturing_process": manufacturing_process,
            "quantity": quantity,
            "bom": rows,
            "totals": {
                "unique_part_count": len(rows),
                "total_instance_count": total_instance_count,
                "total_assembly_mass_kg": round(total_mass_kg, 4),
                "total_assembly_cost_one_unit_inr": round(total_cost_one_assembly_inr, 2),
                "total_assembly_cost_for_quantity_inr": round(
                    total_cost_one_assembly_inr * quantity, 2
                ),
            },
            "missing_data": missing_data,
        }

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

    def get_cost_drivers(self, manufacturing_process: str, quantity: int) -> dict:
        """Break the current part's production cost down into a ranked
        list of cost components (highest cost first), instead of a single
        total -- so a chat answer (or highlight_cost_driver()) can say
        *which* feature or cost element is actually driving the price.

        Concrete (not abstract): built entirely out of get_hole_features()
        / get_features() / get_bend_count() / get_bounding_box_mm() /
        get_mass_properties(), same reasoning as compare_materials() and
        estimate_carbon() above.

        For CNC Machining: every Hole Wizard hole becomes its own ranked
        entry (is_individual_feature=True, feature_name set) using the
        per-hole time estimate from production_cost.py; every other
        feature is lumped into one "Other Features" bucket entry
        (is_individual_feature=False) since those aren't individually
        modeled -- see production_cost.estimate_cnc_machining_cost's
        docstring for why depth:diameter ratio drives each hole's cost.
        Setup time is its own fixed-cost entry, also not tied to a
        feature.

        For Sheet Metal: two entries, "Cutting" and "Bending" (both
        already independently real numbers, not lumped).

        For Injection Molding: two entries, "Machine Cycle Cost" and
        "Tooling Amortization" (also already independently real).

        Raises ValueError if `manufacturing_process` isn't one of
        production_cost.PROCESSES. Returns found=False with a message
        (never guesses) if required geometry can't be read (e.g. no solid
        body for Injection Molding's volume).
        """
        if manufacturing_process not in PROCESSES:
            raise ValueError(
                f"manufacturing_process must be one of {list(PROCESSES)}"
            )

        components: list[dict] = []

        if manufacturing_process == "CNC Machining":
            hole_features = self.get_hole_features()
            hole_names = {h["feature_name"] for h in hole_features}
            other_feature_count = sum(
                1
                for f in self.get_features()
                if not f.suppressed and f.name not in hole_names
            )
            result = estimate_production_cost(
                manufacturing_process,
                hole_features=hole_features,
                other_feature_count=other_feature_count,
            )
            breakdown = result.breakdown

            for hole in breakdown["holes"]:
                flat_note = " (flat estimate -- missing depth/diameter data)" if hole["is_flat_estimate"] else ""
                components.append({
                    "label": f"Hole '{hole['feature_name']}'{flat_note}",
                    "cost_per_unit_inr": hole["estimated_cost_inr"],
                    "is_individual_feature": True,
                    "feature_name": hole["feature_name"],
                    "details": {
                        "diameter_mm": hole["diameter_mm"],
                        "depth_mm": hole["depth_mm"],
                        "depth_to_diameter_ratio": hole["depth_to_diameter_ratio"],
                        "is_flat_estimate": hole["is_flat_estimate"],
                    },
                })

            other = breakdown["other_features"]
            components.append({
                "label": other["label"],
                "cost_per_unit_inr": other["estimated_cost_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {"feature_count": other["count"]},
            })

            setup = breakdown["setup"]
            components.append({
                "label": setup["label"],
                "cost_per_unit_inr": setup["estimated_cost_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {},
            })

        elif manufacturing_process == "Sheet Metal":
            bounding_box_mm = self.get_bounding_box_mm()
            bend_count = self.get_bend_count()
            result = estimate_production_cost(
                manufacturing_process, bounding_box_mm=bounding_box_mm, bend_count=bend_count
            )
            breakdown = result.breakdown
            components.append({
                "label": "Cutting",
                "cost_per_unit_inr": breakdown["cutting_cost_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {"cutting_length_mm_approx": breakdown["cutting_length_mm_approx"]},
            })
            components.append({
                "label": "Bending",
                "cost_per_unit_inr": breakdown["bending_cost_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {"bend_count": breakdown["bend_count"]},
            })

        else:  # Injection Molding
            mass_properties = self.get_mass_properties()
            volume_m3 = mass_properties.get("volume_m3")
            if volume_m3 is None:
                return {
                    "found": False,
                    "message": (
                        "Could not read volume for the current part (no "
                        "solid geometry?) -- cost drivers cannot be computed."
                    ),
                }
            result = estimate_production_cost(
                manufacturing_process, volume_m3=volume_m3, order_quantity=quantity
            )
            breakdown = result.breakdown
            components.append({
                "label": "Machine Cycle Cost",
                "cost_per_unit_inr": breakdown["machine_cost_per_shot_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {"cycle_time_seconds": breakdown["cycle_time_seconds"]},
            })
            components.append({
                "label": "Tooling Amortization",
                "cost_per_unit_inr": breakdown["tooling_cost_per_unit_inr"],
                "is_individual_feature": False,
                "feature_name": None,
                "details": {"order_quantity_used_for_amortization": breakdown["order_quantity_used_for_amortization"]},
            })

        production_cost_inr = result.production_cost_inr
        for component in components:
            component["percentage_of_total_production_cost"] = (
                round(component["cost_per_unit_inr"] / production_cost_inr * 100, 2)
                if production_cost_inr
                else 0.0
            )

        components.sort(key=lambda c: c["cost_per_unit_inr"], reverse=True)

        return {
            "found": True,
            "manufacturing_process": manufacturing_process,
            "quantity": quantity,
            "production_cost_per_unit_inr": production_cost_inr,
            "cost_drivers": components,
            "assumptions": result.assumptions,
        }

    def highlight_cost_driver(self, manufacturing_process: str, quantity: int) -> dict:
        """Identify the single highest-cost driver (via get_cost_drivers())
        and, if it's an individual feature (e.g. a specific hole), select
        it in the CAD viewport via highlight_feature() -- so the AI can
        point the user directly at the offending feature instead of just
        naming it.

        If the top driver is a flat, non-individual bucket (e.g. "Other
        Features" or "Setup Time" for CNC, or either Sheet Metal/
        Injection Molding component, none of which correspond to one
        selectable feature), this says so explicitly instead of silently
        skipping the highlight or picking an arbitrary feature to select.

        Returns {"found": bool, "message": str, "highlighted": bool,
        "top_driver": dict | None}. The "message" field is meant to be
        relayed directly to the user in chat.
        """
        drivers_result = self.get_cost_drivers(manufacturing_process, quantity)
        if not drivers_result["found"]:
            return {"found": False, "message": drivers_result["message"], "highlighted": False, "top_driver": None}

        cost_drivers = drivers_result["cost_drivers"]
        if not cost_drivers:
            return {
                "found": False,
                "message": "No cost drivers were computed for the current part.",
                "highlighted": False,
                "top_driver": None,
            }

        top = cost_drivers[0]

        if not top["is_individual_feature"]:
            return {
                "found": True,
                "message": (
                    f"The top cost driver is '{top['label']}', accounting for "
                    f"~{top['percentage_of_total_production_cost']:.0f}% of the "
                    f"production cost (Rs {top['cost_per_unit_inr']:.2f}/unit). "
                    "This is a flat/aggregate estimate, not a single feature, "
                    "so there's nothing specific to highlight in the viewport."
                ),
                "highlighted": False,
                "top_driver": top,
            }

        highlight_result = self.highlight_feature(top["feature_name"])

        details = top.get("details", {})
        reason = ""
        if manufacturing_process == "CNC Machining" and details.get("depth_to_diameter_ratio") is not None:
            reason = (
                f" due to its depth:diameter ratio of "
                f"{details['depth_to_diameter_ratio']:.1f}:1 "
                f"(diameter {details['diameter_mm']:.2f}mm, depth "
                f"{details['depth_mm']:.2f}mm)"
            )

        if highlight_result["success"]:
            message = (
                f"Feature '{top['feature_name']}' ({top['label']}) accounts for "
                f"~{top['percentage_of_total_production_cost']:.0f}% of the "
                f"machining cost{reason} -- highlighted in the CAD viewport."
            )
        else:
            message = (
                f"Feature '{top['feature_name']}' ({top['label']}) accounts for "
                f"~{top['percentage_of_total_production_cost']:.0f}% of the "
                f"machining cost{reason}, but it could not be highlighted in "
                f"the viewport: {highlight_result['message']}"
            )

        return {
            "found": True,
            "message": message,
            "highlighted": highlight_result["success"],
            "top_driver": top,
        }
