"""Incremental, append-only ingest of one backup into the archive.

Batch 1 writes every message/contact/call/reaction row plus as much new media
as fits on disk. Further batches add more media; between batches we publish,
wait for iCloud to upload, hash-verify, then evict the uploaded blobs to free
space. Every batch is one transaction followed by a publish, so a crash resumes
from the last published batch. Re-running is always safe: known rows are
skipped and existing blobs are never rewritten.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .backup import CALLS_DB, CHAT_DB, CONTACTS_DB, MEDIA_PREFIX, SHARED_DOMAIN, BackupSource
from .cloud import CloudState
from .config import Paths, private_dir, sweep_tmp
from .spike import manifest_census
from .store import ArchiveError, BlobStore, Store, clean_ext, now_iso, run_lock, sha256_file
from .whatsapp_reader import (EDITABLE_CAPTION_TYPES, EDITABLE_TEXT_TYPES, BackupData, Message,
                              equivalent_jids, load_backup)

log = logging.getLogger("wa_archive.ingest")
GB = 1024 ** 3


class UploadTimeout(ArchiveError):
    pass


@dataclass
class Options:
    margin: int = 2 * GB
    upload_timeout: float = 6 * 3600
    poll: float = 15
    dry_run: bool = False


@dataclass
class Item:
    kind: str           # 'media' or 'extra'
    path: str           # relative to Message/
    size: int
    msg_key: str | None
    order: tuple


def message_key(chat_jid: str, m: Message) -> str:
    if m.stanza:
        basis = f"{chat_jid}|{m.from_me}|{m.stanza}"
    else:
        basis = f"sys|{chat_jid}|{m.ts}|{m.type}|{m.group_event}"
    return hashlib.sha256(basis.encode()).hexdigest()


def fts_body(text: str | None, title: str | None) -> str:
    return "\n".join(x for x in (text, title) if x)


class Ingestor:
    def __init__(self, paths: Paths, source: BackupSource, cloud, *, options: Options | None = None,
                 free_space: Callable[[], int] | None = None, progress=None,
                 after_batch: Callable[[int], None] | None = None):
        self.paths = paths
        self.source = source
        self.cloud = cloud
        self.opt = options or Options()
        self.store = Store(paths, cloud)
        self.blobs = BlobStore(paths.archive_dir)
        self.free_space = free_space or (lambda: shutil.disk_usage(_existing_parent(paths.archive_dir)).free)
        self.say = progress or (lambda msg: None)
        self.after_batch = after_batch
        self.stats: Counter = Counter({k: 0 for k in (
            "messages_new", "messages_edited", "messages_revoked", "reactions_new", "calls_new", "chats_new",
            "chats_merged", "media_archived", "extra_files_archived", "blobs_new", "bytes_new",
            "blobs_deduplicated", "media_unavailable", "extract_errors", "evicted")})

    # ------------------------------------------------------------------ main

    def run(self) -> dict:
        sync = self.store.sync()
        con = self.store.open()
        if sync == "needs-publish":
            self.say("Publishing changes left from an interrupted run...")
            self.store.publish(con, None, snapshot_previous=False)
        work = Path(tempfile.mkdtemp(prefix="ingest-", dir=private_dir(self.paths.tmp_dir)))
        try:
            return self._run(con, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            con.close()

    def _run(self, con, work: Path) -> dict:
        info = self.source.info
        self.say("Reading backup manifest...")
        _, shared, dbs = manifest_census(self.source)
        if CHAT_DB not in dbs:
            raise ArchiveError("ChatStorage.sqlite not found in this backup.")
        local = {}
        for name, (domain, rel) in dbs.items():
            local[name] = work / name
            self.source.extract(domain, rel, local[name])
        fingerprint = hashlib.sha256("|".join([
            info.backup_id, str(info.last_backup), sha256_file(local[CHAT_DB]),
            sha256_file(local[CHAT_DB + "-wal"]) if CHAT_DB + "-wal" in local else ""]).encode()).hexdigest()
        self.say("Reading WhatsApp databases...")
        data = load_backup(local[CHAT_DB], local.get(CONTACTS_DB), local.get(CALLS_DB))
        db_reserve = 3 * local[CHAT_DB].stat().st_size

        con.execute("BEGIN")
        run_id, resumed, batches_done = self._start_run(con, fingerprint)
        self.run_id = run_id
        self.say(("Resuming" if resumed else "Starting") + f" run {run_id}; merging rows...")
        items = self._merge_rows(con, data, shared)
        items.sort(key=lambda it: it.order)

        total = sum(it.size for it in items)
        budget = self.free_space() - self.opt.margin - db_reserve
        largest = max((it.size for it in items), default=0)
        n_batches = _count_batches([it.size for it in items], budget)
        plan = {"run_id": run_id, "resumed": resumed, "new_files": len(items), "new_bytes": total,
                "free_bytes": self.free_space(), "budget_bytes": budget, "batches": n_batches}
        self.stats.update({k: v for k, v in plan.items() if isinstance(v, int) and not isinstance(v, bool)})
        evictable = sum(size for _, size in self._evictable(con))
        if largest > budget + evictable:
            con.execute("ROLLBACK")
            raise ArchiveError(f"A single media file ({largest:,} bytes) is larger than the usable free space "
                               f"({max(budget, 0):,} bytes). Free up disk space and re-run.")
        if n_batches > 1 and self.cloud.state(self.paths.archive_dir) == CloudState.NOT_CLOUD:
            con.execute("ROLLBACK")
            raise ArchiveError("New media doesn't fit on disk and the archive folder isn't in iCloud Drive, "
                               "so it can't be evicted between batches. Free up space or move the archive.")
        if self.opt.dry_run:
            con.execute("ROLLBACK")
            return {"dry_run": True, **plan, **dict(self.stats)}

        pos, batch, first = 0, batches_done, True
        while True:
            if not first:
                con.execute("BEGIN")
            end = self._pick(con, items, pos, db_reserve)
            new_blobs: list[tuple[str, Path]] = []
            tick = time.monotonic()
            for i, it in enumerate(items[pos:end], 1):
                self._archive_item(con, it, work, new_blobs)
                if time.monotonic() - tick > 30:
                    self.say(f"Batch {batch + 1}: {i:,}/{end - pos:,} files archived...")
                    tick = time.monotonic()
            self._verify(con, new_blobs)
            batch += 1
            done = end >= len(items)
            con.execute("UPDATE runs SET batches_done = ? WHERE run_id = ?", (batch, run_id))
            if done:
                con.execute("UPDATE runs SET status = 'complete', finished_at = ?, stats_json = ? WHERE run_id = ?",
                            (now_iso(), _json(self.stats), run_id))
            con.execute("COMMIT")
            self.store.publish(con, run_id, snapshot_previous=first)
            self.say(f"Batch {batch} published ({end - pos} files).")
            if self.after_batch:
                self.after_batch(batch)
            if done:
                break
            self._wait_uploads([self.store.archive_db, self.store.archive_json] + [p for _, p in new_blobs])
            for _, p in new_blobs:
                if self.cloud.evict(p):
                    self.stats["evicted"] += 1
            pos, first = end, False
        return {"run_id": run_id, "resumed": resumed, **dict(self.stats)}

    # ------------------------------------------------------------------ runs

    def _start_run(self, con, fingerprint: str) -> tuple[str, bool, int]:
        row = con.execute("SELECT run_id, batches_done FROM runs WHERE fingerprint = ? AND status = 'running' "
                          "ORDER BY rowid DESC LIMIT 1", (fingerprint,)).fetchone()
        if row:
            return row["run_id"], True, row["batches_done"] or 0
        # An unfinished run of a *different* backup is superseded by this one.
        con.execute("UPDATE runs SET status = 'abandoned' WHERE status = 'running'")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        while con.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
            run_id += "x"
        info = self.source.info
        con.execute("""INSERT INTO runs (run_id, started_at, status, backup_id, backup_date, ios_version,
                       whatsapp_version, fingerprint) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)""",
                    (run_id, now_iso(), info.backup_id, info.last_backup.isoformat() if info.last_backup else None,
                     info.ios_version, info.whatsapp_version, fingerprint))
        return run_id, False, 0

    # ------------------------------------------------------------------ rows

    def _merge_rows(self, con, data: BackupData, shared: dict[str, int]) -> list[Item]:
        run = self.run_id
        s = self.stats

        # Chats and aliases (phone <-> LID spellings of the same person share one chat).
        alias = {r[0]: r[1] for r in con.execute("SELECT jid, chat_id FROM chat_aliases")}
        chat_of: dict[str, int] = {}
        for sess in data.sessions:
            cands = [sess.jid, *equivalent_jids(sess.jid, data.lid_map)]
            ids = sorted({alias[c] for c in cands if c in alias})
            if ids:
                cid = ids[0]
                for other in ids[1:]:
                    con.execute("UPDATE chat_aliases SET chat_id = ? WHERE chat_id = ?", (cid, other))
                    con.execute("UPDATE chats SET merged_into = ? WHERE chat_id = ?", (cid, other))
                    con.execute("INSERT INTO chat_merges VALUES (?, ?, ?)", (other, cid, run))
                    alias = {j: (cid if c == other else c) for j, c in alias.items()}
                    s["chats_merged"] += 1
                con.execute("UPDATE chats SET last_seen_run = ? WHERE chat_id = ?", (run, cid))
            else:
                cid = con.execute("INSERT INTO chats (kind, first_seen_run, last_seen_run) VALUES (?, ?, ?)",
                                  (sess.kind, run, run)).lastrowid
                s["chats_new"] += 1
            for c in cands:
                if c not in alias:
                    con.execute("INSERT INTO chat_aliases VALUES (?, ?, ?, ?)",
                                (c, cid, "session" if c == sess.jid else "lid-map", run))
                    alias[c] = cid
        for sess in data.sessions:
            chat_of[sess.jid] = alias[sess.jid]

        for jid, name, source in set(data.names):
            con.execute("""INSERT INTO names VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT (jid, source, name) DO UPDATE SET last_seen_run = excluded.last_seen_run""",
                        (jid, name, source, run, run))

        # Existing messages, indexed by canonical chat.
        index: dict[tuple, str] = {}
        state: dict[str, list] = {}
        for r in con.execute("""SELECT m.msg_key, m.from_me, m.stanza_id, a.chat_id, m.type, m.text, m.title,
                                       m.revoked_run FROM messages m JOIN chat_aliases a ON a.jid = m.chat_jid"""):
            if r["stanza_id"]:
                index[(r["chat_id"], r["from_me"], r["stanza_id"])] = r["msg_key"]
            state[r["msg_key"]] = [r["type"], r["text"], r["title"], r["revoked_run"]]
        for r in con.execute("SELECT msg_key, text, title FROM message_versions WHERE kind = 'edit' ORDER BY id"):
            if r["msg_key"] in state:
                state[r["msg_key"]][1:3] = [r["text"], r["title"]]
        media_state = {r[0]: (r[1], r[2]) for r in con.execute("SELECT msg_key, status, thumb_path FROM media")}

        items: list[Item] = []
        main_paths: set[str] = set()
        for m in data.messages:
            cid = chat_of[m.chat_jid]
            key = index.get((cid, m.from_me, m.stanza)) if m.stanza else None
            if key is None:
                key = message_key(m.chat_jid, m)
            if key not in state:
                con.execute("""INSERT INTO messages (msg_key, chat_jid, stanza_id, from_me, sender_jid, ts, sent_ts,
                               type, group_event, text, title, quoted_stanza, quoted_jid, raw_json, first_seen_run,
                               last_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (key, m.chat_jid, m.stanza, m.from_me, m.sender_jid, m.ts, m.sent_ts, m.type,
                             m.group_event, m.text, m.title, m.quoted_stanza, m.quoted_jid, m.raw_json, run, run))
                if body := fts_body(m.text, m.title):
                    con.execute("INSERT INTO fts (msg_key, body) VALUES (?, ?)", (key, body))
                state[key] = [m.type, m.text, m.title, None]
                if m.stanza:
                    index[(cid, m.from_me, m.stanza)] = key
                s["messages_new"] += 1
            else:
                con.execute("UPDATE messages SET last_seen_run = ? WHERE msg_key = ?", (run, key))
                self._edit_or_revoke(con, key, m, state[key])
            for r in m.reactions:
                cur = con.execute("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?, ?, ?)",
                                  (key, r.reaction_id, r.reactor_jid, r.emoji, r.ts, run))
                s["reactions_new"] += cur.rowcount
            if m.media:
                items.extend(self._media_row(con, key, m, cid, shared, media_state.get(key)))
                if m.media.path:
                    main_paths.add(m.media.path)

        for c in data.calls:
            cur = con.execute("INSERT OR IGNORE INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (c["call_id"], c["ts"], c["duration"], c["group_jid"], c["creator_jid"],
                               _json(c["participants"]), _json(c["raw"]), run))
            s["calls_new"] += cur.rowcount

        # Everything else under Message/ (thumbnails, link previews): archive by path.
        known_extra = {r[0] for r in con.execute("SELECT path FROM extra_files")}
        for full, size in shared.items():
            if not full.startswith(MEDIA_PREFIX):
                continue
            rel = full[len(MEDIA_PREFIX):]
            if rel in main_paths:
                continue
            if rel in known_extra:
                con.execute("UPDATE extra_files SET last_in_backup_run = ? WHERE path = ?", (run, rel))
            else:
                items.append(Item("extra", rel, size, None, (0, 0, rel)))
        return items

    def _edit_or_revoke(self, con, key: str, m: Message, st: list) -> None:
        old_type, cur_text, cur_title, revoked = st
        if m.type == 14 and old_type != 14:
            if revoked is None:
                con.execute("UPDATE messages SET revoked_run = ? WHERE msg_key = ?", (self.run_id, key))
                con.execute("INSERT INTO message_versions (msg_key, run_id, kind) VALUES (?, ?, 'revoke')",
                            (key, self.run_id))
                st[3] = self.run_id
                self.stats["messages_revoked"] += 1
            return
        if revoked is not None or m.type != old_type:
            return
        text_changed = m.type in EDITABLE_TEXT_TYPES and m.text is not None and m.text != cur_text
        title_changed = m.type in EDITABLE_CAPTION_TYPES and m.title is not None and m.title != cur_title
        if text_changed or title_changed:
            con.execute("INSERT INTO message_versions (msg_key, run_id, kind, text, title) VALUES (?, ?, 'edit', ?, ?)",
                        (key, self.run_id, m.text, m.title))
            con.execute("UPDATE messages SET edited = 1 WHERE msg_key = ?", (key,))
            if body := fts_body(m.text, m.title):
                con.execute("INSERT INTO fts (msg_key, body) VALUES (?, ?)", (key, body))
            st[1:3] = [m.text, m.title]
            self.stats["messages_edited"] += 1

    def _media_row(self, con, key: str, m: Message, cid: int, shared: dict[str, int], row) -> list[Item]:
        md = m.media
        in_backup = bool(md.path) and (MEDIA_PREFIX + md.path) in shared
        if row is None:
            con.execute("""INSERT INTO media (msg_key, status, size_expected, orig_path, thumb_path, last_in_backup_run)
                           VALUES (?, 'missing', ?, ?, ?, ?)""",
                        (key, md.size, md.path, md.thumb_path, self.run_id if in_backup else None))
            status = "missing"
        else:
            status, thumb = row
            if in_backup:
                con.execute("UPDATE media SET last_in_backup_run = ? WHERE msg_key = ?", (self.run_id, key))
            if md.thumb_path and thumb is None:
                con.execute("UPDATE media SET thumb_path = ? WHERE msg_key = ?", (md.thumb_path, key))
        if in_backup and status == "missing":
            return [Item("media", md.path, shared[MEDIA_PREFIX + md.path], key, (1, cid, m.ts or 0))]
        if not in_backup and status == "missing":
            self.stats["media_unavailable"] += 1
        return []

    # ------------------------------------------------------------------ files

    def _evictable(self, con) -> list[tuple[Path, int]]:
        """Archived blobs that are verified, uploaded and still local: safe to evict to make room."""
        out = []
        for r in con.execute("SELECT sha256, ext, size FROM blobs WHERE verified_run IS NOT NULL"):
            p = self.blobs.path(r["sha256"], r["ext"])
            if self.cloud.state(p) == CloudState.SYNCED:
                out.append((p, r["size"]))
        return out

    def _pick(self, con, items: list[Item], pos: int, db_reserve: int) -> int:
        """End index of the next batch that fits in free space, evicting uploaded blobs if needed."""
        if pos >= len(items):
            return pos
        for attempt in range(30):
            if attempt == 1:
                freed = [p for p, _ in self._evictable(con) if self.cloud.evict(p)]
                self.stats["evicted"] += len(freed)
                if freed:
                    self.say(f"Evicted {len(freed)} uploaded file(s) to make room.")
            budget = self.free_space() - self.opt.margin - db_reserve
            end, used = pos, 0
            while end < len(items) and used + items[end].size <= budget:
                used += items[end].size
                end += 1
            if end > pos:
                return end
            time.sleep(min(self.opt.poll, 10))
        raise ArchiveError("Not enough free disk space for the next media file, even after evicting uploaded files.")

    def _archive_item(self, con, it: Item, work: Path, new_blobs: list) -> None:
        tmp = work / "extract.tmp"
        try:
            self.source.extract(SHARED_DOMAIN, MEDIA_PREFIX + it.path, tmp)
        except Exception as e:  # one unreadable file must not stop the run
            log.warning("could not extract one file (%s); it stays recorded as missing", type(e).__name__)
            self.stats["extract_errors"] += 1
            tmp.unlink(missing_ok=True)
            return
        sha, size = sha256_file(tmp), tmp.stat().st_size
        row = con.execute("SELECT ext FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        if row is None:
            dest = self.blobs.place(tmp, sha, clean_ext(it.path))
            con.execute("INSERT INTO blobs (sha256, size, ext, first_run) VALUES (?, ?, ?, ?)",
                        (sha, size, clean_ext(it.path), self.run_id))
            new_blobs.append((sha, dest))
            self.stats["blobs_new"] += 1
            self.stats["bytes_new"] += size
        else:
            dest = self.blobs.path(sha, row["ext"])
            if self.cloud.state(dest) == CloudState.MISSING:  # self-heal a blob file lost from disk
                self.blobs.place(tmp, sha, row["ext"])
                new_blobs.append((sha, dest))
            else:
                tmp.unlink()
            self.stats["blobs_deduplicated"] += 1
        if it.kind == "media":
            con.execute("""UPDATE media SET status = 'present', sha256 = ?, first_present_run = ?
                           WHERE msg_key = ? AND status = 'missing'""", (sha, self.run_id, it.msg_key))
            self.stats["media_archived"] += 1
        else:
            con.execute("INSERT OR IGNORE INTO extra_files VALUES (?, ?, ?, ?, ?)",
                        (it.path, sha, size, self.run_id, self.run_id))
            self.stats["extra_files_archived"] += 1

    def _verify(self, con, new_blobs: list[tuple[str, Path]]) -> None:
        for sha, path in new_blobs:
            if sha256_file(path) != sha:
                raise ArchiveError("A freshly written media file failed its hash check; disk problem? Aborting batch.")
            con.execute("UPDATE blobs SET verified_run = ?, verified_at = ? WHERE sha256 = ?",
                        (self.run_id, now_iso(), sha))

    def _wait_uploads(self, files: list[Path]) -> None:
        deadline = time.monotonic() + self.opt.upload_timeout
        last = 0.0
        while True:
            pending = [p for p in files if not self.cloud.state(p).uploaded]
            if not pending:
                return
            if any(self.cloud.state(p) == CloudState.NOT_CLOUD for p in pending):
                raise ArchiveError("Archive files aren't in iCloud Drive, so they can't be evicted.")
            if time.monotonic() > deadline:
                raise UploadTimeout(f"{len(pending)} file(s) still uploading to iCloud. Progress is saved; "
                                    "re-run `wa-archive ingest` later to continue.")
            if time.monotonic() - last > 60:
                self.say(f"Waiting for iCloud upload: {len(pending)} file(s) pending...")
                last = time.monotonic()
            time.sleep(self.opt.poll)


def _count_batches(sizes: list[int], budget: int) -> int:
    if not sizes:
        return 1
    if budget <= 0:
        return 0
    n, used = 1, 0
    for sz in sizes:
        if used + sz > budget and used > 0:
            n, used = n + 1, 0
        used += sz
    return n


def _existing_parent(p: Path) -> Path:
    while not p.exists():
        p = p.parent
    return p


def _json(v) -> str:
    import json
    return json.dumps(v, sort_keys=True, default=str)


def evict_uploaded(paths: Paths, cloud) -> tuple[int, int]:
    """Evict every archived blob that is hash-verified and fully uploaded. Returns (files, bytes)."""
    with run_lock(paths):
        store = Store(paths, cloud)
        store.sync()
        con = store.open()
        try:
            ing = Ingestor.__new__(Ingestor)
            ing.cloud, ing.blobs = cloud, BlobStore(paths.archive_dir)
            done = [(p, size) for p, size in ing._evictable(con) if cloud.evict(p)]
        finally:
            con.close()
    return len(done), sum(size for _, size in done)


def run_ingest(paths: Paths, open_source: Callable[[], BackupSource], cloud, **kw) -> dict:
    """Lock, clear stale temp files, open the backup, ingest, close."""
    with run_lock(paths):
        sweep_tmp(private_dir(paths.tmp_dir))
        source = open_source()
        try:
            return Ingestor(paths, source, cloud, **kw).run()
        finally:
            source.close()
