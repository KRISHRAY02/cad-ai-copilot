"""SQLite persistence for desktop_app.py's conversation history and user
accounts.

Local multi-user store: one file (chat_history.db, created next to this
script), three tables (users, conversations, messages). Each conversation
belongs to exactly one user (conversations.user_id); accounts are local
profiles on this machine, not a cloud login -- passwords are hashed with
bcrypt (see create_user/verify_user) and never leave this database. All
datetimes are naive local time -- there's no multi-timezone concern for a
single desktop app.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import bcrypt

DB_PATH = Path(__file__).parent / "chat_history.db"

TITLE_MAX_LENGTH = 30


@dataclass
class Conversation:
    id: int
    title: str | None
    created_at: str


@dataclass
class Message:
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: str
    # The tool name + parsed JSON behind an "ai" message's structured chat
    # card (see desktop_app.py's STRUCTURED_CARD_BUILDERS) -- both None for
    # every message that's plain text only (user messages, errors, or an
    # "ai" answer no card exists for). Persisted so a card (BOM table,
    # component listing, cost breakdown) still renders when a past
    # conversation is reopened, including after logging out and back in --
    # previously only `content` (the plain text) was stored, so a reopened
    # conversation always fell back to the text-only bubble.
    structured_tool: str | None = None
    structured_data: dict | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )

        # Migration for a chat_history.db created before accounts existed:
        # CREATE TABLE IF NOT EXISTS above only applies to a brand-new file,
        # so an existing conversations table needs user_id added on top.
        # Pre-existing conversations have no known owner and are left with
        # user_id NULL -- they simply won't appear in any account's
        # (user_id-filtered) sidebar, rather than being guessed into
        # belonging to whichever account happens to log in first.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in existing_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
            )

        # Migration for a users table created before CAD-platform selection
        # existed. NULL means "never successfully connected to a platform
        # yet" -- the platform picker treats that the same as "no
        # preference", it never guesses a default.
        existing_user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "last_used_platform" not in existing_user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_used_platform TEXT")

        # Migration for a messages table created before structured chat
        # cards existed. NULL for every pre-existing row (and for every
        # user/error message going forward) -- get_messages() only
        # attempts to render a card when structured_tool is non-NULL, so
        # old rows simply keep showing as plain text, not a broken card.
        existing_message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "structured_tool" not in existing_message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN structured_tool TEXT")
        if "structured_data" not in existing_message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN structured_data TEXT")


def create_user(username: str, password: str) -> dict:
    """Create a new local user profile. Returns {"success": True, "user_id":
    int} on success, or {"success": False, "message": str} if the username
    is already taken or the inputs are empty -- never raises, and never
    includes the plaintext password in the returned message (or anywhere
    else: it's hashed with bcrypt before it ever reaches the database, and
    it's never logged or printed).
    """
    username = (username or "").strip()
    if not username:
        return {"success": False, "message": "Username cannot be empty."}
    if not password:
        return {"success": False, "message": "Password cannot be empty."}

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing is not None:
            return {"success": False, "message": "That username is already taken."}

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, datetime.now().isoformat()),
        )
        return {"success": True, "user_id": cur.lastrowid}


def verify_user(username: str, password: str) -> int | None:
    """Check a username/password pair against the stored bcrypt hash.

    Returns the matching user's id on success, or None if the username
    doesn't exist OR the password is wrong -- deliberately indistinguishable
    from the caller's point of view (see desktop_app.py's login screen,
    which shows a single generic "invalid username or password" message
    either way, not which one was wrong).
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    with _connect() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()

    if row is None:
        return None
    if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        return row["id"]
    return None


def get_last_used_platform(user_id: int) -> str | None:
    """The CAD platform ("solidworks" | "fusion360") this user last
    successfully connected to, or None if they never have (new account, or
    every past attempt failed).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_used_platform FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["last_used_platform"] if row else None


def set_last_used_platform(user_id: int, platform: str) -> None:
    """Record `platform` as this user's default -- called only after a
    real, successful adapter.connect(), never on a mere detection ping.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET last_used_platform = ? WHERE id = ?", (platform, user_id)
        )


def create_conversation(user_id: int) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, user_id) VALUES (NULL, ?, ?)",
            (datetime.now().isoformat(), user_id),
        )
        return cur.lastrowid


def set_conversation_title(conversation_id: int, title: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    structured_tool: str | None = None,
    structured_data: dict | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages "
            "(conversation_id, role, content, created_at, structured_tool, structured_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                datetime.now().isoformat(),
                structured_tool,
                json.dumps(structured_data) if structured_data is not None else None,
            ),
        )


def list_conversations(user_id: int) -> list[Conversation]:
    """Conversations belonging to `user_id` only -- never another user's,
    and never the pre-account-system orphaned rows with user_id NULL (see
    the migration note in init_db()).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM conversations "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [Conversation(r["id"], r["title"], r["created_at"]) for r in rows]


def get_messages(conversation_id: int) -> list[Message]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, created_at, "
            "structured_tool, structured_data FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        messages = []
        for r in rows:
            structured_data = None
            if r["structured_data"] is not None:
                try:
                    structured_data = json.loads(r["structured_data"])
                except json.JSONDecodeError:
                    structured_data = None  # corrupt row -- fall back to plain text, don't crash
            messages.append(
                Message(
                    r["id"],
                    r["conversation_id"],
                    r["role"],
                    r["content"],
                    r["created_at"],
                    r["structured_tool"],
                    structured_data,
                )
            )
        return messages


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    """Delete a conversation and all its messages -- but only if
    `conversation_id` actually belongs to `user_id`. Returns True if the
    conversation was found (owned by this user) and deleted, False
    otherwise (wrong owner, or no such conversation) -- callers must check
    this rather than assuming the delete succeeded, since a False here
    means nothing was removed.
    """
    with _connect() as conn:
        owner = conn.execute(
            "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if owner is None or owner["user_id"] != user_id:
            return False
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True


def make_title(first_message: str) -> str:
    text = " ".join(first_message.split())
    if len(text) <= TITLE_MAX_LENGTH:
        return text
    return text[: TITLE_MAX_LENGTH - 1].rstrip() + "…"


def relative_time(created_at: str) -> str:
    dt = datetime.fromisoformat(created_at)
    now = datetime.now()
    seconds = (now - dt).total_seconds()

    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if dt.date() == now.date():
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if (now.date() - dt.date()).days == 1:
        return "Yesterday"
    return dt.strftime("%b %d")
