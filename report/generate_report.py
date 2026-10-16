"""Assembles a one-click "manufacturing readiness report" PDF out of
everything the copilot already knows how to compute about the currently
open CAD part: identity, mass, material, feature tree, a viewport
screenshot, a cost estimate (material + production cost breakdown, see
production_cost.py), a carbon estimate, and DFM check results.

Layout (single part):
  1. Header + "At a glance" tiles (mass, material, cost, carbon, DFM status)
  2. Key findings + recommended next steps (derived only from the numbers
     below -- nothing is invented)
  3. Part overview (screenshot + identity/geometry table)
  4. Cost estimate (+ ranked cost drivers), Carbon, DFM, optional AM section
  5. Assumptions & limitations (the long caveat text lives here so the body
     stays readable)

Reuses mcp_server.py's `adapter` and `estimate_cost` directly (accessed as
`mcp_server.adapter` / `mcp_server.estimate_cost`, not imported by name, so
a platform switch via mcp_server.set_backend() mid-session is picked up
immediately). This module never re-implements cost-model feature assembly,
so the report always reflects exactly the same numbers a chat answer would
show.

Built with reportlab (see requirements.txt).
"""

import datetime
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import mcp_server

# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------
_PAGE_MARGIN_MM = 16
_CONTENT_WIDTH_MM = 210 - 2 * _PAGE_MARGIN_MM  # 178 mm usable on A4
_CW = _CONTENT_WIDTH_MM * mm

_ASSEMBLY_BOM_COL_WIDTHS = [
    38 * mm, 24 * mm, 20 * mm, 36 * mm, 10 * mm, 25 * mm, 25 * mm,
]

# Defaults used only when the caller doesn't tell generate_manufacturing_report
# what the user actually asked for -- always reported explicitly in the PDF
# as defaults, never silently presented as the user's real request. Matches
# estimate_cost()'s own "CNC 3-axis is the general-purpose default" choice
# in mcp_server.py.
_DEFAULT_MANUFACTURING_PROCESS = "CNC Machining"
_DEFAULT_QUANTITY = 1

# Palette
_NAVY = colors.HexColor("#1B2A41")
_GREY_TEXT = colors.HexColor("#5B6B85")
_RULE = colors.HexColor("#DDE3EC")
_BAND = colors.HexColor("#F5F7FA")
_TILE_BG = colors.HexColor("#EEF2F8")
_ACCENT = colors.HexColor("#2F6FDE")

_DFM_STATUS_COLORS = {
    "flagged": colors.HexColor("#B23A3A"),
    "pass": colors.HexColor("#2E7D4F"),
    "not_applicable": colors.HexColor("#8A8F98"),
}

# If the ML model and the formula disagree by more than this fraction, the
# report says so instead of presenting two numbers with no comment.
_ESTIMATE_DIVERGENCE_THRESHOLD = 0.25


# ---------------------------------------------------------------------------
# Styles and small helpers
# ---------------------------------------------------------------------------
def _styles():
    s = getSampleStyleSheet()
    s.add(ParagraphStyle("ReportTitle", parent=s["Title"], fontSize=18, leading=22,
                         alignment=TA_LEFT, textColor=_NAVY, spaceAfter=2))
    s.add(ParagraphStyle("ReportSubtitle", parent=s["Normal"], fontSize=9,
                         textColor=_GREY_TEXT, spaceAfter=6))
    s.add(ParagraphStyle("SectionHeading", parent=s["Heading2"], fontSize=12.5,
                         leading=15, spaceBefore=14, spaceAfter=5, textColor=_NAVY))
    s.add(ParagraphStyle("Note", parent=s["Normal"], fontSize=8, leading=10.5,
                         textColor=_GREY_TEXT, spaceBefore=3))
    s.add(ParagraphStyle("Cell", parent=s["Normal"], fontSize=8.5, leading=11))
    s.add(ParagraphStyle("CellHead", parent=s["Normal"], fontSize=8.5, leading=11,
                         textColor=colors.white, fontName="Helvetica-Bold"))
    s.add(ParagraphStyle("Body", parent=s["Normal"], fontSize=9.5, leading=13))
    s.add(ParagraphStyle("FindingBullet", parent=s["Normal"], fontSize=9.5, leading=13,
                         leftIndent=11, bulletIndent=1, spaceBefore=2))
    s.add(ParagraphStyle("TileLabel", parent=s["Normal"], fontSize=7.5, leading=9,
                         textColor=_GREY_TEXT))
    s.add(ParagraphStyle("TileValue", parent=s["Normal"], fontSize=13, leading=16,
                         textColor=_NAVY, fontName="Helvetica-Bold"))
    s.add(ParagraphStyle("TileSub", parent=s["Normal"], fontSize=7.5, leading=9,
                         textColor=_GREY_TEXT))
    s.add(ParagraphStyle("Callout", parent=s["Normal"], fontSize=9, leading=12.5,
                         textColor=colors.HexColor("#7A4B00")))
    return s


def _p(text, style) -> Paragraph:
    """Paragraph with the text XML-escaped, so a feature name, file path or
    message containing <, > or & can never break the PDF build."""
    return Paragraph(escape(str(text)), style)


def _table(rows: list[list], col_widths=None, styles=None, zebra: bool = True) -> Table:
    """Branded table. Every string cell is wrapped in a Paragraph so long
    text wraps inside its column instead of running off the page (the old
    version used raw strings, which is why long DFM messages were clipped).
    The first row is the header."""
    st = styles or _styles()
    wrapped = []
    for r_i, row in enumerate(rows):
        out = []
        for cell in row:
            if isinstance(cell, str):
                out.append(_p(cell, st["CellHead"] if r_i == 0 else st["Cell"]))
            else:
                out.append(cell)
        wrapped.append(out)

    table = Table(wrapped, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if zebra:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _BAND]))
    table.setStyle(TableStyle(style))
    return table


def _kv_table(rows: list[tuple], col_widths, styles) -> Table:
    """Two-column label/value table with no header row (used for the part
    overview), bold label column."""
    bold = ParagraphStyle("CellBold", parent=styles["Cell"], fontName="Helvetica-Bold")
    data = [[_p(k, bold), _p(v, styles["Cell"])] for k, v in rows]
    table = Table(data, colWidths=col_widths, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _BAND]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    return table


def _heading(text, styles):
    return Paragraph(escape(text), styles["SectionHeading"])


def _screenshot_flowable(screenshot_path: str, max_w_mm: float, max_h_mm: float) -> Image:
    with PILImage.open(screenshot_path) as im:
        px_width, px_height = im.size
    max_w = max_w_mm * mm
    max_h = max_h_mm * mm
    aspect = px_height / px_width
    width = max_w
    height = width * aspect
    if height > max_h:
        height = max_h
        width = height / aspect
    return Image(screenshot_path, width=width, height=height)


def _basis_note(manufacturing_process, quantity, process_is_default, quantity_is_default) -> str:
    return (
        f"Manufacturing process: {manufacturing_process}"
        + (" (default -- not specified by the user)" if process_is_default else "")
        + f" | Quantity: {quantity}"
        + (" (default -- not specified by the user)" if quantity_is_default else "")
    )


def _money(value) -> str:
    return f"Rs {value:,.2f}"


def _page_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(_RULE)
    canvas.setLineWidth(0.5)
    y = 11 * mm
    canvas.line(_PAGE_MARGIN_MM * mm, y + 3.5 * mm, (210 - _PAGE_MARGIN_MM) * mm, y + 3.5 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(_GREY_TEXT)
    canvas.drawString(_PAGE_MARGIN_MM * mm, y,
                      "CAD AI Copilot  |  Advisory estimates only -- not a manufacturing quote")
    canvas.drawRightString((210 - _PAGE_MARGIN_MM) * mm, y, f"Page {doc.page}")
    canvas.restoreState()


def _build_doc(output_path: str, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=_PAGE_MARGIN_MM * mm,
        rightMargin=_PAGE_MARGIN_MM * mm,
        topMargin=_PAGE_MARGIN_MM * mm,
        bottomMargin=(_PAGE_MARGIN_MM + 4) * mm,
        title=title,
        author="CAD AI Copilot",
    )


def _header_block(styles, title: str, subtitle: str) -> list:
    return [
        Paragraph(escape(title), styles["ReportTitle"]),
        Paragraph(escape(subtitle), styles["ReportSubtitle"]),
        HRFlowable(width="100%", thickness=1.2, color=_ACCENT, spaceAfter=8),
    ]


def _tile(label: str, value: str, sub: str, styles) -> list:
    return [
        Paragraph(escape(label.upper()), styles["TileLabel"]),
        Paragraph(escape(value), styles["TileValue"]),
        Paragraph(escape(sub), styles["TileSub"]),
    ]


def _tiles_row(tiles: list[list], styles) -> Table:
    n = len(tiles)
    gap = 3 * mm
    w = (_CW - gap * (n - 1)) / n
    row, widths = [], []
    for i, t in enumerate(tiles):
        row.append(t)
        widths.append(w)
        if i < n - 1:
            row.append("")
            widths.append(gap)
    table = Table([row], colWidths=widths, hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]
    for i in range(0, len(row), 2):
        style += [
            ("BACKGROUND", (i, 0), (i, 0), _TILE_BG),
            ("LEFTPADDING", (i, 0), (i, 0), 7),
            ("RIGHTPADDING", (i, 0), (i, 0), 5),
            ("TOPPADDING", (i, 0), (i, 0), 6),
            ("BOTTOMPADDING", (i, 0), (i, 0), 6),
        ]
    table.setStyle(TableStyle(style))
    return table


def _bullets(items: list[str], styles) -> list:
    return [Paragraph(escape(text), styles["FindingBullet"], bulletText="•") for text in items]


# ---------------------------------------------------------------------------
# Data gathering (each piece is fetched once, then reused by the summary and
# the detailed sections so the numbers can never disagree with each other).
# ---------------------------------------------------------------------------
def _gather_cost(manufacturing_process, quantity):
    result = mcp_server.estimate_cost(manufacturing_process=manufacturing_process, quantity=quantity)
    drivers = None
    if result.get("found"):
        try:
            d = mcp_server.adapter.get_cost_drivers(manufacturing_process, quantity)
            if d.get("found", True) and d.get("components"):
                drivers = d["components"]
        except Exception:
            drivers = None  # cost drivers are a bonus; never fail the report on them
    return result, drivers


def _gather_geometry():
    """Bounding box and volume are optional extras: any adapter that can't
    provide them simply leaves the row out (never a guessed value)."""
    bbox = volume_cm3 = None
    try:
        dims = sorted(mcp_server.adapter.get_bounding_box_mm(), reverse=True)
        bbox = dims
    except Exception:
        pass
    try:
        vol_m3 = mcp_server.adapter.get_mass_properties().get("volume_m3")
        if vol_m3 is not None:
            volume_cm3 = vol_m3 * 1_000_000
    except Exception:
        pass
    return bbox, volume_cm3


def _dedupe_dfm(findings: list[dict]) -> list[dict]:
    seen, out = set(), []
    for f in findings:
        key = (f.get("check"), f.get("status"), f.get("feature"), f.get("message"))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _feature_summary(features) -> str:
    active = [f for f in features if not f.suppressed]
    counts = Counter(f.feature_type for f in active)
    top = ", ".join(f"{t} x{n}" for t, n in counts.most_common(4))
    more = "" if len(counts) <= 4 else f", +{len(counts) - 4} other type(s)"
    suppressed = len(features) - len(active)
    tail = f" ({suppressed} suppressed)" if suppressed else ""
    return f"{len(active)} active{tail}" + (f" -- {top}{more}" if top else "")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _summary_section(styles, part_info, mass_kg, material, cost, carbon, dfm_findings,
                     cost_drivers, process, quantity) -> list:
    flowables = []

    # --- KPI tiles ---
    if cost.get("found"):
        cost_tile = _tile("Total cost / unit", _money(cost["total_cost_per_unit_inr"]),
                          f"{process}, qty {quantity}", styles)
    else:
        cost_tile = _tile("Total cost / unit", "N/A", "estimate unavailable", styles)

    if carbon.get("found"):
        carbon_tile = _tile("Embodied carbon", f"{carbon['estimated_kg_co2e']:.2f} kg",
                            "CO2e, material only", styles)
    else:
        carbon_tile = _tile("Embodied carbon", "N/A", "estimate unavailable", styles)

    evaluated = [f for f in dfm_findings if f["status"] in ("flagged", "pass")]
    flagged = [f for f in evaluated if f["status"] == "flagged"]
    not_eval = [f for f in dfm_findings if f["status"] == "not_applicable"]
    if not evaluated:
        dfm_tile = _tile("DFM checks", "Not verified", f"{len(not_eval)} could not run", styles)
    elif flagged:
        dfm_tile = _tile("DFM checks", f"{len(flagged)} flagged",
                         f"{len(evaluated) - len(flagged)} passed, {len(not_eval)} n/a", styles)
    else:
        dfm_tile = _tile("DFM checks", "All passed",
                         f"{len(evaluated)} passed, {len(not_eval)} n/a", styles)

    flowables.append(_tiles_row([
        _tile("Mass", f"{mass_kg:.3f} kg", "from CAD mass properties", styles),
        _tile("Material", material.name, f"{material.density_kg_m3:,.0f} kg/m3", styles),
        cost_tile,
        carbon_tile,
        dfm_tile,
    ], styles))

    # --- Key findings (all derived from the numbers; nothing invented) ---
    findings = []
    if cost.get("found"):
        total = cost["total_cost_per_unit_inr"]
        if total > 0:
            mat_share = cost["material_cost_per_unit_inr"] / total * 100
            findings.append(
                f"Cost: material is {mat_share:.0f}% and production {100 - mat_share:.0f}% of the "
                f"{_money(total)} per-unit estimate."
            )
        if cost_drivers:
            top = max(cost_drivers, key=lambda c: c["cost_per_unit_inr"])
            findings.append(
                f"Biggest production cost driver: {top['label']} "
                f"({top['percentage_of_total_production_cost']:.0f}% of production cost)."
            )
        ml = cost.get("ml_predicted_total_cost_per_unit_inr")
        if ml and total > 0 and abs(ml - total) / total > _ESTIMATE_DIVERGENCE_THRESHOLD:
            lo, hi = sorted((total, ml))
            findings.append(
                f"The formula estimate ({_money(total)}) and the ML estimate ({_money(ml)}) differ "
                f"noticeably -- treat the real cost as a range of {_money(lo)} to {_money(hi)} until "
                f"actual shop rates are entered."
            )
    if carbon.get("found"):
        findings.append(
            f"Carbon: about {carbon['estimated_kg_co2e']:.2f} kg CO2e embodied in the material "
            f"({carbon['carbon_factor_kg_co2_per_kg']} kg CO2e per kg of {carbon['material_matched']})."
        )
    if flagged:
        names = "; ".join(
            f"{f['check'].replace('_', ' ')}" + (f" ({f['feature']})" if f.get("feature") else "")
            for f in flagged[:4]
        )
        findings.append(f"DFM: {len(flagged)} issue(s) flagged -- {names}.")
    elif evaluated:
        findings.append(f"DFM: all {len(evaluated)} evaluated check(s) passed.")
    else:
        findings.append(
            "DFM: none of the checks could be evaluated on this part, so design rules have NOT "
            "been verified. This is not a pass."
        )

    flowables.append(_heading("Key findings", styles))
    flowables += _bullets(findings, styles)

    # --- Next steps (rule-based on the same data) ---
    steps = []
    if flagged:
        steps.append("Review the flagged DFM items below and correct the geometry before releasing the part.")
    if not evaluated and not_eval:
        steps.append("Check hole sizes, wall thickness and draft manually -- the copilot could not measure them on this part.")
    if cost.get("found"):
        steps.append("Replace the placeholder shop rates with real quotes before using the cost for decisions.")
    if not steps:
        steps.append("No action items from the automated checks.")
    flowables.append(_heading("Recommended next steps", styles))
    flowables += _bullets(steps, styles)
    return flowables


def _overview_section(styles, part_info, mass_kg, material, features, bbox, volume_cm3, screenshot_path) -> list:
    flowables = [_heading("Part overview", styles)]

    rows = [
        ("Name", part_info.name),
        ("Type", part_info.part_type),
        ("Units", part_info.units),
        ("Material", f"{material.name}" + (f" ({material.category})" if material.category else "")),
        ("Density", f"{material.density_kg_m3:,.1f} kg/m3"),
        ("Mass", f"{mass_kg:.4f} kg"),
    ]
    if volume_cm3 is not None:
        rows.append(("Volume", f"{volume_cm3:,.1f} cm3"))
    if bbox:
        rows.append(("Bounding box", f"{bbox[0]:.1f} x {bbox[1]:.1f} x {bbox[2]:.1f} mm"))
    rows.append(("Features", _feature_summary(features)))

    img_w_mm = 74
    info_w = _CW - (img_w_mm + 4) * mm
    info = _kv_table(rows, [24 * mm, info_w - 24 * mm], styles)
    img = _screenshot_flowable(screenshot_path, img_w_mm, 70)
    layout = Table([[img, info]], colWidths=[(img_w_mm + 4) * mm, info_w], hAlign="LEFT")
    layout.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 4 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    flowables.append(layout)
    flowables.append(Spacer(1, 3))
    flowables.append(Paragraph(f"File: {escape(part_info.file_path)}", styles["Note"]))
    return flowables


def _cost_section(styles, cost, cost_drivers, manufacturing_process, quantity,
                  process_is_default, quantity_is_default) -> list:
    head = [
        _heading("Cost estimate", styles),
        Paragraph(escape(_basis_note(manufacturing_process, quantity, process_is_default,
                                     quantity_is_default)), styles["Note"]),
    ]
    if not cost.get("found"):
        return head + [_p(f"Cost estimate unavailable: {cost.get('message', 'unknown error')}", styles["Body"])]

    total = cost["total_cost_per_unit_inr"]

    def share(v):
        return f"{v / total * 100:.0f}%" if total else "-"

    rows = [
        ["Item", "Amount", "Share of unit cost"],
        [f"Material ({cost['material_used']})", _money(cost["material_cost_per_unit_inr"]),
         share(cost["material_cost_per_unit_inr"])],
        ["Production", _money(cost["production_cost_per_unit_inr"]),
         share(cost["production_cost_per_unit_inr"])],
        [Paragraph("<b>Total per unit</b>", styles["Cell"]),
         Paragraph(f"<b>{escape(_money(total))}</b>", styles["Cell"]),
         Paragraph("<b>100%</b>", styles["Cell"])],
        [f"Total for quantity ({quantity})", _money(cost["total_cost_for_quantity_inr"]), ""],
    ]
    table = _table(rows, [80 * mm, 49 * mm, 49 * mm], styles)
    table.setStyle(TableStyle([
        ("LINEABOVE", (0, 3), (-1, 3), 0.8, _NAVY),
    ]))
    out = head + [table]

    ml = cost.get("ml_predicted_total_cost_per_unit_inr")
    if ml is not None:
        diff = (ml - total) / total * 100 if total else 0
        out.append(Spacer(1, 6))
        out.append(_table([
            ["Cross-check (per unit)", "Amount"],
            ["Formula estimate (used above)", _money(total)],
            ["ML estimate (Random Forest, comparison only)", f"{_money(ml)}  ({diff:+.0f}% vs formula)"],
        ], [80 * mm, 98 * mm], styles))

    if cost_drivers:
        ranked = sorted(cost_drivers, key=lambda c: c["cost_per_unit_inr"], reverse=True)[:5]
        rows = [["Production cost driver", "Cost / unit", "Share of production"]]
        for c in ranked:
            rows.append([c["label"], _money(c["cost_per_unit_inr"]),
                         f"{c['percentage_of_total_production_cost']:.0f}%"])
        out.append(Spacer(1, 6))
        out.append(_table(rows, [80 * mm, 49 * mm, 49 * mm], styles))

    return [KeepTogether(out)]


def _carbon_section(styles, carbon) -> list:
    head = [_heading("Carbon estimate", styles)]
    if not carbon.get("found"):
        return head + [_p(f"Carbon estimate unavailable: {carbon.get('message', 'unknown error')}", styles["Body"])]
    rows = [
        ["Item", "Value"],
        ["Estimated embodied carbon", f"{carbon['estimated_kg_co2e']:.3f} kg CO2e"],
        ["Carbon factor", f"{carbon['carbon_factor_kg_co2_per_kg']} kg CO2e per kg"],
        ["Material matched", carbon["material_matched"] + (" (fuzzy match)" if carbon["is_fuzzy_match"] else "")],
    ]
    return [KeepTogether(head + [_table(rows, [80 * mm, 98 * mm], styles)])]


def _dfm_section(styles, findings) -> list:
    out = [_heading("DFM check results", styles)]
    evaluated = [f for f in findings if f["status"] in ("flagged", "pass")]
    not_eval = [f for f in findings if f["status"] == "not_applicable"]

    if evaluated:
        rows = [["Check", "Status", "Feature", "Detail"]]
        for f in evaluated:
            color = _DFM_STATUS_COLORS[f["status"]].hexval()[2:]
            label = "FLAGGED" if f["status"] == "flagged" else "Pass"
            rows.append([
                f["check"].replace("_", " ").title(),
                Paragraph(f'<font color="#{color}"><b>{label}</b></font>', styles["Cell"]),
                f.get("feature") or "-",
                f.get("message", ""),
            ])
        out.append(_table(rows, [30 * mm, 22 * mm, 32 * mm, 94 * mm], styles))
    else:
        box = Table([[Paragraph(
            "<b>No DFM rule could be evaluated on this part.</b> That means the design was not "
            "checked -- it does not mean the design passed. See the reasons below.", styles["Callout"])]],
            colWidths=[_CW])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF5E0")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E8B44C")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        out.append(box)

    if not_eval:
        out.append(Spacer(1, 6))
        out.append(Paragraph("Checks that could not be evaluated", styles["Body"]))
        rows = [["Check", "Why it was not evaluated"]]
        for f in not_eval:
            rows.append([f["check"].replace("_", " ").title(), f.get("message", "")])
        out.append(_table(rows, [38 * mm, 140 * mm], styles))
    return out


def _am_readiness_section(styles, guide_result: dict) -> list:
    """"Additive manufacturing readiness" section, built entirely from a
    previously-computed generate_am_readiness_guide() result (see
    mcp_server.get_last_am_guide_result_for_current_part()) -- this
    function never calls the guide itself, per the explicit requirement
    that a report never triggers AM analysis as a side effect of being
    generated.
    """
    flowables = [_heading("Additive Manufacturing Readiness", styles)]

    top_orientations = guide_result.get("top_orientations") or []
    if top_orientations:
        rows = [["Orientation", "Overhang area", "Overhang regions"]]
        for entry in top_orientations:
            rows.append([
                str(entry.get("orientation", "?")),
                f"{entry.get('total_overhang_area_cm2', 0):.1f} cm2",
                str(entry.get("overhang_region_count", "?")),
            ])
        flowables.append(_table(rows, [80 * mm, 50 * mm, 48 * mm], styles))

    support = guide_result.get("support_strategy") or {}
    if support:
        flowables.append(Spacer(1, 8))
        flowables.append(Paragraph(
            "Support strategy (estimated): "
            f"<b>{escape(str(support.get('recommended_support_strategy', '?')).replace('_', ' '))}</b>",
            styles["Body"]))
        if support.get("reasoning"):
            flowables.append(_p(support["reasoning"], styles["Note"]))

    if not guide_result.get("material_questions_pending", True):
        rec = guide_result.get("material_recommendation") or {}
        recommendations = rec.get("recommendations", []) if isinstance(rec, dict) else []
        if recommendations:
            flowables.append(Spacer(1, 8))
            flowables.append(Paragraph("Recommended materials", styles["Body"]))
            rows = [["Material", "Process", "Reason"]]
            for m in recommendations:
                rows.append([m.get("material", "?"), m.get("process_type", "?"), m.get("reason", "")])
            flowables.append(_table(rows, [45 * mm, 35 * mm, 98 * mm], styles))
            assumptions = rec.get("assumptions") if isinstance(rec, dict) else None
            if assumptions:
                flowables.append(_p(assumptions, styles["Note"]))

    warnings = guide_result.get("printability_warnings") or []
    not_checked = guide_result.get("not_checked") or []
    checks_run = guide_result.get("checks_run") or []
    flowables.append(Spacer(1, 8))
    flowables.append(Paragraph("Check before printing", styles["Body"]))
    if warnings:
        rows = [["Check", "Severity", "Detail"]]
        for w in warnings:
            rows.append([w.get("check", "?"), w.get("severity", "warning"), w.get("detail", "")])
        flowables.append(_table(rows, [32 * mm, 25 * mm, 121 * mm], styles))
    else:
        checks_label = ", ".join(checks_run) if checks_run else "none"
        flowables.append(_p(
            f"No printability issues found by the checks we run ({checks_label}).", styles["Body"]))
    for nc in not_checked:
        flowables.append(_p(f"Not checked -- {nc.get('check', '?')}: {nc.get('reason', '')}", styles["Note"]))

    flowables.append(Spacer(1, 6))
    flowables.append(_p(guide_result.get("limits_footer", mcp_server.AM_GUIDE_LIMITS_FOOTER), styles["Note"]))
    return flowables


def _assumptions_section(styles, cost, carbon) -> list:
    """The long caveat text, kept verbatim from the cost/carbon tools (so the
    report never softens what a chat answer would say), but moved out of the
    body so the numbers are readable first."""
    out = [_heading("Assumptions & limitations", styles)]
    if cost.get("found"):
        out.append(Paragraph("<b>Cost</b>", styles["Body"]))
        out.append(_p(cost["production_cost_assumptions"], styles["Note"]))
        out.append(_p(cost["assumptions"], styles["Note"]))
    if carbon.get("found"):
        out.append(Spacer(1, 4))
        out.append(Paragraph("<b>Carbon</b>", styles["Body"]))
        out.append(_p(carbon["assumptions"], styles["Note"]))
    out.append(Spacer(1, 4))
    out.append(Paragraph("<b>General</b>", styles["Body"]))
    out.append(_p(
        "All figures are advisory estimates computed from the open CAD model and the copilot's own "
        "material and rate tables. They are not manufacturing quotes. Values marked 'default' were "
        "not specified by the user.", styles["Note"]))
    return out


# ---------------------------------------------------------------------------
# Assembly report
# ---------------------------------------------------------------------------
def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _bom_row_cells(row: dict) -> list[str]:
    return [
        row["part_name"],
        row["configuration"],
        row["classification"],
        row["material"] or "-",
        str(row["quantity_per_assembly"]),
        _fmt(row["unit_cost_inr"], " Rs") if row["unit_cost_inr"] is not None else "N/A",
        _fmt(row["total_cost_inr"], " Rs") if row["total_cost_inr"] is not None else "N/A",
    ]


def _assembly_bom_section(
    styles, manufacturing_process: str, quantity: int, process_is_default: bool, quantity_is_default: bool
) -> tuple[list, dict | None]:
    """BOM table + assembly-level rollup totals for an assembly report.

    Returns (flowables, bom_result) -- bom_result is reused by
    _assembly_cost_drivers_section() so get_assembly_bom() (which walks
    the whole component tree via COM) only runs once per report.
    """
    flowables = [
        _heading("Bill of Materials", styles),
        Paragraph(escape(_basis_note(manufacturing_process, quantity, process_is_default,
                                     quantity_is_default)), styles["Note"]),
    ]
    bom_result = mcp_server.adapter.get_assembly_bom(manufacturing_process, quantity)

    rows = [["Part", "Config", "Make/Buy", "Material", "Qty", "Unit Cost", "Total Cost"]]
    for row in bom_result["bom"]:
        rows.append(_bom_row_cells(row))
    flowables.append(_table(rows, _ASSEMBLY_BOM_COL_WIDTHS, styles))

    totals = bom_result["totals"]
    flowables.append(Spacer(1, 10))
    flowables.append(_heading("Assembly totals", styles))
    flowables.append(_table([
        ["Item", "Value"],
        ["Unique parts", str(totals["unique_part_count"])],
        ["Total part instances (incl. duplicates)", str(totals["total_instance_count"])],
        ["Total assembly mass", f"{totals['total_assembly_mass_kg']:.3f} kg"],
        ["Total cost -- 1 assembly", _money(totals["total_assembly_cost_one_unit_inr"])],
        [f"Total cost -- {quantity} assembl{'y' if quantity == 1 else 'ies'}",
         _money(totals["total_assembly_cost_for_quantity_inr"])],
    ], [90 * mm, 88 * mm], styles))

    if bom_result["missing_data"]:
        flowables.append(Spacer(1, 6))
        flowables.append(Paragraph(
            "Components with missing material or pricing data (excluded from the totals above):",
            styles["Note"]))
        for entry in bom_result["missing_data"]:
            flowables.append(_p(
                f"- {entry['part_name']} ({entry['configuration']}): {entry['reason']}", styles["Note"]))
    return flowables, bom_result


def _assembly_cost_drivers_section(styles, bom_result: dict) -> list:
    """Top cost-driving components, reusing the already-computed BOM
    rather than re-walking the assembly tree a second time."""
    flowables = [_heading("Top cost drivers", styles)]
    priced_rows = [r for r in bom_result["bom"] if r["total_cost_inr"] is not None]
    ranked = sorted(priced_rows, key=lambda r: r["total_cost_inr"], reverse=True)[:5]
    if not ranked:
        flowables.append(Paragraph("No priced components to rank.", styles["Body"]))
        return flowables

    grand = sum(r["total_cost_inr"] for r in priced_rows) or 1
    rows = [["Part", "Config", "Make/Buy", "Total cost (1 assembly)", "Share"]]
    for row in ranked:
        rows.append([row["part_name"], row["configuration"], row["classification"],
                     _money(row["total_cost_inr"]), f"{row['total_cost_inr'] / grand * 100:.0f}%"])
    flowables.append(_table(rows, [50 * mm, 32 * mm, 26 * mm, 46 * mm, 24 * mm], styles))
    return flowables


def _generate_assembly_report(
    output_path: str,
    manufacturing_process: str,
    quantity: int,
    process_is_default: bool,
    quantity_is_default: bool,
) -> str:
    part_info = mcp_server.adapter.get_current_part_info()
    screenshot_path = mcp_server.adapter.capture_screenshot()

    styles = _styles()
    title = f"Assembly Manufacturing Readiness Report -- {part_info.name}"
    doc = _build_doc(output_path, title)
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    story = _header_block(styles, title, f"Generated {generated_at}  |  Platform: {mcp_server.adapter.PLATFORM_ID}")
    bom_flowables, bom_result = _assembly_bom_section(
        styles, manufacturing_process, quantity, process_is_default, quantity_is_default)
    totals = bom_result["totals"]

    story.append(_tiles_row([
        _tile("Unique parts", str(totals["unique_part_count"]),
              f"{totals['total_instance_count']} instances", styles),
        _tile("Assembly mass", f"{totals['total_assembly_mass_kg']:.2f} kg", "sum of priced parts", styles),
        _tile("Cost / assembly", _money(totals["total_assembly_cost_one_unit_inr"]),
              f"{manufacturing_process}", styles),
        _tile(f"Cost x {quantity}", _money(totals["total_assembly_cost_for_quantity_inr"]),
              "for the quantity", styles),
    ], styles))
    story.append(Spacer(1, 8))
    story.append(_heading("Assembly overview", styles))
    story.append(_screenshot_flowable(screenshot_path, 120, 80))
    story.append(Spacer(1, 4))
    story.append(_kv_table([
        ("Name", part_info.name), ("Type", part_info.part_type),
        ("Units", part_info.units), ("File", part_info.file_path),
    ], [28 * mm, _CW - 28 * mm], styles))

    story += bom_flowables
    story += _assembly_cost_drivers_section(styles, bom_result)

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return output_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
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

    if mcp_server.adapter.is_assembly():
        # Assembly documents have no single mass/material/feature tree, so
        # they get an entirely different report shape (BOM + rollup totals
        # + cost drivers).
        return _generate_assembly_report(
            output_path, manufacturing_process, quantity, process_is_default, quantity_is_default
        )

    part_info = mcp_server.adapter.get_current_part_info()
    mass_kg = mcp_server.adapter.get_mass()
    material = mcp_server.adapter.get_material()
    features = mcp_server.adapter.get_features()
    screenshot_path = mcp_server.adapter.capture_screenshot()
    bbox, volume_cm3 = _gather_geometry()

    cost, cost_drivers = _gather_cost(manufacturing_process, quantity)
    carbon = mcp_server.adapter.estimate_carbon()
    dfm_findings = _dedupe_dfm(mcp_server.adapter.run_dfm_check())

    styles = _styles()
    title = f"Manufacturing Readiness Report -- {part_info.name}"
    doc = _build_doc(output_path, title)
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    story = _header_block(styles, title, f"Generated {generated_at}  |  Platform: {mcp_server.adapter.PLATFORM_ID}")
    story += _summary_section(styles, part_info, mass_kg, material, cost, carbon, dfm_findings,
                              cost_drivers, manufacturing_process, quantity)
    story += _overview_section(styles, part_info, mass_kg, material, features, bbox, volume_cm3, screenshot_path)
    story += _cost_section(styles, cost, cost_drivers, manufacturing_process, quantity,
                           process_is_default, quantity_is_default)
    story += _carbon_section(styles, carbon)
    story += _dfm_section(styles, dfm_findings)

    # Additive manufacturing readiness -- included ONLY if (a) Fusion 360
    # is the active CAD platform and (b) generate_am_readiness_guide()
    # already produced a result for THIS part earlier in the session. Never
    # runs the guide itself -- if neither condition holds, no placeholder
    # and no empty heading.
    if mcp_server.adapter.PLATFORM_ID == mcp_server._FUSION_PLATFORM_ID:
        guide_result = mcp_server.get_last_am_guide_result_for_current_part()
        if guide_result is not None:
            story += _am_readiness_section(styles, guide_result)

    story += _assumptions_section(styles, cost, carbon)

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return output_path
