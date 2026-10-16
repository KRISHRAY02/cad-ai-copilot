"""Tests for the additive-manufacturing (AM) readiness guide: the 6
axis-aligned build-orientation default set + dedup in
CadAdapter.compare_build_orientations() (base_adapter.py), and
mcp_server.generate_am_readiness_guide()'s top-3/printability-warnings/
Fusion-only-gating behavior built on top of it.

Uses MockAdapter's known synthetic geometry (cad_adapters/mock_adapter.py
-- a box with one 30-degree overhanging shelf face, see its own comment
for the exact face layout) so the expected best orientation and ranking
are a real computed result from known geometry, not a hardcoded fixture
checked against itself.

generate_am_readiness_guide() is gated to Fusion 360 only (checks
adapter.PLATFORM_ID) -- MockAdapter's PLATFORM_ID defaults to
"solidworks" (CadAdapter's base-class default; MockAdapter never
overrides it, and this test suite must not change that -- see
test_found_false_when_adapter_platform_is_not_fusion below, which relies
on exactly this default to prove the gate works without needing a real
SolidWorksAdapter/COM session). To exercise the gated code path, a
*separate* MockAdapter instance has its PLATFORM_ID overridden at the
instance level (not the class) purely within this test file -- this
never touches cad_adapters/mock_adapter.py itself, so every other
caller of MockAdapter() (cost/BOM tests, demos, etc.) is completely
unaffected.

Run with:

    C:\\Python314\\python.exe -m pytest tests/test_am_guide.py -v

or, with no pytest installed, just:

    C:\\Python314\\python.exe tests/test_am_guide.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_adapters.base_adapter import FaceGeometry
from cad_adapters.mock_adapter import MockAdapter
import mcp_server


def _fusion_mock_adapter() -> MockAdapter:
    """A MockAdapter instance that reports as Fusion 360 for the purposes
    of mcp_server's Fusion-only AM gate, without changing the MockAdapter
    class itself (see module docstring)."""
    adapter = MockAdapter()
    adapter.PLATFORM_ID = mcp_server._FUSION_PLATFORM_ID
    return adapter


def _with_adapter(adapter):
    """Context manager swapping mcp_server.adapter for the duration of a
    `with` block, restoring the original afterward -- mcp_server's AM
    tools read the module-level `adapter` global directly."""

    class _Swap:
        def __enter__(self_inner):
            self_inner.original = mcp_server.adapter
            mcp_server.adapter = adapter
            return adapter

        def __exit__(self_inner, *exc_info):
            mcp_server.adapter = self_inner.original

    return _Swap()


# -- compare_build_orientations(): 6-direction default set + dedup -------


def test_six_distinct_orientations_in_default_candidate_set():
    """mcp_server's default candidate set resolves to exactly 6 distinct
    effective build directions (the 6 axis-aligned directions), not 7 --
    "as-modeled" and the 5 explicit rotations never collide with each
    other for this geometry (or any geometry, since they're 6 genuinely
    different directions), so dedup should not have anything to remove
    here, but the count itself is the thing under test.
    """
    adapter = MockAdapter()
    results = adapter.compare_build_orientations(
        list(mcp_server._DEFAULT_AM_CANDIDATE_ORIENTATIONS)
    )
    assert len(results) == 6, f"expected 6 distinct orientations, got {len(results)}"

    directions = {r["effective_build_direction"] for r in results}
    assert len(directions) == 6, "expected 6 distinct effective build directions"


def test_dedup_removes_a_literal_repeat_and_keeps_first_label():
    """A candidate list with "as-modeled" listed twice collapses to one
    entry for +Z, keeping the first occurrence's label."""
    adapter = MockAdapter()
    results = adapter.compare_build_orientations(["as-modeled", "as-modeled", "rotated 180"])
    assert len(results) == 2
    labels = {r["orientation"] for r in results}
    assert labels == {"as-modeled", "rotated 180"}


def test_dedup_removes_a_different_label_with_the_same_effective_direction():
    """A Z-axis-only rotation never changes the effective build-up
    direction (rotating around Z stays in the XY plane) -- "rotated 45
    about Z" must collapse into "as-modeled"'s +Z result, not appear as a
    7th/separate orientation."""
    adapter = MockAdapter()
    results = adapter.compare_build_orientations(["as-modeled", "rotated 45 about Z"])
    assert len(results) == 1
    assert results[0]["orientation"] == "as-modeled"


def test_best_orientation_matches_known_geometry():
    """Known geometry (see module docstring): the 30-degree shelf face
    only overhangs when it's near the top, so pointing it sideways (not
    up or down) minimizes total overhang. By hand computation, the 4
    purely-horizontal directions (+Y/-Y/+X/-X) all tie at 3000mm^2 across
    1 region (one side wall each), beating both +Z (bottom + shelf,
    5400mm^2 across 2 regions) and -Z (top, 4800mm^2 across 1 region) --
    so the winner is whichever of those 4 appears first in the default
    candidate list: "rotated 90 about X" (+Y facing up).
    """
    adapter = MockAdapter()
    results = adapter.compare_build_orientations(
        list(mcp_server._DEFAULT_AM_CANDIDATE_ORIENTATIONS)
    )
    best = results[0]
    assert best["orientation"] == "rotated 90 about X"
    assert best["total_overhang_area_mm2"] == 3000.0
    assert best["overhang_region_count"] == 1

    # The 3 next-best (also 3000mm^2/1 region, tied) should immediately
    # follow, then -Z (4800/1), then +Z (5400/2) last.
    assert [r["total_overhang_area_mm2"] for r in results] == [
        3000.0, 3000.0, 3000.0, 3000.0, 4800.0, 5400.0,
    ]
    assert results[-1]["orientation"] == "as-modeled"
    assert results[-1]["overhang_region_count"] == 2


# -- generate_am_readiness_guide(): platform gate -------------------------


def test_found_false_when_adapter_platform_is_not_fusion():
    """generate_am_readiness_guide() must refuse (found=False) for any
    adapter whose PLATFORM_ID isn't "fusion360" -- exercised here via a
    plain, unmodified MockAdapter(), whose PLATFORM_ID defaults to
    "solidworks" (CadAdapter's base-class default). This is the same
    gate a real SolidWorksAdapter session would hit, driven by the same
    PLATFORM_ID check, without needing live SolidWorks/COM to prove it.
    """
    adapter = MockAdapter()
    assert adapter.PLATFORM_ID == "solidworks"
    with _with_adapter(adapter):
        result = mcp_server.generate_am_readiness_guide()
    assert result["found"] is False
    assert "fusion" in result["message"].lower()


# -- generate_am_readiness_guide(): full result shape ---------------------


def test_guide_top_orientations_and_warnings_structure():
    adapter = _fusion_mock_adapter()
    with _with_adapter(adapter):
        result = mcp_server.generate_am_readiness_guide()

    assert result["found"] is True

    # top_orientations: top 3, each with cm^2 + region count, matching
    # the ranking computed above.
    top = result["top_orientations"]
    assert len(top) == 3
    assert top[0]["orientation"] == "rotated 90 about X"
    assert top[0]["total_overhang_area_cm2"] == 30.0
    assert top[0]["overhang_region_count"] == 1
    assert {t["orientation"] for t in top} == {
        "rotated 90 about X", "rotated -90 about X", "rotated -90 about Y",
    }

    # printability_warnings / not_checked / checks_run: real
    # run_am_dfm_check() findings for the best orientation. Best
    # orientation's total overhang (3000mm^2) is above the negligible
    # threshold (25mm^2), so "overhang" must be flagged; the two
    # not-yet-implemented checks must be "not_checked", never silently
    # dropped or fabricated as pass/fail.
    warnings = result["printability_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["check"] == "overhang"
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["detail"]  # non-empty real message, not invented

    not_checked_names = {nc["check"] for nc in result["not_checked"]}
    assert not_checked_names == {"min_feature_size", "trapped_volume"}
    for nc in result["not_checked"]:
        assert nc["reason"]  # every not_checked entry carries a real reason

    assert result["checks_run"] == ["overhang"]

    # Fixed limits footer always present verbatim.
    assert result["limits_footer"] == mcp_server.AM_GUIDE_LIMITS_FOOTER

    # Support strategy: largest region (3000mm^2) clears the "broad flat
    # region" threshold (1500mm^2), so this should recommend dense
    # grid/block supports, not tree/branch.
    assert result["support_strategy"]["recommended_support_strategy"] == "dense_grid_block_supports"

    # No material questions were answered this call.
    assert result["material_questions_pending"] is True
    assert len(result["material_questions_to_ask"]) == 4


def test_guide_with_no_printability_warnings_names_the_checks_run():
    """When every check that actually runs comes back "pass" (not just
    "not_applicable"), printability_warnings must be empty but checks_run
    must still name what was actually checked, so a caller can say "no
    issues found by X" rather than a bare, over-broad "no issues".

    The bracket's real synthetic geometry always has at least one
    downward-facing wall in any orientation (it's a closed box -- see
    compare_build_orientations()'s own documented limitation that it
    can't tell which face actually rests on the print bed), so it can
    never produce zero overhang. A single-face, upward-only geometry is
    substituted here just for this one adapter instance (mutating only
    this test's own MockAdapter object, not the shared class) to exercise
    the genuinely-zero-overhang path honestly.
    """
    adapter = _fusion_mock_adapter()
    adapter._face_geometry = [
        FaceGeometry(normal=(0.0, 0.0, 1.0), area_mm2=100.0, adjacent_indices=[])
    ]
    findings = adapter.run_am_dfm_check("as-modeled")
    overhang = next(f for f in findings if f["check"] == "overhang")
    assert overhang["status"] == "pass"

    with _with_adapter(adapter):
        result = mcp_server.generate_am_readiness_guide(candidate_orientations=["as-modeled"])
    assert result["found"] is True
    assert result["printability_warnings"] == []
    assert result["checks_run"] == ["overhang"]
    not_checked_names = {nc["check"] for nc in result["not_checked"]}
    assert not_checked_names == {"min_feature_size", "trapped_volume"}


if __name__ == "__main__":
    tests = [
        test_six_distinct_orientations_in_default_candidate_set,
        test_dedup_removes_a_literal_repeat_and_keeps_first_label,
        test_dedup_removes_a_different_label_with_the_same_effective_direction,
        test_best_orientation_matches_known_geometry,
        test_found_false_when_adapter_platform_is_not_fusion,
        test_guide_top_orientations_and_warnings_structure,
        test_guide_with_no_printability_warnings_names_the_checks_run,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL: {test.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR: {test.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
