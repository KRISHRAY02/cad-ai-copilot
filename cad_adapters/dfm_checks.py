"""Shared constants and result-shape helper for CadAdapter.run_dfm_check().

Four checks, each protecting against a specific real manufacturing
problem (see docstrings on SolidWorksAdapter._check_* / MockAdapter's
run_dfm_check() for the plain-language "why"):

1. Hole geometry (diameter, depth:diameter ratio) -- drills that are too
   thin or too deep relative to their diameter break or wander.
2. Wall thickness (Shell features only) -- walls too thin for the
   process (injection molding, casting) warp, sink, or don't fill.
3. Draft angle -- insufficient draft on molded/cast faces makes parts
   stick in the mold/die, tearing or scoring the surface on ejection.
4. Dimension tolerance -- tolerances tighter than a shop's normal
   process capability cost disproportionately more (special tooling,
   100% inspection, higher scrap rate).

Every finding is a dict: {"check", "status", "feature", "message", ...}.
"status" is always one of "flagged", "pass", "not_applicable" -- never a
guessed/estimated number substituting for real feature or dimension data
that isn't actually present on the part.
"""

# ---------------------------------------------------------------------
# DFM thresholds -- tune these here, not in the check logic itself.
# ---------------------------------------------------------------------
MIN_HOLE_DIAMETER_MM = 1.0
MAX_HOLE_DEPTH_TO_DIAMETER_RATIO = 4.0
MIN_WALL_THICKNESS_MM = 1.0
MIN_DRAFT_ANGLE_DEG = 1.0
MIN_TOLERANCE_BAND_MM = 0.001


def make_finding(check: str, status: str, feature: str | None = None, message: str = "", **extra) -> dict:
    """Build one DFM finding dict with a consistent shape.

    `check` is one of "hole", "wall_thickness", "draft_angle",
    "tolerance". `status` is one of "flagged", "pass", "not_applicable".
    `extra` holds check-specific numeric fields (e.g. diameter_mm) when
    there's real data to report.
    """
    finding = {"check": check, "status": status, "feature": feature, "message": message}
    finding.update(extra)
    return finding
