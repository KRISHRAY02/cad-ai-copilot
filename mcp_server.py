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
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from cad_adapters.base_adapter import CadAdapter
from cad_adapters.mock_adapter import MockAdapter
from cost_model.features import infer_material_type
from materials_db import csv_path_for_platform, get_material_cost_and_carbon
from production_cost import PROCESSES

_MIN_FILLET_RADIUS_MM = 1.0

# Default machine type/supplier for estimate_cost() when the user doesn't
# specify one -- CNC 3-axis is the most general-purpose machining process,
# and Supplier_A is just the first of the synthetic suppliers the cost
# model was trained on (see cost_model/generate_synthetic_dataset.py).
_DEFAULT_MACHINE_TYPE = "CNC_3axis"
_DEFAULT_SUPPLIER = "Supplier_A"


def _build_adapter(backend: str | None = None) -> CadAdapter:
    """Construct the CadAdapter to use.

    `backend` defaults to the CAD_ADAPTER env var (itself defaulting to
    "solidworks") when not given explicitly -- explicit callers (see
    set_backend() below) can pick a platform at runtime without touching
    the environment first. Set CAD_ADAPTER=mock to use MockAdapter instead
    — synthetic aluminum bracket data, no CAD software required. Every tool
    below is written purely against the CadAdapter interface, so adding a
    platform only ever means adding a branch here.
    """
    backend = (backend or os.environ.get("CAD_ADAPTER", "solidworks")).lower()
    if backend == "mock":
        return MockAdapter()
    if backend == "mock_assembly":
        # Synthetic multi-level assembly (see MockAdapter's
        # _build_raw_assembly_components()) -- lets the assembly BOM/
        # rollup feature be demoed and tested without a real SolidWorks
        # assembly open.
        return MockAdapter(simulate_assembly=True)
    if backend in ("fusion360", "fusion"):
        from cad_adapters.fusion_adapter import FusionAdapter

        return FusionAdapter()

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


def set_backend(backend: str) -> CadAdapter:
    """Switch the live CAD backend for this process, in place.

    Rebuilds and reassigns the module-level `adapter` singleton that every
    tool function below reads as a global (so an in-process caller like
    desktop_app.py, which imports this whole module to drive the BOM/report
    buttons directly, sees the new adapter immediately) AND sets
    CAD_ADAPTER in os.environ (so any *new* mcp_server.py subprocess spawned
    afterwards -- ai_orchestrator.run_ai_orchestrator() spawns a fresh one
    per call, reading CAD_ADAPTER at that moment -- picks up the same
    backend). This is the one place both code paths' notion of "current CAD
    platform" gets updated together, so chat answers and the BOM/report
    buttons can never drift onto different platforms mid-session.
    """
    global adapter
    adapter = _build_adapter(backend)
    os.environ["CAD_ADAPTER"] = backend
    return adapter

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
    """Get the mass of the currently open document, in kilograms.

    Returns {"mass_kg": <float>}. Use this for any question about how
    heavy, light, or massive the current part IS -- and also for the
    TOTAL mass of the currently open assembly ("what's the mass of the
    whole assembly") if one is open, since this returns the whole active
    document's mass either way, with NO manufacturing process or cost
    involved. Do NOT use get_assembly_bom for a pure mass question -- that
    tool exists for cost, and will unnecessarily ask for a manufacturing
    process it doesn't need just to report mass. Also usable as an input
    to cost/weight calculations.
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

    **This is a single-part tool only.** If the currently open document is
    an assembly (check with is_assembly(), or just remember if you already
    called get_assembly_bom() in this conversation), call
    get_assembly_bom() or get_assembly_cost_drivers() instead -- an
    assembly's root document has no solid bodies/material of its own, so
    this tool will return found=False if called against one. This applies
    to *any* follow-up question about the same assembly, including "what
    if I used a different process/quantity" -- don't switch to this tool
    for those just because the phrasing sounds like a single-part question.

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

    if adapter.is_assembly():
        return {
            "found": False,
            "message": (
                "The currently open document is an assembly, not a single "
                "part -- estimate_cost() only reads geometry/material off "
                "the root document and will fail (assemblies have no "
                "solid bodies/material of their own; those live on the "
                "sub-components). Call get_assembly_bom() or "
                "get_assembly_cost_drivers() instead, with the same "
                "manufacturing_process/quantity."
            ),
        }

    mass_kg = adapter.get_mass()
    material = adapter.get_material()
    mass_properties = adapter.get_mass_properties()
    face_count = adapter.get_face_count()
    bend_count = adapter.get_bend_count()
    try:
        bounding_box_mm = adapter.get_bounding_box_mm()
    except NotImplementedError:
        # Only Sheet Metal's formula actually needs bounding box (see
        # production_cost.estimate_production_cost's dispatch) -- adapters
        # that haven't wired this up yet (e.g. FusionAdapter) shouldn't
        # block CNC Machining/Injection Molding cost estimates over it. For
        # Sheet Metal specifically, a silent (0.0, 0.0, 0.0) fallback would
        # produce a wrong-but-plausible-looking zero cutting cost, so fail
        # loudly there instead of guessing.
        if manufacturing_process == "Sheet Metal":
            return {
                "found": False,
                "message": (
                    f"{type(adapter).__name__} does not implement "
                    "get_bounding_box_mm() yet, which Sheet Metal cost "
                    "estimation requires for cutting-length approximation."
                ),
            }
        bounding_box_mm = (0.0, 0.0, 0.0)

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

    material_lookup = get_material_cost_and_carbon(material.name, platform=adapter.PLATFORM_ID)
    if not material_lookup["found"] or material_lookup["cost_per_kg"] is None:
        return {
            "found": False,
            "message": (
                f"Material '{material.name}' cost_per_kg is unavailable in "
                f"{csv_path_for_platform(adapter.PLATFORM_ID).name} -- "
                "material cost (and therefore total cost) cannot be "
                "estimated."
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
        "cost_basis": material_lookup.get("cost_basis"),
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


@mcp.tool()
def compare_materials(
    material_names: list[str],
    quantity: int = 1,
    manufacturing_process: str | None = None,
) -> dict:
    """Compare cost and carbon footprint for the CURRENTLY OPEN part's
    REAL geometry as if it were made from each material in
    `material_names` instead -- a "what if I used a different material"
    comparison. This is a hypothetical, read-only comparison: it does
    NOT change the material assigned in the actual CAD file, and does not
    modify the model in any way. Use this when the user asks things like
    "what would this cost in aluminum vs steel?" or "compare titanium and
    stainless steel for this part" -- do NOT use estimate_cost/
    estimate_carbon repeatedly for this, since those use the part's
    actual assigned material, not a hypothetical one.

    Each material name is looked up in materials.csv the same
    exact-then-fuzzy way estimate_cost() does; the geometry (volume, face
    count, bend count, bounding box) is read once from the real part and
    reused unchanged for every material -- only the material itself
    varies between comparison entries.

    `manufacturing_process` MUST be one of "CNC Machining", "Injection
    Molding", "Sheet Metal" (same as estimate_cost). If the user's
    question doesn't specify one, ASK THE USER rather than guessing --
    if called without one anyway, returns found=False listing the valid
    options.

    Returns {"found": True, "comparison": [one dict per material queried,
    each with material_matched, hypothetical_mass_kg, material_cost_inr,
    production_cost_inr, total_cost_per_unit_inr,
    total_cost_for_quantity_inr, estimated_kg_co2e -- or found=False +
    message for a material that couldn't be resolved/priced], "note":
    ...}. Present this to the user as a small comparison table (material
    / mass / cost / carbon), not prose. production_cost_inr is identical
    across every material in the comparison, by design -- it depends
    only on the part's geometry, not material choice; only
    material_cost_inr and estimated_kg_co2e actually differ per
    material.
    """
    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume, then "
                "call compare_materials again with their answer."
            ),
        }

    comparison = adapter.compare_materials(material_names, quantity, manufacturing_process)

    return {
        "found": True,
        "manufacturing_process": manufacturing_process,
        "quantity": quantity,
        "comparison": comparison,
        "note": (
            "production_cost_inr is identical across all materials here "
            "since production cost depends only on the part's geometry "
            "(face count / volume / bounding box / bend count), not "
            "material choice -- material_cost_inr and estimated_kg_co2e "
            "are what actually differ per material. This compares "
            "hypothetical materials against the real part's geometry; "
            "the actual CAD file's assigned material is unchanged."
        ),
    }


@mcp.tool()
def get_cost_drivers(manufacturing_process: str | None = None, quantity: int = 1) -> dict:
    """Break down the CURRENTLY OPEN part's production cost into a ranked
    list of cost components (highest cost first) -- use this instead of
    estimate_cost() when the user asks "what's driving the cost" / "which
    feature is most expensive" / "why does this cost so much", since
    estimate_cost() only returns a single total.

    For CNC Machining: each Hole Wizard hole is its own ranked entry
    (with the specific feature name, diameter, depth, and depth:diameter
    ratio that drove its cost -- deeper/narrower holes cost more, since a
    thin long drill has to slow down for chip evacuation, deflection, and
    heat), plus one flat "Other Features" bucket for every non-hole
    feature (clearly labeled as a flat/aggregate estimate, not per-feature)
    and one "Setup Time" entry for the fixed per-job cost.

    For Sheet Metal: two entries, "Cutting" and "Bending" (both real,
    independently computed).

    For Injection Molding: two entries, "Machine Cycle Cost" and "Tooling
    Amortization" (both real, independently computed).

    Each entry has a label, cost_per_unit_inr, percentage_of_total_
    production_cost, and is_individual_feature (True only for CNC holes --
    use this to know which entries correspond to a real, individually
    selectable feature vs. a flat/aggregate bucket). Present this as a
    ranked table, not prose.

    `manufacturing_process` MUST be one of "CNC Machining", "Injection
    Molding", "Sheet Metal" -- ASK THE USER if not specified, same as
    estimate_cost(). Returns found=False listing the valid options if
    called without one.
    """
    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume, then "
                "call get_cost_drivers again with their answer."
            ),
        }
    return adapter.get_cost_drivers(manufacturing_process, quantity)


@mcp.tool()
def highlight_cost_driver(manufacturing_process: str | None = None, quantity: int = 1) -> dict:
    """Find the single highest-cost driver for the CURRENTLY OPEN part
    (via the same ranking as get_cost_drivers()) and, if it's an
    individual feature (e.g. a specific deep/narrow hole), select it in
    the live CAD viewport -- the same visual highlight as clicking it in
    the feature tree -- so the user can see exactly which feature is
    driving the cost. Use this when the user asks to "highlight the most
    expensive feature", "show me what's driving the cost", or similar.

    If the top cost driver is a flat/aggregate bucket rather than one
    individual feature (e.g. "Other Features" or "Setup Time" for CNC, or
    either component for Sheet Metal/Injection Molding, none of which
    correspond to a single selectable feature), nothing is highlighted --
    the returned message says so explicitly instead of guessing a feature
    to select.

    Returns {"found", "message" (relay this directly to the user in
    chat), "highlighted" (bool), "top_driver"}. Same
    `manufacturing_process`/`quantity` requirements as get_cost_drivers().
    """
    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume, then "
                "call highlight_cost_driver again with their answer."
            ),
        }
    return adapter.highlight_cost_driver(manufacturing_process, quantity)


@mcp.tool()
def list_assembly_components() -> dict:
    """List the CURRENTLY OPEN ASSEMBLY's unique components and their
    quantities -- part name, quantity, mass, material, and where it sits
    in the tree, with NO cost/manufacturing-process involved.

    Use this for purely structural/quantity questions like "list the
    components in this assembly", "how many unique parts are there",
    "what quantity of [part] does this assembly use", or "what's this
    assembly made of" -- anything that doesn't ask about cost. Do NOT ask
    the user for a manufacturing process to answer this kind of question;
    only get_assembly_bom/get_assembly_cost_drivers/export_bom (which
    compute Make-part production cost) need one.

    This tool's result is COMPLETE on its own for these questions -- do
    NOT follow it up by calling get_assembly_bom, get_assembly_cost_drivers,
    or any other tool "to be thorough". If the user didn't ask about cost,
    answer directly from this tool's output and stop.

    Returns found=False if the currently open document isn't an assembly
    (call get_current_part_info() first if you're not sure).
    """
    if not adapter.is_assembly():
        return {
            "found": False,
            "message": (
                "The currently open document is not an assembly. Open an "
                "assembly to list its components."
            ),
        }

    components = adapter.get_assembly_components()
    return {
        "found": True,
        "unique_part_count": len(components),
        "total_instance_count": sum(c.quantity for c in components),
        "components": [dataclasses.asdict(c) for c in components],
    }


@mcp.tool()
def get_assembly_bom(manufacturing_process: str | None = None, quantity: int = 1) -> dict:
    """Roll up the CURRENTLY OPEN ASSEMBLY into a structured Bill of
    Materials: one row per unique (part, configuration) combination found
    anywhere in the assembly tree (including nested sub-assemblies), each
    classified as "Make" (custom-manufactured -- cost from material +
    production cost, same formulas as estimate_cost()) or "Buy" (purchased
    standard hardware -- cost from standard_hardware.csv), plus
    assembly-level totals (unique part count, total part instance count,
    total mass, total cost for one assembly and for `quantity` assemblies)
    and a list of any components with missing material/pricing data.

    Use this for questions like "what's the total cost of this assembly
    for a production run of N" or "which parts are missing
    material/pricing" -- this tool answers all of those directly from one
    call, rather than needing several get_mass/estimate_cost calls on
    individual parts. For a plain list of components/quantities with NO
    cost involved, use list_assembly_components() instead -- it needs no
    manufacturing_process. **For a pure mass question with no cost
    involved ("what's the mass of the complete assembly"), use get_mass()
    instead -- it returns the whole open document's total mass without
    ever needing a manufacturing_process.**

    `manufacturing_process` MUST be one of "CNC Machining", "Injection
    Molding", or "Sheet Metal" -- same as estimate_cost(), since it drives
    which production cost formula applies to every "Make" component. **If
    the user's question doesn't specify a process, ASK THE USER which one
    to assume before calling this tool.** Returns found=False listing the
    three valid options if called without one anyway.

    Returns found=False if the currently open document isn't an assembly
    (call get_current_part_info() first if you're not sure).
    """
    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume for "
                "this assembly's Make parts, then call get_assembly_bom "
                "again with their answer."
            ),
        }

    if not adapter.is_assembly():
        return {
            "found": False,
            "message": (
                "The currently open document is not an assembly. Open an "
                "assembly to get a BOM, or use estimate_cost() for a "
                "single part."
            ),
        }

    result = adapter.get_assembly_bom(manufacturing_process, quantity)
    return {"found": True, **result}


@mcp.tool()
def get_assembly_cost_drivers(manufacturing_process: str | None = None, quantity: int = 1) -> dict:
    """Return the CURRENTLY OPEN ASSEMBLY's Bill of Materials sorted by
    total cost (highest first) -- use this directly for questions like
    "which components are driving the cost the most" or "what's the most
    expensive part in this assembly", instead of calling get_assembly_bom
    and re-sorting it yourself.

    Rows with unknown total cost (missing material/pricing data -- see
    "missing_data" in the response) are sorted to the end, not treated as
    zero, since a missing price is not the same as a free part.

    Same `manufacturing_process`/`quantity` parameters and found=False
    behavior as get_assembly_bom() -- ask the user for the manufacturing
    process if they haven't specified one.
    """
    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume, then "
                "call get_assembly_cost_drivers again with their answer."
            ),
        }

    if not adapter.is_assembly():
        return {
            "found": False,
            "message": "The currently open document is not an assembly.",
        }

    result = adapter.get_assembly_bom(manufacturing_process, quantity)
    ranked = sorted(
        result["bom"],
        key=lambda row: (row["total_cost_inr"] is None, -(row["total_cost_inr"] or 0)),
    )
    return {
        "found": True,
        "manufacturing_process": manufacturing_process,
        "quantity": quantity,
        "cost_drivers": ranked,
        "totals": result["totals"],
        "missing_data": result["missing_data"],
    }


@mcp.tool()
def export_bom(
    manufacturing_process: str | None = None,
    quantity: int = 1,
    output_path: str | None = None,
    indented: bool = False,
) -> dict:
    """Export the CURRENTLY OPEN ASSEMBLY's Bill of Materials to a real
    .xlsx BOM file with standard columns (Item No, Part Name,
    Configuration, Make/Buy, Material, Qty, Unit Mass, Total Mass, Unit
    Cost, Total Cost). Call this when the user asks to "export the BOM",
    "give me a parts list", or similar.

    `indented=False` (default) produces a flat parts-only list: one row
    per unique (part, configuration) with its TOTAL quantity across the
    whole assembly -- what a purchasing/stores department needs.
    `indented=True` preserves sub-assembly grouping (a bold sub-assembly
    header row followed by its indented children) -- what an
    assembler/planner needs to see build structure. If the user doesn't
    say which they want, a flat BOM is the more common default; ask if
    it's ambiguous from context.

    Same `manufacturing_process`/`quantity` requirements as
    get_assembly_bom() -- ask the user for the manufacturing process if
    unspecified. `output_path` defaults to a timestamped .xlsx in the
    system temp directory. After this returns found=True, tell the user
    the exact file path.
    """
    from bom_export import export_bom as write_bom_file

    if manufacturing_process not in PROCESSES:
        return {
            "found": False,
            "message": (
                f"manufacturing_process must be one of {list(PROCESSES)}. "
                "Ask the user which manufacturing process to assume, then "
                "call export_bom again with their answer."
            ),
        }

    if not adapter.is_assembly():
        return {
            "found": False,
            "message": "The currently open document is not an assembly.",
        }

    if output_path is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(Path(tempfile.gettempdir()) / f"bom_{timestamp}.xlsx")

    bom_result = adapter.get_assembly_bom(manufacturing_process, quantity)
    try:
        saved_path = write_bom_file(bom_result, output_path, indented)
    except Exception as e:
        return {"found": False, "message": f"Could not export the BOM: {e}"}

    return {"found": True, "bom_path": saved_path, "indented": indented}


@mcp.tool()
def generate_report(
    manufacturing_process: str | None = None,
    quantity: int | None = None,
    output_path: str | None = None,
) -> dict:
    """Generate a "manufacturing readiness report" PDF for the currently
    open CAD part -- a single document combining a viewport screenshot,
    part identity/mass/material info, a cost breakdown, a carbon
    estimate, and DFM check results. Call this when the user asks to
    "export a report", "generate a summary/report", "give me a PDF", or
    similar phrasing, instead of trying to answer with several separate
    tool calls.

    `manufacturing_process` (one of "CNC Machining", "Injection
    Molding", "Sheet Metal") and `quantity` should be whatever the user
    already told you earlier in this conversation, if anything -- pass
    those through here rather than re-asking, so the report reflects the
    same assumptions the chat has been using. If you don't know either
    one from the conversation, leave it as None: the report will use a
    clearly-labeled default (CNC Machining, quantity 1) rather than the
    tool guessing silently.

    `output_path` defaults to a timestamped PDF in the system temp
    directory if not given.

    After this returns found=True, tell the user the exact file path so
    they can open it.
    """
    from report.generate_report import generate_manufacturing_report

    if output_path is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(Path(tempfile.gettempdir()) / f"manufacturing_report_{timestamp}.pdf")

    try:
        saved_path = generate_manufacturing_report(
            output_path, manufacturing_process=manufacturing_process, quantity=quantity
        )
    except Exception as e:
        return {
            "found": False,
            "message": f"Could not generate the report: {e}",
        }

    return {"found": True, "report_path": saved_path}


if __name__ == "__main__":
    mcp.run()
