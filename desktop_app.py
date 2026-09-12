"""Flet desktop chat UI for the CAD AI copilot.

A native desktop window (not a browser tab) wrapping run_ai_orchestrator()
from ai_orchestrator.py, with a ChatGPT/Claude-style sidebar of past
conversations persisted to a local SQLite database (chat_db.py). Each
question is sent to the orchestrator on a background thread so the UI
never freezes while Ollama/MCP/SolidWorks are working.

Run with:  C:\\Python314\\python.exe desktop_app.py
"""

import asyncio
import datetime
import tempfile
from dataclasses import dataclass
from pathlib import Path

import flet as ft

import chat_db
import mcp_server
from ai_orchestrator import (
    AiOrchestrator,
    McpServerUnavailableError,
    run_ai_orchestrator_with_data,
)
from bom_export import export_bom as write_bom_file
from cad_adapters.fusion_adapter import FusionAdapter
from cad_adapters.solidworks_adapter import SolidWorksAdapter
from report.generate_report import generate_manufacturing_report

# CAD platform selection (post-login, see build_platform_popup /
# run_platform_selection). Values match mcp_server._build_adapter()'s
# CAD_ADAPTER branches and chat_db.users.last_used_platform.
PLATFORM_SOLIDWORKS = "solidworks"
PLATFORM_FUSION = "fusion360"
PLATFORM_LABELS = {PLATFORM_SOLIDWORKS: "SolidWorks", PLATFORM_FUSION: "Fusion 360"}

COLOR_STATUS_RUNNING = "#2E7D32"
COLOR_STATUS_NOT_DETECTED = "#C62828"

# Defaults used only when exporting a BOM from the button (no chat context
# to pull manufacturing_process/quantity from) -- always stated explicitly
# in the resulting chat bubble, same "never silently present a default as
# the user's real request" rule generate_manufacturing_report follows.
_DEFAULT_BOM_MANUFACTURING_PROCESS = "CNC Machining"
_DEFAULT_BOM_QUANTITY = 1

# --------------------------------------------------------------------------
# Style constants -- change colors/fonts/spacing here, nowhere else.
# --------------------------------------------------------------------------

APP_TITLE = "AI Copilot for CAD"
APP_SUBTITLE = "Ask about the currently open CAD model"

COLOR_SIDEBAR_BG = "#1B2A41"
COLOR_SIDEBAR_TEXT = "#C7D1DE"
COLOR_SIDEBAR_TEXT_HOVER = "#FFFFFF"
COLOR_SIDEBAR_TEXT_MUTED = "#8496AD"
COLOR_SIDEBAR_ROW_ACTIVE_BG = "#2A3C58"
COLOR_SIDEBAR_ROW_HOVER_BG = "#22314A"
COLOR_SIDEBAR_DIVIDER = "#2E3F5B"

COLOR_HEADER_BG = "#1B2A41"
COLOR_HEADER_TEXT = "#F5F7FA"
COLOR_HEADER_SUBTEXT = "#8FB8E0"

COLOR_CHAT_BG = "#F5F7FA"
COLOR_INPUT_BAR_BG = "#FFFFFF"
COLOR_INPUT_BORDER = "#DDE3EC"

COLOR_USER_BUBBLE = "#2F6FED"
COLOR_USER_TEXT = "#FFFFFF"

COLOR_AI_BUBBLE = "#FFFFFF"
COLOR_AI_TEXT = "#1B2A41"
COLOR_AI_BORDER = "#E4E9F1"

COLOR_ERROR_BUBBLE = "#FDEAEA"
COLOR_ERROR_TEXT = "#8A2222"

COLOR_ACCENT = "#2F6FED"
COLOR_ACCENT_HOVER = "#4C82F0"
COLOR_TIMESTAMP = "#9AA4B2"

# Login/signup screen -- dark slate card on a dark slate page, matching the
# sidebar's existing look (COLOR_SIDEBAR_BG family) rather than the light
# chat area, since this screen is the app's "shell" chrome, not chat content.
COLOR_LOGIN_PAGE_BG = COLOR_SIDEBAR_BG
COLOR_LOGIN_CARD_BG = "#22314A"
COLOR_LOGIN_FIELD_BG = "#1B2A41"
COLOR_LOGIN_TITLE = COLOR_HEADER_TEXT
COLOR_LOGIN_SUBTEXT = COLOR_SIDEBAR_TEXT_MUTED
COLOR_LOGIN_LABEL = COLOR_SIDEBAR_TEXT_MUTED
COLOR_LOGIN_TEXT = COLOR_HEADER_TEXT
COLOR_LOGIN_ERROR = "#FF8A80"  # lighter red than COLOR_ERROR_TEXT -- readable on a dark card

FONT_FAMILY = "Segoe UI"
FONT_SIZE_MESSAGE = 14
FONT_SIZE_TIMESTAMP = 11
FONT_SIZE_TITLE = 18
FONT_SIZE_SUBTITLE = 12
FONT_SIZE_SIDEBAR_TITLE = 13
FONT_SIZE_SIDEBAR_TIME = 11

SIDEBAR_WIDTH = 260
BUBBLE_RADIUS = 16
BUBBLE_MAX_WIDTH_RATIO = 0.7
AVATAR_RADIUS = 15
SPACING = 16

WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 780

ANIM_DURATION_MS = 250


def pad_symmetric(horizontal: int = 0, vertical: int = 0) -> ft.Padding:
    return ft.Padding(left=horizontal, right=horizontal, top=vertical, bottom=vertical)


def pad_all(value: int) -> ft.Padding:
    return ft.Padding(left=value, top=value, right=value, bottom=value)


@dataclass
class ChatMessage:
    role: str  # "user" | "ai" | "error"
    text: str
    timestamp: str
    # Populated only for an "ai" message that answered via one of the
    # cost/BOM tools (see build_bubble's STRUCTURED_CARD_BUILDERS) --
    # lets the bubble render a real table/breakdown card instead of the
    # plain Text `text` above. `text` is always still set (the model's
    # own prose, or the deterministic text summary) so nothing regresses
    # for tools this doesn't have a card for, and so reopening a past
    # conversation from chat_db (which only ever stores `text`, not this)
    # still shows something sensible.
    structured_tool: str | None = None
    structured_data: dict | None = None


EXAMPLE_QUESTIONS = [
    "What's the mass of this part?",
    "What material is this made of?",
    "Estimate the cost for a batch of 50",
    "Run a DFM check on this part",
]


# --------------------------------------------------------------------------
# Startup environment check
# --------------------------------------------------------------------------


async def check_environment() -> str | None:
    """Return a friendly problem description, or None if everything is up.

    Reuses AiOrchestrator.start() (which really launches mcp_server.py and
    does the MCP handshake) plus a lightweight Ollama ping, so this check
    exercises the same code paths the chat will actually use instead of
    guessing.
    """
    orchestrator = AiOrchestrator()
    try:
        await orchestrator.start()
    except McpServerUnavailableError:
        return (
            "The CAD MCP server didn't start. Make sure mcp_server.py runs "
            "without errors (and, if you're using the SolidWorks adapter, "
            "that SolidWorks is installed)."
        )

    try:
        await orchestrator._ollama.list()
    except Exception:
        return (
            "Ollama isn't reachable. Start it with `ollama serve` and "
            "reopen this app."
        )
    finally:
        await orchestrator.stop()

    return None


# --------------------------------------------------------------------------
# CAD platform detection & selection
# --------------------------------------------------------------------------


def _detect_solidworks() -> bool:
    return SolidWorksAdapter.is_running()


def _detect_fusion360() -> bool:
    return FusionAdapter.is_running()


async def detect_platforms() -> dict[str, bool]:
    """Quick, side-effect-free "is it running" check for both platforms,
    run in parallel off the UI thread so a platform that isn't running
    (which each is_running() still has to wait out a short timeout for)
    doesn't double the total wait.
    """
    sw_running, fusion_running = await asyncio.gather(
        asyncio.to_thread(_detect_solidworks),
        asyncio.to_thread(_detect_fusion360),
    )
    return {PLATFORM_SOLIDWORKS: sw_running, PLATFORM_FUSION: fusion_running}


def _try_connect_platform(platform: str) -> tuple[bool, str]:
    """Actually attempt adapter.connect() for `platform` (unlike
    detect_platforms(), which only pings/probes) -- this is a real
    connection attempt, e.g. it will fail with a specific message if
    SolidWorks is running but has no document open. Returns (success,
    error_message); error_message is "" on success. Never raises: every
    adapter's connect() already carries a clear, specific message on its
    own exception, and reusing str(exc) here keeps this one place from
    duplicating/drifting from that wording.
    """
    try:
        if platform == PLATFORM_SOLIDWORKS:
            SolidWorksAdapter().connect()
        elif platform == PLATFORM_FUSION:
            FusionAdapter().connect()
        else:
            return False, f"Unknown CAD platform '{platform}'."
        return True, ""
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the popup
        return False, str(exc)


def _activate_platform(platform: str, user_id: int) -> None:
    """Make `platform` the active CAD backend for the whole app and
    remember it as this user's default for next login.

    mcp_server.set_backend() updates both the in-process `adapter`
    singleton (used directly by the BOM/report export buttons) and
    CAD_ADAPTER in os.environ (picked up by every future
    run_ai_orchestrator() call, which spawns a fresh mcp_server.py
    subprocess per chat message) -- see that function's docstring for why
    both need to move together. Only called after a real successful
    connect(), never after a mere detection ping.
    """
    mcp_server.set_backend(platform)
    chat_db.set_last_used_platform(user_id, platform)


async def show_platform_selector(
    page: ft.Page,
    user_id: int,
    initial_detected: dict[str, bool],
) -> str:
    """Show the SolidWorks / Fusion 360 selection popup as a page overlay
    and block until the user successfully connects to one platform, then
    return its constant (PLATFORM_SOLIDWORKS / PLATFORM_FUSION).

    Built as a plain page.overlay entry (Container scrim + centered card),
    not ft.AlertDialog -- this project has a documented history in this
    Flet environment of unreliable rendering for less battle-tested
    widgets (see the login screen's own avoidance of ft.Tabs), so this
    reuses only Container/Row/Column/Text/ProgressRing, the same controls
    already proven to render correctly elsewhere in this file. Works
    identically whether called during initial login (chat not built yet)
    or mid-session via "Switch CAD Software" (the existing chat/sidebar
    underneath is untouched -- this only ever adds/removes one overlay
    entry).
    """
    loop = asyncio.get_running_loop()
    resolved: asyncio.Future[str] = loop.create_future()

    last_used = chat_db.get_last_used_platform(user_id)
    detected = dict(initial_detected)
    card_refs: dict[str, dict] = {}

    def _set_card_status(platform: str, running: bool) -> None:
        refs = card_refs[platform]
        refs["status_dot"].bgcolor = COLOR_STATUS_RUNNING if running else COLOR_STATUS_NOT_DETECTED
        refs["status_label"].value = "Running" if running else "Not detected"
        refs["status_label"].color = COLOR_STATUS_RUNNING if running else COLOR_STATUS_NOT_DETECTED

    async def attempt_connect(platform: str) -> None:
        refs = card_refs[platform]
        refs["connect_btn"].disabled = True
        refs["spinner"].visible = True
        refs["connect_text"].value = "Connecting..."
        refs["error_text"].visible = False
        page.update()

        success, message = await asyncio.to_thread(_try_connect_platform, platform)

        refs["spinner"].visible = False
        refs["connect_text"].value = "Connect"
        refs["connect_btn"].disabled = False
        _set_card_status(platform, success)

        if success:
            page.update()
            _activate_platform(platform, user_id)
            if overlay in page.overlay:
                page.overlay.remove(overlay)
            page.update()
            if not resolved.done():
                resolved.set_result(platform)
        else:
            refs["error_text"].value = message
            refs["error_text"].visible = True
            page.update()

    def build_card(platform: str) -> ft.Container:
        is_running = detected.get(platform, False)
        is_preselected = platform == last_used

        status_dot = ft.Container(
            width=10, height=10, border_radius=5,
            bgcolor=COLOR_STATUS_RUNNING if is_running else COLOR_STATUS_NOT_DETECTED,
        )
        status_label = ft.Text(
            "Running" if is_running else "Not detected",
            size=12,
            color=COLOR_STATUS_RUNNING if is_running else COLOR_STATUS_NOT_DETECTED,
            weight=ft.FontWeight.W_600,
            font_family=FONT_FAMILY,
        )
        spinner = ft.ProgressRing(width=14, height=14, stroke_width=2, color="#FFFFFF", visible=False)
        connect_text = ft.Text("Connect", size=13, weight=ft.FontWeight.W_600, color="#FFFFFF", font_family=FONT_FAMILY)
        error_text = ft.Text("", size=11, color=COLOR_LOGIN_ERROR, font_family=FONT_FAMILY, visible=False)

        connect_btn = ft.Container(
            content=ft.Row([spinner, connect_text], spacing=6, alignment=ft.MainAxisAlignment.CENTER, tight=True),
            bgcolor=COLOR_ACCENT,
            border_radius=8,
            padding=pad_symmetric(horizontal=14, vertical=10),
            alignment=ft.Alignment(0, 0),
            ink=True,
        )

        async def _on_connect_click(e: ft.ControlEvent, platform: str = platform) -> None:
            await attempt_connect(platform)

        connect_btn.on_click = _on_connect_click

        card_refs[platform] = {
            "status_dot": status_dot,
            "status_label": status_label,
            "spinner": spinner,
            "connect_text": connect_text,
            "connect_btn": connect_btn,
            "error_text": error_text,
        }

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                PLATFORM_LABELS[platform], size=15, weight=ft.FontWeight.BOLD,
                                color=COLOR_LOGIN_TITLE, font_family=FONT_FAMILY, expand=True,
                            ),
                            ft.Row([status_dot, status_label], spacing=6),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    connect_btn,
                    error_text,
                ],
                spacing=12,
                tight=True,
            ),
            bgcolor=COLOR_LOGIN_FIELD_BG,
            border=ft.Border.all(2 if is_preselected else 1, COLOR_ACCENT if is_preselected else COLOR_SIDEBAR_DIVIDER),
            border_radius=12,
            padding=pad_all(16),
            width=220,
        )

    async def retry_detection(e: ft.ControlEvent | None = None) -> None:
        nonlocal detected
        retry_btn.disabled = True
        retry_text.value = "Checking..."
        page.update()

        detected = await detect_platforms()
        for platform in (PLATFORM_SOLIDWORKS, PLATFORM_FUSION):
            _set_card_status(platform, detected[platform])
            card_refs[platform]["error_text"].visible = False

        retry_btn.disabled = False
        retry_text.value = "Retry Detection"
        page.update()

    retry_text = ft.Text("Retry Detection", size=13, weight=ft.FontWeight.W_600, color=COLOR_LOGIN_SUBTEXT, font_family=FONT_FAMILY)
    retry_btn = ft.Container(
        content=retry_text,
        bgcolor="transparent",
        border=ft.Border.all(1, COLOR_SIDEBAR_DIVIDER),
        border_radius=8,
        padding=pad_symmetric(horizontal=14, vertical=10),
        alignment=ft.Alignment(0, 0),
        ink=True,
        on_click=retry_detection,
    )

    card_row = ft.Row([build_card(PLATFORM_SOLIDWORKS), build_card(PLATFORM_FUSION)], spacing=16)

    popup_card = ft.Container(
        content=ft.Column(
            [
                ft.Text("Connect to CAD Software", size=18, weight=ft.FontWeight.BOLD, color=COLOR_LOGIN_TITLE, font_family=FONT_FAMILY),
                ft.Text(
                    "Choose which CAD platform to connect to for this session.",
                    size=12, color=COLOR_LOGIN_SUBTEXT, font_family=FONT_FAMILY,
                ),
                card_row,
                retry_btn,
            ],
            spacing=16,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=COLOR_LOGIN_CARD_BG,
        border_radius=16,
        padding=pad_all(28),
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=24, color="#40000000", offset=ft.Offset(0, 8)),
    )

    overlay = ft.Container(content=popup_card, alignment=ft.Alignment(0, 0), expand=True, bgcolor="#B3000000")

    page.overlay.append(overlay)
    page.update()

    return await resolved


# --------------------------------------------------------------------------
# Chat area controls
# --------------------------------------------------------------------------


def build_avatar(role: str) -> ft.CircleAvatar:
    if role == "user":
        return ft.CircleAvatar(
            content=ft.Text("U", size=12, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
            bgcolor=COLOR_ACCENT,
            radius=AVATAR_RADIUS,
        )
    return ft.CircleAvatar(
        content=ft.Icon(ft.Icons.SMART_TOY_OUTLINED, size=16, color="#FFFFFF"),
        bgcolor="#5B6B85",
        radius=AVATAR_RADIUS,
    )


# --------------------------------------------------------------------------
# Structured result cards (BOM tables / cost breakdowns)
#
# Renders the real found=True dict a cost/BOM tool returned (see
# AiOrchestrator.last_structured_tool/last_structured_result in
# ai_orchestrator.py) as actual Flet widgets -- a styled table with a
# totals footer, Make/Buy chips, and per-row cost-share bars -- instead of
# the plain text lines _format_get_assembly_bom_answer() etc. produce for
# the model's own context. Purely a display change: every number here is
# read directly off the same dict the text formatters already use, none
# of it is recomputed.
# --------------------------------------------------------------------------

COLOR_CHIP_MAKE_BG = "#E3F2E5"
COLOR_CHIP_MAKE_TEXT = "#2E7D32"
COLOR_CHIP_BUY_BG = "#E3ECFB"
COLOR_CHIP_BUY_TEXT = COLOR_ACCENT
COLOR_CHIP_BUY_NOPRICE_BG = "#FFF3E0"
COLOR_CHIP_BUY_NOPRICE_TEXT = "#B26A00"

COST_BAR_TRACK_WIDTH = 80
COST_BAR_HEIGHT = 5
COST_BAR_TRACK_COLOR = "#E4E9F1"
COST_BAR_FILL_COLOR = COLOR_ACCENT

# Above this many BOM rows, show a one-line summary bubble with a "View
# Full BOM" toggle instead of the full table by default (Step 5) -- a
# small BOM is more useful shown in full immediately.
BOM_SUMMARY_ROW_THRESHOLD = 5

CARD_TOP_RADIUS = ft.BorderRadius(top_left=BUBBLE_RADIUS, top_right=BUBBLE_RADIUS, bottom_left=0, bottom_right=0)
CARD_BOTTOM_RADIUS = ft.BorderRadius(top_left=0, top_right=0, bottom_left=BUBBLE_RADIUS, bottom_right=BUBBLE_RADIUS)


def _fmt_mass(value) -> str:
    return f"{value:.3f} kg" if isinstance(value, (int, float)) else "—"


def _fmt_money(value) -> str:
    return f"Rs {value:,.2f}" if isinstance(value, (int, float)) else "—"


def build_classification_chip(classification: str) -> ft.Container:
    if classification == "Make":
        bg, text_color, label = COLOR_CHIP_MAKE_BG, COLOR_CHIP_MAKE_TEXT, "Make"
    elif classification == "Buy":
        bg, text_color, label = COLOR_CHIP_BUY_BG, COLOR_CHIP_BUY_TEXT, "Buy"
    else:  # "Buy - price not available"
        bg, text_color, label = COLOR_CHIP_BUY_NOPRICE_BG, COLOR_CHIP_BUY_NOPRICE_TEXT, "Buy (no price)"
    return ft.Container(
        content=ft.Text(label, size=11, weight=ft.FontWeight.W_600, color=text_color),
        bgcolor=bg,
        border_radius=10,
        padding=pad_symmetric(horizontal=8, vertical=3),
    )


def build_cost_share_bar(fraction: float) -> ft.Container:
    """A thin filled-track bar showing `fraction` (0..1) of some total --
    the row's share of the assembly's total cost, or the material/
    production split of a single part's cost. Two nested Containers (an
    outer fixed-width "track" and an inner width-scaled "fill") rather
    than a progress-bar control, to match this app's existing hand-built
    Container-based styling instead of introducing a new widget family.
    """
    fraction = max(0.0, min(fraction, 1.0))
    return ft.Container(
        content=ft.Container(
            width=COST_BAR_TRACK_WIDTH * fraction,
            height=COST_BAR_HEIGHT,
            bgcolor=COST_BAR_FILL_COLOR,
            border_radius=3,
        ),
        width=COST_BAR_TRACK_WIDTH,
        height=COST_BAR_HEIGHT,
        bgcolor=COST_BAR_TRACK_COLOR,
        border_radius=3,
        alignment=ft.Alignment(-1, 0),
    )


def _card_header(text: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600),
        bgcolor=COLOR_HEADER_BG,
        padding=pad_symmetric(horizontal=12, vertical=8),
        border_radius=CARD_TOP_RADIUS,
    )


def _stat(label: str, value: str, value_color: str = COLOR_AI_TEXT) -> ft.Column:
    return ft.Column(
        [
            ft.Text(label, size=11, color=COLOR_TIMESTAMP),
            ft.Text(value, size=15, weight=ft.FontWeight.BOLD, color=value_color),
        ],
        spacing=2,
    )


def _card_container(children: list[ft.Control]) -> ft.Container:
    return ft.Container(
        content=ft.Column(children, spacing=0, tight=True),
        border_radius=BUBBLE_RADIUS,
        border=ft.Border.all(1, COLOR_AI_BORDER),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=6, color="#14000000", offset=ft.Offset(0, 2)),
    )


COLOR_WARNING_TEXT = "#B26A00"
COLOR_WARNING_ICON = "#B26A00"


def build_material_warning_icon(material_name) -> ft.Icon:
    """Small warning triangle for a BOM row whose material_verified is
    False -- see AssemblyComponent.material_verified's docstring
    (base_adapter.py) for what this actually means: the material shown
    isn't necessarily wrong, but the adapter can't confirm it was a real
    designer assignment rather than the CAD platform's own untouched
    default (diagnosed live for Fusion: a body that was never given a
    material explicitly still resolves to Fusion's built-in "Steel").
    Tooltip names the actual material so the warning is specific, not
    just a generic "something's off" flag.
    """
    material_label = material_name if material_name else "this component"
    return ft.Icon(
        ft.Icons.WARNING_AMBER_ROUNDED,
        size=15,
        color=COLOR_WARNING_ICON,
        tooltip=(
            f"Material \"{material_label}\" could not be confirmed as a "
            "deliberate assignment -- it may just be the CAD platform's "
            "own default for a part nobody set a material on. Verify in "
            "the CAD tool before relying on this for cost/carbon figures."
        ),
    )


def build_bom_stats_block(
    totals: dict,
    manufacturing_process,
    quantity,
    missing_data: list[dict],
    unverified_count: int,
    position: str = "bottom",
) -> ft.Container:
    """The tinted, bordered stats block shared by the single-flat-table BOM
    card (as a footer, `position="bottom"`) and the categorized BOM view's
    prominent summary-first header (`position="top"`) -- same visual
    language either way, just which edge carries the border/radius and
    which corners round off.
    """
    stats = ft.Row(
        [
            _stat("Unique parts", str(totals.get("unique_part_count", "?"))),
            _stat("Instances", str(totals.get("total_instance_count", "?"))),
            _stat("Total mass", _fmt_mass(totals.get("total_assembly_mass_kg"))),
            _stat("Cost / assembly", _fmt_money(totals.get("total_assembly_cost_one_unit_inr")), COLOR_ACCENT),
            _stat(f"Total for {quantity}", _fmt_money(totals.get("total_assembly_cost_for_quantity_inr")), COLOR_ACCENT),
        ],
        spacing=22,
        wrap=True,
    )
    children = [
        ft.Text(f"{manufacturing_process} · quantity {quantity}", size=11, color=COLOR_TIMESTAMP),
        stats,
    ]
    if unverified_count:
        children.append(
            ft.Row(
                [
                    ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=13, color=COLOR_WARNING_ICON),
                    ft.Text(
                        f"{unverified_count} component(s) have an unverified material "
                        "(⚠ icon in the table below) -- may be the CAD platform's "
                        "untouched default, not a real assignment.",
                        size=11,
                        color=COLOR_WARNING_TEXT,
                    ),
                ],
                spacing=6,
            )
        )
    if missing_data:
        children.append(
            ft.Text(
                f"{len(missing_data)} component(s) missing pricing data -- see chat text for details.",
                size=11,
                color=COLOR_ERROR_TEXT,
            )
        )
    return ft.Container(
        content=ft.Column(children, spacing=8),
        bgcolor=COLOR_CHAT_BG,
        padding=pad_symmetric(horizontal=14, vertical=12),
        border=ft.Border(
            top=ft.BorderSide(1, COLOR_AI_BORDER) if position == "bottom" else None,
            bottom=ft.BorderSide(1, COLOR_AI_BORDER) if position == "top" else None,
        ),
        border_radius=CARD_BOTTOM_RADIUS if position == "bottom" else CARD_TOP_RADIUS,
    )


def build_bom_totals_footer(totals: dict, manufacturing_process, quantity, missing_data: list[dict], unverified_count: int) -> ft.Container:
    return build_bom_stats_block(totals, manufacturing_process, quantity, missing_data, unverified_count, position="bottom")


def build_bom_rows_table(rows: list[dict], total_cost_one_unit) -> ft.Column:
    header = ft.Container(
        content=ft.Row(
            [
                ft.Text("Component", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=4),
                ft.Text("Qty", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=1),
                ft.Text("Unit Mass", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=2),
                ft.Text("Total Mass", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=2),
                ft.Text("Unit Cost", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=2),
                ft.Text("Total Cost", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=3),
            ],
            spacing=8,
        ),
        bgcolor=COLOR_HEADER_BG,
        padding=pad_symmetric(horizontal=12, vertical=8),
        border_radius=CARD_TOP_RADIUS,
    )

    row_controls: list[ft.Control] = [header]
    for i, row in enumerate(rows):
        total_cost = row.get("total_cost_inr")
        fraction = (
            (total_cost / total_cost_one_unit)
            if isinstance(total_cost, (int, float)) and total_cost_one_unit
            else 0.0
        )
        name_cell_children = [
            ft.Text(
                row.get("part_name", "unnamed"),
                size=13,
                color=COLOR_AI_TEXT,
                weight=ft.FontWeight.W_500,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            build_classification_chip(row.get("classification", "Make")),
        ]
        if row.get("material_verified") is False:
            name_cell_children.append(build_material_warning_icon(row.get("material")))
        name_cell = ft.Row(name_cell_children, spacing=8, tight=True)
        cost_cell = ft.Column(
            [
                ft.Text(_fmt_money(total_cost), size=13, color=COLOR_AI_TEXT, weight=ft.FontWeight.W_600),
                build_cost_share_bar(fraction),
            ],
            spacing=3,
            tight=True,
        )
        row_controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Container(name_cell, expand=4),
                        ft.Text(str(row.get("quantity_per_assembly", "?")), size=13, color=COLOR_AI_TEXT, expand=1),
                        ft.Text(_fmt_mass(row.get("unit_mass_kg")), size=13, color=COLOR_AI_TEXT, expand=2),
                        ft.Text(_fmt_mass(row.get("total_mass_kg")), size=13, color=COLOR_AI_TEXT, expand=2),
                        ft.Text(_fmt_money(row.get("unit_cost_inr")), size=13, color=COLOR_AI_TEXT, expand=2),
                        ft.Container(cost_cell, expand=3),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=pad_symmetric(horizontal=12, vertical=10),
                bgcolor="#FFFFFF" if i % 2 == 0 else "#FAFBFD",
                border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
            )
        )
    return ft.Column(row_controls, spacing=0, tight=True)


def build_bom_card(data: dict, rows_key: str) -> ft.Control:
    """`rows_key` is "bom" for get_assembly_bom, "cost_drivers" for
    get_assembly_cost_drivers -- same row shape either way, just a
    different key and (for cost_drivers) pre-sorted order.
    """
    rows = data.get(rows_key, [])
    totals = data.get("totals", {})
    manufacturing_process = data.get("manufacturing_process", "?")
    quantity = data.get("quantity", "?")
    missing_data = data.get("missing_data", [])
    total_cost_one_unit = totals.get("total_assembly_cost_one_unit_inr")
    unverified_count = sum(1 for row in rows if row.get("material_verified") is False)

    table = build_bom_rows_table(rows, total_cost_one_unit)
    footer = build_bom_totals_footer(totals, manufacturing_process, quantity, missing_data, unverified_count)
    card = _card_container([table, footer])

    if len(rows) <= BOM_SUMMARY_ROW_THRESHOLD:
        return card

    # Step 5: summary-first for large BOMs, expandable in place.
    card.visible = False
    view_button = ft.TextButton("View Full BOM")
    summary_text = (
        f"BOM generated — {totals.get('unique_part_count', '?')} unique parts, "
        f"{totals.get('total_instance_count', '?')} total instances, "
        f"{_fmt_money(total_cost_one_unit)} per assembly."
    )
    if unverified_count:
        summary_text += f" ⚠ {unverified_count} unverified material(s)."
    summary_container = ft.Container(
        content=ft.Row(
            [
                ft.Text(
                    summary_text,
                    size=13,
                    color=COLOR_WARNING_TEXT if unverified_count else COLOR_AI_TEXT,
                    expand=True,
                ),
                view_button,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=COLOR_AI_BUBBLE,
        border=ft.Border.all(1, COLOR_AI_BORDER),
        border_radius=BUBBLE_RADIUS,
        padding=pad_symmetric(horizontal=14, vertical=10),
    )

    def _toggle(e: ft.ControlEvent) -> None:
        expanding = not card.visible
        card.visible = expanding
        summary_container.visible = not expanding
        view_button.text = "Hide Full BOM" if expanding else "View Full BOM"
        card.update()
        summary_container.update()
        view_button.update()

    view_button.on_click = _toggle
    return ft.Column([summary_container, card], spacing=8, tight=True)


# --------------------------------------------------------------------------
# Category grouping for get_assembly_bom()'s categorized view
#
# Pure display-layer heuristic -- buckets rows by simple keyword matches on
# part_name, purely for how the BOM is presented in chat. Doesn't touch
# get_assembly_cost_drivers() (still the flat, cost-ranked build_bom_card
# above -- categorizing would defeat the point of "highest cost first").
# --------------------------------------------------------------------------

# Checked in this order -- e.g. "Wheel Axle Bolt" should land in Fasteners
# (its own real category), not get swept into Wheels & Casters just
# because "wheel"/"axle" also appear in the name, so Fasteners' fairly
# unambiguous keywords are checked first.
_CATEGORY_KEYWORDS = {
    "Fasteners & Hardware": [
        "screw", "bolt", "nut", "washer", "tee-nut", "tee nut", "insert",
        "rivet", "pin", "fastener", "anchor", "threaded rod", "hex ",
    ],
    "Soft Goods": [
        "foam", "leather", "rubber", "anti-slip", "anti slip", "mat",
        "cushion", "upholstery", "fabric", "padding",
    ],
    "Wheels & Casters": ["wheel", "caster", "castor", "tire", "tyre", "roller", "axle"],
    "Structural/Frame": [
        "tube", "bracket", "plate", "frame", "beam", "rail", "channel",
        "gusset", "support", "chassis",
    ],
}

# Render order -- Fasteners & Hardware deliberately placed just before the
# "Other" catch-all (Step 2: collapsed by default, rarely need individual
# review), everything else expanded and shown first.
CATEGORY_DISPLAY_ORDER = [
    "Structural/Frame",
    "Wheels & Casters",
    "Soft Goods",
    "Fasteners & Hardware",
    "Other",
]

_CATEGORIES_COLLAPSED_BY_DEFAULT = {"Fasteners & Hardware"}


def categorize_component(part_name: str) -> str:
    name_lower = (part_name or "").lower()
    for category in CATEGORY_DISPLAY_ORDER:
        for keyword in _CATEGORY_KEYWORDS.get(category, []):
            if keyword in name_lower:
                return category
    return "Other"


def _group_rows_by_category(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {category: [] for category in CATEGORY_DISPLAY_ORDER}
    for row in rows:
        groups[categorize_component(row.get("part_name", ""))].append(row)
    return groups


def _category_subtotal(rows: list[dict]) -> dict:
    mass_values = [r["total_mass_kg"] for r in rows if isinstance(r.get("total_mass_kg"), (int, float))]
    cost_values = [r["total_cost_inr"] for r in rows if isinstance(r.get("total_cost_inr"), (int, float))]
    return {
        "part_count": len(rows),
        "total_instances": sum(r.get("quantity_per_assembly", 0) or 0 for r in rows),
        "total_mass_kg": sum(mass_values) if mass_values else None,
        "total_cost_inr": sum(cost_values) if cost_values else None,
    }


def build_category_section(category: str, rows: list[dict], total_cost_one_unit, collapsed_by_default: bool) -> ft.Control:
    """One collapsible category section: a clickable dark header line
    (category name + its own subtotal, Step 1) that toggles the full
    Component/Qty/.../Total Cost table (Steps 1, 3, 4 -- reused from
    build_bom_rows_table unchanged) below it. `collapsed_by_default` is a
    fixed per-category rule (Fasteners & Hardware only, Step 2), not the
    row-count heuristic build_bom_card's large-BOM collapse uses.
    """
    subtotal = _category_subtotal(rows)
    unverified_count = sum(1 for r in rows if r.get("material_verified") is False)

    summary_bits = [f"{subtotal['part_count']} type(s)", f"{subtotal['total_instances']} total instance(s)"]
    if subtotal["total_mass_kg"] is not None:
        summary_bits.append(_fmt_mass(subtotal["total_mass_kg"]))
    if subtotal["total_cost_inr"] is not None:
        summary_bits.append(_fmt_money(subtotal["total_cost_inr"]))
    summary_text = f"{category} — " + ", ".join(summary_bits)
    if unverified_count:
        summary_text += f" ⚠ {unverified_count} unverified"

    expanded = not collapsed_by_default
    chevron = ft.Icon(
        ft.Icons.EXPAND_MORE_ROUNDED if expanded else ft.Icons.CHEVRON_RIGHT_ROUNDED,
        size=18,
        color=COLOR_HEADER_TEXT,
    )
    header = ft.Container(
        content=ft.Row(
            [
                chevron,
                ft.Text(
                    summary_text,
                    color=COLOR_WARNING_TEXT if unverified_count and not expanded else COLOR_HEADER_TEXT,
                    size=13,
                    weight=ft.FontWeight.W_600,
                    expand=True,
                ),
            ],
            spacing=8,
        ),
        bgcolor=COLOR_HEADER_BG,
        padding=pad_symmetric(horizontal=12, vertical=10),
        ink=True,
    )
    table_wrapper = ft.Container(content=build_bom_rows_table(rows, total_cost_one_unit), visible=expanded)

    def _toggle(e: ft.ControlEvent) -> None:
        table_wrapper.visible = not table_wrapper.visible
        chevron.name = ft.Icons.EXPAND_MORE_ROUNDED if table_wrapper.visible else ft.Icons.CHEVRON_RIGHT_ROUNDED
        table_wrapper.update()
        chevron.update()

    header.on_click = _toggle
    return ft.Column([header, table_wrapper], spacing=0, tight=True)


def build_categorized_bom_view(data: dict, rows_key: str) -> ft.Control:
    """get_assembly_bom()'s main chat rendering: a prominent summary-stats
    header (Step 3, reusing the same tinted stats block build_bom_card's
    footer uses) followed by one collapsible section per non-empty
    category (Step 1), Fasteners & Hardware collapsed by default (Step 2).
    Per-row material_verified warnings (Step 4) come along for free --
    build_bom_rows_table (shared with build_bom_card, unchanged) already
    adds those.
    """
    rows = data.get(rows_key, [])
    totals = data.get("totals", {})
    manufacturing_process = data.get("manufacturing_process", "?")
    quantity = data.get("quantity", "?")
    missing_data = data.get("missing_data", [])
    total_cost_one_unit = totals.get("total_assembly_cost_one_unit_inr")
    unverified_count = sum(1 for row in rows if row.get("material_verified") is False)

    summary_header = build_bom_stats_block(
        totals, manufacturing_process, quantity, missing_data, unverified_count, position="top"
    )

    groups = _group_rows_by_category(rows)
    sections = [
        build_category_section(
            category, groups[category], total_cost_one_unit, category in _CATEGORIES_COLLAPSED_BY_DEFAULT
        )
        for category in CATEGORY_DISPLAY_ORDER
        if groups[category]
    ]

    return _card_container([summary_header, *sections])


def build_cost_breakdown_card(data: dict) -> ft.Control:
    """Single-part cost card for estimate_cost() -- only ever two line
    items (material cost, production cost), so no Make/Buy chips or
    per-row summary-collapse the way build_bom_card has; the cost-share
    bar here shows each line's share of total_cost_per_unit_inr instead
    of share of an assembly total.
    """
    material_cost = data.get("material_cost_per_unit_inr")
    production_cost = data.get("production_cost_per_unit_inr")
    total_cost = data.get("total_cost_per_unit_inr")
    total_for_qty = data.get("total_cost_for_quantity_inr")
    quantity = data.get("quantity", "?")
    manufacturing_process = data.get("manufacturing_process", "?")
    material_used = data.get("material_used", "?")

    def line_item(label: str, value) -> ft.Container:
        fraction = (value / total_cost) if isinstance(value, (int, float)) and total_cost else 0.0
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(label, size=13, color=COLOR_AI_TEXT, weight=ft.FontWeight.W_500, expand=True),
                            ft.Text(_fmt_money(value), size=13, color=COLOR_AI_TEXT, weight=ft.FontWeight.W_600),
                        ]
                    ),
                    build_cost_share_bar(fraction),
                ],
                spacing=4,
            ),
            padding=pad_symmetric(horizontal=12, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
        )

    header = _card_header(f"Cost breakdown — {material_used}")
    rows = [
        line_item("Material cost", material_cost),
        line_item("Production cost", production_cost),
    ]
    footer = ft.Container(
        content=ft.Column(
            [
                ft.Text(f"{manufacturing_process} · quantity {quantity}", size=11, color=COLOR_TIMESTAMP),
                ft.Row(
                    [
                        _stat("Cost / unit", _fmt_money(total_cost), COLOR_ACCENT),
                        _stat(f"Total for {quantity}", _fmt_money(total_for_qty), COLOR_ACCENT),
                    ],
                    spacing=24,
                ),
            ],
            spacing=8,
        ),
        bgcolor=COLOR_CHAT_BG,
        padding=pad_symmetric(horizontal=14, vertical=12),
        border=ft.Border(top=ft.BorderSide(1, COLOR_AI_BORDER)),
        border_radius=CARD_BOTTOM_RADIUS,
    )
    return _card_container([header, *rows, footer])


# Tool name -> (result dict) -> Flet control. Only tools whose found=True
# shape this app actually knows how to render as a card; every other tool
# (get_mass, run_dfm_check, etc.) keeps using the plain text bubble below,
# unchanged.
STRUCTURED_CARD_BUILDERS = {
    # Categorized view (Step 1-4 redesign) -- get_assembly_cost_drivers
    # deliberately keeps the flat, cost-ranked build_bom_card instead;
    # bucketing by category would defeat its "highest cost first" point.
    "get_assembly_bom": lambda data: build_categorized_bom_view(data, "bom"),
    "get_assembly_cost_drivers": lambda data: build_bom_card(data, "cost_drivers"),
    "estimate_cost": build_cost_breakdown_card,
}


def build_bubble(message: ChatMessage, page_width_hint: int) -> ft.Container:
    is_user = message.role == "user"
    is_error = message.role == "error"

    card_builder = (
        STRUCTURED_CARD_BUILDERS.get(message.structured_tool)
        if message.role == "ai" and message.structured_data is not None
        else None
    )

    if card_builder is not None:
        # The card widget (build_bom_card/build_cost_breakdown_card) already
        # carries its own border/shadow/radius via _card_container, so the
        # wrapping wrapper here stays unstyled -- adding COLOR_AI_BUBBLE's
        # border/shadow on top would double it up. Text still renders too,
        # right below the card, so anything the model added beyond the raw
        # numbers (caveats, units, follow-up prompts) isn't lost.
        bubble = ft.Column(
            [
                card_builder(message.structured_data),
                ft.Text(
                    message.text,
                    color=COLOR_AI_TEXT,
                    size=FONT_SIZE_MESSAGE,
                    font_family=FONT_FAMILY,
                    selectable=True,
                ),
            ],
            spacing=8,
            tight=True,
        )
        bubble_max_width_ratio = 0.92
    else:
        bubble_color = (
            COLOR_ERROR_BUBBLE if is_error else COLOR_USER_BUBBLE if is_user else COLOR_AI_BUBBLE
        )
        text_color = COLOR_ERROR_TEXT if is_error else COLOR_USER_TEXT if is_user else COLOR_AI_TEXT
        bubble = ft.Container(
            content=ft.Text(
                message.text,
                color=text_color,
                size=FONT_SIZE_MESSAGE,
                font_family=FONT_FAMILY,
                selectable=True,
            ),
            bgcolor=bubble_color,
            padding=pad_symmetric(horizontal=14, vertical=10),
            border_radius=BUBBLE_RADIUS,
            border=None if is_user or is_error else ft.Border.all(1, COLOR_AI_BORDER),
            shadow=ft.BoxShadow(
                spread_radius=0,
                blur_radius=6,
                color="#14000000",
                offset=ft.Offset(0, 2),
            ),
        )
        bubble_max_width_ratio = BUBBLE_MAX_WIDTH_RATIO

    meta = ft.Row(
        [
            build_avatar("user" if is_user else "ai"),
            ft.Text(message.timestamp, size=FONT_SIZE_TIMESTAMP, color=COLOR_TIMESTAMP),
        ],
        spacing=6,
        alignment=ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START,
    )

    column = ft.Column(
        [bubble, meta],
        spacing=4,
        horizontal_alignment=ft.CrossAxisAlignment.END if is_user else ft.CrossAxisAlignment.START,
        tight=True,
    )

    wrapper = ft.Container(
        content=column,
        width=int(page_width_hint * bubble_max_width_ratio),
        opacity=0,
        offset=ft.Offset(0, 0.08),
        animate_opacity=ft.Animation(ANIM_DURATION_MS, ft.AnimationCurve.EASE_OUT),
        animate_offset=ft.Animation(ANIM_DURATION_MS, ft.AnimationCurve.EASE_OUT),
    )

    row = ft.Row(
        [wrapper],
        alignment=ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START,
    )
    row.data = wrapper  # stash the animatable control so callers can trigger it
    return row


def build_loading_row() -> ft.Row:
    bubble = ft.Container(
        content=ft.Row(
            [
                ft.ProgressRing(width=14, height=14, stroke_width=2, color=COLOR_ACCENT),
                ft.Text(
                    "Thinking...",
                    color=COLOR_AI_TEXT,
                    size=FONT_SIZE_MESSAGE,
                    font_family=FONT_FAMILY,
                ),
            ],
            spacing=8,
            tight=True,
        ),
        bgcolor=COLOR_AI_BUBBLE,
        border=ft.Border.all(1, COLOR_AI_BORDER),
        padding=pad_symmetric(horizontal=14, vertical=10),
        border_radius=BUBBLE_RADIUS,
    )
    meta = ft.Row(
        [build_avatar("ai"), ft.Text("", size=FONT_SIZE_TIMESTAMP)],
        spacing=6,
    )
    return ft.Row(
        [ft.Column([bubble, meta], spacing=4, tight=True)],
        alignment=ft.MainAxisAlignment.START,
    )


def build_empty_state(on_chip_click) -> ft.Container:
    chips = [
        ft.Container(
            content=ft.Text(q, size=FONT_SIZE_MESSAGE, color=COLOR_ACCENT, font_family=FONT_FAMILY),
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, COLOR_INPUT_BORDER),
            padding=pad_symmetric(horizontal=14, vertical=10),
            border_radius=20,
            on_click=lambda e, question=q: on_chip_click(question),
            ink=True,
        )
        for q in EXAMPLE_QUESTIONS
    ]
    return ft.Container(
        content=ft.Column(
            [
                ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE_ROUNDED, size=40, color="#B7C2D0"),
                ft.Text(
                    "Ask me about your CAD model...",
                    size=16,
                    weight=ft.FontWeight.W_500,
                    color="#5B6B85",
                    font_family=FONT_FAMILY,
                ),
                ft.Row(chips, wrap=True, alignment=ft.MainAxisAlignment.CENTER, spacing=8, run_spacing=8),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=16,
        ),
        alignment=ft.Alignment(0, 0),
        expand=True,
        padding=40,
    )


# --------------------------------------------------------------------------
# Login / signup screen
# --------------------------------------------------------------------------


def build_login_view(page: ft.Page, on_authenticated) -> ft.Control:
    """Centered login/signup card, shown before the chat interface. Local
    accounts only -- "Create Account" makes a profile stored in this
    machine's chat_history.db (see chat_db.create_user), not a cloud
    sign-up.

    `on_authenticated(user_id, username)` is called once login or account
    creation succeeds. Deliberately avoids ft.Tabs for the Log In / Create
    Account toggle -- this project's Flet version has a documented history
    of unreliable rendering for less battle-tested widgets, so this reuses
    only Container/Row/Column/TextField/Text, the same controls already
    proven to render correctly elsewhere in this file.
    """
    mode = {"value": "login"}  # "login" | "signup"

    def field(label: str, is_password: bool) -> ft.TextField:
        return ft.TextField(
            label=label,
            password=is_password,
            can_reveal_password=is_password,
            bgcolor=COLOR_LOGIN_FIELD_BG,
            color=COLOR_LOGIN_TEXT,
            label_style=ft.TextStyle(color=COLOR_LOGIN_LABEL, font_family=FONT_FAMILY),
            text_style=ft.TextStyle(font_family=FONT_FAMILY, color=COLOR_LOGIN_TEXT),
            cursor_color=COLOR_ACCENT,
            border_color=COLOR_SIDEBAR_DIVIDER,
            focused_border_color=COLOR_ACCENT,
            border_radius=10,
        )

    username_field = field("Username", is_password=False)
    password_field = field("Password", is_password=True)
    confirm_field = field("Confirm Password", is_password=True)
    confirm_field.visible = False

    error_text = ft.Text("", color=COLOR_LOGIN_ERROR, size=13, font_family=FONT_FAMILY, visible=False)

    submit_button = ft.Container(
        content=ft.Text("Log In", color="#FFFFFF", size=14, weight=ft.FontWeight.W_600, font_family=FONT_FAMILY),
        bgcolor=COLOR_ACCENT,
        border_radius=10,
        padding=pad_symmetric(horizontal=16, vertical=12),
        alignment=ft.Alignment(0, 0),
        ink=True,
        on_hover=lambda e: _set_bgcolor_on_hover(e, COLOR_ACCENT, COLOR_ACCENT_HOVER),
    )

    def _set_bgcolor_on_hover(e: ft.ControlEvent, base: str, hover: str) -> None:
        e.control.bgcolor = hover if e.data == "true" else base
        e.control.update()

    def pill(label: str, active: bool) -> ft.Container:
        return ft.Container(
            content=ft.Text(
                label,
                color="#FFFFFF" if active else COLOR_LOGIN_SUBTEXT,
                size=13,
                weight=ft.FontWeight.W_600,
                font_family=FONT_FAMILY,
            ),
            bgcolor=COLOR_ACCENT if active else "transparent",
            border_radius=8,
            padding=pad_symmetric(horizontal=14, vertical=8),
            alignment=ft.Alignment(0, 0),
            expand=True,
            ink=True,
        )

    login_pill = pill("Log In", active=True)
    signup_pill = pill("Create Account", active=False)
    toggle_row = ft.Container(
        content=ft.Row([login_pill, signup_pill], spacing=4),
        bgcolor=COLOR_LOGIN_FIELD_BG,
        border_radius=10,
        padding=4,
    )

    def set_error(message: str) -> None:
        error_text.value = message
        error_text.visible = bool(message)
        page.update()

    def switch_mode(new_mode: str) -> None:
        if mode["value"] == new_mode:
            return
        mode["value"] = new_mode
        set_error("")
        confirm_field.value = ""
        confirm_field.visible = new_mode == "signup"
        submit_button.content.value = "Create Account" if new_mode == "signup" else "Log In"
        login_pill.bgcolor = COLOR_ACCENT if new_mode == "login" else "transparent"
        login_pill.content.color = "#FFFFFF" if new_mode == "login" else COLOR_LOGIN_SUBTEXT
        signup_pill.bgcolor = COLOR_ACCENT if new_mode == "signup" else "transparent"
        signup_pill.content.color = "#FFFFFF" if new_mode == "signup" else COLOR_LOGIN_SUBTEXT
        page.update()

    login_pill.on_click = lambda e: switch_mode("login")
    signup_pill.on_click = lambda e: switch_mode("signup")

    def handle_submit(e: ft.ControlEvent) -> None:
        username = (username_field.value or "").strip()
        password = password_field.value or ""

        if mode["value"] == "login":
            if not username or not password:
                set_error("Enter your username and password.")
                return
            user_id = chat_db.verify_user(username, password)
            if user_id is None:
                # Deliberately generic -- never reveals whether the
                # username or the password was the wrong part.
                set_error("Invalid username or password.")
                return
            on_authenticated(user_id, username)
        else:
            confirm = confirm_field.value or ""
            if not username or not password or not confirm:
                set_error("Fill in all fields.")
                return
            if password != confirm:
                set_error("Passwords do not match.")
                return
            result = chat_db.create_user(username, password)
            if not result["success"]:
                set_error(result["message"])
                return
            on_authenticated(result["user_id"], username)

    submit_button.on_click = handle_submit
    username_field.on_submit = handle_submit
    password_field.on_submit = handle_submit
    confirm_field.on_submit = handle_submit

    card = ft.Container(
        content=ft.Column(
            [
                ft.Text(APP_TITLE, color=COLOR_LOGIN_TITLE, size=20, weight=ft.FontWeight.BOLD, font_family=FONT_FAMILY),
                ft.Text(
                    "Local sign-in -- your account and chat history stay on this machine.",
                    color=COLOR_LOGIN_SUBTEXT, size=12, font_family=FONT_FAMILY,
                ),
                toggle_row,
                username_field,
                password_field,
                confirm_field,
                error_text,
                submit_button,
            ],
            spacing=16,
            tight=True,
        ),
        bgcolor=COLOR_LOGIN_CARD_BG,
        border_radius=16,
        padding=pad_all(32),
        width=380,
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=24, color="#40000000", offset=ft.Offset(0, 8)),
    )

    return ft.Container(content=card, alignment=ft.Alignment(0, 0), expand=True, bgcolor=COLOR_LOGIN_PAGE_BG)


# --------------------------------------------------------------------------
# Main app
# --------------------------------------------------------------------------


async def show_chat_interface(page: ft.Page, user_id: int, username: str, on_logout) -> None:
    """Build and show the main chat interface for one logged-in user.

    Everything in here (conversation list, new-chat creation, message
    save/load) is scoped to `user_id` -- see chat_db.list_conversations()/
    create_conversation(), which now require a user_id and only ever
    return/create that user's own rows. `on_logout()` clears this view and
    returns to the login screen without closing the app.
    """
    page.bgcolor = COLOR_CHAT_BG
    page.controls.clear()

    state = {"conversation_id": None, "is_sending": False, "user_id": user_id, "platform": None}

    # ---- controls that get referenced/updated across handlers ----

    chat_list = ft.ListView(expand=True, spacing=SPACING, padding=24, auto_scroll=True)
    empty_state = build_empty_state(lambda q: fill_input(q))
    chat_area = ft.Stack([chat_list, empty_state], expand=True)

    status_banner = ft.Container(visible=False)

    message_input = ft.TextField(
        hint_text="Ask about mass, material, cost, carbon, DFM...",
        expand=True,
        multiline=False,
        border_radius=22,
        border_color=COLOR_INPUT_BORDER,
        focused_border_color=COLOR_ACCENT,
        content_padding=pad_symmetric(horizontal=16, vertical=12),
        text_style=ft.TextStyle(font_family=FONT_FAMILY),
    )

    send_button = ft.IconButton(
        icon=ft.Icons.SEND_ROUNDED,
        icon_color="#FFFFFF",
        bgcolor=COLOR_ACCENT,
        disabled=True,
        style=ft.ButtonStyle(shape=ft.CircleBorder()),
        tooltip="Send",
    )

    export_report_button = ft.IconButton(
        icon=ft.Icons.PICTURE_AS_PDF_OUTLINED,
        icon_color=COLOR_ACCENT,
        style=ft.ButtonStyle(shape=ft.CircleBorder()),
        tooltip="Export manufacturing readiness report (PDF)",
    )

    export_bom_button = ft.IconButton(
        icon=ft.Icons.TABLE_CHART_OUTLINED,
        icon_color=COLOR_ACCENT,
        style=ft.ButtonStyle(shape=ft.CircleBorder()),
        tooltip="Export Bill of Materials (assembly only, .xlsx)",
    )

    sidebar_list = ft.ListView(expand=True, spacing=2, padding=pad_symmetric(vertical=8))
    new_chat_button = ft.Container(
        content=ft.Row(
            [ft.Icon(ft.Icons.ADD_ROUNDED, size=16, color="#FFFFFF"), ft.Text("New Chat", color="#FFFFFF", size=13, weight=ft.FontWeight.W_600)],
            spacing=6,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        bgcolor=COLOR_ACCENT,
        border_radius=20,
        padding=pad_symmetric(horizontal=16, vertical=10),
        margin=pad_symmetric(horizontal=12, vertical=12),
        ink=True,
        on_hover=lambda e: _set_bgcolor_on_hover(e, COLOR_ACCENT, COLOR_ACCENT_HOVER),
    )

    def _set_bgcolor_on_hover(e: ft.ControlEvent, base: str, hover: str) -> None:
        e.control.bgcolor = hover if e.data == "true" else base
        e.control.update()

    # ---- helpers ----

    def timestamp_now() -> str:
        return chat_db.relative_time(__import__("datetime").datetime.now().isoformat())

    def update_platform_badge() -> None:
        platform = state.get("platform")
        platform_badge.visible = platform is not None
        if platform is not None:
            platform_badge_text.value = PLATFORM_LABELS[platform]
        page.update()

    async def show_toast(message: str, success: bool = True) -> None:
        """Brief status banner -- auto-hides after a couple seconds on
        success (a connection confirmation), stays up on failure so the
        user has time to read it. Reuses status_banner (also used by the
        startup Ollama/MCP check below) rather than a second widget, since
        only one of these is ever relevant at a time.
        """
        status_banner.content = ft.Row(
            [
                ft.Icon(
                    ft.Icons.CHECK_CIRCLE_OUTLINE if success else ft.Icons.WARNING_AMBER_ROUNDED,
                    color=COLOR_STATUS_RUNNING if success else COLOR_ERROR_TEXT,
                    size=18,
                ),
                ft.Text(
                    message,
                    color=COLOR_STATUS_RUNNING if success else COLOR_ERROR_TEXT,
                    size=FONT_SIZE_MESSAGE,
                    font_family=FONT_FAMILY,
                    expand=True,
                ),
            ],
            spacing=8,
        )
        status_banner.bgcolor = "#E8F5E9" if success else COLOR_ERROR_BUBBLE
        status_banner.padding = pad_symmetric(horizontal=20, vertical=10)
        status_banner.visible = True
        page.update()
        if success:
            await asyncio.sleep(2.5)
            status_banner.visible = False
            page.update()

    def bubble_width_hint() -> int:
        width = page.window.width or WINDOW_WIDTH
        return int(width - SIDEBAR_WIDTH)

    def fill_input(text: str) -> None:
        message_input.value = text
        send_button.disabled = False
        page.update()
        page.run_task(message_input.focus)

    def update_send_button_state(e: ft.ControlEvent | None = None) -> None:
        send_button.disabled = not bool(message_input.value and message_input.value.strip())
        page.update()

    message_input.on_change = update_send_button_state

    async def add_bubble_animated(message: ChatMessage) -> None:
        row = build_bubble(message, bubble_width_hint())
        chat_list.controls.append(row)
        page.update()
        await asyncio.sleep(0.02)
        animated = row.data
        animated.opacity = 1
        animated.offset = ft.Offset(0, 0)
        page.update()

    def render_sidebar() -> None:
        sidebar_list.controls.clear()
        for conv in chat_db.list_conversations(state["user_id"]):
            sidebar_list.controls.append(build_sidebar_row(conv))
        page.update()

    def build_sidebar_row(conv: chat_db.Conversation) -> ft.Container:
        is_active = state["conversation_id"] == conv.id
        row_state = {"confirming": False}

        title_text = conv.title or "New conversation"
        title_ctrl = ft.Text(
            title_text,
            size=FONT_SIZE_SIDEBAR_TITLE,
            color=COLOR_SIDEBAR_TEXT,
            font_family=FONT_FAMILY,
            weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        time_ctrl = ft.Text(
            chat_db.relative_time(conv.created_at),
            size=FONT_SIZE_SIDEBAR_TIME,
            color=COLOR_SIDEBAR_TEXT_MUTED,
            font_family=FONT_FAMILY,
        )

        action_area = ft.Container(width=20)

        def show_delete_icon() -> None:
            action_area.content = ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                icon_size=16,
                icon_color=COLOR_SIDEBAR_TEXT_MUTED,
                tooltip="Delete conversation",
                on_click=lambda e: enter_confirm_state(),
                style=ft.ButtonStyle(padding=0),
            )

        # Always visible, not hover-only -- hover-based reveal depends on
        # this Flet environment's mouse-hover events firing reliably,
        # which this project has previously found inconsistent (see the
        # desktop UI v2 rendering notes). An always-visible trash icon is
        # simpler and impossible to miss, at the cost of a little visual
        # noise -- an acceptable trade for a "reliable, explicit" delete
        # control.
        show_delete_icon()

        def enter_confirm_state() -> None:
            row_state["confirming"] = True
            action_area.content = ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.CHECK_ROUNDED,
                        icon_size=16,
                        icon_color="#E06666",
                        tooltip="Confirm delete",
                        on_click=lambda e: do_delete(),
                        style=ft.ButtonStyle(padding=0),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE_ROUNDED,
                        icon_size=16,
                        icon_color=COLOR_SIDEBAR_TEXT_MUTED,
                        tooltip="Cancel",
                        on_click=lambda e: cancel_confirm(),
                        style=ft.ButtonStyle(padding=0),
                    ),
                ],
                spacing=0,
                tight=True,
            )
            page.update()

        def cancel_confirm() -> None:
            row_state["confirming"] = False
            show_delete_icon()
            page.update()

        def do_delete() -> None:
            if state["is_sending"] and state["conversation_id"] == conv.id:
                return

            deleted = chat_db.delete_conversation(conv.id, state["user_id"])
            if not deleted:
                # Ownership check failed (or the conversation is already
                # gone) -- deliberately generic, doesn't reveal whether the
                # conversation exists under a different account. Shouldn't
                # be reachable from this UI (the sidebar only ever lists
                # the logged-in user's own conversations), but
                # delete_conversation() enforces it regardless of caller.
                # Shown as inline text in the row's own action area (reuses
                # only Text/Container, same as the rest of this sidebar --
                # avoids ft.SnackBar/dialog widgets, which this project has
                # previously found unreliable to render in this Flet
                # environment). Clears itself the next time the row stops
                # being hovered (see on_hover below).
                row_state["confirming"] = False
                # COLOR_LOGIN_ERROR (not COLOR_ERROR_TEXT), since this sits
                # on the dark sidebar background, not a light chat bubble.
                action_area.content = ft.Text(
                    "Error", size=FONT_SIZE_SIDEBAR_TIME, color=COLOR_LOGIN_ERROR, font_family=FONT_FAMILY
                )
                page.update()
                return  # left showing "Error" instead of the trash icon; re-rendering the sidebar (e.g. clicking another chat) restores it

            if state["conversation_id"] == conv.id:
                state["conversation_id"] = None
                chat_list.controls.clear()
                empty_state.visible = True
            render_sidebar()

        def on_hover(e: ft.ControlEvent) -> None:
            hovering = e.data == "true"
            container.bgcolor = (
                COLOR_SIDEBAR_ROW_ACTIVE_BG
                if is_active
                else (COLOR_SIDEBAR_ROW_HOVER_BG if hovering else "transparent")
            )
            title_ctrl.color = COLOR_SIDEBAR_TEXT_HOVER if hovering or is_active else COLOR_SIDEBAR_TEXT
            page.update()

        async def on_click(e: ft.ControlEvent) -> None:
            await load_conversation(conv.id)

        container = ft.Container(
            content=ft.Row(
                [
                    ft.Column([title_ctrl, time_ctrl], spacing=1, expand=True, tight=True),
                    action_area,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            bgcolor=COLOR_SIDEBAR_ROW_ACTIVE_BG if is_active else "transparent",
            border=ft.Border(left=ft.BorderSide(3, COLOR_ACCENT if is_active else "transparent")),
            border_radius=8,
            padding=pad_symmetric(horizontal=10, vertical=8),
            margin=pad_symmetric(horizontal=8, vertical=1),
            on_hover=on_hover,
            on_click=on_click,
            ink=True,
        )
        return container

    async def load_conversation(conversation_id: int) -> None:
        if state["is_sending"]:
            return
        state["conversation_id"] = conversation_id
        chat_list.controls.clear()
        messages = chat_db.get_messages(conversation_id)
        empty_state.visible = len(messages) == 0
        for m in messages:
            row = build_bubble(
                ChatMessage(m.role, m.content, chat_db.relative_time(m.created_at)),
                bubble_width_hint(),
            )
            animated = row.data
            animated.opacity = 1
            animated.offset = ft.Offset(0, 0)
            chat_list.controls.append(row)
        render_sidebar()
        page.update()

    def start_new_chat(e: ft.ControlEvent | None = None) -> None:
        if state["is_sending"]:
            return
        state["conversation_id"] = None
        chat_list.controls.clear()
        empty_state.visible = True
        message_input.value = ""
        send_button.disabled = True
        render_sidebar()
        page.update()

    new_chat_button.on_click = start_new_chat

    async def send_message(e: ft.ControlEvent) -> None:
        text = message_input.value.strip() if message_input.value else ""
        if not text or state["is_sending"]:
            return

        # Disable input synchronously, before any `await`, so a fast
        # double-Enter/double-click can't invoke send_message a second time
        # while the first call is still between awaits (that race used to
        # cause "list.remove(x): x not in list" when two calls fought over
        # the same loading-bubble control).
        state["is_sending"] = True
        message_input.disabled = True
        send_button.disabled = True
        page.update()

        try:
            is_first_message = state["conversation_id"] is None
            if is_first_message:
                state["conversation_id"] = chat_db.create_conversation(state["user_id"])
                chat_db.set_conversation_title(state["conversation_id"], chat_db.make_title(text))

            conversation_id = state["conversation_id"]
            chat_db.add_message(conversation_id, "user", text)

            empty_state.visible = False
            await add_bubble_animated(ChatMessage("user", text, timestamp_now()))

            message_input.value = ""
            page.update()
            render_sidebar()
            await _send_and_await_reply(conversation_id, text)
        finally:
            state["is_sending"] = False

    async def _send_and_await_reply(conversation_id: int, text: str) -> None:
        loading_row = build_loading_row()
        chat_list.controls.append(loading_row)
        page.update()

        structured_tool: str | None = None
        structured_data: dict | None = None
        try:
            answer, structured_tool, structured_data = await asyncio.to_thread(
                run_ai_orchestrator_with_data, text
            )
            role = "ai"
        except Exception as exc:  # last-resort UI-side guard; orchestrator never raises
            answer = f"Unexpected error: {exc}"
            role = "error"

        # chat_db has no column for structured_data (see ChatMessage's
        # docstring) -- only the text answer is persisted, same as before.
        chat_db.add_message(conversation_id, role, answer)

        # The user may have switched conversations (sidebar click / New Chat)
        # while the request above was in flight, which clears chat_list.
        # The reply is already saved to the DB either way -- only touch
        # chat_list/loading_row if this conversation is still the one being
        # viewed; re-enable the (conversation-independent) input regardless.
        if state["conversation_id"] == conversation_id:
            if loading_row in chat_list.controls:
                chat_list.controls.remove(loading_row)
            await add_bubble_animated(
                ChatMessage(role, answer, timestamp_now(), structured_tool, structured_data)
            )

        message_input.disabled = False
        page.update()
        render_sidebar()
        await message_input.focus()

    async def export_report(e: ft.ControlEvent) -> None:
        # Shares the same is_sending guard as send_message/load_conversation/
        # start_new_chat so a report export can't race with an in-flight
        # chat send (same race class documented in _send_and_await_reply).
        if state["is_sending"]:
            return
        state["is_sending"] = True
        export_report_button.disabled = True
        page.update()

        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(Path(tempfile.gettempdir()) / f"manufacturing_report_{timestamp}.pdf")
            # generate_manufacturing_report does blocking COM/PDF work
            # (screenshot capture, cost/carbon/DFM calls) -- run off the
            # UI thread, same reasoning as run_ai_orchestrator below.
            saved_path = await asyncio.to_thread(generate_manufacturing_report, output_path)
            text = f"Manufacturing readiness report exported to:\n{saved_path}"
            role = "ai"
        except Exception as exc:
            text = f"Could not generate the report: {exc}"
            role = "error"

        empty_state.visible = False
        await add_bubble_animated(ChatMessage(role, text, timestamp_now()))
        page.update()

        export_report_button.disabled = False
        state["is_sending"] = False
        page.update()

    export_report_button.on_click = export_report

    async def export_bom_action(e: ft.ControlEvent) -> None:
        # Same is_sending guard/race-avoidance reasoning as export_report.
        if state["is_sending"]:
            return
        state["is_sending"] = True
        export_bom_button.disabled = True
        page.update()

        def _build_bom_file() -> str:
            # Read live off mcp_server.adapter (not a name captured at
            # import time) so a mid-session platform switch via
            # mcp_server.set_backend() is reflected immediately.
            if not mcp_server.adapter.is_assembly():
                raise RuntimeError(
                    "The currently open document is not an assembly -- "
                    "open an assembly to export a BOM."
                )
            bom_result = mcp_server.adapter.get_assembly_bom(
                _DEFAULT_BOM_MANUFACTURING_PROCESS, _DEFAULT_BOM_QUANTITY
            )
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(Path(tempfile.gettempdir()) / f"bom_{timestamp}.xlsx")
            return write_bom_file(bom_result, output_path, False)

        try:
            # Blocking COM + file I/O work -- run off the UI thread, same
            # reasoning as export_report/run_ai_orchestrator.
            saved_path = await asyncio.to_thread(_build_bom_file)
            text = (
                f"BOM exported to:\n{saved_path}\n\n"
                f"(Flat BOM, manufacturing process "
                f"'{_DEFAULT_BOM_MANUFACTURING_PROCESS}' and quantity "
                f"{_DEFAULT_BOM_QUANTITY} -- defaults, not specified via "
                "chat. Ask in chat for a different process/quantity/"
                "indented BOM.)"
            )
            role = "ai"
        except Exception as exc:
            text = f"Could not export the BOM: {exc}"
            role = "error"

        empty_state.visible = False
        await add_bubble_animated(ChatMessage(role, text, timestamp_now()))
        page.update()

        export_bom_button.disabled = False
        state["is_sending"] = False
        page.update()

    export_bom_button.on_click = export_bom_action

    send_button.on_click = send_message
    message_input.on_submit = send_message

    def do_logout(e: ft.ControlEvent | None = None) -> None:
        # Same is_sending guard as start_new_chat/load_conversation --
        # don't tear the view down while a send is still in flight.
        if state["is_sending"]:
            return
        on_logout()

    async def handle_switch_platform(e: ft.ControlEvent | None = None) -> None:
        # Same is_sending guard as do_logout/start_new_chat -- switching
        # backends mid-send would race the in-flight tool call.
        if state["is_sending"]:
            return
        detected = await detect_platforms()
        new_platform = await show_platform_selector(page, state["user_id"], detected)
        state["platform"] = new_platform
        update_platform_badge()
        await show_toast(f"Switched to {PLATFORM_LABELS[new_platform]}")

    user_footer = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.CircleAvatar(
                            content=ft.Text(username[:1].upper(), size=12, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                            bgcolor=COLOR_ACCENT,
                            radius=AVATAR_RADIUS,
                        ),
                        ft.Text(
                            username,
                            color=COLOR_SIDEBAR_TEXT,
                            size=FONT_SIZE_SIDEBAR_TITLE,
                            weight=ft.FontWeight.W_600,
                            font_family=FONT_FAMILY,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True,
                        ),
                    ],
                    spacing=8,
                ),
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.SYNC_ALT_ROUNDED, size=14, color=COLOR_SIDEBAR_TEXT_MUTED),
                            ft.Text("Switch CAD Software", color=COLOR_SIDEBAR_TEXT_MUTED, size=12, font_family=FONT_FAMILY),
                        ],
                        spacing=6,
                    ),
                    on_click=handle_switch_platform,
                    ink=True,
                    border_radius=6,
                    padding=pad_symmetric(horizontal=4, vertical=6),
                ),
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.LOGOUT_ROUNDED, size=14, color=COLOR_SIDEBAR_TEXT_MUTED),
                            ft.Text("Log Out", color=COLOR_SIDEBAR_TEXT_MUTED, size=12, font_family=FONT_FAMILY),
                        ],
                        spacing=6,
                    ),
                    on_click=do_logout,
                    ink=True,
                    border_radius=6,
                    padding=pad_symmetric(horizontal=4, vertical=6),
                ),
            ],
            spacing=8,
        ),
        padding=pad_symmetric(horizontal=16, vertical=12),
        border=ft.Border(top=ft.BorderSide(1, COLOR_SIDEBAR_DIVIDER)),
    )

    # ---- layout ----

    platform_badge_text = ft.Text("", size=12, weight=ft.FontWeight.W_600, color=COLOR_HEADER_TEXT, font_family=FONT_FAMILY)
    platform_badge = ft.Container(
        content=ft.Row(
            [
                ft.Container(width=8, height=8, border_radius=4, bgcolor=COLOR_STATUS_RUNNING),
                platform_badge_text,
            ],
            spacing=6,
        ),
        bgcolor="#22314A",
        border_radius=20,
        padding=pad_symmetric(horizontal=12, vertical=6),
        visible=False,
    )

    header = ft.Container(
        content=ft.Row(
            [
                ft.Column(
                    [
                        ft.Text(
                            APP_TITLE,
                            color=COLOR_HEADER_TEXT,
                            size=FONT_SIZE_TITLE,
                            weight=ft.FontWeight.BOLD,
                            font_family=FONT_FAMILY,
                        ),
                        ft.Text(
                            APP_SUBTITLE,
                            color=COLOR_HEADER_SUBTEXT,
                            size=FONT_SIZE_SUBTITLE,
                            font_family=FONT_FAMILY,
                        ),
                    ],
                    spacing=4,
                    expand=True,
                ),
                platform_badge,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=COLOR_HEADER_BG,
        padding=pad_symmetric(horizontal=28, vertical=22),
    )

    input_bar = ft.Container(
        content=ft.Row([message_input, export_report_button, export_bom_button, send_button], spacing=10),
        bgcolor=COLOR_INPUT_BAR_BG,
        padding=pad_all(16),
        border=ft.Border(top=ft.BorderSide(1, COLOR_INPUT_BORDER)),
    )

    sidebar = ft.Container(
        content=ft.Column(
            [
                new_chat_button,
                ft.Container(
                    content=ft.Divider(height=1, color=COLOR_SIDEBAR_DIVIDER),
                    padding=pad_symmetric(horizontal=12),
                ),
                sidebar_list,
                # Trailing fixed-height child after an expand=True child --
                # this exact Column shape had unreliable rendering in an
                # earlier session in this Flet version/environment (see the
                # desktop UI v2 history), though it was never conclusively
                # confirmed as a real bug vs. a screenshot-capture artifact.
                # Left as the idiomatic layout; if the username/Log Out row
                # doesn't appear, try resizing the window (forces a
                # relayout) before assuming this code is wrong.
                user_footer,
            ],
            expand=True,
            spacing=0,
        ),
        bgcolor=COLOR_SIDEBAR_BG,
        width=SIDEBAR_WIDTH,
    )

    main_column = ft.Column(
        [
            header,
            status_banner,
            ft.Container(content=chat_area, expand=True, bgcolor=COLOR_CHAT_BG),
            input_bar,
        ],
        expand=True,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    page.add(
        ft.Row(
            [sidebar, ft.Container(content=main_column, expand=True)],
            expand=True,
            spacing=0,
        )
    )
    page.update()

    # ---- CAD platform selection ----

    message_input.disabled = True
    send_button.disabled = True
    page.update()

    detected = await detect_platforms()
    running_platforms = [p for p, ok in detected.items() if ok]

    chosen_platform = None
    if len(running_platforms) == 1:
        candidate = running_platforms[0]
        success, _error = await asyncio.to_thread(_try_connect_platform, candidate)
        if success:
            _activate_platform(candidate, state["user_id"])
            chosen_platform = candidate
        else:
            # Detected as running but a real connect() still failed (e.g.
            # SolidWorks is open but has no document loaded yet) -- fall
            # through to the popup instead of leaving the user stuck with a
            # permanently-disabled chat and no explanation. The popup's own
            # Connect button will re-surface this same error.
            detected[candidate] = False

    if chosen_platform is None:
        chosen_platform = await show_platform_selector(page, state["user_id"], detected)

    state["platform"] = chosen_platform
    update_platform_badge()
    if len(running_platforms) == 1 and chosen_platform in running_platforms:
        await show_toast(f"Connected to {PLATFORM_LABELS[chosen_platform]}")

    # ---- startup environment check ----

    render_sidebar()

    problem = await check_environment()
    if problem:
        status_banner.content = ft.Row(
            [
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=COLOR_ERROR_TEXT, size=18),
                ft.Text(
                    problem,
                    color=COLOR_ERROR_TEXT,
                    size=FONT_SIZE_MESSAGE,
                    font_family=FONT_FAMILY,
                    expand=True,
                ),
            ],
            spacing=8,
        )
        status_banner.bgcolor = COLOR_ERROR_BUBBLE
        status_banner.padding = pad_symmetric(horizontal=20, vertical=10)
        status_banner.visible = True
    else:
        message_input.disabled = False

    page.update()
    await message_input.focus()


async def main(page: ft.Page) -> None:
    page.title = APP_TITLE
    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    page.window.min_width = 760
    page.window.min_height = 520
    page.padding = 0
    page.theme = ft.Theme(font_family=FONT_FAMILY)

    chat_db.init_db()

    def show_login() -> None:
        page.bgcolor = COLOR_LOGIN_PAGE_BG
        page.controls.clear()
        page.add(build_login_view(page, on_authenticated))
        page.update()

    def on_authenticated(user_id: int, username: str) -> None:
        page.run_task(show_chat_interface, page, user_id, username, show_login)

    show_login()


if __name__ == "__main__":
    ft.run(main)
