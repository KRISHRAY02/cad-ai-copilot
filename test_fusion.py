"""Manual smoke test for FusionAdapter.

Before running this:
  1. Open Fusion 360 with a document (design) open.
  2. Enable the fusion_bridge_addin Add-In (Utilities > Add-Ins > Scripts
     and Add-Ins > Add-Ins tab > fusion_bridge_addin > Run). You should see
     a "CAD AI Copilot bridge started" message box inside Fusion.

Then run:

    C:\\Python314\\python.exe test_fusion.py
    C:\\Python314\\python.exe test_fusion.py "Design Name"   # also tests open_document()
"""

import sys

from cad_adapters.fusion_adapter import (
    FusionAdapter,
    FusionBridgeNotReachableError,
    FusionDataUnavailableError,
    NoFusionDocumentOpenError,
)


def main() -> None:
    adapter = FusionAdapter()

    try:
        adapter.connect()
    except FusionBridgeNotReachableError as e:
        print(f"FAILED: could not reach the Fusion bridge Add-In. {e}")
        return

    print("Connected to the Fusion 360 bridge Add-In.")

    target_name = sys.argv[1] if len(sys.argv) > 1 else None
    if target_name:
        try:
            before_info = adapter.get_current_part_info()
            print(f"\nActive document BEFORE open_document(): {before_info.name}")
        except (NoFusionDocumentOpenError, FusionBridgeNotReachableError):
            print("\nActive document BEFORE open_document(): (none open)")

        try:
            adapter.open_document(target_name)
            print(f"open_document({target_name!r}) succeeded.")
        except FusionDataUnavailableError as e:
            print(f"FAILED to open {target_name!r}: {e}")
            return
        except FusionBridgeNotReachableError as e:
            print(f"FAILED: bridge became unreachable during open_document(). {e}")
            return

    try:
        part_info = adapter.get_current_part_info()
    except NoFusionDocumentOpenError as e:
        print(f"FAILED: connected, but {e}")
        return
    except FusionBridgeNotReachableError as e:
        print(f"FAILED: bridge became unreachable mid-request. {e}")
        return

    print("Current document info:")
    print(f"  name:      {part_info.name}")
    print(f"  type:      {part_info.part_type}")
    print(f"  file_path: {part_info.file_path}")
    print(f"  units:     {part_info.units}")

    try:
        mass = adapter.get_mass()
        print(f"\nMass: {mass} kg")
    except FusionDataUnavailableError as e:
        print(f"\nFAILED to get mass: {e}")

    try:
        material = adapter.get_material()
        print("\nMaterial:")
        print(f"  name:          {material.name}")
        print(f"  density_kg_m3: {material.density_kg_m3}")
    except FusionDataUnavailableError as e:
        print(f"\nFAILED to get material: {e}")

    try:
        features = adapter.get_features()
        print(f"\nFeatures ({len(features)}):")
        for feature in features:
            suppressed_tag = " [suppressed]" if feature.suppressed else ""
            print(f"  - {feature.name} ({feature.feature_type}){suppressed_tag}")
    except FusionDataUnavailableError as e:
        print(f"\nFAILED to get features: {e}")

    try:
        face_count = adapter.get_face_count()
        print(f"\nFace count: {face_count}")
    except FusionDataUnavailableError as e:
        print(f"\nFAILED to get face count: {e}")

    try:
        bend_count = adapter.get_bend_count()
        print(f"Bend count: {bend_count}")
    except FusionDataUnavailableError as e:
        print(f"FAILED to get bend count: {e}")

    findings = adapter.run_dfm_check()
    print(f"\nDFM findings ({len(findings)}):")
    for finding in findings:
        feature_tag = f" [{finding['feature']}]" if finding.get("feature") else ""
        print(f"  - {finding['check']}{feature_tag}: {finding['status']} -- {finding['message']}")

    try:
        is_assembly = adapter.is_assembly()
        print(f"\nIs assembly: {is_assembly}")
    except FusionDataUnavailableError as e:
        print(f"\nFAILED to check is_assembly: {e}")
        is_assembly = False

    if is_assembly:
        try:
            components = adapter.get_assembly_components()
            print(f"\nAssembly components ({len(components)} unique):")
            for c in components:
                material_tag = f", material={c.material.name}" if c.material else ", material=None"
                print(
                    f"  - {c.part_name} x{c.quantity} (level {c.level}, "
                    f"parent={c.parent_assembly}) mass_kg={c.mass_kg} "
                    f"faces={c.face_count} bends={c.bend_count}{material_tag}"
                )
        except FusionDataUnavailableError as e:
            print(f"\nFAILED to get assembly components: {e}")
            components = None

        if components is not None:
            try:
                bom = adapter.get_assembly_bom("CNC Machining", quantity=1)
                print("\nAssembly BOM totals:")
                print(f"  unique_part_count:                {bom['totals']['unique_part_count']}")
                print(f"  total_instance_count:              {bom['totals']['total_instance_count']}")
                print(f"  total_assembly_mass_kg:            {bom['totals']['total_assembly_mass_kg']}")
                print(f"  total_assembly_cost_one_unit_inr:  {bom['totals']['total_assembly_cost_one_unit_inr']}")
                if bom["missing_data"]:
                    print(f"  missing_data ({len(bom['missing_data'])}):")
                    for m in bom["missing_data"]:
                        print(f"    - {m['part_name']}: {m['reason']}")
            except ValueError as e:
                print(f"\nFAILED to build assembly BOM: {e}")


if __name__ == "__main__":
    main()
