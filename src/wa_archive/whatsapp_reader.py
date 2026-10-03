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


# ---------------------------------------------------------------- full read for ingest

import base64  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from . import protobuf  # noqa: E402

# Message types whose media row represents a real file (even when the file is gone).
MEDIA_TYPES = {1, 2, 3, 8, 11, 15, 23, 38, 39, 43}
# Types whose text / caption may be edited by the sender.
EDITABLE_TEXT_TYPES = {0, 7}
EDITABLE_CAPTION_TYPES = {1, 2, 11}
SESSION_KINDS = {0: "direct", 1: "group", 2: "status", 3: "status_thread", 4: "community", 5: "channel"}
HIDDEN_KINDS = {"status", "status_thread", "channel"}


def raw_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    out = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, bytes):
            v = {"b64": base64.b64encode(v).decode()}
        out[k] = v
    return out


@dataclass
class Session:
    pk: int
    jid: str
    name: str | None
    kind: str


@dataclass
class Media:
    path: str | None
    thumb_path: str | None
    size: int | None
    title: str | None


@dataclass
class Reaction:
    reaction_id: str
    reactor_jid: str | None  # None = me (no JID recorded)
    emoji: str
    ts: int | None


@dataclass
class Message:
    pk: int
    chat_jid: str
    from_me: int
    ts: float | None
    sent_ts: float | None
    type: int | None
    stanza: str | None
    sender_jid: str | None
    group_event: int | None
    text: str | None
    title: str | None
    quoted_stanza: str | None
    quoted_jid: str | None
    media: Media | None
    reactions: list[Reaction]
    raw_json: str


@dataclass
class BackupData:
    sessions: list[Session] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    names: list[tuple[str, str, str]] = field(default_factory=list)  # (jid, name, source)
    lid_map: dict[str, str] = field(default_factory=dict)            # lid user -> phone user
    calls: list[dict] = field(default_factory=list)
    referenced_paths: set[str] = field(default_factory=set)          # media + thumbnail paths


def _apple(v) -> float | None:
    return APPLE_EPOCH + v if v is not None else None


def _s(v: bytes | int | None) -> str | None:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _ts(v) -> int | None:
    if not isinstance(v, int) or v <= 0:
        return None
    return v // 1000 if v > 10**12 else v


def parse_quote(metadata: bytes | None) -> tuple[str | None, str | None]:
    """ZWAMEDIAITEM.ZMETADATA field 5 = quoted stanza id, field 6 = quoted sender JID."""
    fields = protobuf.try_parse(metadata) if isinstance(metadata, bytes) else None
    if not fields:
        return None, None
    f5, f6 = protobuf.first(fields, 5), protobuf.first(fields, 6)
    return (_s(f5.value) if f5 else None), (_s(f6.value) if f6 else None)


def parse_reactions(receipt: bytes | None) -> list[Reaction]:
    """ZWAMESSAGEINFO.ZRECEIPTINFO field 7: 7.1 = {1 id, 2 jid, 3 emoji, 4 ts}, 7.2 = own {1 id, 2 emoji, 3 ts}."""
    fields = protobuf.try_parse(receipt) if isinstance(receipt, bytes) else None
    out: list[Reaction] = []
    for f in fields or []:
        if f.number != 7 or f.wire_type != protobuf.LEN:
            continue
        for g in protobuf.try_parse(f.value) or []:
            if g.wire_type != protobuf.LEN or g.number not in (1, 2):
                continue
            r = {h.number: h.value for h in protobuf.try_parse(g.value) or []}
            if g.number == 1:
                rid, jid, emoji, ts = _s(r.get(1)), _s(r.get(2)), _s(r.get(3)), _ts(r.get(4))
            else:
                rid, jid, emoji, ts = _s(r.get(1)), None, _s(r.get(2)), _ts(r.get(3))
            if rid:
                out.append(Reaction(rid, jid, emoji or "", ts))
    return out


def _jid_user(jid: str) -> str:
    return jid.split("@", 1)[0]


def _norm_lid(v: str) -> str:
    return v if "@" in v else f"{v}@lid"


def load_backup(chat_db: Path, contacts_db: Path | None, calls_db: Path | None) -> BackupData:
    data = BackupData()
    con = open_db(chat_db)
    s = Schema(con)
    try:
        for r in con.execute("SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION"):
            if not r["ZCONTACTJID"]:
                continue
            kind = SESSION_KINDS.get(r["ZSESSIONTYPE"]) or {"group": "group", "channel": "channel"}.get(
                jid_kind(r["ZCONTACTJID"]), "direct")
            data.sessions.append(Session(r["Z_PK"], r["ZCONTACTJID"], r["ZPARTNERNAME"], kind))
            if r["ZPARTNERNAME"]:
                data.names.append((r["ZCONTACTJID"], r["ZPARTNERNAME"], "chat"))
        sessions = {x.pk: x for x in data.sessions}

        members = {}
        if s.has("ZWAGROUPMEMBER", "ZMEMBERJID"):
            name_cols = [c for c in ("ZCONTACTNAME", "ZFIRSTNAME") if s.has("ZWAGROUPMEMBER", c)]
            for r in con.execute(f"SELECT Z_PK, ZMEMBERJID{''.join(', ' + c for c in name_cols)} FROM ZWAGROUPMEMBER"):
                members[r["Z_PK"]] = r["ZMEMBERJID"]
                name = next((r[c] for c in name_cols if r[c]), None)
                if r["ZMEMBERJID"] and name:
                    data.names.append((r["ZMEMBERJID"], name, "member"))
        if s.has("ZWAPROFILEPUSHNAME", "ZJID", "ZPUSHNAME"):
            for r in con.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL"):
                data.names.append((r["ZJID"], r["ZPUSHNAME"], "push"))

        media_rows = {r["Z_PK"]: r for r in con.execute("SELECT * FROM ZWAMEDIAITEM")}
        media_by_msg = {r["ZMESSAGE"]: r for r in media_rows.values() if r["ZMESSAGE"] is not None}
        link_col = s.has("ZWAMESSAGE", "ZMEDIAITEM")
        receipts_by_msg: dict[int, bytes] = {}
        receipts_by_pk: dict[int, bytes] = {}
        if s.has("ZWAMESSAGEINFO", "ZMESSAGE", "ZRECEIPTINFO"):
            for pk, msg, blob in con.execute(
                    "SELECT Z_PK, ZMESSAGE, ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZRECEIPTINFO IS NOT NULL"):
                receipts_by_pk[pk] = blob
                if msg is not None:
                    receipts_by_msg[msg] = blob
        info_col = s.has("ZWAMESSAGE", "ZMESSAGEINFO")
        thumb_col = next((c for c in ("ZXMPPTHUMBPATH", "ZTHUMBNAILLOCALPATH") if s.has("ZWAMEDIAITEM", c)), None)

        for r in con.execute("SELECT * FROM ZWAMESSAGE"):
            sess = sessions.get(r["ZCHATSESSION"])
            if sess is None:
                continue
            mi = media_rows.get(r["ZMEDIAITEM"]) if link_col and r["ZMEDIAITEM"] else media_by_msg.get(r["Z_PK"])
            mtype = r["ZMESSAGETYPE"]
            media = None
            quoted = (None, None)
            title = None
            if mi is not None:
                quoted = parse_quote(mi["ZMETADATA"])
                title = mi["ZTITLE"]
                path = mi["ZMEDIALOCALPATH"] or None
                thumb = (mi[thumb_col] or None) if thumb_col else None
                if path or (mi["ZFILESIZE"] or 0) > 0 or mtype in MEDIA_TYPES:
                    media = Media(path, thumb, mi["ZFILESIZE"], title)
                    if path:
                        data.referenced_paths.add(path)
                    if thumb:
                        data.referenced_paths.add(thumb)
            from_me = int(r["ZISFROMME"] or 0)
            if from_me:
                sender = None
            elif sess.kind in ("group", "community", "status"):
                sender = members.get(r["ZGROUPMEMBER"]) or r["ZFROMJID"]
            else:
                sender = sess.jid
            receipt = receipts_by_msg.get(r["Z_PK"]) or (
                receipts_by_pk.get(r["ZMESSAGEINFO"]) if info_col and r["ZMESSAGEINFO"] else None)
            raw = {"message": raw_row(r), "media_item": raw_row(mi)}
            data.messages.append(Message(
                pk=r["Z_PK"], chat_jid=sess.jid, from_me=from_me, ts=_apple(r["ZMESSAGEDATE"]),
                sent_ts=_apple(r["ZSENTDATE"]) if "ZSENTDATE" in r.keys() else None, type=mtype,
                stanza=r["ZSTANZAID"] or None, sender_jid=sender,
                group_event=r["ZGROUPEVENTTYPE"] if "ZGROUPEVENTTYPE" in r.keys() else None,
                text=r["ZTEXT"], title=title, quoted_stanza=quoted[0], quoted_jid=quoted[1], media=media,
                reactions=parse_reactions(receipt), raw_json=json.dumps(raw, sort_keys=True)))
    finally:
        con.close()

    if contacts_db:
        con = open_db(contacts_db)
        s = Schema(con)
        try:
            if s.has("ZWAADDRESSBOOKCONTACT", "ZWHATSAPPID"):
                cols = [c for c in ("ZFULLNAME", "ZGIVENNAME", "ZBUSINESSNAME") if s.has("ZWAADDRESSBOOKCONTACT", c)]
                lid = "ZLID" if s.has("ZWAADDRESSBOOKCONTACT", "ZLID") else "NULL AS ZLID"
                for r in con.execute(f"SELECT ZWHATSAPPID, {lid}{''.join(', ' + c for c in cols)} FROM ZWAADDRESSBOOKCONTACT"):
                    name = next((r[c] for c in cols if r[c]), None)
                    wa, lid_v = r["ZWHATSAPPID"], r["ZLID"]
                    if wa and name:
                        data.names.append((wa, name, "contact"))
                    if lid_v and name:
                        data.names.append((_norm_lid(lid_v), name, "contact"))
                    if wa and lid_v and jid_kind(wa) == "phone":
                        data.lid_map[_jid_user(_norm_lid(lid_v))] = _jid_user(wa)
        finally:
            con.close()

    if calls_db:
        con = open_db(calls_db)
        s = Schema(con)
        try:
            if s.has("ZWACDCALLEVENT", "ZCALLIDSTRING"):
                agg = {r["Z_PK"]: r for r in con.execute("SELECT * FROM ZWAAGGREGATECALLEVENT")} \
                    if "ZWAAGGREGATECALLEVENT" in s.cols else {}
                parts: dict[int, list[str]] = {}
                if s.has("ZWACDCALLEVENTPARTICIPANT", "Z1PARTICIPANTS", "ZJIDSTRING"):
                    for r in con.execute("SELECT Z1PARTICIPANTS, ZJIDSTRING FROM ZWACDCALLEVENTPARTICIPANT"):
                        parts.setdefault(r[0], []).append(r[1])
                for r in con.execute("SELECT * FROM ZWACDCALLEVENT WHERE ZCALLIDSTRING IS NOT NULL"):
                    keys = r.keys()
                    a = agg.get(r["Z1CALLEVENTS"]) if "Z1CALLEVENTS" in keys else None
                    data.calls.append({
                        "call_id": r["ZCALLIDSTRING"], "ts": _apple(r["ZDATE"]) if "ZDATE" in keys else None,
                        "duration": r["ZDURATION"] if "ZDURATION" in keys else None,
                        "group_jid": r["ZGROUPJIDSTRING"] if "ZGROUPJIDSTRING" in keys else None,
                        "creator_jid": r["ZGROUPCALLCREATORUSERJIDSTRING"] if "ZGROUPCALLCREATORUSERJIDSTRING" in keys else None,
                        "participants": parts.get(r["Z_PK"], []),
                        "raw": {"event": raw_row(r), "aggregate": raw_row(a)}})
        finally:
            con.close()
    return data


def equivalent_jids(jid: str, lid_map: dict[str, str]) -> list[str]:
    """Other spellings of the same person's 1:1 or status-thread JID (phone <-> LID)."""
    user, _, domain = jid.partition("@")
    phone_to_lid = {v: k for k, v in lid_map.items()}
    pairs = {"s.whatsapp.net": "lid", "lid": "s.whatsapp.net", "status": "lid.status", "lid.status": "status"}
    if domain not in pairs:
        return []
    other_user = lid_map.get(user) if domain.startswith("lid") else phone_to_lid.get(user)
    return [f"{other_user}@{pairs[domain]}"] if other_user else []
