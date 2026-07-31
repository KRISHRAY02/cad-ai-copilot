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

from cad_adapters.base_adapter import CadAdapter
from cad_adapters.mock_adapter import MockAdapter

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


@mcp.tool()
def estimate_cost(quantity: int = 1) -> dict:
    """Estimate the material + machining cost to produce `quantity` units
    of the current part, in INR.

    Looks up the current material's cost_per_kg in materials.csv (exact
    name match, falling back to a fuzzy match), then applies:
    (mass * cost_per_kg + a flat machining fee) * a quantity discount
    multiplier, times quantity. See CadAdapter.estimate_cost() for the
    full formula. Returns found=False with an explanatory message
    instead of guessing if the material isn't in materials.csv. Not a
    real manufacturing quote -- a rough, explainable estimate for a
    student project.
    """
    return adapter.estimate_cost(quantity)


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


if __name__ == "__main__":
    mcp.run()
