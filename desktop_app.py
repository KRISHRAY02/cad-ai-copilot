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
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import flet as ft
from ollama import AsyncClient

import chat_db
from app_paths import bundled_path, exports_dir, user_data_dir
import mcp_server
from ai_orchestrator import (
    DEFAULT_MODEL,
    AiOrchestrator,
    McpServerUnavailableError,
    looks_like_am_material_recommendation_request,
    run_ai_orchestrator_with_data,
    _describe_orientation_plain,
    _SUPPORT_STRATEGY_PLAIN_LANGUAGE,
)
from bom_export import export_bom as write_bom_file
from cad_adapters.fusion_adapter import FusionAdapter
from cad_adapters.solidworks_adapter import SolidWorksAdapter
from production_cost import (
    CNC_LABOR_HOURLY_RATE_INR,
    CNC_MACHINE_HOURLY_RATE_INR,
    IM_MACHINE_HOURLY_RATE_INR,
    IM_TOOLING_COST_INR,
    PROCESSES,
    SM_COST_PER_BEND_INR,
    SM_CUTTING_RATE_PER_MM_INR,
)
from report.generate_report import generate_manufacturing_report
from ui.components.cost_card import build_cost_card as new_build_cost_card

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
    # This message's chat_db row id -- only needed by the am_material_question
    # card (see build_bubble), which has to call back with the specific
    # message to lock/update when its form is submitted. None for every
    # other message type, which never needs to address itself by id.
    id: int | None = None


EXAMPLE_QUESTIONS = [
    "What's the mass of this part?",
    "What material is this made of?",
    "Estimate the cost for a batch of 50",
    "Run a DFM check on this part",
]


# --------------------------------------------------------------------------
# Startup environment check
# --------------------------------------------------------------------------


# Bundled read-only files the running app actually needs -- see
# app_paths.bundled_path's docstring for why these are resolved relative
# to the bundle (sys._MEIPASS) rather than the current working directory
# in a packaged .exe. Checked by name here rather than imported from each
# consumer module so a missing file is reported once, up front, in plain
# language, instead of surfacing later as a confusing per-tool error.
_REQUIRED_DATA_FILES = (
    "materials.csv",
    "materials_fusion.csv",
    "am_materials.csv",
    "standard_hardware.csv",
    "cost_model/trained_model.joblib",
)


@dataclass
class SelfCheckResult:
    label: str
    ok: bool
    detail: str


async def run_startup_self_check() -> list[SelfCheckResult]:
    """Run every startup check and return one result per check, in order.

    Each check is independent (a failure in one doesn't stop the rest from
    running) so the banner built from this can report everything wrong at
    once instead of just the first failure -- e.g. "Ollama isn't running"
    AND "materials.csv is missing" both get surfaced on the same launch,
    rather than needing two restarts to discover both.
    """
    results: list[SelfCheckResult] = []

    # 1. Bundled data files present (see app_paths.bundled_path).
    missing = [name for name in _REQUIRED_DATA_FILES if not bundled_path(name).is_file()]
    if missing:
        results.append(SelfCheckResult(
            "Data files",
            False,
            "Missing: " + ", ".join(missing) + ". Reinstall the app, or if "
            "running from source, run the setup scripts documented in "
            "PACKAGING_NOTES.md to regenerate them.",
        ))
    else:
        results.append(SelfCheckResult("Data files", True, "All required data files found."))

    # 2. Per-user writable folder (chat history, logs, exports).
    try:
        data_dir = user_data_dir()
        probe = data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(SelfCheckResult(
            "User data folder", True, f"Writable: {data_dir}",
        ))
    except OSError as e:
        results.append(SelfCheckResult(
            "User data folder",
            False,
            f"Can't write to {user_data_dir()}: {e}. Check folder "
            "permissions, or that the drive isn't full/read-only.",
        ))

    # 3 & 4. Ollama reachable, and the model the app actually uses is
    # pulled. Done before starting the MCP server since both are quick,
    # independent network/process checks.
    model_names: list[str] = []
    try:
        ollama_client = AsyncClient()
        response = await ollama_client.list()
        model_names = [m.model for m in response.models]
        results.append(SelfCheckResult("Ollama", True, "Ollama is reachable."))
    except Exception:
        results.append(SelfCheckResult(
            "Ollama",
            False,
            "Ollama isn't reachable. Install it from https://ollama.com, "
            "then run `ollama serve` and reopen this app.",
        ))

    if model_names:
        if any(DEFAULT_MODEL in name for name in model_names):
            results.append(SelfCheckResult(
                "AI model", True, f"{DEFAULT_MODEL} is pulled and ready.",
            ))
        else:
            results.append(SelfCheckResult(
                "AI model",
                False,
                f"The {DEFAULT_MODEL} model isn't pulled. Run `ollama pull "
                f"{DEFAULT_MODEL}` and reopen this app.",
            ))
    else:
        results.append(SelfCheckResult(
            "AI model", False, "Can't check which models are pulled -- Ollama isn't reachable.",
        ))

    # 5. MCP server subprocess starts and completes its handshake. Reuses
    # AiOrchestrator.start() so this exercises the exact same launch path
    # (including the packaged-.exe "--mcp-server" re-launch, see
    # AiOrchestrator.__init__) that real chat questions will use.
    orchestrator = AiOrchestrator()
    try:
        await orchestrator.start()
        results.append(SelfCheckResult("CAD MCP server", True, "Started successfully."))
    except McpServerUnavailableError as e:
        results.append(SelfCheckResult(
            "CAD MCP server",
            False,
            "The CAD MCP server didn't start. If running from source, make "
            f"sure mcp_server.py runs without errors under this Python "
            f"interpreter. ({e})",
        ))
    finally:
        try:
            await orchestrator.stop()
        except Exception:
            pass

    return results


async def check_environment() -> str | None:
    """Return a friendly, multi-line problem description, or None if every
    startup check passed. Thin wrapper around run_startup_self_check() for
    callers that just want a single banner message.
    """
    results = await run_startup_self_check()
    problems = [r.detail for r in results if not r.ok]
    if not problems:
        return None
    return "\n".join(f"• {p}" for p in problems)


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
COLOR_WARNING_BG = "#FFF4E0"


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


# --------------------------------------------------------------------------
# Categorized view for list_assembly_components() -- same category-grouping/
# collapsible-section/warning-icon language as build_categorized_bom_view
# above, but for a plain structural listing with no cost/classification
# data at all (list_assembly_components takes no manufacturing_process, so
# its rows have no "classification"/"unit_cost_inr"/"total_cost_inr" the
# way get_assembly_bom's do) -- a dedicated table with a Material column
# instead of Unit/Total Cost, and no Make/Buy chip, reusing the same
# category-grouping helpers (_group_rows_by_category/categorize_component
# key only off part_name, so they work unchanged on either row shape).
# --------------------------------------------------------------------------


def _normalize_component_listing_row(component: dict) -> dict:
    """Reshape one dataclasses.asdict(AssemblyComponent) dict (quantity,
    mass_kg, material={"name":...}) into the field names the category-
    grouping helpers and build_component_listing_table below expect
    (quantity_per_assembly, unit_mass_kg/total_mass_kg, a flat material
    name string) -- purely a display-layer reshape, doesn't touch any
    adapter/calculation logic.
    """
    quantity = component.get("quantity", 0) or 0
    mass_kg = component.get("mass_kg")
    material = component.get("material") or {}
    material_name = material.get("name") if isinstance(material, dict) else None
    return {
        "part_name": component.get("part_name", "unnamed"),
        "quantity_per_assembly": quantity,
        "unit_mass_kg": mass_kg,
        "total_mass_kg": round(mass_kg * quantity, 4) if isinstance(mass_kg, (int, float)) else None,
        "material": material_name,
        "material_verified": component.get("material_verified"),
    }


def build_component_listing_table(rows: list[dict]) -> ft.Column:
    header = ft.Container(
        content=ft.Row(
            [
                ft.Text("Component", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=4),
                ft.Text("Qty", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=1),
                ft.Text("Mass Each", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=2),
                ft.Text("Total Mass", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=2),
                ft.Text("Material", color=COLOR_HEADER_TEXT, size=12, weight=ft.FontWeight.W_600, expand=4),
            ],
            spacing=8,
        ),
        bgcolor=COLOR_HEADER_BG,
        padding=pad_symmetric(horizontal=12, vertical=8),
        border_radius=CARD_TOP_RADIUS,
    )

    row_controls: list[ft.Control] = [header]
    for i, row in enumerate(rows):
        material_name = row.get("material") or "Not specified"
        material_cell_children = [
            ft.Text(
                material_name,
                size=13,
                color=COLOR_AI_TEXT,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
        ]
        if row.get("material_verified") is False:
            material_cell_children.append(build_material_warning_icon(material_name))

        row_controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Text(
                            row.get("part_name", "unnamed"),
                            size=13,
                            color=COLOR_AI_TEXT,
                            weight=ft.FontWeight.W_500,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=4,
                        ),
                        ft.Text(str(row.get("quantity_per_assembly", "?")), size=13, color=COLOR_AI_TEXT, expand=1),
                        ft.Text(_fmt_mass(row.get("unit_mass_kg")), size=13, color=COLOR_AI_TEXT, expand=2),
                        ft.Text(_fmt_mass(row.get("total_mass_kg")), size=13, color=COLOR_AI_TEXT, expand=2),
                        ft.Container(
                            ft.Row(material_cell_children, spacing=6, tight=True),
                            expand=4,
                        ),
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


def build_component_listing_category_section(category: str, rows: list[dict], collapsed_by_default: bool) -> ft.Control:
    """Same collapsible-header-plus-table pattern as build_category_section
    above, just with build_component_listing_table (no cost data) instead
    of build_bom_rows_table, and a mass-only subtotal in the header line.
    """
    subtotal = _category_subtotal(rows)  # total_cost_inr always None here -- simply unused below
    unverified_count = sum(1 for r in rows if r.get("material_verified") is False)

    summary_bits = [f"{subtotal['part_count']} type(s)", f"{subtotal['total_instances']} total instance(s)"]
    if subtotal["total_mass_kg"] is not None:
        summary_bits.append(_fmt_mass(subtotal["total_mass_kg"]))
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
    table_wrapper = ft.Container(content=build_component_listing_table(rows), visible=expanded)

    def _toggle(e: ft.ControlEvent) -> None:
        table_wrapper.visible = not table_wrapper.visible
        chevron.name = ft.Icons.EXPAND_MORE_ROUNDED if table_wrapper.visible else ft.Icons.CHEVRON_RIGHT_ROUNDED
        table_wrapper.update()
        chevron.update()

    header.on_click = _toggle
    return ft.Column([header, table_wrapper], spacing=0, tight=True)


def build_component_listing_view(data: dict) -> ft.Control:
    """list_assembly_components()'s main chat rendering -- same summary-
    first-plus-collapsible-categories structure as
    build_categorized_bom_view, adapted for a plain structural listing:
    unique parts / instances / sub-assembly count / total mass in the
    header (no cost, since this tool never computes any), Material shown
    per-row instead of cost, material_verified warnings unchanged.
    """
    components = data.get("components", [])
    rows = [_normalize_component_listing_row(c) for c in components]
    unverified_count = sum(1 for row in rows if row.get("material_verified") is False)
    total_mass_values = [r["total_mass_kg"] for r in rows if isinstance(r.get("total_mass_kg"), (int, float))]

    stats = ft.Row(
        [
            _stat("Unique parts", str(data.get("unique_part_count", len(rows)))),
            _stat("Instances", str(data.get("total_instance_count", "?"))),
            _stat("Sub-assemblies", str(data.get("sub_assembly_count", "?"))),
            _stat("Total mass", _fmt_mass(sum(total_mass_values)) if total_mass_values else "—"),
        ],
        spacing=22,
        wrap=True,
    )
    header_children = [stats]
    if unverified_count:
        header_children.append(
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
    summary_header = ft.Container(
        content=ft.Column(header_children, spacing=8),
        bgcolor=COLOR_CHAT_BG,
        padding=pad_symmetric(horizontal=14, vertical=12),
        border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
        border_radius=CARD_TOP_RADIUS,
    )

    groups = _group_rows_by_category(rows)
    sections = [
        build_component_listing_category_section(
            category, groups[category], category in _CATEGORIES_COLLAPSED_BY_DEFAULT
        )
        for category in CATEGORY_DISPLAY_ORDER
        if groups[category]
    ]

    return _card_container([summary_header, *sections])


def build_cost_breakdown_card(data: dict, on_what_if_result=None) -> ft.Control:
    """Thin adapter: renders estimate_cost()'s result via the redesigned
    ui.components.cost_card component (header chip + placeholder-rate
    badge, hero stat tiles, colour-blind-safe cost-split bar + legend,
    collapsible "Show calculation" detail table, ML-comparison strip,
    action row) instead of building the card's widget tree here -- see
    that module's docstring for the full visual spec and exactly which
    real field backs each element (it never shows a placeholder number).

    Kept this function's name/signature (`data`, `on_what_if_result`)
    unchanged so build_bubble's STRUCTURED_CARD_BUILDERS dispatch and its
    estimate_cost special-case (passing on_what_if_result through) don't
    need to change at all -- only the rendering underneath it moved.

    `on_what_if_result`, if given, is an async callback
    `(process, quantity, rate_overrides, new_data) -> None` -- see
    post_what_if_result in main(), which posts the recomputed scenario
    into the chat as a real message pair. Reuses the existing
    build_what_if_overlay popup unchanged; only the button that opens it
    now lives inside the new component.
    """
    def _open_what_if(e: ft.ControlEvent) -> None:
        overlay = build_what_if_overlay(e.page, data, on_what_if_result)
        e.page.overlay.append(overlay)
        e.page.update()

    card, _animate_in = new_build_cost_card(
        data,
        on_what_if=_open_what_if if on_what_if_result is not None else None,
    )
    return card


# key -> (field label, default value) per process, for the "What if..."
# dialog's rate-override fields -- see build_what_if_overlay. Keys match
# exactly what production_cost.py's process functions accept in their
# `rate_overrides` dict (see each function's docstring there).
_WHAT_IF_RATE_FIELDS = {
    "CNC Machining": [
        ("machine_hourly_rate_inr", "Machine rate (Rs/hr)", CNC_MACHINE_HOURLY_RATE_INR),
        ("labor_hourly_rate_inr", "Labor rate (Rs/hr)", CNC_LABOR_HOURLY_RATE_INR),
    ],
    "Injection Molding": [
        ("machine_hourly_rate_inr", "Machine rate (Rs/hr)", IM_MACHINE_HOURLY_RATE_INR),
        ("tooling_cost_total_inr", "Tooling cost, total (Rs)", IM_TOOLING_COST_INR),
    ],
    "Sheet Metal": [
        ("cutting_rate_per_mm_inr", "Cutting rate (Rs/mm)", SM_CUTTING_RATE_PER_MM_INR),
        ("cost_per_bend_inr", "Cost per bend (Rs)", SM_COST_PER_BEND_INR),
    ],
}


def build_what_if_overlay(page: ft.Page, original_data: dict, on_what_if_result) -> ft.Container:
    """"What if...?" popup for a cost breakdown card -- lets the user
    compare a different manufacturing process and/or override that
    process's PLACEHOLDER shop-rate constants (see
    _WHAT_IF_RATE_FIELDS/production_cost.py).

    The popup itself only collects inputs (process/quantity/rates) and
    validates them. The actual recompute-and-display happens in
    `on_what_if_result` (main()'s post_what_if_result) -- per Krish's
    request, the result shows up as a normal message pair in the chat
    window (and is saved to chat history like any other answer), not
    inside this popup, so this closes itself as soon as calculation
    succeeds rather than rendering a result card in place.

    Built as a plain page.overlay entry (Container scrim + centered
    card), the same pattern show_platform_selector uses, for the same
    reason documented there: this project has a history in this Flet
    environment of unreliable rendering for less battle-tested widgets
    (ft.AlertDialog, ft.Tabs). Sticks to Container/Row/Column/Text/
    TextField/RadioGroup -- all already proven elsewhere in this file --
    rather than ft.Dropdown/ft.ElevatedButton, which aren't used anywhere
    else here yet.
    """
    original_process = original_data.get("manufacturing_process") or PROCESSES[0]
    original_quantity = original_data.get("quantity", 1)

    process_radio_group = ft.RadioGroup(
        content=ft.Column(
            [ft.Radio(value=p, label=p) for p in PROCESSES],
            spacing=4,
        ),
        value=original_process,
    )
    quantity_field = ft.TextField(
        label="Quantity",
        value=str(original_quantity),
        content_padding=pad_symmetric(horizontal=12, vertical=8),
    )
    rate_field_1 = ft.TextField(content_padding=pad_symmetric(horizontal=12, vertical=8))
    rate_field_2 = ft.TextField(content_padding=pad_symmetric(horizontal=12, vertical=8))
    error_text = ft.Text("", size=12, color=COLOR_ERROR_TEXT)
    calculate_text = ft.Text("Calculate", size=13, weight=ft.FontWeight.W_600, color="#FFFFFF")

    def sync_rate_fields(process: str) -> None:
        fields = _WHAT_IF_RATE_FIELDS.get(process, [])
        for field, entry in zip((rate_field_1, rate_field_2), fields):
            _key, label, default_value = entry
            field.label = label
            field.value = str(default_value)
            field.visible = True
        for field in (rate_field_1, rate_field_2)[len(fields):]:
            field.visible = False

    sync_rate_fields(original_process)

    def on_process_change(e: ft.ControlEvent) -> None:
        sync_rate_fields(process_radio_group.value)
        error_text.value = ""
        page.update()

    process_radio_group.on_change = on_process_change

    async def on_calculate(e: ft.ControlEvent) -> None:
        error_text.value = ""
        try:
            quantity = int(quantity_field.value)
            if quantity < 1:
                raise ValueError
        except (TypeError, ValueError):
            error_text.value = "Quantity must be a whole number of 1 or more."
            page.update()
            return

        process = process_radio_group.value
        fields = _WHAT_IF_RATE_FIELDS.get(process, [])
        rate_overrides = {}
        for (key, label, _default), field in zip(fields, (rate_field_1, rate_field_2)):
            try:
                rate_overrides[key] = float(field.value)
            except (TypeError, ValueError):
                error_text.value = f"'{label}' must be a number."
                page.update()
                return

        calculate_btn.disabled = True
        calculate_text.value = "Calculating..."
        page.update()

        new_data = await asyncio.to_thread(
            mcp_server.estimate_cost,
            manufacturing_process=process,
            quantity=quantity,
            rate_overrides=rate_overrides,
        )

        if overlay in page.overlay:
            page.overlay.remove(overlay)
        page.update()

        await on_what_if_result(process, quantity, rate_overrides, new_data)

    def on_close(e: ft.ControlEvent) -> None:
        if overlay in page.overlay:
            page.overlay.remove(overlay)
        page.update()

    calculate_btn = ft.Container(
        content=calculate_text,
        bgcolor=COLOR_ACCENT,
        border_radius=8,
        padding=pad_symmetric(horizontal=16, vertical=10),
        alignment=ft.Alignment(0, 0),
        ink=True,
        on_click=on_calculate,
    )
    close_btn = ft.Container(
        content=ft.Text("Close", size=13, weight=ft.FontWeight.W_600, color=COLOR_TIMESTAMP),
        border=ft.Border.all(1, COLOR_SIDEBAR_DIVIDER),
        border_radius=8,
        padding=pad_symmetric(horizontal=16, vertical=10),
        alignment=ft.Alignment(0, 0),
        ink=True,
        on_click=on_close,
    )

    popup_card = ft.Container(
        content=ft.Column(
            [
                ft.Text("What if...?", size=18, weight=ft.FontWeight.BOLD, color=COLOR_AI_TEXT),
                ft.Text(
                    "Compare a different manufacturing process, or override its "
                    "placeholder shop rates, against the same part.",
                    size=12,
                    color=COLOR_TIMESTAMP,
                ),
                ft.Text("Manufacturing process", size=12, weight=ft.FontWeight.W_600, color=COLOR_AI_TEXT),
                process_radio_group,
                quantity_field,
                rate_field_1,
                rate_field_2,
                error_text,
                ft.Row([calculate_btn, close_btn], spacing=10),
            ],
            spacing=12,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        ),
        bgcolor=COLOR_AI_BUBBLE,
        border_radius=16,
        padding=pad_all(24),
        width=440,
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=24, color="#40000000", offset=ft.Offset(0, 8)),
    )

    overlay = ft.Container(content=popup_card, alignment=ft.Alignment(0, 0), expand=True, bgcolor="#B3000000")
    return overlay


# (value, label[, sublabel]) options for the AM-material-recommendation
# card's chip groups -- value is exactly what recommend_am_material()/
# _try_parse_am_material_answers expects (case-insensitive), label/
# sublabel are the plain-language text shown to the user. Kept here (not
# imported from am_material_advisor/ai_orchestrator) since these are
# UI-facing strings, same separation as _WHAT_IF_RATE_FIELDS above.
_AM_CARD_LOAD_BEARING_OPTIONS = [("Yes", "Yes"), ("No", "No")]
_AM_CARD_TEMPERATURE_OPTIONS = [
    ("Low", "Low", "room temperature, indoor use"),
    ("Moderate", "Moderate", "near a motor, enclosed outdoor housing"),
    ("High", "High", "near an engine or heat source, sustained hot environment"),
]
_AM_CARD_PRIORITY_OPTIONS = [
    ("Strength", "Strength", ft.Icons.FITNESS_CENTER),
    ("Cost", "Cost", ft.Icons.PAYMENTS_OUTLINED),
    ("Surface finish", "Surface finish", ft.Icons.AUTO_AWESOME_OUTLINED),
]
_AM_CARD_PROCESS_OPTIONS = [
    ("FDM", "FDM / FFF"),
    ("SLA", "SLA"),
    ("SLS", "SLS"),
    ("MJF", "MJF"),
    ("DMLS", "DMLS"),
    ("", "No preference"),
]
_AM_CARD_PROCESS_LABELS = dict(_AM_CARD_PROCESS_OPTIONS)

AM_MATERIAL_QUESTION_CARD_MAX_WIDTH = 560


def _build_am_material_structured_text(answers: dict) -> str:
    """Build the exact labeled text block
    ai_orchestrator._try_parse_am_material_answers expects, from the chip
    selections an answered am_material_question card stores. Every value
    stored in `answers` already comes from the fixed option lists above,
    so this can never produce a shape the parser fails to recognize --
    unlike free-text extraction, there is no "did the model get this
    right" step at all.
    """
    lines = [
        f"Part function: {answers.get('part_function', '')}",
        f"Load bearing: {answers.get('load_bearing', '')}",
        f"Temperature exposure: {answers.get('temperature_exposure', '')}",
        f"Priority: {answers.get('priority', '')}",
    ]
    if answers.get("manufacturing_process"):
        lines.append(f"Manufacturing process: {answers['manufacturing_process']}")
    return "\n".join(lines)


def _am_chip(
    label: str,
    selected: bool,
    sublabel: str | None = None,
    icon=None,
    on_click=None,
    interactive: bool = True,
) -> ft.Container:
    """One tappable (or, if `interactive` is False, read-only) chip for the
    am_material_question card. Selected = filled accent + white text;
    unselected = white with an accent outline; locked/read-only (answered
    or skipped card) = flat grey, no border, with a small check icon
    instead of a fill color, so a reopened conversation visibly reads as
    "this was already decided" rather than "pick again".

    No max_lines/ellipsis anywhere -- the sublabel (temperature chips) is
    free to wrap onto a second/third line rather than being cut off, per
    Krish's explicit requirement that nothing in this card ever clips.
    """
    if not interactive:
        bgcolor, border, text_color, sub_color = COLOR_CHAT_BG, ft.Border.all(1, COLOR_AI_BORDER), COLOR_TIMESTAMP, COLOR_TIMESTAMP
        prefix = ft.Icon(ft.Icons.CHECK_CIRCLE, size=14, color=COLOR_TIMESTAMP) if selected else None
    elif selected:
        bgcolor, border, text_color, sub_color = COLOR_ACCENT, None, "#FFFFFF", "#FFFFFF"
        prefix = None
    else:
        bgcolor, border, text_color, sub_color = "#FFFFFF", ft.Border.all(1, COLOR_ACCENT), COLOR_AI_TEXT, COLOR_TIMESTAMP
        prefix = None

    if icon is not None:
        prefix = ft.Icon(icon, size=16, color=text_color)

    label_children = [ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=text_color)]
    if sublabel:
        label_children.append(ft.Text(sublabel, size=11, color=sub_color))

    row_children = []
    if prefix is not None:
        row_children.append(prefix)
    row_children.append(ft.Column(label_children, spacing=2, tight=True))

    return ft.Container(
        content=ft.Row(row_children, spacing=8, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=bgcolor,
        border=border,
        border_radius=12,
        padding=pad_symmetric(horizontal=14, vertical=11),
        ink=interactive,
        on_click=on_click if interactive else None,
    )


def _am_locked_answers_view(answers: dict | None) -> ft.Control:
    """Read-only chip summary for an answered am_material_question card --
    shown instead of the interactive form once locked (submitted now, or
    reloaded from a past conversation). `answers` is None only for the
    "skipped" status (never answered before the conversation was left).
    """
    if not answers:
        return ft.Container(
            content=ft.Row(
                [ft.Icon(ft.Icons.REMOVE_CIRCLE_OUTLINE, size=16, color=COLOR_TIMESTAMP),
                 ft.Text("Skipped -- this question was not answered.", size=12, color=COLOR_TIMESTAMP)],
                spacing=6,
            ),
        )

    chips = [
        _am_chip(f"Load-bearing: {answers.get('load_bearing', '?')}", True, interactive=False),
        _am_chip(f"Temperature: {answers.get('temperature_exposure', '?')}", True, interactive=False),
        _am_chip(f"Priority: {answers.get('priority', '?')}", True, interactive=False),
        _am_chip(
            f"Process: {_AM_CARD_PROCESS_LABELS.get(answers.get('manufacturing_process', ''), 'No preference') or 'No preference'}",
            True, interactive=False,
        ),
    ]
    return ft.Column(
        [
            ft.Text(answers.get("part_function", ""), size=13, color=COLOR_AI_TEXT),
            ft.Row(chips, wrap=True, spacing=8, run_spacing=8),
        ],
        spacing=10,
    )


def build_am_material_question_card(
    data: dict,
    live: bool,
    on_submit=None,
    card_width: int = AM_MATERIAL_QUESTION_CARD_MAX_WIDTH,
    page: ft.Page | None = None,
) -> ft.Container:
    """Inline chat-list card asking recommend_am_material()'s four required
    questions (plus the optional manufacturing_process filter), replacing
    the page.overlay popup this previously used (build_am_material_form_overlay,
    removed 2026-10-28 per Krish's request) -- the card now scrolls with
    the conversation and persists in chat_db like a normal message instead
    of floating above it.

    `data` is this message's structured_data: {"status": "pending" |
    "answered" | "skipped", "answers": dict | None}. `live` is True only
    for a card just posted in THIS running session that hasn't been
    answered yet -- the single case where the interactive form (rather
    than a locked read-only view) is shown. A reloaded past conversation
    always passes live=False, so a never-answered card from history reads
    as "Skipped" rather than inviting the user to answer a stale question
    out of order (per Krish's explicit requirement).

    `on_submit(answers: dict)` is called once, after the card has already
    locked itself (so a second click is structurally impossible -- no
    button exists anymore once this fires) -- see desktop_app.py's
    handle_am_material_submit, which persists the answer and runs the
    real recommend_am_material tool call.
    """
    status = data.get("status", "pending")
    answers = data.get("answers")
    interactive = live and status == "pending"

    header = ft.Row(
        [
            ft.Container(
                content=ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=18, color="#FFFFFF"),
                bgcolor=COLOR_ACCENT,
                border_radius=100,
                padding=8,
                width=34,
                height=34,
                alignment=ft.Alignment(0, 0),
            ),
            ft.Column(
                [
                    ft.Text(
                        "What material should this be printed in?",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=COLOR_AI_TEXT,
                    ),
                    ft.Text(
                        "Material depends on how the part is used, not its geometry. "
                        "Your answers keep the recommendation grounded in real "
                        "requirements.",
                        size=12,
                        color=COLOR_TIMESTAMP,
                    ),
                ],
                spacing=2,
                tight=True,
                expand=True,
            ),
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )

    # Fade-in is handled by the shared wrapper Container build_bubble/
    # add_bubble_animated already applies to every bubble (opacity 0 -> 1,
    # ANIM_DURATION_MS) -- this card doesn't need its own separate opacity
    # animation on top of that.
    card_container = ft.Container(
        bgcolor=COLOR_AI_BUBBLE,
        border=ft.Border.all(1, COLOR_AI_BORDER),
        border_radius=16,
        padding=pad_all(20),
        width=card_width,
        shadow=ft.BoxShadow(spread_radius=0, blur_radius=6, color="#14000000", offset=ft.Offset(0, 2)),
    )

    if not interactive:
        card_container.content = ft.Column(
            [header, _am_locked_answers_view(answers)], spacing=16, tight=True
        )
        return card_container

    # -- interactive branch below: only reached for a live, still-pending card --
    part_function_field = ft.TextField(
        label="What does this part do?",
        hint_text='e.g. "mounting bracket for a camera", "gasket", "enclosure lid"',
        multiline=True,
        min_lines=2,
        max_lines=4,
        content_padding=pad_symmetric(horizontal=12, vertical=8),
    )
    selection = {
        "load_bearing": None,
        "temperature_exposure": None,
        "priority": None,
        "manufacturing_process": "",  # optional -- "No preference" pre-selected
    }
    submitted = {"done": False}

    load_bearing_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
    temperature_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
    priority_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
    process_row = ft.Row(wrap=True, spacing=8, run_spacing=8)

    submit_text = ft.Text("Get recommendation", size=14, weight=ft.FontWeight.W_600, color="#FFFFFF")
    submit_btn = ft.Container(
        content=submit_text,
        height=44,
        border_radius=10,
        alignment=ft.Alignment(0, 0),
        expand=True,
    )

    def update_submit_state() -> None:
        ready = (
            selection["load_bearing"] is not None
            and selection["temperature_exposure"] is not None
            and selection["priority"] is not None
        )
        if ready:
            submit_btn.bgcolor = COLOR_ACCENT
            submit_btn.ink = True
            submit_btn.on_click = on_submit_click
        else:
            submit_btn.bgcolor = COLOR_SIDEBAR_DIVIDER
            submit_btn.ink = False
            submit_btn.on_click = None

    def refresh_load_bearing() -> None:
        load_bearing_row.controls = [
            _am_chip(label, selection["load_bearing"] == value, on_click=lambda e, v=value: select("load_bearing", v))
            for value, label in _AM_CARD_LOAD_BEARING_OPTIONS
        ]

    def refresh_temperature() -> None:
        temperature_row.controls = [
            _am_chip(
                label, selection["temperature_exposure"] == value, sublabel,
                on_click=lambda e, v=value: select("temperature_exposure", v),
            )
            for value, label, sublabel in _AM_CARD_TEMPERATURE_OPTIONS
        ]

    def refresh_priority() -> None:
        priority_row.controls = [
            _am_chip(
                label, selection["priority"] == value, icon=icon,
                on_click=lambda e, v=value: select("priority", v),
            )
            for value, label, icon in _AM_CARD_PRIORITY_OPTIONS
        ]

    def refresh_process() -> None:
        process_row.controls = [
            _am_chip(
                label, selection["manufacturing_process"] == value,
                on_click=lambda e, v=value: select("manufacturing_process", v),
            )
            for value, label in _AM_CARD_PROCESS_OPTIONS
        ]

    def select(key: str, value) -> None:
        selection[key] = value
        {
            "load_bearing": refresh_load_bearing,
            "temperature_exposure": refresh_temperature,
            "priority": refresh_priority,
            "manufacturing_process": refresh_process,
        }[key]()
        update_submit_state()
        if page is not None:
            page.update()

    def on_part_function_change(e: ft.ControlEvent) -> None:
        update_submit_state()
        if page is not None:
            page.update()

    part_function_field.on_change = on_part_function_change

    async def on_submit_click(e: ft.ControlEvent) -> None:
        if submitted["done"]:
            return
        if not (
            selection["load_bearing"]
            and selection["temperature_exposure"]
            and selection["priority"]
        ):
            return
        submitted["done"] = True

        answers = {
            "part_function": (part_function_field.value or "").strip(),
            "load_bearing": selection["load_bearing"],
            "temperature_exposure": selection["temperature_exposure"],
            "priority": selection["priority"],
            "manufacturing_process": selection["manufacturing_process"],
        }

        # Lock the card in place -- swap this same Container's content to
        # the read-only view, so a second click is structurally impossible
        # (no button exists in the tree anymore) rather than merely
        # discouraged by the `submitted` guard above.
        card_container.content = ft.Column(
            [header, _am_locked_answers_view(answers)], spacing=16, tight=True
        )
        if page is not None:
            page.update()

        if on_submit is not None:
            await on_submit(answers)

    refresh_load_bearing()
    refresh_temperature()
    refresh_priority()
    refresh_process()
    update_submit_state()

    def section(title: str, control: ft.Control) -> ft.Column:
        return ft.Column(
            [ft.Text(title, size=12, weight=ft.FontWeight.W_600, color=COLOR_AI_TEXT), control],
            spacing=8,
        )

    card_container.content = ft.Column(
        [
            header,
            part_function_field,
            section("Load-bearing?", load_bearing_row),
            section("Temperature exposure", temperature_row),
            section("What matters most?", priority_row),
            section("Manufacturing process (optional)", process_row),
            submit_btn,
        ],
        spacing=16,
        tight=True,
    )
    return card_container


def build_am_material_recommendation_card(data: dict) -> ft.Control:
    """recommend_am_material()'s result as a card -- one short row per
    material (name, process, one-line reason) instead of the plain-text
    fallback repeating the same boilerplate clause for every material in
    one run-on, potentially clipped paragraph. Pure formatting change: the
    ranking/data come from `data` exactly as recommend_am_material()
    returned them -- nothing here re-sorts or re-filters.
    """
    header = _card_header("AM Material Recommendation")

    meta_parts = [
        f"Load-bearing: {'Yes' if data.get('load_bearing') else 'No'}",
        f"Temperature: {str(data.get('temperature_exposure', '?')).capitalize()}",
        f"Priority: {str(data.get('priority', '?')).replace('_', ' ').capitalize()}",
    ]
    if data.get("manufacturing_process"):
        meta_parts.append(f"Process: {data['manufacturing_process']}")
    meta = ft.Container(
        content=ft.Column(
            [
                ft.Text(data.get("part_function", ""), size=12, color=COLOR_AI_TEXT),
                ft.Text(" | ".join(meta_parts), size=11, color=COLOR_TIMESTAMP),
            ],
            spacing=2,
        ),
        bgcolor=COLOR_CHAT_BG,
        padding=pad_symmetric(horizontal=14, vertical=10),
        border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
    )

    rows = []
    for i, rec in enumerate(data.get("recommendations", []), start=1):
        rows.append(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            f"{i}. {rec['material']}  ({rec['process_type']})",
                            size=13, weight=ft.FontWeight.BOLD, color=COLOR_AI_TEXT,
                        ),
                        ft.Text(rec["reason"], size=12, color=COLOR_TIMESTAMP),
                    ],
                    spacing=2,
                ),
                bgcolor="#FFFFFF",
                padding=pad_symmetric(horizontal=14, vertical=10),
                border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
            )
        )

    footer = ft.Container(
        content=ft.Text(data.get("assumptions", ""), size=10, italic=True, color=COLOR_TIMESTAMP),
        bgcolor=COLOR_CHAT_BG,
        padding=pad_symmetric(horizontal=14, vertical=10),
        border_radius=CARD_BOTTOM_RADIUS,
    )

    return _card_container([header, meta, *rows, footer])


_SUPPORT_STRATEGY_CHIP_COLORS = {
    "minimal_or_no_support": (COLOR_CHIP_MAKE_BG, COLOR_CHIP_MAKE_TEXT),
    "tree_branch_supports": (COLOR_CHIP_BUY_BG, COLOR_CHIP_BUY_TEXT),
    "dense_grid_block_supports": (COLOR_CHIP_BUY_NOPRICE_BG, COLOR_CHIP_BUY_NOPRICE_TEXT),
}


def build_am_readiness_card(data: dict) -> ft.Control:
    """Beginner-friendly visual card for generate_am_readiness_guide() --
    same "real Flet widgets instead of a text bubble" treatment as
    build_categorized_bom_view/build_cost_breakdown_card above, plus the
    plain-language translation from ai_orchestrator's
    _describe_orientation_plain/_SUPPORT_STRATEGY_PLAIN_LANGUAGE (shared
    with that module's own text-fallback formatter so the two never say
    different things about the same result).

    Krish's explicit request after reviewing the raw-technical-fields
    version live: a beginner asking "help me set up this part for 3D
    printing" shouldn't see a raw orientation label like "rotated 90
    about Y" or an enum value like "dense_grid_block_supports" with no
    explanation of what it means or why it was picked.
    """
    best = data.get("best_orientation", {})
    support = data.get("support_strategy", {})
    orientation = best.get("orientation", "?")
    area_cm2 = (best.get("total_overhang_area_mm2") or 0) / 100
    region_count = best.get("overhang_region_count", "?")
    area_label = "area" if region_count == 1 else "areas"

    strategy_key = support.get("recommended_support_strategy", "")
    strategy_title, strategy_explanation = _SUPPORT_STRATEGY_PLAIN_LANGUAGE.get(
        strategy_key, (strategy_key or "?", support.get("reasoning", ""))
    )
    chip_bg, chip_text = _SUPPORT_STRATEGY_CHIP_COLORS.get(
        strategy_key, (COLOR_CHIP_BUY_BG, COLOR_CHIP_BUY_TEXT)
    )

    header = _card_header("3D-Print Readiness Guide")

    orientation_section = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.THREED_ROTATION, size=18, color=COLOR_ACCENT),
                        ft.Text(
                            "Best orientation",
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color=COLOR_TIMESTAMP,
                        ),
                    ],
                    spacing=6,
                ),
                ft.Text(
                    _describe_orientation_plain(orientation).capitalize() + ".",
                    size=14,
                    weight=ft.FontWeight.W_600,
                    color=COLOR_AI_TEXT,
                ),
                ft.Text(
                    f"Least overhang of the orientations compared -- about "
                    f"{area_cm2:.0f} cm² across {region_count} {area_label}, "
                    "so it needs the least support material and prints more "
                    "reliably.",
                    size=12,
                    color=COLOR_TIMESTAMP,
                ),
            ],
            spacing=4,
        ),
        padding=pad_symmetric(horizontal=14, vertical=12),
        border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
    )

    sections = [header, orientation_section]

    # Top 3 (or fewer, if fewer distinct orientations were compared) --
    # Krish's explicit request so a user can see how close the runner-up(s)
    # were, not just trust the single winner blindly.
    top_orientations = data.get("top_orientations") or []
    if len(top_orientations) > 1:
        rows = []
        for i, entry in enumerate(top_orientations, start=1):
            entry_region_count = entry.get("overhang_region_count", "?")
            entry_label = "area" if entry_region_count == 1 else "areas"
            rows.append(
                ft.Row(
                    [
                        ft.Text(f"{i}.", size=12, color=COLOR_TIMESTAMP, width=16),
                        ft.Text(
                            _describe_orientation_plain(entry.get("orientation", "?")).capitalize(),
                            size=12,
                            color=COLOR_AI_TEXT,
                            expand=True,
                        ),
                        ft.Text(
                            f"{entry.get('total_overhang_area_cm2', 0):.0f} cm² / "
                            f"{entry_region_count} {entry_label}",
                            size=11,
                            color=COLOR_TIMESTAMP,
                        ),
                    ],
                    spacing=8,
                )
            )
        sections.append(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Top orientations compared",
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color=COLOR_TIMESTAMP,
                        ),
                        *rows,
                    ],
                    spacing=6,
                ),
                padding=pad_symmetric(horizontal=14, vertical=12),
                border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
            )
        )

    support_section = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.CONSTRUCTION, size=18, color=COLOR_ACCENT),
                        ft.Text(
                            "Support strategy (estimated)",
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color=COLOR_TIMESTAMP,
                        ),
                    ],
                    spacing=6,
                ),
                ft.Container(
                    content=ft.Text(
                        strategy_title, size=12, weight=ft.FontWeight.W_600, color=chip_text
                    ),
                    bgcolor=chip_bg,
                    border_radius=10,
                    padding=pad_symmetric(horizontal=8, vertical=3),
                ),
                ft.Text(
                    strategy_explanation[:1].upper() + strategy_explanation[1:],
                    size=12,
                    color=COLOR_TIMESTAMP,
                ),
            ],
            spacing=6,
        ),
        padding=pad_symmetric(horizontal=14, vertical=12),
        border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
    )
    sections.append(support_section)

    # "Check before printing" -- real run_am_dfm_check() findings for the
    # best orientation, never fabricated. Amber to signal "read this
    # before you print", not red/error (these are advisory, not failures).
    warnings = data.get("printability_warnings") or []
    not_checked = data.get("not_checked") or []
    checks_run = data.get("checks_run") or []

    printability_children = [
        ft.Row(
            [
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=18, color=COLOR_WARNING_ICON),
                ft.Text(
                    "Check before printing",
                    size=12,
                    weight=ft.FontWeight.W_600,
                    color=COLOR_WARNING_TEXT,
                ),
            ],
            spacing=6,
        ),
    ]
    if warnings:
        for w in warnings:
            printability_children.append(
                ft.Text(
                    f"[{w.get('severity', 'warning')}] {w.get('check', '?').replace('_', ' ').title()}: "
                    f"{w.get('detail', '')}",
                    size=12,
                    color=COLOR_AI_TEXT,
                )
            )
    else:
        checks_label = ", ".join(c.replace("_", " ") for c in checks_run) if checks_run else "none"
        printability_children.append(
            ft.Text(
                f"No printability issues found by the checks we run ({checks_label}).",
                size=12,
                color=COLOR_AI_TEXT,
            )
        )
    if not_checked:
        for nc in not_checked:
            printability_children.append(
                ft.Text(
                    f"Not checked -- {nc.get('check', '?').replace('_', ' ').title()}: {nc.get('reason', '')}",
                    size=11,
                    color=COLOR_TIMESTAMP,
                )
            )
    sections.append(
        ft.Container(
            content=ft.Column(printability_children, spacing=6),
            bgcolor=COLOR_WARNING_BG,
            padding=pad_symmetric(horizontal=14, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
        )
    )

    if data.get("material_questions_pending"):
        questions = data.get("material_questions_to_ask", [])
        sections.append(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.HELP_OUTLINE, size=18, color=COLOR_ACCENT),
                                ft.Text(
                                    "One more step for a material recommendation",
                                    size=12,
                                    weight=ft.FontWeight.W_600,
                                    color=COLOR_TIMESTAMP,
                                ),
                            ],
                            spacing=6,
                        ),
                        *[
                            ft.Text(f"•  {q}", size=13, color=COLOR_AI_TEXT)
                            for q in questions
                        ],
                    ],
                    spacing=6,
                ),
                bgcolor=COLOR_CHAT_BG,
                padding=pad_symmetric(horizontal=14, vertical=12),
                border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
            )
        )
    else:
        rec = data.get("material_recommendation", {})
        recommendations = rec.get("recommendations", []) if isinstance(rec, dict) else []
        material_rows = []
        for m in recommendations:
            material_rows.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text(
                                f"{m.get('material', '?')} ({m.get('process_type', '?')})",
                                size=13,
                                weight=ft.FontWeight.W_600,
                                color=COLOR_AI_TEXT,
                            ),
                            ft.Text(m.get("reason", ""), size=12, color=COLOR_TIMESTAMP),
                        ],
                        spacing=2,
                    ),
                    padding=pad_symmetric(horizontal=14, vertical=10),
                    border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
                )
            )
        assumptions = rec.get("assumptions") if isinstance(rec, dict) else None
        footer_children = []
        if assumptions:
            footer_children.append(ft.Text(assumptions, size=11, color=COLOR_TIMESTAMP))
        sections.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.CATEGORY_OUTLINED, size=18, color=COLOR_ACCENT),
                        ft.Text(
                            "Recommended materials",
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color=COLOR_TIMESTAMP,
                        ),
                    ],
                    spacing=6,
                ),
                padding=pad_symmetric(horizontal=14, vertical=10),
                border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
            )
        )
        sections.extend(material_rows)
        if footer_children:
            sections.append(
                ft.Container(
                    content=ft.Column(footer_children, spacing=4),
                    bgcolor=COLOR_CHAT_BG,
                    padding=pad_symmetric(horizontal=14, vertical=10),
                    border=ft.Border(bottom=ft.BorderSide(1, COLOR_AI_BORDER)),
                )
            )

    # Fixed limits disclaimer, always the true last section of the card
    # regardless of which branch above ran -- Krish's explicit requirement
    # that every guide result carries this, not just the ones with a
    # material recommendation.
    sections.append(
        ft.Container(
            content=ft.Text(
                data.get("limits_footer", mcp_server.AM_GUIDE_LIMITS_FOOTER),
                size=10,
                italic=True,
                color=COLOR_TIMESTAMP,
            ),
            bgcolor=COLOR_CHAT_BG,
            padding=pad_symmetric(horizontal=14, vertical=10),
            border_radius=CARD_BOTTOM_RADIUS,
        )
    )

    return _card_container(sections)


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
    "list_assembly_components": build_component_listing_view,
    "generate_am_readiness_guide": build_am_readiness_card,
    "recommend_am_material": build_am_material_recommendation_card,
    # am_material_question's structured_data has no "found" key (it isn't
    # a tool result at all, just this card's own pending/answered/skipped
    # state) -- build_bubble's found-gated lookup below is bypassed for
    # this one tool name specifically, see the comment there.
    "am_material_question": build_am_material_question_card,
}


def build_bubble(
    message: ChatMessage,
    page_width_hint: int,
    on_what_if_result=None,
    on_am_material_submit=None,
    live: bool = False,
    page: ft.Page | None = None,
) -> ft.Container:
    is_user = message.role == "user"
    is_error = message.role == "error"

    card_builder = (
        STRUCTURED_CARD_BUILDERS.get(message.structured_tool)
        if message.role == "ai" and message.structured_data is not None
        else None
    )

    if card_builder is not None:
        # The card widget (build_bom_card/build_cost_breakdown_card/etc.)
        # already carries its own border/shadow/radius via _card_container,
        # so the wrapping wrapper here stays unstyled -- adding
        # COLOR_AI_BUBBLE's border/shadow on top would double it up. Just
        # the card, no prose repeating the same numbers below it (Krish's
        # explicit request -- the model's own text answer for these tools
        # only ever restates what the card already shows).
        # estimate_cost's card is the only one that needs on_what_if_result
        # (its "What if..." button posts a new scenario back into this same
        # chat -- see post_what_if_result); am_material_question needs
        # live/on_am_material_submit/a pixel width cap instead -- every
        # other builder still takes just the structured data.
        if message.structured_tool == "estimate_cost":
            bubble = card_builder(message.structured_data, on_what_if_result)
        elif message.structured_tool == "am_material_question":
            message_id = message.id

            async def _submit(answers: dict) -> None:
                if on_am_material_submit is not None:
                    await on_am_material_submit(message_id, answers)

            card_width = min(AM_MATERIAL_QUESTION_CARD_MAX_WIDTH, int(page_width_hint * 0.92))
            bubble = card_builder(message.structured_data, live, _submit, card_width, page)
        else:
            bubble = card_builder(message.structured_data)
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

    # Fusion-only mode picker (see ai_orchestrator.AiOrchestrator.ask's
    # fusion_mode parameter): which domain the chat should answer in,
    # "part" (general part/assembly questions) or "am" (additive
    # manufacturing / 3D printing). A radio button here, not a typed
    # answer, because run_ai_orchestrator_with_data() starts a brand-new
    # AiOrchestrator per message -- a conversational "ask once" question
    # could never actually be answered by the user's next message in that
    # one-shot design (this is exactly the bug Krish hit live 2026-10-05:
    # answering "part" just re-asked the same question forever). Only
    # shown for Fusion 360 (see update_fusion_mode_visibility(), called
    # wherever state["platform"] is set) -- SolidWorks/mock sessions never
    # see this row and always get fusion_mode=None (ignored for them).
    fusion_mode_radio_group = ft.RadioGroup(
        content=ft.Row(
            [
                ft.Radio(value="part", label="Part / Assembly"),
                ft.Radio(value="am", label="Additive Manufacturing (3D Printing)"),
            ],
            spacing=20,
        ),
        value="part",
    )
    fusion_mode_row = ft.Container(
        content=ft.Row(
            [
                ft.Text("Ask about:", size=12, color=COLOR_TIMESTAMP, weight=ft.FontWeight.W_600),
                fusion_mode_radio_group,
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=pad_symmetric(horizontal=16, vertical=6),
        bgcolor=COLOR_INPUT_BAR_BG,
        border=ft.Border(top=ft.BorderSide(1, COLOR_INPUT_BORDER)),
        visible=False,
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

    def update_fusion_mode_visibility() -> None:
        fusion_mode_row.visible = state.get("platform") == PLATFORM_FUSION
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

    async def add_bubble_animated(message: ChatMessage, live: bool = False) -> None:
        row = build_bubble(
            message,
            bubble_width_hint(),
            on_what_if_result=post_what_if_result,
            on_am_material_submit=handle_am_material_submit,
            live=live,
            page=page,
        )
        chat_list.controls.append(row)
        page.update()
        await asyncio.sleep(0.02)
        animated = row.data
        animated.opacity = 1
        animated.offset = ft.Offset(0, 0)
        page.update()

    async def post_what_if_result(
        process: str, quantity: int, rate_overrides: dict, new_data: dict
    ) -> None:
        """Callback for a cost card's "What if..." popup (see
        build_what_if_overlay) -- posts the scenario and its recomputed
        result into the chat as a normal user/AI message pair, saved to
        chat_db exactly like any other turn, instead of showing the
        result inside the popup. Per Krish's explicit request: the
        what-if answer should look and persist like a real chat answer,
        not a transient dialog.

        Guards against the user having switched conversations while the
        popup was open, same "DB write always happens, live UI update
        only if still viewing this conversation" pattern
        _send_and_await_reply uses.
        """
        conversation_id = state["conversation_id"]
        if conversation_id is None:
            return

        if rate_overrides:
            field_labels = {key: label for key, label, _default in _WHAT_IF_RATE_FIELDS.get(process, [])}
            overrides_text = ", ".join(
                f"{field_labels.get(key, key)} = {value:g}" for key, value in rate_overrides.items()
            )
            question_text = (
                f"What if I used {process} (quantity {quantity}) with {overrides_text}?"
            )
        else:
            question_text = f"What if I used {process} (quantity {quantity})?"

        chat_db.add_message(conversation_id, "user", question_text)
        if state["conversation_id"] == conversation_id:
            await add_bubble_animated(ChatMessage("user", question_text, timestamp_now()))

        if new_data.get("found"):
            answer_text = "Here's the what-if cost breakdown."
            structured_tool, structured_data = "estimate_cost", new_data
        else:
            answer_text = new_data.get("message", "Could not compute this scenario.")
            structured_tool, structured_data = None, None

        chat_db.add_message(conversation_id, "ai", answer_text, structured_tool, structured_data)
        if state["conversation_id"] == conversation_id:
            await add_bubble_animated(
                ChatMessage("ai", answer_text, timestamp_now(), structured_tool, structured_data)
            )
            render_sidebar()

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
                ChatMessage(
                    m.role, m.content, chat_db.relative_time(m.created_at),
                    m.structured_tool, m.structured_data, id=m.id,
                ),
                bubble_width_hint(),
                on_what_if_result=post_what_if_result,
                # live=False (default) -- a reloaded am_material_question
                # card always shows its locked view (answered chips, or
                # "Skipped" if it never was), never the interactive form.
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

    async def _send_text(text: str) -> None:
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

    async def _post_am_material_question(user_text: str) -> None:
        """Posts the user's question, then an am_material_question card as
        the assistant's reply -- an inline chat-list message (scrolls with
        the conversation, saved to chat_db) instead of the page.overlay
        popup this replaced. See build_am_material_question_card's
        docstring for the card itself; this just handles getting it into
        the chat and persisted.
        """
        if state["is_sending"]:
            return
        state["is_sending"] = True
        message_input.disabled = True
        send_button.disabled = True
        page.update()

        try:
            is_first_message = state["conversation_id"] is None
            if is_first_message:
                state["conversation_id"] = chat_db.create_conversation(state["user_id"])
                chat_db.set_conversation_title(state["conversation_id"], chat_db.make_title(user_text))

            conversation_id = state["conversation_id"]
            chat_db.add_message(conversation_id, "user", user_text)
            empty_state.visible = False
            await add_bubble_animated(ChatMessage("user", user_text, timestamp_now()))

            message_input.value = ""
            page.update()

            card_data = {"status": "pending", "answers": None}
            card_content = "What material should this be printed in?"
            message_id = chat_db.add_message(
                conversation_id, "ai", card_content, "am_material_question", card_data
            )
            await add_bubble_animated(
                ChatMessage(
                    "ai", card_content, timestamp_now(),
                    "am_material_question", card_data, id=message_id,
                ),
                live=True,
            )
            render_sidebar()
        finally:
            message_input.disabled = False
            state["is_sending"] = False
            page.update()

    async def handle_am_material_submit(message_id: int, answers: dict) -> None:
        """Called once an am_material_question card is submitted (see
        build_am_material_question_card's on_submit) -- the card has
        already locked itself visually by this point; this persists that
        answer, runs the real recommend_am_material tool call with it (the
        exact same structured-text path a typed answer would have taken,
        so _try_parse_am_material_answers/the ranking logic are untouched),
        and posts the result as the next assistant message. Mirrors
        _send_and_await_reply's loading-row/to_thread/conversation-switch-
        guard pattern.
        """
        if state["is_sending"]:
            return
        conversation_id = state["conversation_id"]
        if conversation_id is None:
            return

        state["is_sending"] = True
        message_input.disabled = True
        send_button.disabled = True
        page.update()

        chat_db.update_message_structured_data(
            message_id, "am_material_question", {"status": "answered", "answers": answers}
        )

        structured_text = _build_am_material_structured_text(answers)

        loading_row = build_loading_row()
        chat_list.controls.append(loading_row)
        page.update()

        structured_tool: str | None = None
        structured_data: dict | None = None
        try:
            answer, structured_tool, structured_data = await asyncio.to_thread(
                run_ai_orchestrator_with_data, structured_text, fusion_mode_radio_group.value
            )
            role = "ai"
        except Exception as exc:  # last-resort UI-side guard; orchestrator never raises
            answer = f"Unexpected error: {exc}"
            role = "error"

        chat_db.add_message(conversation_id, role, answer, structured_tool, structured_data)

        if state["conversation_id"] == conversation_id:
            if loading_row in chat_list.controls:
                chat_list.controls.remove(loading_row)
            await add_bubble_animated(
                ChatMessage(role, answer, timestamp_now(), structured_tool, structured_data)
            )

        message_input.disabled = False
        state["is_sending"] = False
        page.update()
        render_sidebar()
        await message_input.focus()

    async def send_message(e: ft.ControlEvent) -> None:
        text = message_input.value.strip() if message_input.value else ""
        if not text or state["is_sending"]:
            return

        # Fusion-only, same gate recommend_am_material() itself enforces
        # (mcp_server.py) -- on a SolidWorks session this would only ever
        # return found=False, so don't interrupt the user with a card the
        # backend can't honor; let it fall through to the normal chat path,
        # which already surfaces that "AM tools unavailable" message.
        if state.get("platform") == PLATFORM_FUSION and looks_like_am_material_recommendation_request(
            text
        ):
            await _post_am_material_question(text)
            return

        await _send_text(text)

    async def _send_and_await_reply(conversation_id: int, text: str) -> None:
        loading_row = build_loading_row()
        chat_list.controls.append(loading_row)
        page.update()

        structured_tool: str | None = None
        structured_data: dict | None = None
        try:
            answer, structured_tool, structured_data = await asyncio.to_thread(
                run_ai_orchestrator_with_data, text, fusion_mode_radio_group.value
            )
            role = "ai"
        except Exception as exc:  # last-resort UI-side guard; orchestrator never raises
            answer = f"Unexpected error: {exc}"
            role = "error"

        chat_db.add_message(conversation_id, role, answer, structured_tool, structured_data)

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
            output_path = str(exports_dir() / f"manufacturing_report_{timestamp}.pdf")
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
            output_path = str(exports_dir() / f"bom_{timestamp}.xlsx")
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
        update_fusion_mode_visibility()
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
            fusion_mode_row,
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
    update_fusion_mode_visibility()
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
    # Packaged .exe mode: AiOrchestrator re-launches this same executable
    # with --mcp-server when running frozen (no "python mcp_server.py" on
    # the target machine -- see ai_orchestrator.AiOrchestrator.__init__).
    # Must be checked before ft.run() does anything GUI-related, and must
    # never fall through to the chat window.
    if "--mcp-server" in sys.argv:
        import mcp_server

        mcp_server.mcp.run()
    else:
        ft.run(main)
