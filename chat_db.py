"""SQLite persistence for desktop_app.py's conversation history.

Local single-user store: one file (chat_history.db, created next to this
script), two tables (conversations, messages). All datetimes are naive
local time -- there's no multi-timezone concern for a single desktop app.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                created_at TEXT NOT NULL
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


def create_conversation() -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at) VALUES (NULL, ?)",
            (datetime.now().isoformat(),),
        )
        return cur.lastrowid


def set_conversation_title(conversation_id: int, title: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )


def add_message(conversation_id: int, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, datetime.now().isoformat()),
        )


def list_conversations() -> list[Conversation]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM conversations ORDER BY created_at DESC"
        ).fetchall()
        return [Conversation(r["id"], r["title"], r["created_at"]) for r in rows]


def get_messages(conversation_id: int) -> list[Message]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        return [
            Message(r["id"], r["conversation_id"], r["role"], r["content"], r["created_at"])
            for r in rows
        ]


def delete_conversation(conversation_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


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
