"""Reusable, visual-only redesign of the single-part cost breakdown card
(estimate_cost()'s result) for the Flet desktop UI.

This module reads ONLY fields that mcp_server.estimate_cost() already
returns -- material_cost_per_unit_inr, production_cost_per_unit_inr,
total_cost_per_unit_inr, total_cost_for_quantity_inr,
ml_predicted_total_cost_per_unit_inr, quantity, manufacturing_process,
material_used, material_cost_per_kg_inr, rate_overrides_applied,
production_cost_breakdown, production_cost_assumptions, inputs_used. It
never computes a cost number itself and never invents a placeholder
number for a missing field -- an element whose backing field is missing
is simply omitted (see _cnc_segments/_im_segments/_sm_segments and
_material_detail_rows/_production_detail_rows below for exactly which
field backs each visual element).

Cost-split-bar segments ("Material | Setup | Machining | Labor | Other
features" in the design brief) are mapped to whichever of those concepts
each manufacturing process actually has a real dollar field for --
CNC Machining has Setup + per-hole Machining + Other features (billed at
a combined machine+labor rate, so there is no separate "Labor" dollar
figure to show without inventing a split that doesn't exist in the data);
Injection Molding has Machining (cost per shot) + Tooling; Sheet Metal
has Machining (cutting) + Bending. No process gets a segment it has no
backing field for.

Public entry point: build_cost_card(data, ...) -> (control, animate_in).
`animate_in(page)` is an async function the caller awaits shortly after
adding `control` to the page (same pattern desktop_app.py's own
add_bubble_animated already uses for its fade/slide-in) -- it flips the
entrance fade+slide AND grows the split-bar segments from 0 width to
their real width over 400ms. Calling build_cost_card() without ever
awaiting animate_in still renders a fully correct, fully static card
(e.g. for a one-off screenshot) -- it just skips the motion.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

import flet as ft

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

ENTRANCE_ANIM_MS = 200
BAR_GROW_ANIM_MS = 400
NARROW_WIDTH_BREAKPOINT = 700

# ---------------------------------------------------------------------------
# Palette -- keyed by `dark: bool`. Label/subtitle colors are chosen to meet
# >=4.5:1 contrast against their respective card_bg (checked by eye against
# WCAG tables for these hex pairs; not computed at runtime).
# ---------------------------------------------------------------------------
_PALETTE = {
    False: {
        "card_bg": "#FFFFFF",
        "tile_bg": "#EEF3FC",
        "detail_bg": "#F7F9FC",
        "title": "#1B2A41",
        "subtitle": "#6B7686",
        "label": "#4A5568",
        "body": "#1B2A41",
        "border": "#E4E9F1",
        "zebra_a": "#FFFFFF",
        "zebra_b": "#F3F6FB",
        "accent": "#2F6FED",
        "warn_bg": "#FFF3E0",
        "warn_text": "#8A5A00",
        "warn_icon": "#B26A00",
        "warn_border": "#F3D9A8",
        "scale_track": "#E4E9F1",
        "diff_note_bg": "#FFF3E0",
        "diff_note_text": "#8A5A00",
    },
    True: {
        "card_bg": "#1E2430",
        "tile_bg": "#2A3348",
        "detail_bg": "#242C3B",
        "title": "#F5F7FA",
        "subtitle": "#9AA4B2",
        "label": "#C7D1DE",
        "body": "#F5F7FA",
        "border": "#333E52",
        "zebra_a": "#1E2430",
        "zebra_b": "#252E3F",
        "accent": "#6C9BFA",
        "warn_bg": "#3A2E12",
        "warn_text": "#FFC972",
        "warn_icon": "#FFC972",
        "warn_border": "#5A4420",
        "scale_track": "#333E52",
        "diff_note_bg": "#3A2E12",
        "diff_note_text": "#FFC972",
    },
}

_PROCESS_CHIP = {
    False: {
        "CNC Machining": {"bg": "#E3ECFB", "text": "#2F6FED", "icon": ft.Icons.PRECISION_MANUFACTURING_ROUNDED},
        "Injection Molding": {"bg": "#F1E9FF", "text": "#7C4DFF", "icon": ft.Icons.FACTORY_ROUNDED},
        "Sheet Metal": {"bg": "#E0F2EF", "text": "#00897B", "icon": ft.Icons.VIEW_IN_AR_ROUNDED},
    },
    True: {
        "CNC Machining": {"bg": "#24344F", "text": "#8FB2FF", "icon": ft.Icons.PRECISION_MANUFACTURING_ROUNDED},
        "Injection Molding": {"bg": "#2E2347", "text": "#C7AFFF", "icon": ft.Icons.FACTORY_ROUNDED},
        "Sheet Metal": {"bg": "#17332E", "text": "#5FD9C4", "icon": ft.Icons.VIEW_IN_AR_ROUNDED},
    },
}

# Colour-blind-safe segment palette -- every segment is ALSO labelled with
# text (legend + detail table), so colour alone never carries meaning.
_SEGMENT_COLORS = {
    "material": "#2F6FED",   # blue
    "setup": "#F5A623",      # amber
    "machining": "#00897B",  # teal
    "labor": "#7C4DFF",      # purple (reserved -- see module docstring on why
                             # no process currently emits a "labor" segment)
    "other": "#9AA4B2",      # grey
}


def _format_indian_grouping(int_part: str) -> str:
    if len(int_part) <= 3:
        return int_part
    last3 = int_part[-3:]
    rest = int_part[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return ",".join(groups + [last3])


def _fmt_money(value) -> Optional[str]:
    """Indian-grouped "Rs X,XX,XXX.XX", or None if `value` isn't a real
    number -- callers must skip the element entirely on None, never
    substitute a placeholder (see module docstring).
    """
    if not isinstance(value, (int, float)):
        return None
    negative = value < 0
    magnitude = abs(value)
    int_part = str(int(magnitude))
    decimal_part = f"{magnitude:.2f}".split(".")[1]
    grouped = _format_indian_grouping(int_part)
    sign = "-" if negative else ""
    return f"{sign}Rs {grouped}.{decimal_part}"


def _fmt_num(value, decimals: int = 1, suffix: str = "") -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    return f"{value:,.{decimals}f}{suffix}"


def pad_symmetric(horizontal: int = 0, vertical: int = 0) -> ft.Padding:
    return ft.Padding(left=horizontal, right=horizontal, top=vertical, bottom=vertical)


def pad_all(value: int) -> ft.Padding:
    return ft.Padding(left=value, top=value, right=value, bottom=value)


# ---------------------------------------------------------------------------
# Segment extraction -- one function per process, each reading only fields
# that process's real production_cost_breakdown actually contains.
# ---------------------------------------------------------------------------

def _cnc_segments(data: dict) -> list[dict]:
    breakdown = data.get("production_cost_breakdown") or {}
    segments = []

    material = data.get("material_cost_per_unit_inr")
    if isinstance(material, (int, float)):
        segments.append({"key": "material", "label": "Material", "value": material})

    setup = breakdown.get("setup") or {}
    setup_cost = setup.get("estimated_cost_inr")
    if isinstance(setup_cost, (int, float)):
        segments.append({"key": "setup", "label": "Setup", "value": setup_cost})

    holes = breakdown.get("holes")
    if holes is not None:
        holes_cost = sum(h.get("estimated_cost_inr", 0) for h in holes)
        segments.append({"key": "machining", "label": f"Machining ({len(holes)} holes)", "value": holes_cost})

    other = breakdown.get("other_features") or {}
    other_cost = other.get("estimated_cost_inr")
    if isinstance(other_cost, (int, float)):
        segments.append({"key": "other", "label": "Other features", "value": other_cost})

    return segments


def _im_segments(data: dict) -> list[dict]:
    breakdown = data.get("production_cost_breakdown") or {}
    segments = []

    material = data.get("material_cost_per_unit_inr")
    if isinstance(material, (int, float)):
        segments.append({"key": "material", "label": "Material", "value": material})

    machine_cost = breakdown.get("machine_cost_per_shot_inr")
    if isinstance(machine_cost, (int, float)):
        segments.append({"key": "machining", "label": "Machining (per shot)", "value": machine_cost})

    tooling_unit = breakdown.get("tooling_cost_per_unit_inr")
    if isinstance(tooling_unit, (int, float)):
        segments.append({"key": "other", "label": "Tooling (amortized)", "value": tooling_unit})

    return segments


def _sm_segments(data: dict) -> list[dict]:
    breakdown = data.get("production_cost_breakdown") or {}
    segments = []

    material = data.get("material_cost_per_unit_inr")
    if isinstance(material, (int, float)):
        segments.append({"key": "material", "label": "Material", "value": material})

    cutting_cost = breakdown.get("cutting_cost_inr")
    if isinstance(cutting_cost, (int, float)):
        segments.append({"key": "machining", "label": "Cutting", "value": cutting_cost})

    bending_cost = breakdown.get("bending_cost_inr")
    if isinstance(bending_cost, (int, float)):
        segments.append({"key": "other", "label": "Bending", "value": bending_cost})

    return segments


_SEGMENT_BUILDERS = {
    "CNC Machining": _cnc_segments,
    "Injection Molding": _im_segments,
    "Sheet Metal": _sm_segments,
}


# ---------------------------------------------------------------------------
# Detail table rows -- (label, value_str, tooltip_or_None) tuples, again
# reading only fields that already exist. Material rows are the same for
# every process; production rows are process-specific.
# ---------------------------------------------------------------------------

def _material_detail_rows(data: dict) -> list[tuple[str, str, Optional[str]]]:
    rows = []
    material_cost = _fmt_money(data.get("material_cost_per_unit_inr"))
    rate_per_kg = _fmt_money(data.get("material_cost_per_kg_inr"))
    mass_kg = (data.get("inputs_used") or {}).get("mass_kg")
    mass_str = _fmt_num(mass_kg, 4, " kg")

    if material_cost is not None:
        tooltip = None
        if rate_per_kg is not None and mass_str is not None:
            tooltip = f"= mass ({mass_str}) × rate ({rate_per_kg}/kg)"
        rows.append(("Material cost", material_cost, tooltip))
    if rate_per_kg is not None:
        rows.append(("Rate per kg", f"{rate_per_kg} / kg", None))
    if mass_str is not None:
        rows.append(("Mass", mass_str, None))
    return rows


def _cnc_production_detail_rows(breakdown: dict) -> list[tuple[str, str, Optional[str]]]:
    rows = []
    if breakdown.get("feature_count") is not None:
        rows.append(("Feature count", str(breakdown["feature_count"]), None))

    minutes = breakdown.get("estimated_machining_time_minutes")
    if isinstance(minutes, (int, float)):
        rows.append(("Machining time", f"{_fmt_num(minutes, 1, ' min')} ({_fmt_num(minutes / 60, 2, ' hr')})", None))

    machine_rate = _fmt_money(breakdown.get("machine_hourly_rate_inr"))
    if machine_rate is not None:
        rows.append(("Machine rate", f"{machine_rate} / hr", None))
    labor_rate = _fmt_money(breakdown.get("labor_hourly_rate_inr"))
    if labor_rate is not None:
        rows.append(("Labor rate", f"{labor_rate} / hr", None))

    setup = breakdown.get("setup") or {}
    setup_cost = _fmt_money(setup.get("estimated_cost_inr"))
    if setup_cost is not None:
        setup_min = _fmt_num(setup.get("estimated_time_minutes"), 1, " min")
        rows.append((
            "Setup",
            f"{setup_min} — {setup_cost}" if setup_min else setup_cost,
            "= setup time × (machine rate + labor rate)",
        ))

    holes = breakdown.get("holes") or []
    if holes:
        holes_cost = _fmt_money(sum(h.get("estimated_cost_inr", 0) for h in holes))
        rows.append((f"Holes ({len(holes)})", holes_cost, "= per-hole time (from diameter/depth) × combined rate"))

    other = breakdown.get("other_features") or {}
    other_cost = _fmt_money(other.get("estimated_cost_inr"))
    if other_cost is not None and other.get("count"):
        rows.append((f"Other features ({other['count']})", other_cost, "= flat per-feature time × combined rate"))

    return rows


def _im_production_detail_rows(breakdown: dict) -> list[tuple[str, str, Optional[str]]]:
    rows = []
    volume = _fmt_num(breakdown.get("volume_cm3"), 1, " cm³")
    if volume is not None:
        rows.append(("Part volume", volume, None))
    cycle = _fmt_num(breakdown.get("cycle_time_seconds"), 1, " s")
    if cycle is not None:
        rows.append(("Cycle time", cycle, "base time + (volume × per-cm³ time)"))
    machine_cost = _fmt_money(breakdown.get("machine_cost_per_shot_inr"))
    if machine_cost is not None:
        rows.append(("Machine cost / shot", machine_cost, "= cycle time × machine hourly rate"))
    tooling_total = _fmt_money(breakdown.get("tooling_cost_total_inr"))
    if tooling_total is not None:
        rows.append(("Tooling cost, total", tooling_total, None))
    tooling_unit = _fmt_money(breakdown.get("tooling_cost_per_unit_inr"))
    if tooling_unit is not None:
        qty = breakdown.get("order_quantity_used_for_amortization", "—")
        rows.append((
            "Tooling cost / unit",
            f"{tooling_unit} (over {qty} units)",
            "= tooling cost, total ÷ order quantity",
        ))
    return rows


def _sm_production_detail_rows(breakdown: dict) -> list[tuple[str, str, Optional[str]]]:
    rows = []
    box = breakdown.get("bounding_box_mm")
    if box and len(box) == 3:
        rows.append(("Bounding box", f"{box[0]} × {box[1]} × {box[2]} mm", None))
    cutting_length = _fmt_num(breakdown.get("cutting_length_mm_approx"), 1, " mm")
    if cutting_length is not None:
        rows.append(("Cutting length (approx.)", cutting_length, "= 2 × (longest + 2nd-longest bounding-box dim)"))
    cutting_cost = _fmt_money(breakdown.get("cutting_cost_inr"))
    if cutting_cost is not None:
        rows.append(("Cutting cost", cutting_cost, "= cutting length × cutting rate per mm"))
    if breakdown.get("bend_count") is not None:
        rows.append(("Bend count", str(breakdown["bend_count"]), None))
    bending_cost = _fmt_money(breakdown.get("bending_cost_inr"))
    if bending_cost is not None:
        rows.append(("Bending cost", bending_cost, "= bend count × cost per bend"))
    return rows


_PRODUCTION_DETAIL_BUILDERS = {
    "CNC Machining": _cnc_production_detail_rows,
    "Injection Molding": _im_production_detail_rows,
    "Sheet Metal": _sm_production_detail_rows,
}


def _assumptions_bullets(assumptions_text: Optional[str]) -> list[str]:
    """Re-flow the existing production_cost_assumptions sentence into
    short bullet fragments, purely by splitting on its own " + "/"plus"
    separators -- every word still comes from that one real field, none
    of this is re-derived or invented.
    """
    if not assumptions_text:
        return []
    parts = re.split(r"\s*(?:\+|,?\s+plus)\s*", assumptions_text)
    return [p.strip().rstrip(".") for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Visual building blocks
# ---------------------------------------------------------------------------

def _header(data: dict, palette: dict, dark: bool) -> ft.Control:
    process = data.get("manufacturing_process") or "?"
    quantity = data.get("quantity", "?")
    material_used = data.get("material_used") or "?"

    title = ft.Text(
        material_used,
        size=18,
        weight=ft.FontWeight.W_600,
        color=palette["title"],
        font_family=FONT_FAMILY,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    subtitle = ft.Text(
        f"{process} · Qty {quantity}",
        size=12,
        color=palette["subtitle"],
        font_family=FONT_FAMILY,
    )

    chip_style = _PROCESS_CHIP[dark].get(process, _PROCESS_CHIP[dark]["CNC Machining"])
    chip = ft.Container(
        content=ft.Row(
            [
                ft.Icon(chip_style["icon"], size=14, color=chip_style["text"]),
                ft.Text(process, size=12, weight=ft.FontWeight.W_600, color=chip_style["text"], font_family=FONT_FAMILY),
            ],
            spacing=6,
            tight=True,
        ),
        bgcolor=chip_style["bg"],
        border_radius=8,
        padding=pad_symmetric(horizontal=10, vertical=6),
    )

    top_row = ft.Row(
        [
            ft.Column([title, subtitle], spacing=2, expand=True),
            chip,
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )

    children = [top_row]

    is_placeholder = data.get("rate_overrides_applied") is None
    if is_placeholder:
        badge = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_ROUNDED, size=14, color=palette["warn_icon"]),
                    ft.Text(
                        "Placeholder shop rates",
                        size=12,
                        weight=ft.FontWeight.W_600,
                        color=palette["warn_text"],
                        font_family=FONT_FAMILY,
                        tooltip="Replace with real shop rates in Settings",
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            bgcolor=palette["warn_bg"],
            border=ft.Border.all(1, palette["warn_border"]),
            border_radius=8,
            padding=pad_symmetric(horizontal=10, vertical=6),
            tooltip="Replace with real shop rates in Settings",
        )
        children.append(ft.Container(content=badge, padding=pad_symmetric(vertical=0)))

    return ft.Column(children, spacing=10)


def _hero_tiles(data: dict, palette: dict, width_hint: int) -> ft.Control:
    total_cost = _fmt_money(data.get("total_cost_per_unit_inr"))
    total_for_qty = _fmt_money(data.get("total_cost_for_quantity_inr"))
    quantity = data.get("quantity", "?")

    def tile(label: str, value_str: Optional[str]) -> Optional[ft.Container]:
        if value_str is None:
            return None
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(label.upper(), size=12, weight=ft.FontWeight.W_600, color=palette["subtitle"], font_family=FONT_FAMILY),
                    ft.Text(value_str, size=28, weight=ft.FontWeight.BOLD, color=palette["accent"], font_family=FONT_FAMILY),
                ],
                spacing=4,
            ),
            bgcolor=palette["tile_bg"],
            border_radius=12,
            padding=pad_all(16),
            expand=True,
        )

    tiles = [t for t in (tile("Cost per unit", total_cost), tile(f"Total for {quantity} units", total_for_qty)) if t is not None]
    if not tiles:
        return ft.Container()

    if width_hint < NARROW_WIDTH_BREAKPOINT:
        return ft.Column(tiles, spacing=8)
    return ft.Row(tiles, spacing=8)


def _split_bar(segments: list[dict], palette: dict, bar_width: int) -> tuple[ft.Control, ft.Control, list[tuple[ft.Container, int]]]:
    """Returns (bar_control, legend_control, mount_targets). mount_targets
    is a list of (segment_container, target_width_px) pairs the caller's
    animate_in() grows from 0 after mount -- see build_cost_card.
    """
    total = sum(s["value"] for s in segments)
    track = ft.Container(
        height=12,
        bgcolor=palette["scale_track"],
        border_radius=6,
        width=bar_width,
    )
    if not segments or total <= 0:
        return track, ft.Column([]), []

    widths = [max(3, round((s["value"] / total) * bar_width)) for s in segments]
    drift = bar_width - sum(widths)
    widths[-1] = max(3, widths[-1] + drift)

    mount_targets: list[tuple[ft.Container, int]] = []
    segment_containers = []
    for seg, px in zip(segments, widths):
        color = _SEGMENT_COLORS.get(seg["key"], _SEGMENT_COLORS["other"])
        container = ft.Container(
            width=px,
            bgcolor=color,
            animate=ft.Animation(BAR_GROW_ANIM_MS, ft.AnimationCurve.EASE_OUT),
        )
        mount_targets.append((container, px))
        segment_containers.append(container)

    bar = ft.Container(
        content=ft.Row(segment_containers, spacing=0, tight=True),
        width=bar_width,
        height=12,
        bgcolor=palette["scale_track"],
        border_radius=6,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )

    legend_rows = []
    for seg in segments:
        color = _SEGMENT_COLORS.get(seg["key"], _SEGMENT_COLORS["other"])
        pct = (seg["value"] / total * 100) if total else 0
        legend_rows.append(
            ft.Row(
                [
                    ft.Container(width=8, height=8, border_radius=4, bgcolor=color),
                    ft.Text(seg["label"], size=12, color=palette["label"], font_family=FONT_FAMILY, expand=True),
                    ft.Text(_fmt_money(seg["value"]) or "—", size=12, weight=ft.FontWeight.W_600, color=palette["body"], font_family=FONT_FAMILY),
                    ft.Text(f"{pct:.1f}%", size=12, color=palette["subtitle"], font_family=FONT_FAMILY),
                ],
                spacing=8,
            )
        )
    legend = ft.Column(legend_rows, spacing=6)
    return bar, legend, mount_targets


def _detail_table(rows: list[tuple[str, str, Optional[str]]], palette: dict) -> ft.Control:
    row_controls = []
    for i, (label, value, tooltip) in enumerate(rows):
        label_children = [ft.Text(label, size=12, color=palette["label"], font_family=FONT_FAMILY, expand=True)]
        if tooltip:
            label_children.append(ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=13, color=palette["subtitle"], tooltip=tooltip))
        row_controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Row(label_children, spacing=4, expand=True),
                        ft.Text(value, size=12, weight=ft.FontWeight.W_600, color=palette["body"], font_family=MONO_FONT_FAMILY),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=pad_symmetric(horizontal=10, vertical=7),
                bgcolor=palette["zebra_a"] if i % 2 == 0 else palette["zebra_b"],
            )
        )
    return ft.Column(row_controls, spacing=0)


def _collapsible_section(title: str, body: ft.Control, palette: dict) -> ft.Control:
    chevron = ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED, size=18, color=palette["subtitle"])
    body_container = ft.Container(content=body, visible=False, padding=pad_symmetric(vertical=8))

    def toggle(e: ft.ControlEvent) -> None:
        body_container.visible = not body_container.visible
        chevron.name = (
            ft.Icons.KEYBOARD_ARROW_UP_ROUNDED if body_container.visible else ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED
        )
        e.page.update()

    header = ft.Container(
        content=ft.Row(
            [
                ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=palette["label"], font_family=FONT_FAMILY),
                chevron,
            ],
            spacing=4,
        ),
        on_click=toggle,
        ink=True,
        border_radius=8,
        padding=pad_symmetric(horizontal=4, vertical=6),
    )
    return ft.Column([header, body_container], spacing=0)


def _ml_comparison_strip(data: dict, palette: dict, bar_width: int) -> Optional[ft.Control]:
    ml_estimate = data.get("ml_predicted_total_cost_per_unit_inr")
    rule_based = data.get("total_cost_per_unit_inr")
    if not isinstance(ml_estimate, (int, float)) or not isinstance(rule_based, (int, float)):
        return None

    scale_max = max(ml_estimate, rule_based, 1e-9)

    def scale_row(label: str, value: float, color: str) -> ft.Control:
        fraction = max(0.0, min(1.0, value / scale_max))
        filled_width = max(2, round(fraction * bar_width))
        return ft.Row(
            [
                ft.Text(label, size=11, color=palette["subtitle"], font_family=FONT_FAMILY, width=64),
                ft.Container(
                    content=ft.Container(width=filled_width, height=8, bgcolor=color, border_radius=4),
                    width=bar_width,
                    height=8,
                    bgcolor=palette["scale_track"],
                    border_radius=4,
                ),
                ft.Text(_fmt_money(value) or "—", size=11, weight=ft.FontWeight.W_600, color=palette["body"], font_family=FONT_FAMILY),
            ],
            spacing=8,
        )

    children = [
        ft.Text(
            "ML model estimate (comparison only, not added to the total)",
            size=12,
            weight=ft.FontWeight.W_600,
            color=palette["label"],
            font_family=FONT_FAMILY,
        ),
        scale_row("Rule-based", rule_based, _SEGMENT_COLORS["material"]),
        scale_row("ML", ml_estimate, _SEGMENT_COLORS["machining"]),
    ]

    if rule_based > 0 and abs(ml_estimate - rule_based) / rule_based > 0.5:
        children.append(
            ft.Container(
                content=ft.Text(
                    "Estimates differ significantly; check inputs.",
                    size=11,
                    color=palette["diff_note_text"],
                    font_family=FONT_FAMILY,
                ),
                bgcolor=palette["diff_note_bg"],
                border_radius=6,
                padding=pad_symmetric(horizontal=8, vertical=4),
            )
        )

    return ft.Container(
        content=ft.Column(children, spacing=8),
        bgcolor=palette["detail_bg"],
        border_radius=10,
        padding=pad_all(12),
    )


def _action_row(
    palette: dict,
    on_what_if: Optional[Callable],
    on_export_pdf: Optional[Callable],
    on_compare_materials: Optional[Callable],
) -> Optional[ft.Control]:
    buttons = []
    if on_what_if is not None:
        buttons.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.TUNE_ROUNDED, size=15, color="#FFFFFF"),
                        ft.Text("What if...", size=13, weight=ft.FontWeight.W_600, color="#FFFFFF", font_family=FONT_FAMILY),
                    ],
                    spacing=6,
                    tight=True,
                ),
                bgcolor=palette["accent"],
                border_radius=8,
                padding=pad_symmetric(horizontal=14, vertical=9),
                ink=True,
                on_click=on_what_if,
            )
        )

    def outlined(label: str, icon, handler) -> ft.Container:
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon, size=15, color=palette["accent"]),
                    ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=palette["accent"], font_family=FONT_FAMILY),
                ],
                spacing=6,
                tight=True,
            ),
            border=ft.Border.all(1, palette["accent"]),
            border_radius=8,
            padding=pad_symmetric(horizontal=14, vertical=9),
            ink=True,
            on_click=handler,
        )

    if on_export_pdf is not None:
        buttons.append(outlined("Export PDF", ft.Icons.PICTURE_AS_PDF_OUTLINED, on_export_pdf))
    if on_compare_materials is not None:
        buttons.append(outlined("Compare materials", ft.Icons.SWAP_HORIZ_ROUNDED, on_compare_materials))

    if not buttons:
        return None
    return ft.Row(buttons, spacing=10)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_cost_card(
    data: dict,
    *,
    dark: bool = False,
    width_hint: int = 900,
    on_what_if: Optional[Callable[[ft.ControlEvent], object]] = None,
    on_export_pdf: Optional[Callable[[ft.ControlEvent], object]] = None,
    on_compare_materials: Optional[Callable[[ft.ControlEvent], object]] = None,
) -> tuple[ft.Control, Callable[[ft.Page], Awaitable[None]]]:
    """Build the redesigned cost breakdown card for one estimate_cost()
    result dict.

    Returns (control, animate_in). `control` renders fully and correctly
    on its own (static). `animate_in` is an async function the caller
    should `await` shortly after the control is actually attached to a
    page and `page.update()` has run once -- it fades/slides the card in
    over 200ms and grows the cost-split-bar segments from 0 to their real
    width over 400ms. Skipping the await is harmless (the card is just
    immediately in its final state).

    `on_what_if`/`on_export_pdf`/`on_compare_materials` are plain Flet
    on_click handlers. Each action button is omitted entirely when its
    handler is None -- per the design brief, a feature that isn't wired
    up is hidden, not shown disabled or with a dead link.
    """
    palette = _PALETTE[dark]
    process = data.get("manufacturing_process") or "CNC Machining"
    bar_width = max(220, min(width_hint - 80, 460))

    header = _header(data, palette, dark)
    hero = _hero_tiles(data, palette, width_hint)

    segment_builder = _SEGMENT_BUILDERS.get(process)
    segments = segment_builder(data) if segment_builder else []
    bar, legend, mount_targets = _split_bar(segments, palette, bar_width)

    detail_rows = _material_detail_rows(data)
    production_builder = _PRODUCTION_DETAIL_BUILDERS.get(process)
    if production_builder:
        detail_rows += production_builder(data.get("production_cost_breakdown") or {})
    detail_table = _detail_table(detail_rows, palette) if detail_rows else None

    bullets = _assumptions_bullets(data.get("production_cost_assumptions") or data.get("assumptions"))
    bullet_controls = [
        ft.Row(
            [
                ft.Text("•", size=14, color=palette["subtitle"], font_family=FONT_FAMILY),
                ft.Text(b, size=12, color=palette["label"], font_family=FONT_FAMILY, expand=True),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        for b in bullets
    ]

    collapsible_children = []
    if detail_table is not None:
        collapsible_children.append(detail_table)
    if bullet_controls:
        collapsible_children.append(ft.Column(bullet_controls, spacing=6))
    collapsible = (
        _collapsible_section("Show calculation", ft.Column(collapsible_children, spacing=12), palette)
        if collapsible_children
        else None
    )

    ml_strip = _ml_comparison_strip(data, palette, bar_width)
    actions = _action_row(palette, on_what_if, on_export_pdf, on_compare_materials)

    body_children = [header, hero]
    if segments:
        body_children.append(ft.Column([bar, legend], spacing=10))
    if collapsible is not None:
        body_children.append(collapsible)
    if ml_strip is not None:
        body_children.append(ml_strip)
    if actions is not None:
        body_children.append(actions)

    card = ft.Container(
        content=ft.Column(body_children, spacing=16),
        bgcolor=palette["card_bg"],
        border_radius=12,
        padding=pad_all(16),
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=12, color="#1A000000", offset=ft.Offset(0, 4)),
        # Visible/complete by construction -- NOT opacity=0/width=0 pending a
        # required animate_in call. A caller that never awaits animate_in
        # (e.g. a one-off render, or a screenshot) must still see a fully
        # correct, fully visible card; animate_in (below) is a pure bonus
        # flourish that retroactively hides-then-reveals it, not the only
        # path to a visible state.
        opacity=1,
        offset=ft.Offset(0, 0),
        animate_opacity=ft.Animation(ENTRANCE_ANIM_MS, ft.AnimationCurve.EASE_OUT),
        animate_offset=ft.Animation(ENTRANCE_ANIM_MS, ft.AnimationCurve.EASE_OUT),
    )

    async def animate_in(page: ft.Page) -> None:
        """Optional entrance flourish: snap to hidden/collapsed, let that
        first frame paint, then animate back to the normal visible state
        this card already renders in by default. Call shortly after the
        card is attached to `page` and page.update() has run once.
        """
        import asyncio

        card.opacity = 0
        card.offset = ft.Offset(0, 0.08)
        for container, _target_width in mount_targets:
            container.width = 0
        page.update()
        await asyncio.sleep(0.03)
        card.opacity = 1
        card.offset = ft.Offset(0, 0)
        for container, target_width in mount_targets:
            container.width = target_width
        page.update()

    return card, animate_in
