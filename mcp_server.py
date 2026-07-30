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

# Rough, illustrative reference values for derived estimates. Not
# authoritative — good enough for a project demo, not for real quoting.
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

_MIN_FILLET_RADIUS_MM = 1.0


def _build_adapter() -> CadAdapter:
    """Construct the CadAdapter to use, based on the CAD_ADAPTER env var.

    Defaults to MockAdapter so the server runs without any CAD software.
    Set CAD_ADAPTER=solidworks to target a running SolidWorks instance.
    """
    backend = os.environ.get("CAD_ADAPTER", "mock").lower()
    if backend == "solidworks":
        from cad_adapters.solidworks_adapter import SolidWorksAdapter

        return SolidWorksAdapter()
    return MockAdapter()


adapter = _build_adapter()
adapter.connect()

mcp = MCPServer(
    name="cad-copilot",
    description=(
        "Tools for querying the currently open CAD model: identity, mass, "
        "material, feature tree, and derived cost/carbon/DFM estimates."
    ),
)


@mcp.tool()
def get_part_info() -> dict:
    """Get the name, file path, type, and units of the currently open part."""
    return dataclasses.asdict(adapter.get_current_part_info())


@mcp.tool()
def get_mass() -> dict:
    """Get the mass of the currently open part, in kilograms."""
    return {"mass_kg": adapter.get_mass()}


@mcp.tool()
def get_material() -> dict:
    """Get the material assigned to the currently open part."""
    return dataclasses.asdict(adapter.get_material())


@mcp.tool()
def get_features() -> list[dict]:
    """Get the feature tree of the currently open part."""
    return [dataclasses.asdict(f) for f in adapter.get_features()]


@mcp.tool()
def estimate_cost() -> dict:
    """Estimate the raw material + machining cost of the current part, in USD.

    A simple heuristic: (material $/kg * mass) + a flat machining base fee.
    Intended as a rough, explainable estimate for a student project, not a
    real manufacturing quote.
    """
    mass_kg = adapter.get_mass()
    material = adapter.get_material()

    cost_per_kg = _MATERIAL_COST_PER_KG_USD.get(
        material.category, _DEFAULT_COST_PER_KG_USD
    )
    material_cost = mass_kg * cost_per_kg
    total_cost = material_cost + _MACHINING_BASE_FEE_USD

    return {
        "material_cost_usd": round(material_cost, 2),
        "machining_fee_usd": _MACHINING_BASE_FEE_USD,
        "estimated_total_usd": round(total_cost, 2),
        "assumptions": (
            f"${cost_per_kg}/kg for {material.category or 'unknown material'} "
            f"+ ${_MACHINING_BASE_FEE_USD} flat machining fee"
        ),
    }


@mcp.tool()
def estimate_carbon() -> dict:
    """Estimate the embodied carbon (cradle-to-gate) of the current part, in kg CO2e.

    A simple heuristic: material emission factor (kg CO2e per kg of
    material) multiplied by part mass. Uses generic published averages by
    material category, not a supplier-specific lifecycle assessment.
    """
    mass_kg = adapter.get_mass()
    material = adapter.get_material()

    factor = _MATERIAL_CARBON_PER_KG_CO2E.get(
        material.category, _DEFAULT_CARBON_PER_KG_CO2E
    )
    total_co2e = mass_kg * factor

    return {
        "estimated_kg_co2e": round(total_co2e, 3),
        "emission_factor_kg_co2e_per_kg": factor,
        "assumptions": (
            f"{factor} kg CO2e/kg for {material.category or 'unknown material'}, "
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
