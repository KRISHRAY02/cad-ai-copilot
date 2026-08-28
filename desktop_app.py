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
from ai_orchestrator import (
    AiOrchestrator,
    McpServerUnavailableError,
    run_ai_orchestrator,
)
from bom_export import export_bom as write_bom_file
from mcp_server import adapter
from report.generate_report import generate_manufacturing_report

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


def build_bubble(message: ChatMessage, page_width_hint: int) -> ft.Container:
    is_user = message.role == "user"
    is_error = message.role == "error"

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
        width=int(page_width_hint * BUBBLE_MAX_WIDTH_RATIO),
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

    state = {"conversation_id": None, "is_sending": False, "user_id": user_id}

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

        try:
            answer = await asyncio.to_thread(run_ai_orchestrator, text)
            role = "ai"
        except Exception as exc:  # last-resort UI-side guard; orchestrator never raises
            answer = f"Unexpected error: {exc}"
            role = "error"

        chat_db.add_message(conversation_id, role, answer)

        # The user may have switched conversations (sidebar click / New Chat)
        # while the request above was in flight, which clears chat_list.
        # The reply is already saved to the DB either way -- only touch
        # chat_list/loading_row if this conversation is still the one being
        # viewed; re-enable the (conversation-independent) input regardless.
        if state["conversation_id"] == conversation_id:
            if loading_row in chat_list.controls:
                chat_list.controls.remove(loading_row)
            await add_bubble_animated(ChatMessage(role, answer, timestamp_now()))

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
            if not adapter.is_assembly():
                raise RuntimeError(
                    "The currently open document is not an assembly -- "
                    "open an assembly in SOLIDWORKS to export a BOM."
                )
            bom_result = adapter.get_assembly_bom(
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

    header = ft.Container(
        content=ft.Column(
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

    # ---- startup environment check ----

    message_input.disabled = True
    send_button.disabled = True
    page.update()

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
