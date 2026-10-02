"""Phase-1 extraction spike: prove we can read messages and media with stable IDs.

Output is structure and counts only: no message text, names, phone numbers,
JIDs or file names. Chats are referred to by rank ("chat #3"), never by name.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from . import protobuf
from .backup import (CALLS_DB, CHAT_DB, CONTACTS_DB, MEDIA_PREFIX, SHARED_DOMAIN,
                     BackupSource)
from .whatsapp_reader import Schema, apple_to_datetime, jid_kind, open_db, tables, type_name

KEY_TABLES = ["ZWACHATSESSION", "ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAMESSAGEINFO",
              "ZWAGROUPMEMBER", "ZWAGROUPINFO", "ZWAPROFILEPUSHNAME"]
EXPECTED = {
    "ZWACHATSESSION": ["Z_PK", "ZCONTACTJID", "ZPARTNERNAME", "ZSESSIONTYPE"],
    "ZWAMESSAGE": ["Z_PK", "ZCHATSESSION", "ZISFROMME", "ZMESSAGEDATE", "ZTEXT", "ZMESSAGETYPE",
                   "ZSTANZAID", "ZGROUPMEMBER", "ZFROMJID", "ZFLAGS"],
    "ZWAMEDIAITEM": ["Z_PK", "ZMESSAGE", "ZMEDIALOCALPATH", "ZFILESIZE", "ZMETADATA", "ZTITLE"],
    "ZWAMESSAGEINFO": ["ZMESSAGE", "ZRECEIPTINFO"],
    "ZWAGROUPMEMBER": ["Z_PK", "ZMEMBERJID"],
}
INTERESTING = re.compile(r"EDIT|REVOK|ORIGINAL|LID|ADDRESS|IDENTIT|REACTION|EPHEMERAL|EXPIR|"
                         r"PARENT|QUOT|STAR|KEEP|THUMB|TRANSCRI", re.I)
GB = 1024 ** 3


def _date(v) -> str | None:
    d = apple_to_datetime(v)
    return d.strftime("%Y-%m-%d") if d else None


def _hist(rows) -> dict[str, int]:
    return {str(k): v for k, v in rows}


def manifest_census(source: BackupSource) -> tuple[dict, dict[str, int], dict[str, tuple[str, str]]]:
    by_domain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    shared: dict[str, int] = {}
    dbs: dict[str, tuple[str, str]] = {}
    for f in source.files():
        by_domain[f.domain][0] += 1
        by_domain[f.domain][1] += f.size
        if f.domain == SHARED_DOMAIN:
            shared[f.relative_path] = f.size
        base = f.relative_path.rsplit("/", 1)[-1]
        for name in (CHAT_DB, CHAT_DB + "-wal", CHAT_DB + "-shm", CONTACTS_DB, CALLS_DB):
            # Prefer the shared app-group domain if a name exists in several domains.
            if base == name and (name not in dbs or f.domain == SHARED_DOMAIN):
                dbs[name] = (f.domain, f.relative_path)
    census = {
        "domains": {d: {"files": n, "bytes": b} for d, (n, b) in sorted(by_domain.items())},
        "databases_found": sorted(dbs),
        "message_files": sum(1 for p in shared if p.startswith(MEDIA_PREFIX)),
        "message_bytes": sum(s for p, s in shared.items() if p.startswith(MEDIA_PREFIX)),
    }
    return census, shared, dbs


def chat_db_report(con, shared: dict[str, int]) -> dict:
    s = Schema(con)
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
    r: dict = {}

    r["tables"] = {t: q(f'SELECT count(*) FROM "{t}"') for t in tables(con)}
    r["key_table_columns"] = {t: sorted(s.cols.get(t, [])) for t in KEY_TABLES if t in s.cols}
    r["expected_columns_missing"] = [f"{t}.{c}" for t, cs in EXPECTED.items() for c in cs if not s.has(t, c)]
    r["interesting_columns"] = sorted(f"{t}.{c}" for t, cs in s.cols.items() for c in cs if INTERESTING.search(c))
    if s.has("Z_METADATA", "Z_VERSION"):
        r["coredata_version"] = q("SELECT Z_VERSION FROM Z_METADATA")

    # Chats
    sessions = con.execute("SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION").fetchall()
    r["chats"] = {
        "total": len(sessions),
        "by_session_type": dict(Counter(str(x["ZSESSIONTYPE"]) for x in sessions)),
        "by_jid_kind": dict(Counter(jid_kind(x["ZCONTACTJID"]) for x in sessions)),
    }
    phone_names = {x["ZPARTNERNAME"] for x in sessions if jid_kind(x["ZCONTACTJID"]) == "phone" and x["ZPARTNERNAME"]}
    lid_sessions = [x for x in sessions if jid_kind(x["ZCONTACTJID"]) == "lid"]
    r["chats"]["lid_chats_with_same_named_phone_chat"] = sum(1 for x in lid_sessions if x["ZPARTNERNAME"] in phone_names)
    jid_counts = Counter(x["ZCONTACTJID"] for x in sessions)
    r["chats"]["sessions_sharing_a_jid"] = sum(n for n in jid_counts.values() if n > 1)

    # Messages
    r["messages"] = {
        "total": q("SELECT count(*) FROM ZWAMESSAGE"),
        "without_chat_session": q("SELECT count(*) FROM ZWAMESSAGE WHERE ZCHATSESSION IS NULL"),
        "from_me": q("SELECT count(*) FROM ZWAMESSAGE WHERE ZISFROMME = 1"),
        "date_min": _date(q("SELECT min(ZMESSAGEDATE) FROM ZWAMESSAGE WHERE ZMESSAGEDATE > 0")),
        "date_max": _date(q("SELECT max(ZMESSAGEDATE) FROM ZWAMESSAGE")),
        "by_type": {type_name(t): n for t, n in con.execute(
            "SELECT ZMESSAGETYPE, count(*) FROM ZWAMESSAGE GROUP BY 1 ORDER BY 2 DESC")},
    }
    if s.has("ZWAMESSAGE", "ZGROUPEVENTTYPE"):
        r["messages"]["system_event_types"] = _hist(con.execute(
            "SELECT ZGROUPEVENTTYPE, count(*) FROM ZWAMESSAGE WHERE ZMESSAGETYPE = 6 GROUP BY 1"))
    if s.has("ZWAMESSAGE", "ZFLAGS"):
        bits = Counter()
        for (flags,) in con.execute("SELECT ZFLAGS FROM ZWAMESSAGE WHERE ZFLAGS IS NOT NULL AND ZFLAGS != 0"):
            for b in range(64):
                if int(flags) >> b & 1:
                    bits[b] += 1
        r["messages"]["flag_bits_set"] = {f"bit{b}": n for b, n in sorted(bits.items())}
    if s.has("ZWAMESSAGE", "ZMESSAGESTATUS"):
        r["messages"]["status_values"] = _hist(con.execute(
            "SELECT ZMESSAGESTATUS, count(*) FROM ZWAMESSAGE GROUP BY 1"))

    # Stanza IDs: the basis of our stable key.
    st: dict = {}
    st["null_or_empty"] = q("SELECT count(*) FROM ZWAMESSAGE WHERE ZSTANZAID IS NULL OR ZSTANZAID = ''")
    st["null_or_empty_by_type"] = {type_name(t): n for t, n in con.execute(
        "SELECT ZMESSAGETYPE, count(*) FROM ZWAMESSAGE WHERE ZSTANZAID IS NULL OR ZSTANZAID = '' GROUP BY 1")}
    st["length_histogram"] = _hist(con.execute(
        "SELECT length(ZSTANZAID), count(*) FROM ZWAMESSAGE WHERE ZSTANZAID != '' GROUP BY 1 ORDER BY 1"))
    for label, keycols in [("dup_chat_fromme_stanza", "ZCHATSESSION, ZISFROMME, ZSTANZAID"),
                           ("dup_chat_stanza", "ZCHATSESSION, ZSTANZAID"),
                           ("dup_stanza_any_chat", "ZSTANZAID")]:
        groups, rows = con.execute(f"""
            SELECT count(*), coalesce(sum(n), 0) FROM (
              SELECT count(*) n FROM ZWAMESSAGE WHERE ZSTANZAID != ''
              GROUP BY {keycols} HAVING n > 1)""").fetchone()
        st[label] = {"groups": groups, "rows": rows}
    st["dup_chat_fromme_stanza_type_pairs"] = dict(Counter(
        row[0] for row in con.execute("""
            SELECT group_concat(ZMESSAGETYPE, '+') FROM (
              SELECT ZCHATSESSION, ZISFROMME, ZSTANZAID, ZMESSAGETYPE FROM ZWAMESSAGE
              WHERE ZSTANZAID != '' ORDER BY ZMESSAGETYPE)
            GROUP BY ZCHATSESSION, ZISFROMME, ZSTANZAID HAVING count(*) > 1""")).most_common(10))
    r["stanza_ids"] = st

    # Senders in groups: phone vs LID addressing.
    if s.has("ZWAGROUPMEMBER", "ZMEMBERJID"):
        r["group_members_by_jid_kind"] = dict(Counter(
            jid_kind(j) for (j,) in con.execute("SELECT ZMEMBERJID FROM ZWAGROUPMEMBER")))
    if s.has("ZWAMESSAGE", "ZFROMJID"):
        r["message_fromjid_kinds"] = dict(Counter(
            jid_kind(j) for (j,) in con.execute("SELECT ZFROMJID FROM ZWAMESSAGE WHERE ZISFROMME = 0")))

    r["media"] = media_report(con, s, shared)
    r["per_chat_top"] = per_chat(con, shared)

    if s.has("ZWAMEDIAITEM", "ZMETADATA"):
        r["media_metadata_shapes"] = blob_census(con, "SELECT ZMETADATA FROM ZWAMEDIAITEM WHERE ZMETADATA IS NOT NULL")
        r["quotes"] = quote_resolution(con)
    if s.has("ZWAMESSAGEINFO", "ZRECEIPTINFO"):
        r["receipt_info_shapes"] = blob_census(con, "SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZRECEIPTINFO IS NOT NULL")
    return r


def media_report(con, s: Schema, shared: dict[str, int]) -> dict:
    has_thumb = s.has("ZWAMEDIAITEM", "ZTHUMBNAILLOCALPATH")
    rows = con.execute(f"""
        SELECT mi.ZMEDIALOCALPATH p, mi.ZFILESIZE sz, m.ZMESSAGETYPE t,
               {'mi.ZTHUMBNAILLOCALPATH' if has_thumb else 'NULL'} th
        FROM ZWAMEDIAITEM mi LEFT JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE""").fetchall()
    by_type: dict[str, Counter] = defaultdict(Counter)
    match_style, first_component, ext = Counter(), Counter(), Counter()
    referenced: Counter = Counter()
    thumbs_present = 0
    for x in rows:
        c = by_type[type_name(x["t"])]
        c["items"] += 1
        p = x["p"]
        if x["th"] and (MEDIA_PREFIX + x["th"]) in shared:
            thumbs_present += 1
        if not p:
            c["no_local_path"] += 1
            c["no_local_path_expected_bytes"] += x["sz"] or 0
            continue
        referenced[p] += 1
        first_component[p.split("/", 1)[0] if "/" in p else "(none)"] += 1
        ext[p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else "(none)"] += 1
        if MEDIA_PREFIX + p in shared:
            match_style["Message/+path"] += 1
            c["present"] += 1
            c["present_bytes"] += shared[MEDIA_PREFIX + p]
        elif p in shared:
            match_style["path as-is"] += 1
            c["present"] += 1
            c["present_bytes"] += shared[p]
        else:
            c["missing"] += 1
            c["missing_expected_bytes"] += x["sz"] or 0
    referenced_paths = {MEDIA_PREFIX + p for p in referenced} | set(referenced)
    unref = [sz for p, sz in shared.items() if p.startswith(MEDIA_PREFIX) and p not in referenced_paths]
    return {
        "items": len(rows),
        "by_message_type": {k: dict(v) for k, v in sorted(by_type.items())},
        "path_match_style": dict(match_style),
        "path_first_component": dict(first_component.most_common(10)),
        "extensions": dict(ext.most_common(15)),
        "paths_referenced_more_than_once": sum(1 for n in referenced.values() if n > 1),
        "thumbnails_present": thumbs_present,
        "unreferenced_files_under_Message": {"files": len(unref), "bytes": sum(unref)},
        "present_bytes_total": sum(v.get("present_bytes", 0) for v in by_type.values()),
        "missing_expected_bytes_total": sum(v.get("missing_expected_bytes", 0) for v in by_type.values()),
    }


def per_chat(con, shared: dict[str, int], top: int = 15) -> list[dict]:
    stats: dict[int, Counter] = defaultdict(Counter)
    kinds = {pk: jid_kind(j) for pk, j in con.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")}
    for chat, n, dmin, dmax in con.execute(
            "SELECT ZCHATSESSION, count(*), min(ZMESSAGEDATE), max(ZMESSAGEDATE) FROM ZWAMESSAGE GROUP BY 1"):
        stats[chat].update(messages=n)
        stats[chat]["first_year"] = int(_date(dmin)[:4]) if dmin else 0
        stats[chat]["last_year"] = int(_date(dmax)[:4]) if dmax else 0
    for chat, p in con.execute(
            "SELECT m.ZCHATSESSION, mi.ZMEDIALOCALPATH FROM ZWAMEDIAITEM mi JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE"):
        c = stats[chat]
        if not p:
            c["no_path_items"] += 1  # e.g. rows that only carry quote metadata, or never-downloaded media
            continue
        c["media"] += 1
        size = shared.get(MEDIA_PREFIX + p)
        if size is not None:
            c["present"] += 1
            c["present_bytes"] += size
        else:
            c["missing"] += 1
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["present_bytes"], reverse=True)[:top]
    return [{"chat": f"#{i}", "kind": kinds.get(pk, "null"), **dict(c)} for i, (pk, c) in enumerate(ranked, 1)]


def blob_census(con, sql: str, limit: int = 200_000) -> dict:
    counter: Counter = Counter()
    n = 0
    for (blob,) in con.execute(sql):
        if n >= limit:
            break
        n += 1
        if isinstance(blob, bytes):
            protobuf.shape_census(blob, counter)
        else:
            counter["not-bytes"] += 1
    return {"blobs": n, "field_shapes": dict(counter.most_common(40))}


def quote_resolution(con) -> dict:
    """How often ZMETADATA field 5 (quoted stanza id) resolves to a known message."""
    stanzas = {sid for (sid,) in con.execute("SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZSTANZAID != ''")}
    short = {sid[:17] for sid in stanzas}
    total = full = trunc = 0
    for (blob,) in con.execute("SELECT ZMETADATA FROM ZWAMEDIAITEM WHERE ZMETADATA IS NOT NULL"):
        fields = protobuf.try_parse(blob) if isinstance(blob, bytes) else None
        f5 = protobuf.first(fields, 5) if fields else None
        if not f5 or not isinstance(f5.value, bytes):
            continue
        try:
            qid = f5.value.decode()
        except UnicodeDecodeError:
            continue
        total += 1
        full += qid in stanzas
        trunc += qid[:17] in short
    return {"quote_refs": total, "resolved_full_id": full, "resolved_17char_prefix": trunc}


def aux_db_report(path: Path | None, focus: dict[str, list[str]]) -> dict | None:
    if path is None:
        return None
    con = open_db(path)
    try:
        s = Schema(con)
        out = {"tables": {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in s.cols},
               "columns": {t: sorted(s.cols[t]) for t in focus if t in s.cols}}
        for t, cols in focus.items():
            for c in cols:
                if s.has(t, c):
                    out[f"{t}.{c}_non_null"] = con.execute(
                        f'SELECT count(*) FROM "{t}" WHERE "{c}" IS NOT NULL AND "{c}" != \'\'').fetchone()[0]
        if s.has("ZWACDCALLEVENT", "ZCALLIDSTRING"):
            out["call_id_duplicates"] = con.execute(
                "SELECT count(*) FROM (SELECT 1 FROM ZWACDCALLEVENT GROUP BY ZCALLIDSTRING HAVING count(*) > 1)"
            ).fetchone()[0]
        return out
    finally:
        con.close()


def crosscheck(chat_db: Path, contacts_db: Path | None, work: Path, con) -> dict:
    """Run wtsexporter on the decrypted DB and compare per-chat message sets (by Z_PK), counts only."""
    try:
        import Whatsapp_Chat_Exporter  # noqa: F401
    except ImportError:
        return {"skipped": "install with: uv sync --group crosscheck"}
    out_json = work / "wtsexporter.json"
    media = work / "empty-media"
    media.mkdir()
    cmd = [sys.executable, "-m", "Whatsapp_Chat_Exporter", "-i", "-d", str(chat_db), "-m", str(media),
           "-o", str(work / "wtsexporter-out"), "-j", str(out_json), "--no-html"]
    if contacts_db:
        cmd += ["-w", str(contacts_db)]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=work, capture_output=True)  # output discarded: it may contain names
    if proc.returncode != 0 or not out_json.exists():
        # Report only the exception type from its traceback, never the message.
        last = proc.stderr.decode(errors="replace").strip().rsplit("\n", 1)[-1]
        exc = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception))\b", last)
        return {"error": f"wtsexporter exit code {proc.returncode}" + (f" ({exc.group(1)})" if exc else "")}
    theirs = json.loads(out_json.read_text())
    ours: dict[str, dict[str, int]] = defaultdict(dict)
    for jid, pk, t in con.execute("""SELECT s.ZCONTACTJID, m.Z_PK, m.ZMESSAGETYPE FROM ZWAMESSAGE m
                                     JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION"""):
        ours[jid][str(pk)] = t
    only_ours_types: Counter = Counter()
    only_theirs = mismatched_chats = 0
    for jid in set(ours) | set(theirs):
        a = ours.get(jid, {})
        b = set((theirs.get(jid) or {}).get("messages", {}) or {})
        missing = set(a) - b
        extra = b - set(a)
        only_ours_types.update(type_name(a[pk]) for pk in missing)
        only_theirs += len(extra)
        mismatched_chats += bool(missing or extra)
    return {
        "seconds": round(time.time() - t0, 1),
        "chats_ours": len(ours), "chats_theirs": len(theirs),
        "chats_common": len(set(ours) & set(theirs)),
        "messages_ours": sum(len(v) for v in ours.values()),
        "messages_theirs": sum(len((c or {}).get("messages", {}) or {}) for c in theirs.values()),
        "chats_with_differences": mismatched_chats,
        "only_in_ours_by_type": dict(only_ours_types.most_common()),
        "only_in_theirs": only_theirs,
    }


def disk_verdict(free: int, new_media: int, margin: int = 2 * GB) -> dict:
    budget = free - margin
    return {
        "free_bytes": free,
        "media_bytes_in_backup": new_media,
        "margin_bytes": margin,
        "estimated_batches": (math.ceil(new_media / budget) if budget > 0 else None) if new_media else 0,
    }


def run_spike(source: BackupSource, tmp_dir: Path, free_bytes: int, do_crosscheck: bool = False,
              timings: dict | None = None) -> dict:
    report: dict = {"backup": {k: (str(v) if v is not None else None) for k, v in asdict(source.info).items()
                               if k not in ("path", "device_name")},
                    "timings_s": dict(timings or {})}
    t0 = time.time()
    census, shared, dbs = manifest_census(source)
    report["manifest"] = census
    report["timings_s"]["manifest_scan"] = round(time.time() - t0, 1)

    work = Path(tempfile.mkdtemp(prefix="spike-", dir=tmp_dir))
    try:
        t0 = time.time()
        local: dict[str, Path] = {}
        for name, (domain, rel) in dbs.items():
            dest = work / name
            source.extract(domain, rel, dest)
            local[name] = dest
        report["timings_s"]["extract_databases"] = round(time.time() - t0, 1)
        if CHAT_DB not in local:
            report["error"] = "ChatStorage.sqlite not found in backup"
            return report
        con = open_db(local[CHAT_DB])
        try:
            report["integrity_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
            t0 = time.time()
            report["chatstorage"] = chat_db_report(con, shared)
            report["timings_s"]["analyse"] = round(time.time() - t0, 1)
            if do_crosscheck:
                report["crosscheck_wtsexporter"] = crosscheck(local[CHAT_DB], local.get(CONTACTS_DB), work, con)
        finally:
            con.close()
        report["contacts"] = aux_db_report(local.get(CONTACTS_DB), {
            "ZWAADDRESSBOOKCONTACT": ["ZWHATSAPPID", "ZFULLNAME", "ZPHONENUMBER", "ZLID"]})
        report["calls"] = aux_db_report(local.get(CALLS_DB), {
            "ZWACDCALLEVENT": ["ZCALLIDSTRING", "ZGROUPJIDSTRING"], "ZWACDCALLEVENTPARTICIPANT": ["ZJIDSTRING"]})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["disk"] = disk_verdict(free_bytes, report["chatstorage"]["media"]["present_bytes_total"])
    return report
