"""Looks up cost and carbon data for a material name (e.g. as returned by
a CadAdapter's get_material()) against a platform-specific materials CSV
(materials.csv for SolidWorks, materials_fusion.csv for Fusion 360),
tolerating small naming differences between the CAD software's material
name and however the material is spelled in the CSV.
"""

import csv
import difflib
from pathlib import Path

_DEFAULT_CSV_PATH = Path(__file__).parent / "materials.csv"
_FUSION_CSV_PATH = Path(__file__).parent / "materials_fusion.csv"

# Platform identifier -> which CSV backs it. Used by get_material_cost_and_carbon()
# when it's given a platform instead of a pre-loaded materials dict, and by
# CadAdapter subclasses (via csv_path_for_platform()) to keep their own
# per-platform materials cache pointed at the right file.
_PLATFORM_CSV_PATHS = {
    "solidworks": _DEFAULT_CSV_PATH,
    "fusion360": _FUSION_CSV_PATH,
}

_REQUIRED_COLUMNS = {
    "material_name",
    "cost_per_kg",
    "carbon_factor_kg_co2_per_kg",
}
# Present in materials.csv (extracted from SolidWorks' own .sldmat
# density values); absent from materials_fusion.csv, since Fusion
# already returns each material's real density live from its own API
# (see FusionAdapter.get_material()) rather than needing it from a
# spreadsheet. Optional, not required -- missing means every row's
# density_kg_m3 is None, same as a blank cell would be.
_OPTIONAL_DENSITY_COLUMN = "density_kg_m3"
_FUZZY_MATCH_CUTOFF = 0.75


def csv_path_for_platform(platform: str | None) -> Path:
    """Resolve a platform identifier ("solidworks"/"fusion360") to its CSV
    path. Unknown/None platform falls back to materials.csv (SolidWorks),
    matching this module's historical default behavior."""
    return _PLATFORM_CSV_PATHS.get(platform, _DEFAULT_CSV_PATH)


def _normalize(name: str) -> str:
    return name.strip().lower()


def _to_float_or_none(value: str):
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def load_materials(csv_path: Path | str = _DEFAULT_CSV_PATH) -> dict:
    """Read a materials CSV into a dict keyed by normalized material name.

    Works against either materials.csv (SolidWorks, has a density_kg_m3
    column) or materials_fusion.csv (Fusion, no density column -- Fusion
    reads real density live from its own API instead). Both share the
    same 3 required columns; density_kg_m3 is read if present, else
    every entry's density_kg_m3 is None.

    Each value is a dict with the CSV's canonical (un-normalized)
    material_name plus cost_per_kg, carbon_factor_kg_co2_per_kg, and
    density_kg_m3 as floats (or None for blank cells/a missing column).
    If the CSV has exactly one extra column beyond the required ones and
    density_kg_m3 (e.g. Fusion's material library's "Family/Basis" notes
    column), its value is kept under "notes" -- not used for matching,
    only carried through so get_material_cost_and_carbon() can surface
    it as "cost_basis".

    Raises FileNotFoundError with a message telling the user how to fix
    it, rather than a bare traceback, since this is meant to fail loudly
    at startup rather than surface as a confusing KeyError later.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path.name} not found at {csv_path}. Create it with these "
            f"exact columns: material_name, cost_per_kg, "
            f"carbon_factor_kg_co2_per_kg (density_kg_m3 optional; see "
            f"extract_materials_library.py to generate one from SolidWorks)."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if not _REQUIRED_COLUMNS.issubset(fieldnames):
            raise ValueError(
                f"{csv_path} is missing required columns. Expected at least: "
                f"{', '.join(sorted(_REQUIRED_COLUMNS))}. Found: "
                f"{', '.join(fieldnames)}."
            )
        has_density_column = _OPTIONAL_DENSITY_COLUMN in fieldnames
        known_columns = _REQUIRED_COLUMNS | (
            {_OPTIONAL_DENSITY_COLUMN} if has_density_column else set()
        )
        extra_columns = [c for c in fieldnames if c not in known_columns]
        notes_column = extra_columns[0] if len(extra_columns) == 1 else None

        materials = {}
        for row in reader:
            name = row["material_name"].strip()
            if not name:
                continue
            entry = {
                "material_name": name,
                "cost_per_kg": _to_float_or_none(row["cost_per_kg"]),
                "carbon_factor_kg_co2_per_kg": _to_float_or_none(
                    row["carbon_factor_kg_co2_per_kg"]
                ),
                "density_kg_m3": (
                    _to_float_or_none(row[_OPTIONAL_DENSITY_COLUMN])
                    if has_density_column
                    else None
                ),
            }
            if notes_column is not None:
                entry["notes"] = (row.get(notes_column) or "").strip() or None
            materials[_normalize(name)] = entry

    return materials


def get_material_cost_and_carbon(
    material_name: str, materials: dict | None = None, platform: str | None = None
) -> dict:
    """Look up a material's cost/carbon data, tolerating naming differences.

    Tries an exact match first (case/whitespace-insensitive). If that
    fails, falls back to difflib.get_close_matches against every name in
    the materials CSV and uses the closest one, provided it clears
    _FUZZY_MATCH_CUTOFF. The result always includes "matched_from" (the
    canonical CSV name actually used) and "is_fuzzy_match", so a fuzzy
    hit is visible rather than silent. If nothing is close enough,
    returns found=False instead of guessing.

    `materials` (a pre-loaded dict from load_materials()) takes priority
    when given, unchanged from before -- existing callers that already
    cache their own materials dict are unaffected. Only when `materials`
    is omitted does `platform` ("solidworks"/"fusion360") pick which CSV
    gets loaded fresh; an unrecognized/omitted platform falls back to
    materials.csv, matching this function's historical default.

    If the matched row came from a CSV with a notes/basis column (see
    load_materials()), it's included as "cost_basis" so the AI can
    optionally mention the figure is a representative estimate rather
    than a verified live quote -- None if the CSV has no such column or
    the matched row's cell is blank.
    """
    if materials is None:
        materials = load_materials(csv_path_for_platform(platform))

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
                f"Material '{material_name}' not found in "
                f"{csv_path_for_platform(platform).name} -- please add it."
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
        "cost_basis": entry.get("notes"),
    }
