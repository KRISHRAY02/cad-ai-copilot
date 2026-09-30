"""Rule-based additive-manufacturing material recommendation.

Backs the recommend_am_material MCP tool (see mcp_server.py). This is
deliberately NOT geometry-based -- material choice depends on functional/
use-case answers only a human can give (what the part does, whether it's
load-bearing, what temperatures it will see, and which tradeoff matters
most), none of which can be read off the CAD model. See
recommend_am_material()'s MCP tool docstring in mcp_server.py for why the
AI orchestrator must ask the user these questions conversationally before
ever calling this, rather than guessing them.

Matches the user's answers against am_materials.csv, a small reference
table of common AM process/material combinations with qualitative
strength/cost/surface-finish tiers and an approximate maximum service
temperature -- representative published figures for each material
category, not a live material database or a substitute for a real
datasheet.
"""

import csv
from pathlib import Path

_DEFAULT_CSV_PATH = Path(__file__).parent / "am_materials.csv"

_REQUIRED_COLUMNS = {
    "process_type",
    "material",
    "strength_tier",
    "max_service_temp_c",
    "cost_tier",
    "surface_finish_tier",
    "load_bearing_suitable",
    "notes",
}

_TIER_RANK = {"low": 0, "medium": 1, "high": 2}

# Minimum max_service_temp_c a material must be rated for to be considered
# for each temperature_exposure answer -- named, adjustable thresholds
# (same pattern as cad_adapters/dfm_checks.py's MIN_HOLE_DIAMETER_MM etc.)
# rather than magic numbers inline. Deliberately conservative: even
# "moderate" exposure (e.g. near a motor, an enclosed outdoor housing in
# summer sun) can exceed PLA's ~50C heat deflection temperature.
TEMPERATURE_EXPOSURE_MIN_SERVICE_TEMP_C = {
    "low": 40.0,
    "moderate": 65.0,
    "high": 110.0,
}

VALID_PRIORITIES = {"strength", "cost", "surface_finish"}

# How many ranked candidates to return -- a shortlist to discuss with the
# user, not an exhaustive dump of every material that qualified.
_TOP_N_RECOMMENDATIONS = 3


def _to_bool(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "1")


def load_am_materials(csv_path: Path | str = _DEFAULT_CSV_PATH) -> list[dict]:
    """Load am_materials.csv into a list of row dicts with proper types.

    Raises FileNotFoundError/ValueError with an actionable message rather
    than a bare traceback -- same pattern as materials_db.load_materials().
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path.name} not found at {csv_path}. Create it with "
            f"columns: {', '.join(sorted(_REQUIRED_COLUMNS))}."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if not _REQUIRED_COLUMNS.issubset(fieldnames):
            raise ValueError(
                f"{csv_path} is missing required columns. Expected: "
                f"{', '.join(sorted(_REQUIRED_COLUMNS))}. Found: "
                f"{', '.join(fieldnames)}."
            )

        rows = []
        for row in reader:
            material = (row.get("material") or "").strip()
            if not material:
                continue
            rows.append(
                {
                    "process_type": row["process_type"].strip(),
                    "material": material,
                    "strength_tier": row["strength_tier"].strip().lower(),
                    "max_service_temp_c": float(row["max_service_temp_c"]),
                    "cost_tier": row["cost_tier"].strip().lower(),
                    "surface_finish_tier": row["surface_finish_tier"].strip().lower(),
                    "load_bearing_suitable": _to_bool(row["load_bearing_suitable"]),
                    "notes": (row.get("notes") or "").strip(),
                }
            )
    return rows


def _build_reason(
    row: dict, priority: str, load_bearing: bool, temperature_exposure: str
) -> str:
    """One-line, data-grounded explanation for why this specific row was
    recommended -- cites the actual tier values and thresholds involved,
    not just a bare material name.
    """
    priority_clause = {
        "strength": "ranked highest on strength among the materials that qualified",
        "cost": "ranked lowest-cost among the materials that qualified",
        "surface_finish": "ranked best on as-printed surface finish among the materials that qualified",
    }[priority]

    load_clause = " and is rated suitable for load-bearing use" if load_bearing else ""

    return (
        f"{row['strength_tier']} strength, rated to {row['max_service_temp_c']:.0f}°C, "
        f"{row['cost_tier']} cost, {row['surface_finish_tier']} surface finish -- meets the "
        f"'{temperature_exposure}' temperature requirement{load_clause}; {priority_clause}."
    )


def recommend_am_material(
    part_function: str,
    load_bearing: bool,
    temperature_exposure: str,
    priority: str,
) -> dict:
    """Rule-based AM material shortlist. See recommend_am_material()'s MCP
    tool wrapper in mcp_server.py for the full contract, valid input
    values, and why the caller must have already asked the user for these
    answers rather than guessing them.

    Pipeline: filter am_materials.csv rows to those meeting the
    temperature_exposure requirement (TEMPERATURE_EXPOSURE_MIN_SERVICE_TEMP_C),
    then (if load_bearing) further filter to load_bearing_suitable rows
    only, then rank what's left by `priority` and return the top
    _TOP_N_RECOMMENDATIONS with a one-line, data-grounded reason each.

    `part_function` is carried through in the response for context/
    explainability only -- am_materials.csv has no per-application
    suitability column, so it deliberately does not filter or re-rank the
    candidate list (never pretends to use data that doesn't exist).

    Returns found=False with an explanatory message (never guesses) if
    `temperature_exposure`/`priority` aren't recognized values, or if no
    material in am_materials.csv meets the combined temperature/
    load-bearing requirement.
    """
    temperature_exposure_normalized = temperature_exposure.strip().lower()
    if temperature_exposure_normalized not in TEMPERATURE_EXPOSURE_MIN_SERVICE_TEMP_C:
        return {
            "found": False,
            "message": (
                "temperature_exposure must be one of "
                f"{sorted(TEMPERATURE_EXPOSURE_MIN_SERVICE_TEMP_C)}. Ask the "
                "user to describe the part's expected temperature exposure "
                "in one of these terms, then call recommend_am_material "
                "again."
            ),
        }

    priority_normalized = priority.strip().lower()
    if priority_normalized not in VALID_PRIORITIES:
        return {
            "found": False,
            "message": (
                f"priority must be one of {sorted(VALID_PRIORITIES)}. Ask "
                "the user which of these matters most for this part, then "
                "call recommend_am_material again."
            ),
        }

    try:
        materials = load_am_materials()
    except (FileNotFoundError, ValueError) as e:
        return {"found": False, "message": str(e)}

    min_temp = TEMPERATURE_EXPOSURE_MIN_SERVICE_TEMP_C[temperature_exposure_normalized]
    candidates = [m for m in materials if m["max_service_temp_c"] >= min_temp]

    if load_bearing:
        candidates = [m for m in candidates if m["load_bearing_suitable"]]

    if not candidates:
        return {
            "found": False,
            "message": (
                "No material in am_materials.csv meets both the "
                f"'{temperature_exposure_normalized}' temperature requirement "
                f"(>= {min_temp:.0f}°C service temp)"
                + (" and load-bearing suitability" if load_bearing else "")
                + " -- consider relaxing one of these requirements with the "
                "user, or add a suitable entry to am_materials.csv."
            ),
        }

    if priority_normalized == "strength":
        candidates.sort(
            key=lambda m: (_TIER_RANK[m["strength_tier"]], m["max_service_temp_c"]),
            reverse=True,
        )
    elif priority_normalized == "cost":
        candidates.sort(key=lambda m: _TIER_RANK[m["cost_tier"]])
    else:  # surface_finish
        candidates.sort(
            key=lambda m: (_TIER_RANK[m["surface_finish_tier"]], _TIER_RANK[m["strength_tier"]]),
            reverse=True,
        )

    top = candidates[:_TOP_N_RECOMMENDATIONS]

    recommendations = [
        {
            "material": row["material"],
            "process_type": row["process_type"],
            "strength_tier": row["strength_tier"],
            "max_service_temp_c": row["max_service_temp_c"],
            "cost_tier": row["cost_tier"],
            "surface_finish_tier": row["surface_finish_tier"],
            "load_bearing_suitable": row["load_bearing_suitable"],
            "reason": _build_reason(
                row, priority_normalized, load_bearing, temperature_exposure_normalized
            ),
        }
        for row in top
    ]

    return {
        "found": True,
        "part_function": part_function,
        "load_bearing": load_bearing,
        "temperature_exposure": temperature_exposure_normalized,
        "priority": priority_normalized,
        "recommendations": recommendations,
        "assumptions": (
            "Ranked from am_materials.csv's qualitative strength/cost/"
            "surface_finish tiers and approximate max service temperature "
            "per material -- representative published figures, not a live "
            "material database or a substitute for a real datasheet. "
            "part_function is included above for context only; it does not "
            "filter or re-rank the candidate list."
        ),
    }
