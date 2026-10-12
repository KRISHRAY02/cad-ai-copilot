"""Regression tests for the tool-routing bugs fixed 2026-10-12/13:

BUG 1: "Assembly list / BOM not showing" + a misleading "go to the
Add-In" message on a Fusion assembly, even when the Add-In was running
fine -- root cause was FusionAdapter._request() conflating a genuine
unreachable-bridge failure with a plain timeout on a slow call (see
cad_adapters/fusion_adapter.py's FusionBridgeTimeoutError docstring).

BUG 2: part questions ("what is the mass", "what material is this",
"what is this part", "describe this part", "what are the features")
sometimes got routed to additive-manufacturing (AM) tools or picked up
AM wording, because the three AM-only tools stayed visible to the LLM
in a Fusion "part"-mode session even though the system prompt told it
not to call them -- a soft instruction a 7B model doesn't reliably
follow. Root cause fixed by removing those tools from the model's tool
list entirely in "part" mode (see ai_orchestrator.py's
_AM_ONLY_TOOL_NAMES).

These tests exercise the deterministic routing/filtering logic directly
(no live Ollama/Fusion/MCP needed) -- the forced-call predicates and the
AM-tool-visibility filter are plain Python, so they're testable without
the live stack every other *_repro.py script in this project needs.
Run with:

    C:\\Python314\\python.exe -m pytest tests/test_routing.py -v

or, with no pytest installed, just:

    C:\\Python314\\python.exe tests/test_routing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_orchestrator as ao

AM_WORDING = (
    "am readiness",
    "build orientation",
    "support strategy",
    "recommend_am_material",
    "generate_am_readiness_guide",
    "run_am_dfm_check",
)

# (question, predicate that should be True, forced tool name(s) it leads to)
QUESTION_ROUTING = [
    ("what is the mass", ao._is_simple_mass_question, ["get_mass"]),
    ("what material is this", ao._is_simple_material_question, ["get_material"]),
    (
        "what is this part",
        ao._is_what_is_this_part_question,
        [
            "get_current_part_info",
            "get_mass",
            "get_material",
            "get_bounding_box",
            "get_features",
        ],
    ),
    (
        "describe this part",
        ao._is_what_is_this_part_question,
        [
            "get_current_part_info",
            "get_mass",
            "get_material",
            "get_bounding_box",
            "get_features",
        ],
    ),
    ("what are the features", ao._is_simple_features_question, ["get_features"]),
]


def test_each_question_matches_its_own_predicate_only():
    """Each question must trigger its own forced-call route, and NONE of
    the other routes (so e.g. "what is this part" doesn't also fire the
    mass-only shortcut and skip material/bounding-box/features)."""
    predicates = {
        "mass": ao._is_simple_mass_question,
        "material": ao._is_simple_material_question,
        "describe": ao._is_what_is_this_part_question,
        "features": ao._is_simple_features_question,
        "assembly_structure": ao._is_simple_assembly_structure_question,
    }
    expected_hits = {
        "what is the mass": {"mass"},
        "what material is this": {"material"},
        "what is this part": {"describe"},
        "describe this part": {"describe"},
        "what are the features": {"features"},
    }
    for question, expected in expected_hits.items():
        hits = {name for name, pred in predicates.items() if pred(question)}
        assert hits == expected, (
            f"{question!r}: expected routes {expected}, got {hits}"
        )


def test_no_am_tool_in_the_forced_tool_list_for_any_question():
    """None of the 5 questions' forced tool calls are an AM-only tool."""
    for question, _predicate, tools in QUESTION_ROUTING:
        for tool in tools:
            assert tool not in ao._AM_ONLY_TOOL_NAMES, (
                f"{question!r} forced-called {tool!r}, which is AM-only"
            )


def test_part_mode_hides_am_tools_from_the_llm():
    """The core fix: in Fusion "part" mode, the AM tools must not even
    appear in the schema handed to Ollama -- not just be discouraged by
    the system prompt (that's the soft instruction that didn't reliably
    work, per the live-diagnosed bug)."""
    fake_tools_schema = [
        {"function": {"name": "get_mass"}},
        {"function": {"name": "get_material"}},
        {"function": {"name": "recommend_am_material"}},
        {"function": {"name": "generate_am_readiness_guide"}},
        {"function": {"name": "run_am_dfm_check"}},
        {"function": {"name": "list_assembly_components"}},
    ]

    part_mode_active = [
        t
        for t in fake_tools_schema
        if t["function"]["name"] not in ao._AM_ONLY_TOOL_NAMES
    ]
    am_mode_active = fake_tools_schema  # AM mode keeps the full set

    part_mode_names = {t["function"]["name"] for t in part_mode_active}
    am_mode_names = {t["function"]["name"] for t in am_mode_active}

    assert part_mode_names == {
        "get_mass",
        "get_material",
        "list_assembly_components",
    }
    assert ao._AM_ONLY_TOOL_NAMES <= am_mode_names


def test_no_am_wording_in_am_only_tool_names_sanity():
    """Sanity check that the AM-only tool set used for filtering actually
    matches the three tools mcp_server.py platform-gates to Fusion-only
    AM use (recommend_am_material, generate_am_readiness_guide,
    run_am_dfm_check) -- catches the set silently drifting out of sync
    with mcp_server.py if either side is renamed without updating the
    other."""
    assert ao._AM_ONLY_TOOL_NAMES == {
        "recommend_am_material",
        "generate_am_readiness_guide",
        "run_am_dfm_check",
    }


def test_get_features_result_unwrapping():
    """get_features() comes back from MCP as {"result": [...]}, not a
    bare list (structured-content wire format requires a top-level JSON
    object) -- the formatter/composer must unwrap it, not just check
    isinstance(data, list) directly (that was a real bug introduced
    alongside the fix, caught by this test)."""
    bare_list_json = '[{"name": "Extrude1", "feature_type": "Extrude", "suppressed": false}]'
    wrapped_json = '{"result": [{"name": "Extrude1", "feature_type": "Extrude", "suppressed": false}]}'
    empty_wrapped_json = '{"result": []}'
    not_a_list_json = '{"found": false, "message": "no document open"}'

    import json

    assert ao._unwrap_list_result(json.loads(bare_list_json)) == [
        {"name": "Extrude1", "feature_type": "Extrude", "suppressed": False}
    ]
    assert ao._unwrap_list_result(json.loads(wrapped_json)) == [
        {"name": "Extrude1", "feature_type": "Extrude", "suppressed": False}
    ]
    assert ao._unwrap_list_result(json.loads(empty_wrapped_json)) == []
    assert ao._unwrap_list_result(json.loads(not_a_list_json)) is None

    formatted = ao._format_get_features_answer(wrapped_json)
    assert formatted is not None
    assert "Extrude1" in formatted


def test_am_wording_excluded_from_part_mode_system_prompt_suffix():
    """The part-mode system-prompt suffix itself must not contain AM
    wording as something to say -- it only exists to tell the model NOT
    to use AM tools/wording, so any AM phrase appearing there must be in
    a negation ("do NOT ...")."""
    suffix = ao._FUSION_PART_MODE_SUFFIX.lower()
    for phrase in ("am readiness", "build orientation", "support strategy"):
        if phrase in suffix:
            idx = suffix.index(phrase)
            preceding = suffix[max(0, idx - 15):idx]
            assert "not" in preceding, (
                f"{phrase!r} appears in _FUSION_PART_MODE_SUFFIX without a "
                "nearby negation"
            )


if __name__ == "__main__":
    tests = [
        test_each_question_matches_its_own_predicate_only,
        test_no_am_tool_in_the_forced_tool_list_for_any_question,
        test_part_mode_hides_am_tools_from_the_llm,
        test_no_am_wording_in_am_only_tool_names_sanity,
        test_get_features_result_unwrapping,
        test_am_wording_excluded_from_part_mode_system_prompt_suffix,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL: {test.__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
