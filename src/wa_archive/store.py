"""The archive database: schema, append-only guarantees, and cloud-safe publishing.

Ingest only ever writes the local working copy (state_dir/work/archive.sqlite).
Publishing produces a complete snapshot in a local temp file and atomically
renames it into the archive folder, so iCloud never sees a half-written DB.
Old published versions are kept in snapshots/ and never deleted.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Paths, private_dir

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
  backup_id TEXT, backup_date TEXT, ios_version TEXT, whatsapp_version TEXT,
  fingerprint TEXT, batches_done INTEGER DEFAULT 0, stats_json TEXT);
CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, first_seen_run TEXT, last_seen_run TEXT, merged_into INTEGER);
CREATE TABLE IF NOT EXISTS chat_aliases (
  jid TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, source TEXT, first_seen_run TEXT);
CREATE TABLE IF NOT EXISTS chat_merges (from_chat INTEGER, into_chat INTEGER, run_id TEXT);
CREATE TABLE IF NOT EXISTS names (
  jid TEXT, name TEXT, source TEXT, first_seen_run TEXT, last_seen_run TEXT, PRIMARY KEY (jid, source, name));
CREATE TABLE IF NOT EXISTS messages (
  msg_key TEXT PRIMARY KEY, chat_jid TEXT NOT NULL, stanza_id TEXT, from_me INTEGER NOT NULL,
  sender_jid TEXT, ts REAL, sent_ts REAL, type INTEGER, group_event INTEGER,
  text TEXT, title TEXT, quoted_stanza TEXT, quoted_jid TEXT, raw_json TEXT,
  first_seen_run TEXT NOT NULL, last_seen_run TEXT NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0, revoked_run TEXT);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages (chat_jid, ts);
CREATE INDEX IF NOT EXISTS messages_stanza ON messages (stanza_id);
CREATE TABLE IF NOT EXISTS message_versions (
  id INTEGER PRIMARY KEY, msg_key TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT, title TEXT);
CREATE INDEX IF NOT EXISTS message_versions_key ON message_versions (msg_key);
CREATE TABLE IF NOT EXISTS media (
  msg_key TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('present', 'missing')),
  sha256 TEXT, size_expected INTEGER, orig_path TEXT, thumb_path TEXT,
  first_present_run TEXT, last_in_backup_run TEXT);
CREATE INDEX IF NOT EXISTS media_sha ON media (sha256);
CREATE TABLE IF NOT EXISTS extra_files (
  path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER, first_seen_run TEXT, last_in_backup_run TEXT);
CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, ext TEXT, first_run TEXT, verified_run TEXT, verified_at TEXT);
CREATE TABLE IF NOT EXISTS reactions (
  msg_key TEXT NOT NULL, reaction_id TEXT NOT NULL, reactor_jid TEXT, emoji TEXT NOT NULL, ts INTEGER,
  first_seen_run TEXT, PRIMARY KEY (msg_key, reaction_id, emoji));
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY, ts REAL, duration REAL, group_jid TEXT, creator_jid TEXT,
  participants_json TEXT, raw_json TEXT, first_seen_run TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5 (msg_key UNINDEXED, body, tokenize = 'unicode61 remove_diacritics 2');
"""


def _no_delete(table: str) -> str:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'append-only: {table} rows cannot be deleted'); END;")


def _frozen(table: str, cols: str) -> str:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE OF {cols} ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'append-only: {table} content cannot change'); END;")


TRIGGERS = "\n".join([
    *(_no_delete(t) for t in ("messages", "message_versions", "media", "extra_files", "blobs", "reactions",
                              "calls", "chats", "chat_aliases", "chat_merges", "names")),
    _frozen("messages", "msg_key, chat_jid, stanza_id, from_me, sender_jid, ts, sent_ts, type, group_event, "
                        "text, title, quoted_stanza, quoted_jid, raw_json, first_seen_run"),
    _frozen("message_versions", "id, msg_key, run_id, kind, text, title"),
    _frozen("media", "msg_key, size_expected, orig_path"),
    _frozen("extra_files", "path, sha256, size, first_seen_run"),
    _frozen("blobs", "sha256, size, ext, first_run"),
    _frozen("reactions", "msg_key, reaction_id, reactor_jid, emoji, ts, first_seen_run"),
    _frozen("calls", "call_id, ts, duration, group_jid, creator_jid, participants_json, raw_json, first_seen_run"),
    _frozen("names", "jid, name, source, first_seen_run"),
    _frozen("chats", "chat_id, kind, first_seen_run"),
    _frozen("chat_aliases", "jid, source, first_seen_run"),
    """CREATE TRIGGER IF NOT EXISTS messages_edited_once BEFORE UPDATE OF edited ON messages
       WHEN NEW.edited < OLD.edited BEGIN SELECT RAISE(ABORT, 'append-only: edited flag cannot be cleared'); END;""",
    """CREATE TRIGGER IF NOT EXISTS messages_revoked_once BEFORE UPDATE OF revoked_run ON messages
       WHEN OLD.revoked_run IS NOT NULL AND NEW.revoked_run IS NOT OLD.revoked_run
       BEGIN SELECT RAISE(ABORT, 'append-only: revoked_run is set once'); END;""",
    """CREATE TRIGGER IF NOT EXISTS media_never_unpresent BEFORE UPDATE OF status ON media
       WHEN OLD.status = 'present' AND NEW.status != 'present'
       BEGIN SELECT RAISE(ABORT, 'append-only: archived media cannot become missing'); END;""",
    """CREATE TRIGGER IF NOT EXISTS media_sha_once BEFORE UPDATE OF sha256, first_present_run ON media
       WHEN (OLD.sha256 IS NOT NULL AND NEW.sha256 IS NOT OLD.sha256)
         OR (OLD.first_present_run IS NOT NULL AND NEW.first_present_run IS NOT OLD.first_present_run)
       BEGIN SELECT RAISE(ABORT, 'append-only: media hash is set once'); END;""",
    """CREATE TRIGGER IF NOT EXISTS media_thumb_once BEFORE UPDATE OF thumb_path ON media
       WHEN OLD.thumb_path IS NOT NULL AND NEW.thumb_path IS NOT OLD.thumb_path
       BEGIN SELECT RAISE(ABORT, 'append-only: thumbnail path is set once'); END;""",
])


class ArchiveError(RuntimeError):
    pass


def check_schema(version, where: str) -> None:
    """Never let an older wa-archive write to an archive created by a newer one."""
    if version is not None and int(version) > SCHEMA_VERSION:
        raise ArchiveError(f"The {where} was written by a newer version of wa-archive (archive format {version}; "
                           f"this version understands up to {SCHEMA_VERSION}). Upgrade wa-archive and try again.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    try:
        fsync_file(path)
    except OSError:
        pass


def atomic_place(src: Path, dest: Path) -> None:
    """Move a complete file into place with a single rename (copy+rename across volumes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
        shutil.copyfile(src, tmp)
        fsync_file(tmp)
        os.replace(tmp, dest)
        os.unlink(src)  # our own temp file
    fsync_dir(dest.parent)


def write_json_atomic(data: dict, dest: Path, tmp_dir: Path) -> None:
    tmp = tmp_dir / f"{dest.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2, default=str))
    fsync_file(tmp)
    atomic_place(tmp, dest)


@contextmanager
def run_lock(paths: Paths):
    private_dir(paths.state_dir)
    fh = open(paths.state_dir / "lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise ArchiveError("Another wa-archive run is in progress.")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@dataclass
class Published:
    generation: int
    sha256: str
    size: int
    run_id: str | None


class Store:
    def __init__(self, paths: Paths, cloud):
        self.paths = paths
        self.cloud = cloud
        self.archive_db = paths.archive_dir / "archive.sqlite"
        self.archive_json = paths.archive_dir / "archive.json"
        self.snapshots = paths.archive_dir / "snapshots"
        self.work_db = paths.work_dir / "archive.sqlite"

    # ---------------------------------------------------------- local DB

    def open(self) -> sqlite3.Connection:
        private_dir(self.paths.work_dir)
        con = sqlite3.connect(self.work_db, isolation_level=None)  # explicit BEGIN/COMMIT
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
            row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            try:
                check_schema(row[0] if row else None, "local working copy")
            except ArchiveError:
                con.close()
                raise
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = FULL")
        con.executescript(SCHEMA + TRIGGERS)
        if self.meta(con, "schema_version") is None:
            self.set_meta(con, "schema_version", SCHEMA_VERSION)
            self.set_meta(con, "generation", 0)
        return con

    @staticmethod
    def meta(con, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_meta(con, key: str, value) -> None:
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, str(value)))

    def local_generation(self) -> int | None:
        if not self.work_db.exists():
            return None
        con = sqlite3.connect(f"file:{self.work_db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def _local_has_runs(self) -> bool:
        con = sqlite3.connect(f"file:{self.work_db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT count(*) FROM runs").fetchone()[0] > 0
        except sqlite3.Error:
            return False
        finally:
            con.close()

    # ---------------------------------------------------------- published copy

    def published(self) -> Published | None:
        if not self.archive_json.exists():
            return None
        self.cloud.ensure_local(self.archive_json)
        d = json.loads(self.archive_json.read_text())
        check_schema(d.get("schema_version"), "archive in the archive folder")
        return Published(int(d["generation"]), d["sha256"], int(d["size"]), d.get("run_id"))

    def sync(self) -> str:
        """Bring the local working copy in line with the published archive.

        Returns 'new', 'in-sync', 'pulled' or 'needs-publish'.
        """
        local = self.local_generation()
        pub = self.published()
        if pub is None:
            if self.archive_db.exists():
                raise ArchiveError("archive.sqlite exists without archive.json; refusing to guess. Check the folder.")
            if local:  # generation > 0: this archive has been published before, so it should be here
                raise ArchiveError(
                    f"No archive found at {self.paths.archive_dir}, but this Mac has already published one. "
                    "If you moved the archive folder, run `wa-archive config set archive-dir <new location>`; "
                    "if iCloud is still syncing it, wait and try again.")
            # A local DB with no runs is just an empty schema (e.g. left by --dry-run): nothing to publish.
            return "needs-publish" if local is not None and self._local_has_runs() else "new"
        if local is None or local < pub.generation:
            self.pull(pub)
            return "pulled"
        if local > pub.generation:
            return "needs-publish"
        return "in-sync"

    def pull(self, pub: Published | None = None) -> None:
        pub = pub or self.published()
        if pub is None:
            raise ArchiveError("No published archive to restore from.")
        if not self.cloud.ensure_local(self.archive_db):
            raise ArchiveError("archive.sqlite is still downloading from iCloud; try again shortly.")
        tmp_dir = private_dir(self.paths.tmp_dir)
        tmp = tmp_dir / "pull.sqlite"
        shutil.copyfile(self.archive_db, tmp)
        if sha256_file(tmp) != pub.sha256:
            tmp.unlink()
            raise ArchiveError("archive.sqlite does not match archive.json (sync in progress or conflict).")
        con = sqlite3.connect(tmp)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        if ok != "ok":
            raise ArchiveError("Published archive failed its integrity check.")
        private_dir(self.paths.work_dir)
        # Keep, never delete, a stale local copy (and its WAL) by renaming it aside.
        stale = self.paths.work_dir / f"archive.stale-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.work_db) + suffix)
            if p.exists():
                os.replace(p, Path(str(stale) + suffix))
        os.replace(tmp, self.work_db)

    def publish(self, con, run_id: str | None, snapshot_previous: bool) -> Published:
        gen = int(self.meta(con, "generation") or 0) + 1
        self.set_meta(con, "generation", gen)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tmp_dir = private_dir(self.paths.tmp_dir)
        tmp = tmp_dir / f"publish-{gen}.sqlite"
        if tmp.exists():
            tmp.unlink()
        con.execute("VACUUM INTO ?", (str(tmp),))
        fsync_file(tmp)
        digest, size = sha256_file(tmp), tmp.stat().st_size
        self.paths.archive_dir.mkdir(parents=True, exist_ok=True)
        old = self.published()
        if snapshot_previous and old and self.archive_db.exists():
            snap = self.snapshots / f"archive-gen{old.generation:05d}.sqlite"
            if not snap.exists() and self.cloud.ensure_local(self.archive_db):
                copy = tmp_dir / "snapshot.tmp"
                shutil.copyfile(self.archive_db, copy)
                atomic_place(copy, snap)
        atomic_place(tmp, self.archive_db)
        write_json_atomic({"generation": gen, "sha256": digest, "size": size, "run_id": run_id,
                           "schema_version": SCHEMA_VERSION, "published_at": now_iso()},
                          self.archive_json, tmp_dir)
        return Published(gen, digest, size, run_id)


class BlobStore:
    """Immutable content-addressed media files: media/ab/cd/<sha256>.<ext>."""

    def __init__(self, archive_dir: Path):
        self.root = archive_dir / "media"

    def path(self, sha: str, ext: str | None) -> Path:
        return self.root / sha[:2] / sha[2:4] / (f"{sha}.{ext}" if ext else sha)

    def place(self, tmp_file: Path, sha: str, ext: str | None) -> Path:
        dest = self.path(sha, ext)
        if dest.exists():
            tmp_file.unlink()  # identical content already archived
        else:
            atomic_place(tmp_file, dest)
        return dest


def clean_ext(name: str | None) -> str | None:
    if not name or "." not in name.rsplit("/", 1)[-1]:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return ext if ext.isalnum() and len(ext) <= 8 else None


CONTENT_QUERIES = [
    "SELECT msg_key, chat_jid, stanza_id, from_me, sender_jid, ts, type, text, title, quoted_stanza, raw_json, "
    "first_seen_run, edited, revoked_run FROM messages ORDER BY msg_key",
    "SELECT msg_key, run_id, kind, text, title FROM message_versions ORDER BY id",
    "SELECT msg_key, status, sha256, size_expected, orig_path, thumb_path, first_present_run FROM media ORDER BY msg_key",
    "SELECT path, sha256, size FROM extra_files ORDER BY path",
    "SELECT sha256, size, ext FROM blobs ORDER BY sha256",
    "SELECT * FROM reactions ORDER BY msg_key, reaction_id, emoji",
    "SELECT * FROM calls ORDER BY call_id",
    "SELECT chat_id, kind, merged_into FROM chats ORDER BY chat_id",
    "SELECT jid, chat_id FROM chat_aliases ORDER BY jid",
    "SELECT jid, name, source FROM names ORDER BY jid, source, name",
]


def content_digest(con) -> str:
    """Hash of all archived content, ignoring per-run observation columns."""
    h = hashlib.sha256()
    for q in CONTENT_QUERIES:
        for row in con.execute(q):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()
