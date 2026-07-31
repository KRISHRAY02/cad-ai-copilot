"""Proof-of-extensibility demo: any CadAdapter can be swapped in transparently.

`describe_part` below is written entirely against the CadAdapter interface —
it has no idea whether it's talking to SolidWorks or hardcoded mock data.
That's the point: adding support for a new CAD platform later only means
writing one new CadAdapter subclass, with zero changes to code like this
(or to the real AI/MCP/UI layers, which follow the same pattern).

Run with:

    python demo_adapter_swap.py
"""

from cad_adapters.base_adapter import CadAdapter
from cad_adapters.mock_adapter import MockAdapter
from cad_adapters.solidworks_adapter import (
    NoDocumentOpenError,
    SolidWorksAdapter,
    SolidWorksNotRunningError,
)


def describe_part(adapter: CadAdapter) -> None:
    """Print a summary of the active part, using only the CadAdapter interface."""
    adapter.connect()

    part_info = adapter.get_current_part_info()
    mass = adapter.get_mass()
    material = adapter.get_material()
    features = adapter.get_features()

    print(f"Adapter:   {type(adapter).__name__}")
    print(f"Part:      {part_info.name} ({part_info.part_type})")
    print(f"File:      {part_info.file_path}")
    print(f"Mass:      {mass} kg")
    print(f"Material:  {material.name} ({material.density_kg_m3} kg/m^3)")
    print(f"Features:  {len(features)} total")
    for feature in features:
        print(f"  - {feature.name} ({feature.feature_type})")


def main() -> None:
    print("=== Mock adapter (no CAD software required) ===")
    describe_part(MockAdapter())

    print("\n=== SolidWorks adapter (live session) ===")
    try:
        describe_part(SolidWorksAdapter())
    except (SolidWorksNotRunningError, NoDocumentOpenError) as e:
        print(f"Skipped: {e}")


if __name__ == "__main__":
    main()
