"""Standalone script: dump every material from SolidWorks' loaded material
databases into materials.csv, leaving cost/carbon columns blank for manual
fill-in.

Run this with SolidWorks open (no part needs to be open, just the
application):

    C:\\Python314\\python.exe extract_materials_library.py

How it finds the databases: ISldWorks.GetMaterialDatabases is a zero-arg
property (dynamic COM dispatch auto-invokes it on attribute access) that
returns the file paths of every material database file currently
configured in SolidWorks (Options > File Locations > Material Databases).
Despite the ".sldmat" extension, these are plain UTF-16 XML documents, so
they're parsed directly with ElementTree rather than through the
COM/materials API.
"""

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pythoncom
import win32com.client

_OUTPUT_CSV = Path(__file__).parent / "materials.csv"
_FIELDNAMES = [
    "material_name",
    "cost_per_kg",
    "carbon_factor_kg_co2_per_kg",
    "density_kg_m3",
]


def _get_material_database_paths() -> list[str]:
    pythoncom.CoInitialize()
    sw_app = win32com.client.Dispatch("SldWorks.Application")
    paths = sw_app.GetMaterialDatabases
    return list(paths) if paths else []


def _parse_materials(db_path: str) -> list[dict]:
    """Parse one .sldmat XML file into {name, category, density_kg_m3} dicts.

    The root element (<mstns:materials>) declares the sldmaterials
    namespace under the "mstns" prefix, but its children (classification,
    material, physicalproperties, DENS, ...) are written unprefixed with
    no default xmlns, so they parse into ElementTree with *no* namespace
    at all -- confirmed by inspecting a live file, since naively
    namespace-qualifying these tags matches nothing.
    """
    tree = ET.parse(db_path)
    root = tree.getroot()

    materials = []
    for classification in root.findall("classification"):
        category = classification.get("name", "")
        for material in classification.findall("material"):
            name = material.get("name", "")
            if not name:
                continue
            density_elem = material.find("physicalproperties/DENS")
            density = density_elem.get("value") if density_elem is not None else ""
            materials.append(
                {"name": name, "category": category, "density_kg_m3": density}
            )
    return materials


def main() -> None:
    db_paths = _get_material_database_paths()
    if not db_paths:
        print(
            "No material databases returned by SolidWorks. "
            "Make sure SolidWorks is running."
        )
        return

    seen_names: set[str] = set()
    rows = []
    per_db_new_count: dict[str, int] = {}
    per_db_error: dict[str, str] = {}

    for db_path in db_paths:
        try:
            materials = _parse_materials(db_path)
        except Exception as e:
            per_db_error[db_path] = str(e)
            continue

        new_count = 0
        for m in materials:
            if m["name"] in seen_names:
                continue
            seen_names.add(m["name"])
            rows.append(
                {
                    "material_name": m["name"],
                    "cost_per_kg": "",
                    "carbon_factor_kg_co2_per_kg": "",
                    "density_kg_m3": m["density_kg_m3"],
                }
            )
            new_count += 1
        per_db_new_count[db_path] = new_count

    rows.sort(key=lambda r: r["material_name"].lower())

    with open(_OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} unique materials to {_OUTPUT_CSV}")
    print("\nBy database:")
    for db_path, count in per_db_new_count.items():
        print(f"  {count:4d} new materials  <-  {db_path}")
    for db_path, error in per_db_error.items():
        print(f"  FAILED to parse  <-  {db_path}  ({error})")


if __name__ == "__main__":
    main()
