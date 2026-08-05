"""Assembles a one-click "manufacturing readiness report" PDF out of
everything the copilot already knows how to compute about the currently
open CAD part: identity, mass, material, feature tree, a viewport
screenshot, a cost estimate (material + production cost breakdown, see
production_cost.py), a carbon estimate, and DFM check results.

Reuses mcp_server.py's already-built `adapter` and its `estimate_cost`
tool function directly (both are plain, directly-callable objects even
though `estimate_cost` is also MCP-tool-decorated -- see mcp_server.py),
rather than re-implementing cost-model feature assembly here, so the
report always reflects exactly the same numbers a chat answer would show.

Built with reportlab (see requirements.txt).
"""

import datetime
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from mcp_server import adapter, estimate_cost

_PAGE_MARGIN_MM = 18
_MAX_IMAGE_WIDTH_MM = 170
_MAX_IMAGE_HEIGHT_MM = 100

# Defaults used only when the caller doesn't tell generate_manufacturing_report
# what the user actually asked for -- always reported explicitly in the PDF
# as defaults, never silently presented as the user's real request. Matches
# estimate_cost()'s own "CNC 3-axis is the general-purpose default" choice
# in mcp_server.py.
_DEFAULT_MANUFACTURING_PROCESS = "CNC Machining"
_DEFAULT_QUANTITY = 1

_DFM_STATUS_COLORS = {
    "flagged": colors.HexColor("#B23A3A"),
    "pass": colors.HexColor("#2E7D4F"),
    "not_applicable": colors.HexColor("#8A8F98"),
}


def _styles():
    stylesheet = getSampleStyleSheet()
    stylesheet.add(
        ParagraphStyle(
            "ReportTitle",
            parent=stylesheet["Title"],
            fontSize=20,
            spaceAfter=2,
        )
    )
    stylesheet.add(
        ParagraphStyle(
            "ReportSubtitle",
            parent=stylesheet["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#5B6B85"),
            spaceAfter=16,
        )
    )
    stylesheet.add(
        ParagraphStyle(
            "SectionHeading",
            parent=stylesheet["Heading2"],
            fontSize=13,
            spaceBefore=16,
            spaceAfter=6,
            textColor=colors.HexColor("#1B2A41"),
        )
    )
    stylesheet.add(
        ParagraphStyle(
            "Note",
            parent=stylesheet["Normal"],
            fontSize=8.5,
            textColor=colors.HexColor("#5B6B85"),
            spaceBefore=4,
        )
    )
    return stylesheet


def _table(rows: list[list[str]], col_widths=None) -> Table:
    table = Table(rows, colWidths=col_widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B2A41")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDE3EC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _screenshot_flowable(screenshot_path: str) -> Image:
    with PILImage.open(screenshot_path) as im:
        px_width, px_height = im.size

    max_w = _MAX_IMAGE_WIDTH_MM * mm
    max_h = _MAX_IMAGE_HEIGHT_MM * mm
    aspect = px_height / px_width
    width = max_w
    height = width * aspect
    if height > max_h:
        height = max_h
        width = height / aspect

    return Image(screenshot_path, width=width, height=height)


def _cost_section(styles, manufacturing_process: str, quantity: int, process_is_default: bool, quantity_is_default: bool) -> list:
    flowables = [Paragraph("Cost Estimate", styles["SectionHeading"])]

    assumption_note = (
        f"Manufacturing process: {manufacturing_process}"
        + (" (default -- not specified by the user)" if process_is_default else "")
        + f" | Quantity: {quantity}"
        + (" (default -- not specified by the user)" if quantity_is_default else "")
    )
    flowables.append(Paragraph(assumption_note, styles["Note"]))

    result = estimate_cost(manufacturing_process=manufacturing_process, quantity=quantity)
    if not result.get("found"):
        flowables.append(
            Paragraph(f"Cost estimate unavailable: {result.get('message', 'unknown error')}", styles["Normal"])
        )
        return flowables

    rows = [
        ["Item", "Value"],
        ["Material used", result["material_used"]],
        ["Material cost / unit", f"Rs {result['material_cost_per_unit_inr']:.2f}"],
        ["Production cost / unit", f"Rs {result['production_cost_per_unit_inr']:.2f}"],
        ["Total cost / unit", f"Rs {result['total_cost_per_unit_inr']:.2f}"],
        ["Total for quantity", f"Rs {result['total_cost_for_quantity_inr']:.2f}"],
        ["ML-predicted total / unit (comparison only)", f"Rs {result['ml_predicted_total_cost_per_unit_inr']:.2f}"],
    ]
    flowables.append(_table(rows, col_widths=[85 * mm, 85 * mm]))
    flowables.append(Paragraph(result["production_cost_assumptions"], styles["Note"]))
    flowables.append(Paragraph(result["assumptions"], styles["Note"]))
    return flowables


def _carbon_section(styles) -> list:
    flowables = [Paragraph("Carbon Estimate", styles["SectionHeading"])]
    result = adapter.estimate_carbon()
    if not result.get("found"):
        flowables.append(
            Paragraph(f"Carbon estimate unavailable: {result.get('message', 'unknown error')}", styles["Normal"])
        )
        return flowables

    rows = [
        ["Item", "Value"],
        ["Estimated embodied carbon", f"{result['estimated_kg_co2e']:.3f} kg CO2e"],
        ["Carbon factor", f"{result['carbon_factor_kg_co2_per_kg']} kg CO2e/kg"],
        ["Material matched", result["material_matched"] + (" (fuzzy match)" if result["is_fuzzy_match"] else "")],
    ]
    flowables.append(_table(rows, col_widths=[85 * mm, 85 * mm]))
    flowables.append(Paragraph(result["assumptions"], styles["Note"]))
    return flowables


def _dfm_section(styles) -> list:
    flowables = [Paragraph("DFM Check Results", styles["SectionHeading"])]
    findings = adapter.run_dfm_check()

    rows = [["Check", "Status", "Feature", "Message"]]
    for finding in findings:
        rows.append(
            [
                finding["check"].replace("_", " ").title(),
                finding["status"].replace("_", " ").title(),
                finding.get("feature") or "-",
                finding.get("message", ""),
            ]
        )

    table = _table(rows, col_widths=[28 * mm, 24 * mm, 30 * mm, 88 * mm])
    for row_index, finding in enumerate(findings, start=1):
        status_color = _DFM_STATUS_COLORS.get(finding["status"])
        if status_color:
            table.setStyle(
                TableStyle(
                    [("TEXTCOLOR", (1, row_index), (1, row_index), status_color)]
                )
            )
    flowables.append(table)
    return flowables


def generate_manufacturing_report(
    output_path: str,
    manufacturing_process: str | None = None,
    quantity: int | None = None,
) -> str:
    """Generate a manufacturing readiness report PDF for the currently
    open CAD part and save it to `output_path`.

    `manufacturing_process` and `quantity` should be whatever the user
    most recently specified in the conversation, if anything -- pass
    them through when known. If either is None, a reasonable default is
    used instead (CNC Machining / quantity 1) and the PDF explicitly
    labels that value as a default, so a reader never mistakes an
    assumption for something the user actually asked for.

    Returns `output_path` on success.
    """
    process_is_default = manufacturing_process is None
    quantity_is_default = quantity is None
    manufacturing_process = manufacturing_process or _DEFAULT_MANUFACTURING_PROCESS
    quantity = quantity or _DEFAULT_QUANTITY

    part_info = adapter.get_current_part_info()
    mass_kg = adapter.get_mass()
    material = adapter.get_material()
    features = adapter.get_features()
    screenshot_path = adapter.capture_screenshot()

    styles = _styles()
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=_PAGE_MARGIN_MM * mm,
        rightMargin=_PAGE_MARGIN_MM * mm,
        topMargin=_PAGE_MARGIN_MM * mm,
        bottomMargin=_PAGE_MARGIN_MM * mm,
        title=f"Manufacturing Readiness Report -- {part_info.name}",
    )

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    story = [
        Paragraph(f"Manufacturing Readiness Report -- {part_info.name}", styles["ReportTitle"]),
        Paragraph(f"Generated {generated_at}", styles["ReportSubtitle"]),
        _screenshot_flowable(screenshot_path),
        Spacer(1, 12),
        Paragraph("Part Information", styles["SectionHeading"]),
        _table(
            [
                ["Item", "Value"],
                ["Name", part_info.name],
                ["File path", part_info.file_path],
                ["Type", part_info.part_type],
                ["Units", part_info.units],
                ["Mass", f"{mass_kg:.4f} kg"],
                ["Material", material.name],
                ["Material density", f"{material.density_kg_m3:.1f} kg/m^3"],
                ["Feature count", str(len(features))],
            ],
            col_widths=[45 * mm, 125 * mm],
        ),
    ]

    story += _cost_section(styles, manufacturing_process, quantity, process_is_default, quantity_is_default)
    story += _carbon_section(styles)
    story += _dfm_section(styles)

    doc.build(story)
    return output_path
