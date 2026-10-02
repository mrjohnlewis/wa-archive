"""Synthetic WhatsApp-iOS-like backup for tests. No real data.

Builds a DirSource folder: Info.plist, Manifest.plist and
files/<domain>/<relative_path>, with ChatStorage.sqlite, ContactsV2.sqlite,
CallHistory.sqlite and media files, using a subset of the real iOS schema.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SHARED = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
APPLE_EPOCH = 978307200

CHAT_SCHEMA = """
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZCONTACTJID VARCHAR, ZPARTNERNAME VARCHAR,
  ZSESSIONTYPE INTEGER, ZLASTMESSAGEDATE TIMESTAMP, ZMESSAGECOUNTER INTEGER, ZGROUPINFO INTEGER,
  ZREMOVED INTEGER, ZARCHIVED INTEGER, ZCONTACTIDENTIFIER VARCHAR);
CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZCREATORJID VARCHAR, ZCREATIONDATE TIMESTAMP);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID VARCHAR,
  ZCONTACTNAME VARCHAR, ZFIRSTNAME VARCHAR);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
  ZMESSAGEDATE TIMESTAMP, ZSENTDATE TIMESTAMP, ZTEXT VARCHAR, ZMESSAGETYPE INTEGER, ZGROUPMEMBER INTEGER,
  ZSTANZAID VARCHAR, ZFROMJID VARCHAR, ZTOJID VARCHAR, ZMEDIAITEM INTEGER, ZFLAGS INTEGER,
  ZGROUPEVENTTYPE INTEGER, ZMESSAGESTATUS INTEGER, ZSORT INTEGER, ZPARENTMESSAGE INTEGER);
CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH VARCHAR,
  ZTHUMBNAILLOCALPATH VARCHAR, ZFILESIZE INTEGER, ZMOVIEDURATION INTEGER, ZTITLE VARCHAR, ZMEDIAURL VARCHAR,
  ZVCARDSTRING VARCHAR, ZVCARDNAME VARCHAR, ZMETADATA BLOB, ZMEDIAKEY BLOB, ZLATITUDE FLOAT, ZLONGITUDE FLOAT);
CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID VARCHAR, ZPUSHNAME VARCHAR);
CREATE TABLE ZWAVCARDMENTION (Z_PK INTEGER PRIMARY KEY, ZMEDIAITEM INTEGER, ZWHATSAPPID VARCHAR);
"""
CONTACTS_SCHEMA = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, ZFULLNAME VARCHAR, ZGIVENNAME VARCHAR,
  ZWHATSAPPID VARCHAR, ZPHONENUMBER VARCHAR, ZLID VARCHAR, ZABOUTTEXT VARCHAR);
"""
CALLS_SCHEMA = """
CREATE TABLE ZWAAGGREGATECALLEVENT (Z_PK INTEGER PRIMARY KEY, ZFIRSTDATE TIMESTAMP);
CREATE TABLE ZWACDCALLEVENT (Z_PK INTEGER PRIMARY KEY, Z1CALLEVENTS INTEGER, ZCALLIDSTRING VARCHAR,
  ZGROUPCALLCREATORUSERJIDSTRING VARCHAR, ZGROUPJIDSTRING VARCHAR, ZDATE TIMESTAMP, ZOUTCOME INTEGER,
  ZBYTESRECEIVED INTEGER, ZBYTESSENT INTEGER, ZDURATION FLOAT, ZVIDEO INTEGER, ZMISSED INTEGER, ZINCOMING INTEGER);
CREATE TABLE ZWACDCALLEVENTPARTICIPANT (Z_PK INTEGER PRIMARY KEY, Z1PARTICIPANTS INTEGER, ZJIDSTRING VARCHAR,
  ZOUTCOME INTEGER);
"""


def pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_field(number: int, value: bytes | str | int) -> bytes:
    if isinstance(value, int):
        return pb_varint(number << 3) + pb_varint(value)
    if isinstance(value, str):
        value = value.encode()
    return pb_varint(number << 3 | 2) + pb_varint(len(value)) + value


def stanza(i: int, length: int = 20) -> str:
    return hashlib.sha1(f"stanza-{i}".encode()).hexdigest().upper()[:length]


@dataclass
class Media:
    local_path: str | None
    content: bytes | None  # None = referenced but not in backup
    size: int | None = None
    title: str | None = None
    metadata: bytes | None = None


@dataclass
class Msg:
    pk: int
    chat: int
    from_me: int
    date: float
    text: str | None
    type: int = 0
    stanza: str | None = None
    member: int | None = None
    from_jid: str | None = None
    media: Media | None = None
    receipt: bytes | None = None
    group_event: int | None = None


@dataclass
class Fixture:
    chats: dict[int, tuple[str, str, int]] = field(default_factory=dict)  # pk -> (jid, name, session type)
    members: dict[int, tuple[int, str, str]] = field(default_factory=dict)  # pk -> (chat, jid, name)
    messages: list[Msg] = field(default_factory=list)
    contacts: list[tuple[str, str, str | None]] = field(default_factory=list)  # (wa id, name, lid)
    calls: list[tuple[str, str, float]] = field(default_factory=list)  # (call id, jid, date)
    backup_date: datetime = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    def write(self, root: Path) -> Path:
        files = root / "files" / SHARED
        files.mkdir(parents=True, exist_ok=True)
        with open(root / "Info.plist", "wb") as f:
            plistlib.dump({"Device Name": "Fixture-Phone", "Product Type": "iPhone99,1",
                           "Product Version": "26.0", "Last Backup Date": self.backup_date.replace(tzinfo=None),
                           "Applications": {"net.whatsapp.WhatsApp": {"iTunesMetadata": plistlib.dumps(
                               {"bundleShortVersionString": "26.1.0"})}}}, f)
        with open(root / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": False}, f)

        con = sqlite3.connect(files / "ChatStorage.sqlite")
        con.executescript(CHAT_SCHEMA)
        con.execute("INSERT INTO Z_METADATA VALUES (1, 'fixture', NULL)")
        for pk, (jid, name, stype) in self.chats.items():
            con.execute("INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE) VALUES (?,?,?,?)",
                        (pk, jid, name, stype))
        for pk, (chat, jid, name) in self.members.items():
            con.execute("INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (?,?,?,?)",
                        (pk, chat, jid, name))
        media_pk = 0
        for m in self.messages:
            mi = None
            if m.media:
                media_pk += 1
                mi = media_pk
                md = m.media
                size = md.size if md.size is not None else (len(md.content) if md.content else 1000)
                con.execute("""INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE, ZTITLE, ZMETADATA)
                               VALUES (?,?,?,?,?,?)""", (mi, m.pk, md.local_path, size, md.title, md.metadata))
                if md.local_path and md.content is not None:
                    p = files / "Message" / md.local_path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(md.content)
            con.execute("""INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE,
                           ZGROUPMEMBER, ZSTANZAID, ZFROMJID, ZMEDIAITEM, ZFLAGS, ZGROUPEVENTTYPE)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (m.pk, m.chat, m.from_me, m.date, m.text, m.type, m.member, m.stanza, m.from_jid, mi, 0,
                         m.group_event))
            if m.receipt:
                con.execute("INSERT INTO ZWAMESSAGEINFO (ZMESSAGE, ZRECEIPTINFO) VALUES (?,?)", (m.pk, m.receipt))
        con.commit()
        con.close()

        con = sqlite3.connect(files / "ContactsV2.sqlite")
        con.executescript(CONTACTS_SCHEMA)
        con.executemany("INSERT INTO ZWAADDRESSBOOKCONTACT (ZWHATSAPPID, ZFULLNAME, ZLID) VALUES (?,?,?)",
                        self.contacts)
        con.commit()
        con.close()

        con = sqlite3.connect(files / "CallHistory.sqlite")
        con.executescript(CALLS_SCHEMA)
        con.execute("INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK) VALUES (1)")
        for cid, jid, date in self.calls:
            con.execute("""INSERT INTO ZWACDCALLEVENT (Z1CALLEVENTS, ZCALLIDSTRING, ZGROUPCALLCREATORUSERJIDSTRING,
                           ZDATE, ZDURATION, ZINCOMING) VALUES (1,?,?,?,60,1)""", (cid, jid, date))
        con.commit()
        con.close()
        return root


# Values that must never appear in any output produced from this fixture.
SECRETS = ["Contact-Alpha", "Contact-Bravo", "Group-Charlie", "Member-Delta", "64210000001", "88880000002",
           "120363000000001", "77770000003", "64210000009", "fixture-secret-text", "Fixture-Phone-Doc", "👍",
           "img1.jpg", "voice1.opus"]


def basic_fixture() -> Fixture:
    """A small but varied fixture: 1:1 phone + LID chats, a group, all main media types."""
    fx = Fixture()
    alpha, bravo_lid, group = "64210000001@s.whatsapp.net", "88880000002@lid", "120363000000001@g.us"
    fx.chats = {1: (alpha, "Contact-Alpha", 0), 2: (bravo_lid, "Contact-Bravo", 0), 3: (group, "Group-Charlie", 1)}
    fx.members = {1: (3, "64210000009@s.whatsapp.net", "Member-Delta"), 2: (3, "77770000003@lid", "Member-Echo")}
    fx.contacts = [(alpha, "Contact-Alpha", None), ("64210000002@s.whatsapp.net", "Contact-Bravo", bravo_lid)]
    fx.calls = [("CALLID0001", alpha, 800_000_000.0), ("CALLID0002", bravo_lid, 800_000_100.0)]

    t = 800_000_000.0  # 2026-05-09
    pk = 0

    def add(chat, text=None, *, type=0, from_me=0, media=None, member=None, stanza_id=..., receipt=None, ev=None):
        nonlocal pk, t
        pk += 1
        t += 60
        sid = stanza(pk) if stanza_id is ... else stanza_id
        fx.messages.append(Msg(pk, chat, from_me, t, text, type, sid, member,
                               fx.chats[chat][0] if not from_me else None, media, receipt, ev))
        return fx.messages[-1]

    def media_path(chat, name):
        return f"Media/{fx.chats[chat][0]}/a/b/{name}"

    for i in range(10):
        add(1, f"fixture-secret-text {i}", from_me=i % 2)
    first = fx.messages[0]
    add(1, None, type=1, media=Media(media_path(1, "img1.jpg"), b"\xff\xd8 image-one"))
    add(1, None, type=1, media=Media(media_path(1, "img2.jpg"), b"\xff\xd8 image-two"))
    add(1, None, type=2, media=Media(media_path(1, "vid1.mp4"), None, size=5_000_000))  # never downloaded
    add(1, None, type=3, media=Media(media_path(1, "voice1.opus"), b"OggS voice"))
    add(1, None, type=8, media=Media(media_path(1, "doc1.pdf"), b"%PDF doc", title="Fixture-Phone-Doc"))
    add(1, "fixture-secret-text reply", media=Media(None, None, metadata=pb_field(5, first.stanza)))
    add(1, None, type=14)  # deleted for everyone
    add(1, "fixture-secret-text reacted",
        receipt=pb_field(1, pb_field(1, "64210000001@s.whatsapp.net") + pb_field(2, "👍")))
    for i in range(5):
        add(2, f"fixture-secret-text lid {i}", from_me=i % 2)
    add(2, None, type=15, media=Media(media_path(2, "sticker.webp"), b"RIFF sticker"))
    add(3, None, type=6, stanza_id="", ev=1)  # system event without stanza id
    for i in range(6):
        add(3, f"fixture-secret-text group {i}", member=1 + i % 2)
    add(3, None, type=1, member=1, media=Media(media_path(3, "gimg.jpg"), b"\xff\xd8 group image"))
    return fx
