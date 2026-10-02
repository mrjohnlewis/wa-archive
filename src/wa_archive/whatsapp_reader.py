"""Shared helpers for reading WhatsApp iOS databases (ChatStorage / ContactsV2 / CallHistory)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

APPLE_EPOCH = 978307200  # 2001-01-01T00:00:00Z

# ZWAMESSAGE.ZMESSAGETYPE values seen in iOS WhatsApp (non-exhaustive).
MESSAGE_TYPES = {
    0: "text", 1: "image", 2: "video", 3: "audio/voice", 4: "contact", 5: "location",
    6: "system", 7: "link", 8: "document", 10: "call-notice", 11: "gif", 14: "deleted",
    15: "sticker", 38: "view-once", 46: "poll", 59: "event", 66: "poll-v3",
}


def type_name(t: int | None) -> str:
    return f"{t} {MESSAGE_TYPES.get(t, '?')}" if t is not None else "null"


def apple_to_datetime(v: float | None) -> datetime | None:
    if v is None:
        return None
    return datetime.fromtimestamp(APPLE_EPOCH + v, tz=timezone.utc)


def jid_kind(jid: str | None) -> str:
    """Classify a JID by suffix only, e.g. 'phone', 'lid', 'group'."""
    if not jid:
        return "null"
    if jid.endswith("@s.whatsapp.net"):
        return "phone"
    if jid.endswith("@lid"):
        return "lid"
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@broadcast"):
        return "status" if jid.startswith("status@") else "broadcast"
    if jid.endswith("@newsletter"):
        return "channel"
    return "other"


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


class Schema:
    def __init__(self, con: sqlite3.Connection):
        self.cols = {t: set(columns(con, t)) for t in tables(con)}

    def has(self, table: str, *cols: str) -> bool:
        return table in self.cols and all(c in self.cols[table] for c in cols)
