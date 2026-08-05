"""MCP server exposing CAD model queries as tools for the AI orchestrator.

Wraps a CadAdapter instance (SolidWorksAdapter, MockAdapter, or any future
platform adapter) and publishes its capabilities — part info, mass,
material, features, and derived checks like cost/carbon estimates and
basic DFM checks — as MCP tools. This module knows nothing about which
concrete adapter is in use; it only depends on the CadAdapter interface,
so swapping CAD platforms requires no code changes here, only a change
to CAD_ADAPTER (env var) or the default in `_build_adapter()`.
"""

import dataclasses
import datetime
import os

from mcp.server.mcpserver import MCPServer

from cad_adapters.base_adapter import CadAdapter
from cad_adapters.mock_adapter import MockAdapter
from cost_model.features import infer_material_type
from materials_db import get_material_cost_and_carbon
from production_cost import PROCESSES

_MIN_FILLET_RADIUS_MM = 1.0

# Default machine type/supplier for estimate_cost() when the user doesn't
# specify one -- CNC 3-axis is the most general-purpose machining process,
# and Supplier_A is just the first of the synthetic suppliers the cost
# model was trained on (see cost_model/generate_synthetic_dataset.py).
_DEFAULT_MACHINE_TYPE = "CNC_3axis"
_DEFAULT_SUPPLIER = "Supplier_A"


def _build_adapter() -> CadAdapter:
    """Construct the CadAdapter to use, based on the CAD_ADAPTER env var.

    Defaults to SolidWorksAdapter, targeting a running SolidWorks instance.
    Set CAD_ADAPTER=mock to use MockAdapter instead — synthetic aluminum
    bracket data, no CAD software required. This is the only place that
    needs to change to swap CAD platforms; every tool below is written
    purely against the CadAdapter interface.
    """
    backend = os.environ.get("CAD_ADAPTER", "solidworks").lower()
    if backend == "mock":
        return MockAdapter()

    from cad_adapters.solidworks_adapter import SolidWorksAdapter

    return SolidWorksAdapter()


# Deliberately not calling adapter.connect() here: this line runs once at
# import time in the server subprocess, so raising here (e.g. SolidWorks not
# running, or no document open yet) would crash the whole subprocess before
# a single tool could be called. Every CadAdapter method already establishes
# its own connection on each call, so it's safer to defer that check to each
# individual tool invocation — a bad connection then surfaces as one failed
# tool call with a clear message, not a dead server.
adapter = _build_adapter()

mcp = MCPServer(
    name="cad-copilot",
    description=(
        "Tools for querying the currently open CAD model: identity, mass, "
        "material, feature tree, and derived cost/carbon/DFM estimates."
    ),
)


@mcp.tool()
def get_current_part_info() -> dict:
    """Identify the CAD part currently open in the active document.

    Returns the part's name, its full file path on disk, its document
    type ("part", "assembly", or "drawing"), and its linear unit system
    (e.g. "mm", "in"). Call this first when the user asks about "this
    part" or "the current model" and you don't already know what it is.
    """
    return dataclasses.asdict(adapter.get_current_part_info())


@mcp.tool()
def get_mass() -> dict:
    """Get the mass of the currently open part, in kilograms.

    Returns {"mass_kg": <float>}. Use this for any question about how
    heavy, light, or massive the current part is, or as an input to
    cost/weight calculations.
    """
    return {"mass_kg": adapter.get_mass()}


@mcp.tool()
def get_material() -> dict:
    """Get the material assigned to the currently open part.

    Returns the material's name, density in kg/m^3, and category (e.g.
    "Aluminum", "Steel", "Plastic") if known. If no material has been
    assigned in the CAD software, the name field will indicate that
    rather than the call failing.
    """
    return dataclasses.asdict(adapter.get_material())


@mcp.tool()
def get_features() -> list[dict]:
    """Get the feature tree of the currently open part.

    Returns a list of features in modeling order, each with a name, a
    feature type (e.g. "Extrude", "Fillet", "Chamfer", "Sketch"), and
    whether it is currently suppressed. Use this to answer questions
    about how the part was built, what operations it contains, or to
    check for specific feature types (e.g. "does it have a fillet?").
    """
    return [dataclasses.asdict(f) for f in adapter.get_features()]


@mcp.tool()
def estimate_cost(
    manufacturing_process: str | None = None,
    quantity: int = 1,
    order_year: int | None = None,
    supplier: str = _DEFAULT_SUPPLIER,
    machine_type: str = _DEFAULT_MACHINE_TYPE,
) -> dict:
    """Estimate the cost to produce `quantity` units of the current part,
    in INR, using a trained Random Forest Regression model (see
    cost_model/) for a total, PLUS an explicit breakdown into material
    cost and production cost computed directly from real data (see
    production_cost.py).

    `manufacturing_process` MUST be one of "CNC Machining", "Injection
    Molding", or "Sheet Metal" -- it drives which production cost formula
    is used (machining time for CNC, cycle time + tooling amortization for
    Injection Molding, cutting length + bend count for Sheet Metal), and
    real parts cost very differently to produce depending on which of
    these processes makes them. **If the user's question doesn't specify
    a process, ASK THE USER which one to assume before calling this tool
    -- do not silently guess one.** If you call this tool without a
    process anyway (e.g. because the user can't be reached), it returns
    found=False listing the three valid options rather than guessing.

    Pulls the part's geometry (volume, surface area, face count, bend
    count, bounding box) and material (name, density, cost_per_kg) live
    from the CAD adapter and materials.csv, combines them with the given
    order-level parameters (quantity, order_year -- defaults to the
    current year -- supplier, machine_type), and passes all of it to
    cost_model.predict.predict_cost(). The model was trained on a
    synthetic dataset whose per-row cost target is grounded in real
    cost_per_kg figures (materials.csv) plus production_cost.py's
    formulas (see cost_model/generate_synthetic_dataset.py) -- it is a
    student-project estimate, not a real manufacturing quote, and the
    production cost formulas use PLACEHOLDER shop-rate constants (see
    production_cost.py) that haven't been replaced with real researched
    figures yet.

    Returns found=False with an explanatory message if the trained model
    file doesn't exist yet (run cost_model/generate_synthetic_dataset.py
    then cost_model/train_model.py first), if materials.csv can't
    resolve the part's material cost, or if manufacturing_process is
    missing/invalid -- instead of guessing.
    """
    from cost_model.predict import predict_cost

    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume for "
                "this part, then call estimate_cost again with their "
                "answer."
            ),
        }

    mass_kg = adapter.get_mass()
    material = adapter.get_material()
    mass_properties = adapter.get_mass_properties()
    face_count = adapter.get_face_count()
    bend_count = adapter.get_bend_count()
    bounding_box_mm = adapter.get_bounding_box_mm()

    volume_m3 = mass_properties.get("volume_m3")
    surface_area_m2 = mass_properties.get("surface_area_m2")
    if volume_m3 is None or surface_area_m2 is None:
        return {
            "found": False,
            "message": (
                "Could not read volume/surface area for the current part "
                "(no solid geometry?) -- cost cannot be estimated."
            ),
        }

    material_lookup = get_material_cost_and_carbon(material.name)
    if not material_lookup["found"] or material_lookup["cost_per_kg"] is None:
        return {
            "found": False,
            "message": (
                f"Material '{material.name}' cost_per_kg is unavailable in "
                "materials.csv -- material cost (and therefore total cost) "
                "cannot be estimated."
            ),
        }

    features = {
        "volume_m3": volume_m3,
        "surface_area_m2": surface_area_m2,
        "face_count": face_count,
        "bend_count": bend_count,
        "density_kg_m3": material.density_kg_m3,
        "mass_kg": mass_kg,
        "order_quantity": quantity,
        "order_year": order_year or datetime.date.today().year,
        "material_type": infer_material_type(material.name),
        "supplier": supplier,
        "machine_type": machine_type,
        "manufacturing_process": manufacturing_process,
        "cost_per_kg": material_lookup["cost_per_kg"],
        "bounding_box_mm": bounding_box_mm,
    }

    try:
        prediction = predict_cost(features)
    except FileNotFoundError as e:
        return {"found": False, "message": str(e)}

    material_cost_per_unit = prediction["material_cost_inr"]
    production_cost_per_unit = prediction["production_cost_inr"]
    predicted_total_per_unit = prediction["predicted_total_cost_inr"]

    return {
        "found": True,
        "manufacturing_process": manufacturing_process,
        "material_cost_per_unit_inr": material_cost_per_unit,
        "production_cost_per_unit_inr": production_cost_per_unit,
        "total_cost_per_unit_inr": round(
            material_cost_per_unit + production_cost_per_unit, 2
        ),
        "ml_predicted_total_cost_per_unit_inr": predicted_total_per_unit,
        "total_cost_for_quantity_inr": round(
            (material_cost_per_unit + production_cost_per_unit) * quantity, 2
        ),
        "quantity": quantity,
        "material_used": material.name,
        "material_cost_per_kg_inr": material_lookup["cost_per_kg"],
        "production_cost_breakdown": prediction["production_cost_breakdown"],
        "production_cost_assumptions": prediction["production_cost_assumptions"],
        "inputs_used": prediction["features_used"],
        "assumptions": (
            "total_cost_per_unit_inr = material_cost_per_unit_inr "
            "(mass x materials.csv cost_per_kg) + "
            "production_cost_per_unit_inr (process-specific formula, see "
            "production_cost_assumptions -- uses PLACEHOLDER shop rates, "
            "not real researched figures). "
            "ml_predicted_total_cost_per_unit_inr is a separate Random "
            "Forest Regression estimate (see cost_model/) trained on a "
            "synthetic dataset grounded in these same real material and "
            "production cost figures -- shown for comparison, not "
            "summed into the total. Neither figure is a real "
            "manufacturing quote."
        ),
    }


@mcp.tool()
def estimate_carbon() -> dict:
    """Estimate the embodied carbon (cradle-to-gate) of the current part,
    in kg CO2e.

    Looks up the current material's carbon_factor_kg_co2_per_kg in
    materials.csv the same way estimate_cost() looks up cost_per_kg, then
    returns mass * factor. Returns found=False with an explanatory
    message instead of guessing if the material isn't in materials.csv.
    A generic published average, not a supplier-specific lifecycle
    assessment.
    """
    return adapter.estimate_carbon()


@mcp.tool()
def run_dfm_checks() -> list[dict]:
    """Run basic design-for-manufacturing checks on the current part.

    Lightweight, rule-of-thumb checks based on the feature tree only
    (fillet/chamfer sizes, suppressed features left in the tree). Not a
    substitute for a full DFM analysis, but enough to flag obvious issues
    for a student-project demo.
    """
    findings = []

    for feature in adapter.get_features():
        if feature.suppressed:
            findings.append(
                {
                    "severity": "info",
                    "feature": feature.name,
                    "message": (
                        "Feature is suppressed. Confirm this is intentional "
                        "before finalizing the design."
                    ),
                }
            )

        if feature.feature_type == "Fillet":
            radius = feature.parameters.get("radius_mm")
            if radius is not None and radius < _MIN_FILLET_RADIUS_MM:
                findings.append(
                    {
                        "severity": "warning",
                        "feature": feature.name,
                        "message": (
                            f"Fillet radius {radius}mm is below "
                            f"{_MIN_FILLET_RADIUS_MM}mm, which may be difficult "
                            "or costly to machine reliably."
                        ),
                    }
                )

    if not any(f.feature_type in ("Fillet", "Chamfer") for f in adapter.get_features()):
        findings.append(
            {
                "severity": "warning",
                "feature": None,
                "message": (
                    "No fillet or chamfer features found. Sharp edges/corners "
                    "increase stress concentration and handling risk."
                ),
            }
        )

    if not findings:
        findings.append(
            {
                "severity": "info",
                "feature": None,
                "message": "No issues found by the basic DFM checks.",
            }
        )

    return findings


@mcp.tool()
def run_dfm_check() -> list[dict]:
    """Run four geometry-driven design-for-manufacturability checks:
    hole diameter/depth ratio, wall thickness (Shell features), draft
    angle, and dimension tolerance. Distinct from (and more detailed
    than) `run_dfm_checks` above, which only looks at fillet/chamfer/
    suppression on the feature tree -- this one reads exact feature and
    dimension parameters (e.g. a Hole Wizard hole's real diameter, a
    Shell feature's real thickness) via the CAD adapter.

    Each finding has a "check" (hole/wall_thickness/draft_angle/
    tolerance), a "status" (flagged/pass/not_applicable), the
    feature/dimension name involved (if any), and a message. Never
    estimates a value where real feature/dimension data isn't available
    on the part -- reports not_applicable instead (e.g. no Shell feature
    to measure, or a part with curved surfaces where thickness can't be
    determined at all).
    """
    return adapter.run_dfm_check()


if __name__ == "__main__":
    mcp.run()
