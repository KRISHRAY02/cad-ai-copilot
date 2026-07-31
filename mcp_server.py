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
import os

from mcp.server.mcpserver import MCPServer

from cad_adapters.base_adapter import CadAdapter, MaterialInfo
from cad_adapters.mock_adapter import MockAdapter
from materials_db import get_material_cost_and_carbon, load_materials

# Rough, illustrative per-category fallbacks, used only when materials.csv
# has no usable entry for the current material (missing file, name not
# found, or a blank cost/carbon cell). Not authoritative — good enough for
# a project demo, not for real quoting.
_MATERIAL_COST_PER_KG_USD = {
    "Aluminum": 4.50,
    "Steel": 2.00,
    "Plastic": 3.00,
}
_DEFAULT_COST_PER_KG_USD = 5.00
_MACHINING_BASE_FEE_USD = 25.00

_MATERIAL_CARBON_PER_KG_CO2E = {
    "Aluminum": 11.5,
    "Steel": 2.0,
    "Plastic": 3.5,
}
_DEFAULT_CARBON_PER_KG_CO2E = 4.0

# Loaded once at import time, same reasoning as _build_adapter() below:
# materials.csv is a static file checked into the repo, so failing fast
# here is fine, but a missing/broken file shouldn't crash the whole
# server -- estimate_cost/estimate_carbon just fall back to the category
# defaults above if this is None.
try:
    _materials = load_materials()
except (FileNotFoundError, ValueError) as e:
    print(f"materials.csv unavailable, falling back to category defaults: {e}")
    _materials = None

_MIN_FILLET_RADIUS_MM = 1.0


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


def _resolve_cost_and_carbon(material: MaterialInfo) -> dict:
    """Resolve a cost_per_kg and carbon_factor for a material, with sources.

    Prefers materials.csv, looked up by the material's actual name (via
    materials_db's exact-then-fuzzy matching), since it can hold
    per-material figures Krish has specifically researched rather than a
    generic per-category average. Falls back to the rough
    _MATERIAL_COST_PER_KG_USD / _MATERIAL_CARBON_PER_KG_CO2E category
    defaults when materials.csv is unavailable, the name isn't found, or
    the matched row's cost/carbon cell is blank -- cost and carbon fall
    back independently, since one could be filled in without the other.
    """
    cost_per_kg = None
    carbon_factor = None
    cost_source = None
    carbon_source = None

    if _materials is not None:
        lookup = get_material_cost_and_carbon(material.name, _materials)
        if lookup["found"]:
            match_note = lookup["matched_from"]
            if lookup["is_fuzzy_match"]:
                match_note += " (fuzzy match)"
            if lookup["cost_per_kg"] is not None:
                cost_per_kg = lookup["cost_per_kg"]
                cost_source = f"materials.csv: {match_note}"
            if lookup["carbon_factor_kg_co2_per_kg"] is not None:
                carbon_factor = lookup["carbon_factor_kg_co2_per_kg"]
                carbon_source = f"materials.csv: {match_note}"

    if cost_per_kg is None:
        cost_per_kg = _MATERIAL_COST_PER_KG_USD.get(
            material.category, _DEFAULT_COST_PER_KG_USD
        )
        cost_source = f"category default ({material.category or 'unknown material'})"

    if carbon_factor is None:
        carbon_factor = _MATERIAL_CARBON_PER_KG_CO2E.get(
            material.category, _DEFAULT_CARBON_PER_KG_CO2E
        )
        carbon_source = f"category default ({material.category or 'unknown material'})"

    return {
        "cost_per_kg": cost_per_kg,
        "cost_source": cost_source,
        "carbon_factor": carbon_factor,
        "carbon_source": carbon_source,
    }


@mcp.tool()
def estimate_cost() -> dict:
    """Estimate the raw material + machining cost of the current part, in USD.

    A simple heuristic: (material $/kg * mass) + a flat machining base fee.
    The $/kg figure comes from materials.csv when the current material is
    found there, otherwise a rough per-category default. Intended as a
    rough, explainable estimate for a student project, not a real
    manufacturing quote.
    """
    mass_kg = adapter.get_mass()
    material = adapter.get_material()

    resolved = _resolve_cost_and_carbon(material)
    cost_per_kg = resolved["cost_per_kg"]
    material_cost = mass_kg * cost_per_kg
    total_cost = material_cost + _MACHINING_BASE_FEE_USD

    return {
        "material_cost_usd": round(material_cost, 2),
        "machining_fee_usd": _MACHINING_BASE_FEE_USD,
        "estimated_total_usd": round(total_cost, 2),
        "assumptions": (
            f"${cost_per_kg}/kg for '{material.name}' ({resolved['cost_source']}) "
            f"+ ${_MACHINING_BASE_FEE_USD} flat machining fee"
        ),
    }


@mcp.tool()
def estimate_carbon() -> dict:
    """Estimate the embodied carbon (cradle-to-gate) of the current part, in kg CO2e.

    A simple heuristic: material emission factor (kg CO2e per kg of
    material) multiplied by part mass. The factor comes from materials.csv
    when the current material is found there, otherwise a rough
    per-category default -- either way, a generic published average, not
    a supplier-specific lifecycle assessment.
    """
    mass_kg = adapter.get_mass()
    material = adapter.get_material()

    resolved = _resolve_cost_and_carbon(material)
    factor = resolved["carbon_factor"]
    total_co2e = mass_kg * factor

    return {
        "estimated_kg_co2e": round(total_co2e, 3),
        "emission_factor_kg_co2e_per_kg": factor,
        "assumptions": (
            f"{factor} kg CO2e/kg for '{material.name}' ({resolved['carbon_source']}), "
            "cradle-to-gate only (excludes machining energy and transport)"
        ),
    }


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


if __name__ == "__main__":
    mcp.run()
