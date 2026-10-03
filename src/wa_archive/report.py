"""The verification gate: which chats are safe to clear media for on the phone.

A chat is safe only when every media file the latest backup holds for it is in
the archive, hash-verified (now, or earlier and since evicted by iCloud), and
fully uploaded to iCloud, and the published archive DB itself is uploaded.
Reports contain chat names and counts only, never message contents.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .cloud import CloudState, conflict_copies
from .config import Paths, private_dir
from .store import BlobStore, Store, atomic_place, sha256_file, write_json_atomic
from .whatsapp_reader import HIDDEN_KINDS

NAME_PRIORITY = {"contact": 0, "chat": 1, "push": 2, "member": 3}


@dataclass
class ChatRow:
    chat_id: int
    name: str
    kind: str
    messages: int = 0
    messages_new: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    not_in_latest: int = 0
    media_present: int = 0
    media_missing: int = 0
    media_new: int = 0
    media_in_latest_backup: int = 0
    never_available: int = 0
    archived_bytes: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def hidden(self) -> bool:
        return self.kind in HIDDEN_KINDS

    @property
    def safe(self) -> bool:
        return not self.reasons


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "-"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def chat_names(con) -> dict[int, str]:
    """Best display name per chat: address-book name, then chat/group name, push name, member name."""
    best: dict[int, tuple[int, str]] = {}
    for r in con.execute("""SELECT a.chat_id, n.name, n.source FROM names n JOIN chat_aliases a ON a.jid = n.jid
                            ORDER BY n.last_seen_run DESC"""):
        prio = NAME_PRIORITY.get(r["source"], 9)
        if r["chat_id"] not in best or prio < best[r["chat_id"]][0]:  # most recent wins within a priority
            best[r["chat_id"]] = (prio, r["name"])
    out = {cid: name for cid, (_, name) in best.items()}
    for r in con.execute("SELECT chat_id, min(jid) jid FROM chat_aliases GROUP BY chat_id"):
        out.setdefault(r["chat_id"], r["jid"].split("@", 1)[0])
    return out


def pending_uploads(store: Store, con, cloud) -> list[Path]:
    blobs = BlobStore(store.paths.archive_dir)
    paths = [store.archive_db, store.archive_json]
    paths += [blobs.path(r["sha256"], r["ext"]) for r in con.execute("SELECT sha256, ext FROM blobs")]
    return [p for p in paths if cloud.state(p) == CloudState.PENDING]


def wait_for_uploads(store: Store, con, cloud, timeout: float, poll: float = 15, say=print) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        pending = pending_uploads(store, con, cloud)
        if not pending:
            return True
        if time.monotonic() > deadline:
            return False
        say(f"Waiting for iCloud: {len(pending)} file(s) still uploading...")
        time.sleep(poll)


def build_report(paths: Paths, cloud, *, quick: bool = False, max_age_hours: float = 6,
                 now: datetime | None = None, progress=None) -> dict:
    now = now or datetime.now(timezone.utc)
    store = Store(paths, cloud)
    sync = store.sync()
    con = store.open()
    blobs = BlobStore(paths.archive_dir)
    blockers: list[str] = []
    warnings: list[str] = []
    try:
        run = con.execute("SELECT * FROM runs WHERE status != 'abandoned' ORDER BY rowid DESC LIMIT 1").fetchone()
        if run is None:
            return {"error": "The archive is empty. Run `wa-archive ingest` first."}
        R = run["run_id"]
        backup_date = datetime.fromisoformat(run["backup_date"]) if run["backup_date"] else None
        age_h = (now - backup_date).total_seconds() / 3600 if backup_date else None

        if run["status"] != "complete":
            blockers.append(f"The latest ingest ({R}) is unfinished. Re-run `wa-archive ingest` to complete it.")
        if sync == "needs-publish":
            blockers.append("Local changes are not yet published to the archive folder. Re-run `wa-archive ingest`.")
        for p, label in ((store.archive_db, "archive.sqlite"), (store.archive_json, "archive.json")):
            st = cloud.state(p)
            if st == CloudState.PENDING:
                blockers.append(f"{label} is still uploading to iCloud.")
            elif st == CloudState.NOT_CLOUD:
                blockers.append(f"{label} is not in iCloud Drive, so there is no off-Mac copy.")
            elif st == CloudState.MISSING:
                blockers.append(f"{label} is missing from the archive folder.")
        if conflicts := conflict_copies(paths.archive_dir):
            warnings.append(f"iCloud conflict copies found: {', '.join(p.name for p in conflicts)}. "
                            "Don't edit them; tell the maintainer.")
        if age_h is None or age_h > max_age_hours:
            warnings.append(f"The backup is {age_h:.0f} hours old (more than {max_age_hours:g}). Anything received "
                            "since then is NOT archived, but Manage Storage will still clear it. Take a fresh Finder "
                            "backup and re-run `wa-archive ingest` before clearing."
                            if age_h is not None else "The backup date is unknown.")

        # ---- verify blobs
        blob_state: dict[str, str] = {}
        rows = con.execute("SELECT sha256, ext, size, verified_run FROM blobs").fetchall()
        hashed = 0
        for i, b in enumerate(rows):
            path = blobs.path(b["sha256"], b["ext"])
            st = cloud.state(path)
            if st == CloudState.MISSING:
                blob_state[b["sha256"]] = "missing blob"
            elif st.local and not quick:
                if progress and i % 500 == 0:
                    progress(i, len(rows))
                ok = sha256_file(path) == b["sha256"]
                hashed += 1
                blob_state[b["sha256"]] = ("pending upload" if st == CloudState.PENDING else
                                           "not in iCloud" if st == CloudState.NOT_CLOUD else "ok") \
                    if ok else "hash mismatch"
            elif st.local:  # --quick
                blob_state[b["sha256"]] = "unverified" if b["verified_run"] is None else (
                    "pending upload" if st == CloudState.PENDING else
                    "not in iCloud" if st == CloudState.NOT_CLOUD else "ok")
            else:  # evicted: trust an earlier local verification
                blob_state[b["sha256"]] = "ok" if b["verified_run"] else "unverified"
        if quick:
            blockers.append("--quick skips re-hashing; run without it to get a safe-to-clear list.")

        # ---- per chat
        names = chat_names(con)
        chats: dict[int, ChatRow] = {}
        for r in con.execute("SELECT chat_id, kind FROM chats WHERE merged_into IS NULL"):
            chats[r["chat_id"]] = ChatRow(r["chat_id"], names.get(r["chat_id"], "?"), r["kind"])
        for r in con.execute("""
                SELECT a.chat_id, count(*) n, sum(m.first_seen_run = :R) new, min(m.ts) t0, max(m.ts) t1,
                       sum(m.last_seen_run != :R) gone
                FROM messages m JOIN chat_aliases a ON a.jid = m.chat_jid GROUP BY a.chat_id""", {"R": R}):
            c = chats.get(r["chat_id"])
            if c:
                c.messages, c.messages_new, c.first_ts, c.last_ts, c.not_in_latest = \
                    r["n"], r["new"], r["t0"], r["t1"], r["gone"]
        seen_blob: dict[int, set] = defaultdict(set)
        sizes = {b["sha256"]: b["size"] for b in rows}
        for r in con.execute("""SELECT a.chat_id, md.status, md.sha256, md.first_present_run, md.last_in_backup_run
                                FROM media md JOIN messages m ON m.msg_key = md.msg_key
                                JOIN chat_aliases a ON a.jid = m.chat_jid"""):
            c = chats.get(r["chat_id"])
            if c is None:
                continue
            in_latest = r["last_in_backup_run"] == R
            c.media_in_latest_backup += in_latest
            if r["status"] == "present":
                c.media_present += 1
                c.media_new += r["first_present_run"] == R
                if r["sha256"] not in seen_blob[c.chat_id]:
                    seen_blob[c.chat_id].add(r["sha256"])
                    c.archived_bytes += sizes.get(r["sha256"], 0)
                reason = blob_state.get(r["sha256"], "missing blob")
            else:
                c.media_missing += 1
                if in_latest:
                    reason = "not archived yet"
                else:
                    c.never_available += 1
                    reason = "ok"
            if in_latest and reason != "ok":
                c.reasons[reason] = c.reasons.get(reason, 0) + 1
        for r in con.execute("""SELECT a.chat_id, count(*) n FROM (
                                  SELECT a.chat_id, m.stanza_id FROM messages m JOIN chat_aliases a ON a.jid = m.chat_jid
                                  WHERE m.stanza_id IS NOT NULL GROUP BY a.chat_id, m.stanza_id
                                  HAVING count(DISTINCT m.msg_key) > 1) a GROUP BY a.chat_id"""):
            if c := chats.get(r["chat_id"]):
                c.reasons["stanza collision"] = r["n"]
        if blockers:
            for c in chats.values():
                c.reasons.setdefault("archive not ready", 1)

        rows_out = sorted(chats.values(), key=lambda c: (c.hidden, -c.archived_bytes, c.name.lower()))
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "run_id": R,
            "backup_id": run["backup_id"],
            "backup_date": backup_date.isoformat() if backup_date else None,
            "backup_age_hours": round(age_h, 1) if age_h is not None else None,
            "blockers": blockers,
            "warnings": warnings,
            "blobs_rehashed": hashed,
            "totals": {
                "chats": len(chats),
                "messages": sum(c.messages for c in chats.values()),
                "messages_new": sum(c.messages_new for c in chats.values()),
                "media_present": sum(c.media_present for c in chats.values()),
                "media_missing": sum(c.media_missing for c in chats.values()),
                "media_new": sum(c.media_new for c in chats.values()),
                "archived_bytes": sum(b["size"] for b in rows),
                "blob_problems": {k: sum(1 for v in blob_state.values() if v == k)
                                  for k in set(blob_state.values()) if k != "ok"},
            },
            "chats": [_chat_dict(c) for c in rows_out],
        }
    finally:
        con.close()


def _chat_dict(c: ChatRow) -> dict:
    return {"chat_id": c.chat_id, "name": c.name, "kind": c.kind, "hidden": c.hidden,
            "messages": c.messages, "messages_new": c.messages_new,
            "first": _fmt_ts(c.first_ts), "last": _fmt_ts(c.last_ts), "not_in_latest_backup": c.not_in_latest,
            "media_present": c.media_present, "media_missing": c.media_missing, "media_new": c.media_new,
            "media_in_latest_backup": c.media_in_latest_backup, "never_available": c.never_available,
            "archived_bytes": c.archived_bytes, "safe": c.safe, "reasons": c.reasons}


def status_label(c: dict) -> str:
    if not c["safe"]:
        return "NOT SAFE: " + ", ".join(f"{k} ({v})" for k, v in c["reasons"].items())
    if c["media_in_latest_backup"] == 0:
        return "safe (no media on phone)"
    if c["never_available"]:
        return f"safe; {c['never_available']} item(s) were never available"
    return "safe"


def to_markdown(rep: dict) -> str:
    t = rep["totals"]
    lines = [f"# WhatsApp archive report: run {rep['run_id']}", "",
             f"**Backup taken:** {rep['backup_date']} ({rep['backup_age_hours']} h before this report)", ""]
    lines += [f"> **BLOCKER:** {b}" for b in rep["blockers"]]
    lines += [f"> **WARNING:** {w}" for w in rep["warnings"]]
    lines += ["", "Media received after the backup is not archived; Manage Storage clears it anyway.", "",
              f"Totals: {t['chats']} chats, {t['messages']:,} messages ({t['messages_new']:,} new), "
              f"{t['media_present']:,} media archived ({t['media_new']:,} new), {t['media_missing']:,} missing, "
              f"{human(t['archived_bytes'])} archived.", "", "## Safe to clear media", ""]
    safe = [c for c in rep["chats"] if c["safe"] and c["media_in_latest_backup"]]
    lines += [f"- {c['name']} ({c['kind']}): {status_label(c)}" for c in safe] or ["(none)"]
    lines += ["", "## All chats", "",
              "| Chat | Kind | Messages | New | From | To | Media present/missing/new | Archived | Status |",
              "|---|---|---|---|---|---|---|---|---|"]
    for c in rep["chats"]:
        lines.append(f"| {c['name'].replace('|', '/')} | {c['kind']} | {c['messages']:,} | {c['messages_new']:,} | "
                     f"{c['first']} | {c['last']} | {c['media_present']}/{c['media_missing']}/{c['media_new']} | "
                     f"{human(c['archived_bytes'])} | {status_label(c)} |")
    return "\n".join(lines) + "\n"


def save_report(rep: dict, paths: Paths) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = paths.archive_dir / "reports" / f"{stamp}-run{rep['run_id']}"
    tmp_dir = private_dir(paths.tmp_dir)
    md_tmp = tmp_dir / "report.md.tmp"
    md_tmp.write_text(to_markdown(rep))
    os.chmod(md_tmp, 0o600)
    atomic_place(md_tmp, base.with_suffix(".md"))
    write_json_atomic(rep, base.with_suffix(".json"), tmp_dir)
    return base.with_suffix(".md"), base.with_suffix(".json")


def dumps(rep: dict) -> str:
    return json.dumps(rep, indent=2)
