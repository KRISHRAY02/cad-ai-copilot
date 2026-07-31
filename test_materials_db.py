"""Manual smoke test for materials_db.py's normalization and fuzzy
matching, using a small sample materials.csv written to a temp file
(never touches the real project materials.csv).

Run with:

    C:\\Python314\\python.exe test_materials_db.py
"""

import csv
import tempfile
from pathlib import Path

from materials_db import get_material_cost_and_carbon, load_materials

_SAMPLE_ROWS = [
    {
        "material_name": "Aluminum 6061-T6",
        "cost_per_kg": "2.50",
        "carbon_factor_kg_co2_per_kg": "8.24",
        "density_kg_m3": "2700",
    },
    {
        "material_name": "Stainless Steel 304",
        "cost_per_kg": "3.50",
        "carbon_factor_kg_co2_per_kg": "6.15",
        "density_kg_m3": "8000",
    },
    {
        "material_name": "ABS Plastic",
        "cost_per_kg": "",
        "carbon_factor_kg_co2_per_kg": "",
        "density_kg_m3": "1040",
    },
]


def _write_sample_csv(path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "material_name",
                "cost_per_kg",
                "carbon_factor_kg_co2_per_kg",
                "density_kg_m3",
            ],
        )
        writer.writeheader()
        writer.writerows(_SAMPLE_ROWS)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        sample_csv = Path(tmp_dir) / "sample_materials.csv"
        _write_sample_csv(sample_csv)
        materials = load_materials(sample_csv)

        test_queries = [
            "aluminum 6061-t6",  # different case, exact match
            "  Stainless Steel 304  ",  # extra whitespace, exact match
            "Aluminium 6061-T6",  # British spelling, should fuzzy-match
            "Stainless Steel304",  # missing space, should fuzzy-match
            "Unobtainium",  # nothing close, should report not found
        ]

        for query in test_queries:
            result = get_material_cost_and_carbon(query, materials)
            print(f"Query: {query!r}")
            print(f"  -> {result}")
            print()


if __name__ == "__main__":
    main()
