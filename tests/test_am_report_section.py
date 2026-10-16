"""Tests for the optional "Additive Manufacturing Readiness" section in
report/generate_report.py's generate_manufacturing_report().

Covers the three behaviors the task asked for explicitly:
  1. A report generated after generate_am_readiness_guide() ran (for the
     SAME part, on a Fusion-platform adapter) contains the section.
  2. A report generated with no stored guide result is unchanged -- no
     placeholder/empty section.
  3. A non-Fusion ("solidworks") adapter never gets the section, even if
     a guide result happens to be stored (platform check wins).

Uses MockAdapter (synthetic geometry, no real CAD/Ollama needed) and, for
test 1, a real PLATFORM_ID override on the instance only -- see
tests/test_am_guide.py's module docstring for why this doesn't touch
cad_adapters/mock_adapter.py itself.

Needs `pypdf` to read back the generated PDF's text (an ad hoc dev/test
dependency, not added to requirements.txt -- same choice already made for
report/generate_report.py's own verification history; the app itself
never reads PDFs, only writes them). If pypdf isn't installed, this file
reports that plainly and skips rather than failing.

Run with:

    C:\\Python314\\python.exe -m pip install pypdf   # once, if needed
    C:\\Python314\\python.exe tests/test_am_report_section.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_adapters.mock_adapter import MockAdapter
import mcp_server

try:
    from pypdf import PdfReader
    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

_AM_SECTION_HEADING = "Additive Manufacturing Readiness"


def _pdf_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _with_adapter(adapter):
    class _Swap:
        def __enter__(self_inner):
            self_inner.original = mcp_server.adapter
            mcp_server.adapter = adapter
            return adapter

        def __exit__(self_inner, *exc_info):
            mcp_server.adapter = self_inner.original

    return _Swap()


def _fusion_mock_adapter() -> MockAdapter:
    adapter = MockAdapter()
    adapter.PLATFORM_ID = mcp_server._FUSION_PLATFORM_ID
    return adapter


def _reset_last_am_guide_result():
    mcp_server._last_am_guide_result = None


def test_report_contains_am_section_after_guide_ran_for_same_part():
    _reset_last_am_guide_result()
    adapter = _fusion_mock_adapter()
    with _with_adapter(adapter):
        guide_result = mcp_server.generate_am_readiness_guide(
            candidate_orientations=["as-modeled", "rotated 180"]
        )
        assert guide_result["found"] is True

        from report.generate_report import generate_manufacturing_report

        output_path = os.path.join(tempfile.gettempdir(), "test_am_report_with_section.pdf")
        generate_manufacturing_report(output_path)

    if not _PYPDF_AVAILABLE:
        print("  (pypdf not installed -- skipping PDF text verification)")
        return
    text = _pdf_text(output_path)
    assert _AM_SECTION_HEADING in text
    assert "Check before printing" in text
    os.remove(output_path)


def test_report_without_stored_guide_result_has_no_am_section():
    _reset_last_am_guide_result()
    adapter = _fusion_mock_adapter()
    with _with_adapter(adapter):
        from report.generate_report import generate_manufacturing_report

        output_path = os.path.join(tempfile.gettempdir(), "test_am_report_without_section.pdf")
        generate_manufacturing_report(output_path)

    if not _PYPDF_AVAILABLE:
        print("  (pypdf not installed -- skipping PDF text verification)")
        return
    text = _pdf_text(output_path)
    assert _AM_SECTION_HEADING not in text
    os.remove(output_path)


def test_solidworks_platform_never_gets_the_section_even_with_a_stored_result():
    """A guide result IS stored (computed on a Fusion-platform adapter for
    this part), but the report is then generated with a non-Fusion
    ("solidworks") adapter for the SAME part -- the platform check must
    still win and exclude the section, since generate_am_readiness_guide()
    itself would have refused (found=False) on a non-Fusion platform in
    the first place.
    """
    _reset_last_am_guide_result()
    fusion_adapter = _fusion_mock_adapter()
    with _with_adapter(fusion_adapter):
        guide_result = mcp_server.generate_am_readiness_guide()
        assert guide_result["found"] is True

    solidworks_adapter = MockAdapter()
    assert solidworks_adapter.PLATFORM_ID == "solidworks"
    # Same file_path as the Fusion adapter used above (both are plain
    # MockAdapter() instances with the same hardcoded part info), so the
    # part-identity check alone would otherwise still match.
    assert solidworks_adapter.get_current_part_info().file_path == \
        fusion_adapter.get_current_part_info().file_path

    with _with_adapter(solidworks_adapter):
        from report.generate_report import generate_manufacturing_report

        output_path = os.path.join(tempfile.gettempdir(), "test_am_report_solidworks.pdf")
        generate_manufacturing_report(output_path)

    if not _PYPDF_AVAILABLE:
        print("  (pypdf not installed -- skipping PDF text verification)")
        return
    text = _pdf_text(output_path)
    assert _AM_SECTION_HEADING not in text
    os.remove(output_path)


if __name__ == "__main__":
    tests = [
        test_report_contains_am_section_after_guide_ran_for_same_part,
        test_report_without_stored_guide_result_has_no_am_section,
        test_solidworks_platform_never_gets_the_section_even_with_a_stored_result,
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
