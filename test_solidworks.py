"""Manual smoke test for SolidWorksAdapter.

Run this with a part or assembly already open in a running SolidWorks
session:

    python test_solidworks.py
"""

from cad_adapters.solidworks_adapter import (
    NoDocumentOpenError,
    SolidWorksAdapter,
    SolidWorksNotRunningError,
)


def main() -> None:
    adapter = SolidWorksAdapter()

    try:
        adapter.connect()
    except SolidWorksNotRunningError as e:
        print(f"FAILED: could not reach SolidWorks. {e}")
        return
    except NoDocumentOpenError as e:
        print(f"FAILED: connected, but {e}")
        return

    print("Connected to SolidWorks.")

    part_info = adapter.get_current_part_info()
    print("Current part info:")
    print(f"  name:      {part_info.name}")
    print(f"  type:      {part_info.part_type}")
    print(f"  file_path: {part_info.file_path}")
    print(f"  units:     {part_info.units}")

    try:
        mass = adapter.get_mass()
        print(f"\nMass: {mass} kg")
    except Exception as e:
        print(f"\nFAILED to get mass: {e}")

    try:
        mass_properties = adapter.get_mass_properties()
        print("\nMass properties:")
        print(f"  mass_kg:         {mass_properties['mass_kg']}")
        print(f"  volume_m3:       {mass_properties['volume_m3']}")
        print(f"  surface_area_m2: {mass_properties['surface_area_m2']}")
    except Exception as e:
        print(f"\nFAILED to get mass properties: {e}")

    try:
        material = adapter.get_material()
        print("\nMaterial:")
        print(f"  name:          {material.name}")
        print(f"  density_kg_m3: {material.density_kg_m3}")
    except Exception as e:
        print(f"\nFAILED to get material: {e}")

    try:
        features = adapter.get_features()
        print(f"\nFeatures ({len(features)}):")
        for feature in features:
            suppressed_tag = " [suppressed]" if feature.suppressed else ""
            print(f"  - {feature.name} ({feature.feature_type}){suppressed_tag}")
    except Exception as e:
        print(f"\nFAILED to get features: {e}")


if __name__ == "__main__":
    main()
