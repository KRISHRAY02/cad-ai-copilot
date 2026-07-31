"""Looks up cost and carbon data for a material name (e.g. as returned by
SolidWorks' get_material()) against materials.csv, tolerating small
naming differences between SolidWorks' material name and however the
material is spelled in the CSV.
"""

import csv
import difflib
from pathlib import Path

_DEFAULT_CSV_PATH = Path(__file__).parent / "materials.csv"
_REQUIRED_COLUMNS = {
    "material_name",
    "cost_per_kg",
    "carbon_factor_kg_co2_per_kg",
    "density_kg_m3",
}
_FUZZY_MATCH_CUTOFF = 0.75


def _normalize(name: str) -> str:
    return name.strip().lower()


def _to_float_or_none(value: str):
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def load_materials(csv_path: Path | str = _DEFAULT_CSV_PATH) -> dict:
    """Read materials.csv into a dict keyed by normalized material name.

    Each value is a dict with the CSV's canonical (un-normalized)
    material_name plus cost_per_kg, carbon_factor_kg_co2_per_kg, and
    density_kg_m3 as floats (or None for blank cells).

    Raises FileNotFoundError with a message telling the user how to fix
    it, rather than a bare traceback, since this is meant to fail loudly
    at startup rather than surface as a confusing KeyError later.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"materials.csv not found at {csv_path}. Create it with these "
            f"exact columns: material_name, cost_per_kg, "
            f"carbon_factor_kg_co2_per_kg, density_kg_m3 (see "
            f"extract_materials_library.py to generate one from SolidWorks)."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not _REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError(
                f"{csv_path} is missing required columns. Expected exactly: "
                f"{', '.join(sorted(_REQUIRED_COLUMNS))}. Found: "
                f"{', '.join(reader.fieldnames or [])}."
            )

        materials = {}
        for row in reader:
            name = row["material_name"].strip()
            if not name:
                continue
            materials[_normalize(name)] = {
                "material_name": name,
                "cost_per_kg": _to_float_or_none(row["cost_per_kg"]),
                "carbon_factor_kg_co2_per_kg": _to_float_or_none(
                    row["carbon_factor_kg_co2_per_kg"]
                ),
                "density_kg_m3": _to_float_or_none(row["density_kg_m3"]),
            }

    return materials


def get_material_cost_and_carbon(
    material_name: str, materials: dict | None = None
) -> dict:
    """Look up a material's cost/carbon data, tolerating naming differences.

    Tries an exact match first (case/whitespace-insensitive). If that
    fails, falls back to difflib.get_close_matches against every name in
    materials.csv and uses the closest one, provided it clears
    _FUZZY_MATCH_CUTOFF. The result always includes "matched_from" (the
    canonical CSV name actually used) and "is_fuzzy_match", so a fuzzy
    hit is visible rather than silent. If nothing is close enough,
    returns found=False instead of guessing.
    """
    if materials is None:
        materials = load_materials()

    normalized_query = _normalize(material_name)

    entry = materials.get(normalized_query)
    is_fuzzy_match = False

    if entry is None:
        close = difflib.get_close_matches(
            normalized_query, materials.keys(), n=1, cutoff=_FUZZY_MATCH_CUTOFF
        )
        if close:
            entry = materials[close[0]]
            is_fuzzy_match = True

    if entry is None:
        return {
            "found": False,
            "message": (
                f"Material '{material_name}' not found in materials.csv "
                f"-- please add it."
            ),
        }

    return {
        "found": True,
        "material_name": entry["material_name"],
        "cost_per_kg": entry["cost_per_kg"],
        "carbon_factor_kg_co2_per_kg": entry["carbon_factor_kg_co2_per_kg"],
        "density_kg_m3": entry["density_kg_m3"],
        "matched_from": entry["material_name"],
        "is_fuzzy_match": is_fuzzy_match,
    }
