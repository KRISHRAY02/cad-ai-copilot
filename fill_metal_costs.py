"""Standalone script: fill in cost_per_kg and carbon_factor_kg_co2_per_kg
for metal materials only in materials.csv, using representative published
averages per metal family (raw material market price and cradle-to-gate
embodied carbon, in the same spirit as the ICE database figures widely
used in coursework). Non-metal materials (plastics, rubber, wood, glass
fibers, etc.) are left blank, same as extract_materials_library.py left
them.

cost_per_kg is in INR. The source figures were researched in USD and
converted at a fixed approximate rate (_USD_TO_INR_RATE below) -- not a
live/current exchange rate, just enough to get INR-scale numbers.
carbon_factor_kg_co2_per_kg needs no currency conversion (kg CO2e/kg is
unit-independent of currency).

These are rough order-of-magnitude estimates, not live market quotes or a
supplier-specific LCA -- consistent with how mcp_server.py already
describes its cost/carbon heuristics. Sanity-check before relying on them
for anything beyond a rough estimate.

Run this with SolidWorks open (needed to re-derive each material's
category, since materials.csv itself doesn't store it):

    C:\\Python314\\python.exe fill_metal_costs.py
"""

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pythoncom
import win32com.client

_CSV_PATH = Path(__file__).parent / "materials.csv"

# Fixed approximate conversion, not a live rate -- see module docstring.
_USD_TO_INR_RATE = 86.0

# category -> (cost_usd_per_kg, carbon_kg_co2e_per_kg)
# Representative published averages for primary/typical-mix production,
# cost still in USD here -- converted to INR down in main().
_METAL_CATEGORY_VALUES_USD = {
    "Steel": (0.85, 1.9),
    "DIN Steel (Alloyed)": (1.20, 2.0),
    "DIN Steel (Free Cutting)": (0.95, 1.9),
    "DIN Steel (Hot Work Tool)": (3.50, 2.5),
    "DIN Steel (Nitriding Alloy)": (1.80, 2.1),
    "DIN Steel (Structural)": (0.80, 1.9),
    "DIN Steel (Toolmaking)": (3.50, 2.5),
    "DIN Steel (Unalloyed)": (0.80, 1.9),
    "DIN Steel (Stainless)": (3.50, 6.15),
    "Iron": (0.35, 1.7),
    "DIN Iron": (0.35, 1.7),
    "Aluminium Alloys": (2.50, 8.24),
    "DIN Aluminum Alloys": (2.50, 8.24),
    "Copper Alloys": (9.00, 3.99),
    "DIN Copper Alloys": (9.00, 3.99),
    "Titanium Alloys": (30.00, 35.0),
    "Zinc Alloys": (2.80, 3.31),
}

# Individual overrides for materials in the "Other Metals" / "Other
# Alloys" catch-all categories, where a single category-wide average
# would be wildly wrong (e.g. gold vs. lead). Cost still in USD here.
_METAL_NAME_OVERRIDES_USD = {
    "Beryllium": (850.0, 325.0),
    "Cobalt": (33.0, 8.0),
    "Molybdenum": (45.0, 19.0),
    "Nickel": (16.0, 12.8),
    "Pure Gold": (65000.0, 15000.0),
    "Pure Lead": (2.10, 1.87),
    "Pure Silver": (900.0, 2000.0),
    "Titanium": (30.0, 35.0),
    "Tungsten": (40.0, 35.0),
    "Vanadium": (27.0, 19.0),
    "Zirconium": (40.0, 30.0),
    "Duranickel(R) 301": (16.0, 12.8),
    "Magnesium Alloy": (5.00, 24.9),
    "Monel(R) 400": (18.0, 11.0),
}


def _to_inr(usd_and_carbon_table: dict) -> dict:
    """Convert a {name: (cost_usd, carbon)} table to {name: (cost_inr, carbon)}."""
    return {
        name: (round(cost_usd * _USD_TO_INR_RATE, 2), carbon)
        for name, (cost_usd, carbon) in usd_and_carbon_table.items()
    }


_METAL_CATEGORY_VALUES = _to_inr(_METAL_CATEGORY_VALUES_USD)
_METAL_NAME_OVERRIDES = _to_inr(_METAL_NAME_OVERRIDES_USD)

_OTHER_METAL_CATEGORIES = {"Other Metals", "Other Alloys"}


def _get_name_to_category() -> dict[str, str]:
    pythoncom.CoInitialize()
    sw_app = win32com.client.Dispatch("SldWorks.Application")
    db_paths = list(sw_app.GetMaterialDatabases)

    name_to_category = {}
    for db_path in db_paths:
        try:
            root = ET.parse(db_path).getroot()
        except Exception:
            continue
        for classification in root.findall("classification"):
            category = classification.get("name", "")
            for material in classification.findall("material"):
                name = material.get("name", "")
                if name and name not in name_to_category:
                    name_to_category[name] = category
    return name_to_category


def _lookup_metal_values(name: str, category: str) -> tuple[float, float] | None:
    if category in _OTHER_METAL_CATEGORIES:
        return _METAL_NAME_OVERRIDES.get(name)

    # SolidWorks' generic "Steel" category (unlike the separate DIN Steel
    # (Stainless) category) mixes stainless grades in with plain carbon/
    # alloy steel -- e.g. "AISI 316 Stainless Steel Sheet (SS)" sits right
    # next to "AISI 1020". Verified by dumping the category's contents
    # live. Detect by name instead of trusting the category for these.
    lowered = name.lower()
    if "stainless" in lowered:
        return _METAL_CATEGORY_VALUES["DIN Steel (Stainless)"]
    if "tool steel" in lowered:
        return _METAL_CATEGORY_VALUES["DIN Steel (Toolmaking)"]

    return _METAL_CATEGORY_VALUES.get(category)


def main() -> None:
    if not _CSV_PATH.exists():
        print(f"{_CSV_PATH} not found. Run extract_materials_library.py first.")
        return

    name_to_category = _get_name_to_category()

    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    filled = 0
    unmatched_metal_categories = set()

    for row in rows:
        name = row["material_name"]
        category = name_to_category.get(name, "")
        if not category:
            continue

        values = _lookup_metal_values(name, category)
        if values is not None:
            row["cost_per_kg"] = values[0]
            row["carbon_factor_kg_co2_per_kg"] = values[1]
            filled += 1
        elif category in _OTHER_METAL_CATEGORIES:
            unmatched_metal_categories.add(name)

    with open(_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Filled cost/carbon for {filled} metal materials in {_CSV_PATH}")
    if unmatched_metal_categories:
        print(
            "\nNo override value for these metals (left blank) -- add them "
            "to _METAL_NAME_OVERRIDES if you want them filled:"
        )
        for name in sorted(unmatched_metal_categories):
            print(f"  - {name}")


if __name__ == "__main__":
    main()
