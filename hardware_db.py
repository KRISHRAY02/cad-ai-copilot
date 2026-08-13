"""Looks up purchased-hardware unit prices against standard_hardware.csv,
and classifies an assembly component as "Make" (custom-manufactured, cost
comes from the material+production cost model) or "Buy" (purchased
standard hardware, cost comes from a real purchased unit price) -- the
same distinction a real BOM makes between a machined bracket and a
catalog bolt.

Mirrors materials_db.py's exact-then-fuzzy matching approach (same
normalization, same difflib cutoff) so a component's name only has to be
*close* to a standard_hardware.csv row, not byte-identical, to be priced.
"""

import csv
import difflib
from pathlib import Path

_DEFAULT_CSV_PATH = Path(__file__).parent / "standard_hardware.csv"
_REQUIRED_COLUMNS = {"part_name_pattern", "unit_cost"}
_FUZZY_MATCH_CUTOFF = 0.75

# Substrings (checked case-insensitively) that identify a component's file
# path as coming from SOLIDWORKS' Toolbox library of standard hardware
# (bolts, nuts, washers, bearings, ...) rather than a custom-designed part.
# Toolbox installs default to a "SOLIDWORKS Data\browser\...\Toolbox"-style
# path, but the exact folder name varies by SOLIDWORKS version/install --
# "toolbox" itself is the one substring guaranteed to appear regardless.
_TOOLBOX_PATH_MARKERS = ("toolbox",)


def _normalize(name: str) -> str:
    return name.strip().lower()


def load_hardware(csv_path: Path | str = _DEFAULT_CSV_PATH) -> dict:
    """Read standard_hardware.csv into a dict keyed by normalized pattern.

    Raises FileNotFoundError with a message telling the user the exact
    required columns if the file's missing -- same defensive pattern as
    materials_db.load_materials().
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"standard_hardware.csv not found at {csv_path}. Create it with "
            f"these exact columns: part_name_pattern, unit_cost (one row per "
            f"standard hardware item you buy, e.g. 'M6x20 Hex Bolt', 45)."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not _REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError(
                f"{csv_path} is missing required columns. Expected exactly: "
                f"{', '.join(sorted(_REQUIRED_COLUMNS))}. Found: "
                f"{', '.join(reader.fieldnames or [])}."
            )

        hardware = {}
        for row in reader:
            pattern = row["part_name_pattern"].strip()
            if not pattern:
                continue
            unit_cost_raw = (row["unit_cost"] or "").strip()
            hardware[_normalize(pattern)] = {
                "part_name_pattern": pattern,
                "unit_cost": float(unit_cost_raw) if unit_cost_raw else None,
            }

    return hardware


def get_hardware_unit_cost(name: str, hardware: dict | None = None) -> dict:
    """Look up a component name's purchased unit cost, tolerating naming
    differences (exact match first, then fuzzy) -- same approach as
    materials_db.get_material_cost_and_carbon().

    Returns found=False instead of guessing if nothing clears the fuzzy
    match cutoff.
    """
    if hardware is None:
        hardware = load_hardware()

    normalized_query = _normalize(name)
    entry = hardware.get(normalized_query)
    is_fuzzy_match = False

    if entry is None:
        close = difflib.get_close_matches(
            normalized_query, hardware.keys(), n=1, cutoff=_FUZZY_MATCH_CUTOFF
        )
        if close:
            entry = hardware[close[0]]
            is_fuzzy_match = True

    if entry is None:
        return {
            "found": False,
            "message": f"'{name}' not found in standard_hardware.csv.",
        }

    return {
        "found": True,
        "matched_from": entry["part_name_pattern"],
        "unit_cost": entry["unit_cost"],
        "is_fuzzy_match": is_fuzzy_match,
    }


def is_toolbox_path(file_path: str) -> bool:
    """True if `file_path` looks like it comes from the SOLIDWORKS Toolbox
    standard-hardware library rather than a custom-designed part.
    """
    lowered = (file_path or "").lower()
    return any(marker in lowered for marker in _TOOLBOX_PATH_MARKERS)


def classify_component(
    part_name: str, file_path: str, hardware: dict | None = None
) -> dict:
    """Classify an assembly component as "Make" or "Buy".

    The real-world distinction: a "Make" part is something this shop (or
    its subcontractor) actually manufactures from raw material -- its cost
    has to be *estimated* from geometry and material (the existing
    material+production cost model). A "Buy" part is purchased complete
    from a supplier's catalog at a fixed price -- estimating its cost from
    geometry would be meaningless (a Hex bolt's price isn't a function of
    its CAD volume), so it needs a real purchased unit price instead.

    Classification logic, in order:
    1. If `part_name` (or its file name) matches a standard_hardware.csv
       pattern (exact-then-fuzzy, same as materials_db), classify "Buy"
       and use that CSV's unit_cost -- regardless of where the file lives,
       since a purchased part could be modeled outside Toolbox too.
    2. Else if `file_path` looks like a SOLIDWORKS Toolbox library path,
       classify "Buy" but with unit_cost=None and classification
       "Buy - price not available", since it's clearly purchased hardware
       but this shop hasn't priced it yet -- never guess a cost here.
    3. Otherwise classify "Make" -- cost comes from the material +
       production cost model (see production_cost.py), computed by the
       caller.
    """
    if hardware is None:
        try:
            hardware = load_hardware()
        except (FileNotFoundError, ValueError):
            hardware = {}

    file_stem = Path(file_path).stem if file_path else ""
    lookup = get_hardware_unit_cost(part_name, hardware)
    if not lookup["found"] and file_stem:
        lookup = get_hardware_unit_cost(file_stem, hardware)

    if lookup["found"] and lookup["unit_cost"] is not None:
        return {
            "classification": "Buy",
            "unit_cost_inr": lookup["unit_cost"],
            "matched_pattern": lookup["matched_from"],
            "is_fuzzy_match": lookup["is_fuzzy_match"],
        }

    if is_toolbox_path(file_path):
        return {
            "classification": "Buy - price not available",
            "unit_cost_inr": None,
            "matched_pattern": None,
            "is_fuzzy_match": False,
        }

    return {
        "classification": "Make",
        "unit_cost_inr": None,  # computed by the caller from material + production cost
        "matched_pattern": None,
        "is_fuzzy_match": False,
    }
