"""Merge-logic tests on synthetic fixtures: the archive must never forget."""

import copy
import json
import logging
import shutil
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from wa_archive.backup import DirSource
from wa_archive.cloud import CloudState, FakeCloud
from wa_archive.config import Paths
from wa_archive.ingest import Options, UploadTimeout, run_ingest
from wa_archive.report import build_report, save_report, to_markdown
from wa_archive.store import ArchiveError, BlobStore, Store, content_digest, run_lock

from .fixture import basic_fixture

GB = 1024 ** 3
# Message contents that must never appear in reports, stats or logs (chat names may).
TEXT_SECRETS = ["fixture-secret-text", "👍", "❤️", "img1.jpg", "voice1.opus", "REACTIONID", "preview1",
                "Fixture-Phone-Doc", "image-one"]


@pytest.fixture
def env(tmp_path):
    paths = Paths(backup_root=tmp_path / "backups", state_dir=tmp_path / "state", archive_dir=tmp_path / "archive")
    return SimpleNamespace(paths=paths, cloud=FakeCloud(CloudState.SYNCED), tmp=tmp_path, n=0)


def ingest(env, fx, same_backup=False, **kw):
    """Ingest fx as a new backup folder, or re-read the previous folder (same_backup=True)."""
    if not same_backup:
        env.n += 1
        fx.write(env.tmp / "backups" / f"backup{env.n}")
    root = env.tmp / "backups" / f"backup{env.n}"
    kw.setdefault("free_space", lambda: 100 * GB)
    return run_ingest(env.paths, lambda: DirSource(root), env.cloud, **kw)


def db(env):
    con = sqlite3.connect(env.paths.state_dir / "work" / "archive.sqlite")
    con.row_factory = sqlite3.Row
    return con


def one(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


def report(env, fx, hours_after_backup=1, **kw):
    return build_report(env.paths, env.cloud, now=fx.backup_date + timedelta(hours=hours_after_backup), **kw)


def chat(rep, name):
    return next(c for c in rep["chats"] if c["name"] == name)


# ------------------------------------------------------------------ basic ingest

def test_first_ingest(env):
    fx = basic_fixture()
    stats = ingest(env, fx)
    con = db(env)
    assert stats["messages_new"] == 32
    assert one(con, "SELECT count(*) FROM messages") == 32
    assert one(con, "SELECT count(*) FROM chats") == 3
    assert one(con, "SELECT count(*) FROM media WHERE status = 'present'") == 6
    assert one(con, "SELECT count(*) FROM media WHERE status = 'missing'") == 1  # never-downloaded video
    assert one(con, "SELECT count(*) FROM extra_files") == 2  # thumbnail + link-preview favicon
    assert one(con, "SELECT count(*) FROM reactions") == 2
    assert one(con, "SELECT count(*) FROM calls") == 2
    assert one(con, "SELECT count(*) FROM blobs WHERE verified_run IS NULL") == 0
    first = fx.messages[0]
    assert one(con, "SELECT count(*) FROM messages WHERE quoted_stanza = ?", first.stanza) == 1
    assert one(con, "SELECT count(*) FROM fts WHERE fts MATCH 'caption'") == 1
    # Every blob is on disk and its name is its hash.
    blobs = BlobStore(env.paths.archive_dir)
    for r in con.execute("SELECT sha256, ext FROM blobs"):
        assert blobs.path(r["sha256"], r["ext"]).exists()
    pub = json.loads((env.paths.archive_dir / "archive.json").read_text())
    assert pub["generation"] >= 1


def test_reingest_same_backup_adds_nothing(env):
    fx = basic_fixture()
    ingest(env, fx)
    before = content_digest(db(env))
    stats = ingest(env, fx)
    assert stats["messages_new"] == 0 and stats.get("blobs_new", 0) == 0 and stats.get("media_archived", 0) == 0
    assert content_digest(db(env)) == before


@pytest.mark.parametrize("clear_path", [False, True], ids=["file-gone", "path-nulled"])
def test_backup_after_clearing_media_removes_nothing(env, clear_path):
    fx = basic_fixture()
    ingest(env, fx)
    con = db(env)
    before = content_digest(con)
    present_before = one(con, "SELECT count(*) FROM media WHERE status = 'present'")
    con.close()

    fx2 = copy.deepcopy(fx)
    fx2.backup_date += timedelta(days=90)
    for m in fx2.messages:
        if m.media:
            m.media.content = None
            m.media.thumb = None
            if clear_path:
                m.media.local_path = None
    fx2.extra_files.clear()
    stats = ingest(env, fx2)
    con = db(env)
    assert stats["messages_new"] == 0
    assert one(con, "SELECT count(*) FROM media WHERE status = 'present'") == present_before
    assert content_digest(con) == before
    blobs = BlobStore(env.paths.archive_dir)
    for r in con.execute("SELECT sha256, ext FROM blobs"):
        assert blobs.path(r["sha256"], r["ext"]).exists()


def test_missing_media_filled_in_by_later_backup(env):
    fx = basic_fixture()
    ingest(env, fx)
    video = next(m for m in fx.messages if m.type == 2)
    fx2 = copy.deepcopy(fx)
    next(m for m in fx2.messages if m.pk == video.pk).media.content = b"\x00\x00 video bytes now downloaded"
    stats = ingest(env, fx2)
    con = db(env)
    assert stats["media_archived"] == 1
    assert one(con, "SELECT count(*) FROM media WHERE status = 'missing'") == 0


def test_edit_appends_version_and_keeps_original(env):
    fx = basic_fixture()
    ingest(env, fx)
    fx2 = copy.deepcopy(fx)
    fx2.messages[0].text = "fixture-secret-text edited"
    img = next(m for m in fx2.messages if m.media and m.media.title)
    img.media.title = "fixture-secret-text new caption"
    stats = ingest(env, fx2)
    con = db(env)
    assert stats["messages_edited"] == 2
    key = one(con, "SELECT msg_key FROM messages WHERE stanza_id = ?", fx.messages[0].stanza)
    assert one(con, "SELECT text FROM messages WHERE msg_key = ?", key) == fx.messages[0].text
    assert one(con, "SELECT edited FROM messages WHERE msg_key = ?", key) == 1
    assert one(con, "SELECT text FROM message_versions WHERE msg_key = ? AND kind = 'edit'", key) == \
        "fixture-secret-text edited"
    assert one(con, "SELECT count(*) FROM fts WHERE fts MATCH 'edited'") == 1
    # Re-ingesting the edited backup doesn't add another version.
    assert ingest(env, fx2).get("messages_edited", 0) == 0


def test_deleted_for_everyone_keeps_original(env):
    fx = basic_fixture()
    ingest(env, fx)
    fx2 = copy.deepcopy(fx)
    fx2.messages[1].type, fx2.messages[1].text = 14, None
    stats = ingest(env, fx2)
    con = db(env)
    key = one(con, "SELECT msg_key FROM messages WHERE stanza_id = ?", fx.messages[1].stanza)
    assert stats["messages_revoked"] == 1
    assert one(con, "SELECT text FROM messages WHERE msg_key = ?", key) == fx.messages[1].text
    assert one(con, "SELECT revoked_run IS NOT NULL FROM messages WHERE msg_key = ?", key) == 1
    assert one(con, "SELECT count(*) FROM message_versions WHERE kind = 'revoke'") == 1


def test_deleted_for_me_is_kept_and_reported(env):
    fx = basic_fixture()
    ingest(env, fx)
    fx2 = copy.deepcopy(fx)
    fx2.messages = fx2.messages[1:]
    ingest(env, fx2)
    assert one(db(env), "SELECT count(*) FROM messages") == 32
    assert chat(report(env, fx2), "Contact-Alpha")["not_in_latest_backup"] == 1


def test_lid_migration_creates_no_duplicates(env):
    fx = basic_fixture()
    ingest(env, fx)
    fx2 = copy.deepcopy(fx)
    lid = "99990000001@lid"
    fx2.chats[1] = (lid, "Contact-Alpha", 0)  # the 1:1 chat is now addressed by LID
    fx2.contacts[0] = (fx.chats[1][0], "Contact-Alpha", lid)  # contacts DB links phone <-> LID
    fx2.members[1] = (3, "55550000009@lid", "Member-Delta")  # a group member moved to LID too
    stats = ingest(env, fx2)
    con = db(env)
    assert stats["messages_new"] == 0
    assert one(con, "SELECT count(*) FROM messages") == 32
    assert one(con, "SELECT count(*) FROM chats WHERE merged_into IS NULL") == 3
    assert one(con, "SELECT chat_id FROM chat_aliases WHERE jid = ?", lid) == \
        one(con, "SELECT chat_id FROM chat_aliases WHERE jid = ?", fx.chats[1][0])


def test_two_archived_chats_are_merged_when_mapping_appears(env):
    fx = basic_fixture()
    lid = "99990000001@lid"
    fx.chats[4] = (lid, "Contact-Alpha", 0)  # phone and LID chats coexist, no mapping known yet
    ingest(env, fx)
    assert one(db(env), "SELECT count(*) FROM chats") == 4
    fx2 = copy.deepcopy(fx)
    fx2.contacts[0] = (fx.chats[1][0], "Contact-Alpha", lid)
    stats = ingest(env, fx2)
    con = db(env)
    assert stats["chats_merged"] == 1
    assert one(con, "SELECT count(*) FROM chats WHERE merged_into IS NULL") == 3
    assert one(con, "SELECT count(*) FROM messages") == 32


def test_reactions_and_calls_are_not_duplicated(env):
    fx = basic_fixture()
    ingest(env, fx)
    ingest(env, fx)
    con = db(env)
    assert one(con, "SELECT count(*) FROM reactions") == 2
    assert one(con, "SELECT count(*) FROM reactions WHERE reactor_jid IS NULL") == 1  # my own reaction
    assert one(con, "SELECT count(*) FROM calls") == 2


# ------------------------------------------------------------------ append-only enforcement

@pytest.mark.parametrize("sql", [
    "DELETE FROM messages",
    "DELETE FROM media",
    "DELETE FROM blobs",
    "DELETE FROM reactions",
    "UPDATE messages SET text = 'x'",
    "UPDATE messages SET edited = 0",
    "UPDATE media SET status = 'missing' WHERE status = 'present'",
    "UPDATE media SET sha256 = 'x' WHERE sha256 IS NOT NULL",
    "UPDATE blobs SET size = 0",
    "DELETE FROM chats",
])
def test_triggers_block_deletes_and_overwrites(env, sql):
    fx = basic_fixture()
    fx.messages[0].text = "fixture-secret-text"
    ingest(env, fx)
    con = db(env)
    con.execute("UPDATE messages SET edited = 1 WHERE rowid = 1")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute(sql)


# ------------------------------------------------------------------ publishing, crashes, recovery

def _tiny_disk(env, fx, room: int):
    """Free space with only `room` bytes for media, counting blob files that aren't evicted.

    Ingest reserves 3x the ChatStorage size for DB growth, so add exactly that on top.
    """
    def free():
        chat_db = next((env.tmp / "backups" / f"backup{env.n}").rglob("ChatStorage.sqlite"))
        media = env.paths.archive_dir / "media"
        used = sum(p.stat().st_size for p in media.rglob("*")
                   if p.is_file() and env.cloud.state(p) != CloudState.EVICTED) if media.exists() else 0
        return 3 * chat_db.stat().st_size + room - used
    return free


def test_batches_evict_only_verified_uploaded_blobs(env):
    fx = basic_fixture()
    batches = []
    stats = ingest(env, fx, free_space=_tiny_disk(env, fx, room=30), options=Options(margin=0, poll=0),
                   after_batch=batches.append)
    con = db(env)
    assert stats["batches"] >= 3 and len(batches) >= 3
    assert one(con, "SELECT count(*) FROM media WHERE status = 'present'") == 6
    assert env.cloud.evicted, "earlier batches should have been evicted"
    verified = {r[0] for r in con.execute("SELECT sha256 FROM blobs WHERE verified_run IS NOT NULL")}
    assert all(p.name.split(".")[0] in verified for p in env.cloud.evicted)
    assert one(con, "SELECT status FROM runs") == "complete"


def test_pending_upload_blocks_eviction(env):
    env.cloud.default = CloudState.PENDING
    fx = basic_fixture()
    with pytest.raises(UploadTimeout):
        ingest(env, fx, free_space=_tiny_disk(env, fx, room=30),
               options=Options(margin=0, poll=0, upload_timeout=0))
    assert env.cloud.evicted == []
    # Uploads finish later; re-running resumes the same run and completes it.
    env.cloud.default = CloudState.SYNCED
    stats = ingest(env, fx, same_backup=True, free_space=_tiny_disk(env, fx, room=30),
                   options=Options(margin=0, poll=0))
    assert stats["resumed"] is True
    con = db(env)
    assert one(con, "SELECT count(*) FROM runs") == 1
    assert one(con, "SELECT count(*) FROM media WHERE status = 'present'") == 6


def test_single_file_larger_than_free_space_aborts(env):
    fx = basic_fixture()
    with pytest.raises(ArchiveError, match="single media file"):
        ingest(env, fx, free_space=_tiny_disk(env, fx, room=5), options=Options(margin=0, poll=0))
    assert not (env.paths.archive_dir / "archive.json").exists()


def test_crash_after_batch_resumes_without_duplicates(env):
    fx = basic_fixture()

    def crash(batch):
        if batch == 2:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ingest(env, fx, free_space=_tiny_disk(env, fx, room=30), options=Options(margin=0, poll=0),
               after_batch=crash)
    con = db(env)
    assert one(con, "SELECT status FROM runs") == "running"
    con.close()
    stats = ingest(env, fx, same_backup=True, free_space=_tiny_disk(env, fx, room=30),
                   options=Options(margin=0, poll=0))
    con = db(env)
    assert stats["resumed"] is True and stats["messages_new"] == 0
    assert one(con, "SELECT count(*) FROM messages") == 32
    assert one(con, "SELECT count(*) FROM media WHERE status = 'present'") == 6
    assert one(con, "SELECT status FROM runs") == "complete"


def test_crash_mid_batch_leaves_last_published_snapshot(env, monkeypatch):
    fx = basic_fixture()
    ingest(env, fx)
    pub_before = json.loads((env.paths.archive_dir / "archive.json").read_text())
    fx2 = copy.deepcopy(fx)
    fx2.messages[0].text = "fixture-secret-text changed"
    orig = DirSource.extract

    def boom(self, domain, rel, dest):
        if rel.startswith("Message/"):
            raise KeyboardInterrupt  # dies while archiving media, before the batch commits
        return orig(self, domain, rel, dest)
    monkeypatch.setattr(DirSource, "extract", boom)
    fx2.messages[12].media.content = b"\x00 the video arrives"
    with pytest.raises(KeyboardInterrupt):
        ingest(env, fx2)
    pub_after = json.loads((env.paths.archive_dir / "archive.json").read_text())
    assert pub_after == pub_before
    store = Store(env.paths, env.cloud)
    from wa_archive.store import sha256_file
    assert sha256_file(store.archive_db) == pub_before["sha256"]
    assert one(db(env), "SELECT count(*) FROM message_versions") == 0  # uncommitted batch rolled back
    monkeypatch.setattr(DirSource, "extract", orig)
    stats = ingest(env, fx2)
    assert stats["messages_edited"] == 1 and stats["media_archived"] == 1


def test_unpublished_commit_is_published_on_next_run(env, monkeypatch):
    fx = basic_fixture()
    calls = {"n": 0}
    orig = Store.publish

    def flaky(self, con, run_id, snapshot_previous):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt  # committed locally, never published
        return orig(self, con, run_id, snapshot_previous)
    monkeypatch.setattr(Store, "publish", flaky)
    with pytest.raises(KeyboardInterrupt):
        ingest(env, fx)
    assert not (env.paths.archive_dir / "archive.json").exists()
    ingest(env, fx)
    assert json.loads((env.paths.archive_dir / "archive.json").read_text())["generation"] >= 2


def test_recovery_from_archive_folder_only(env):
    fx = basic_fixture()
    ingest(env, fx)
    digest = content_digest(db(env))
    shutil.rmtree(env.paths.state_dir)  # new Mac: only the synced archive folder exists
    assert Store(env.paths, env.cloud).sync() == "pulled"
    assert content_digest(db(env)) == digest
    rep = report(env, fx)
    assert rep["totals"]["messages"] == 32 and not rep["blockers"]


def test_tampered_published_db_is_refused(env):
    fx = basic_fixture()
    ingest(env, fx)
    shutil.rmtree(env.paths.state_dir)
    with open(env.paths.archive_dir / "archive.sqlite", "ab") as f:
        f.write(b"garbage")
    with pytest.raises(ArchiveError, match="does not match"):
        Store(env.paths, env.cloud).sync()


def test_dry_run_writes_nothing(env):
    fx = basic_fixture()
    stats = ingest(env, fx, options=Options(dry_run=True))
    assert stats["dry_run"] and stats["new_files"] == 8
    assert not (env.paths.archive_dir / "archive.json").exists()
    assert not (env.paths.archive_dir / "media").exists()


def test_dry_run_then_real_run_publishes_once(env):
    fx = basic_fixture()
    ingest(env, fx, options=Options(dry_run=True))
    assert Store(env.paths, env.cloud).sync() == "new"
    ingest(env, fx, same_backup=True)
    pub = json.loads((env.paths.archive_dir / "archive.json").read_text())
    assert pub["generation"] == 1
    assert not (env.paths.archive_dir / "snapshots").exists()


def test_concurrent_runs_are_refused(env):
    with run_lock(env.paths):
        with pytest.raises(ArchiveError, match="in progress"):
            ingest(env, basic_fixture())


def test_snapshots_are_kept(env):
    fx = basic_fixture()
    ingest(env, fx)
    ingest(env, fx)
    assert len(list((env.paths.archive_dir / "snapshots").glob("*.sqlite"))) == 1


# ------------------------------------------------------------------ report

def test_report_safe_list(env):
    fx = basic_fixture()
    ingest(env, fx)
    rep = report(env, fx)
    assert rep["blockers"] == [] and rep["warnings"] == []
    alpha = chat(rep, "Contact-Alpha")
    assert alpha["safe"] and alpha["never_available"] == 1
    assert alpha["media_present"] == 4 and alpha["media_missing"] == 1
    assert all(c["safe"] for c in rep["chats"])
    assert rep["blobs_rehashed"] == one(db(env), "SELECT count(*) FROM blobs")


def test_report_pending_upload_and_db_upload(env):
    fx = basic_fixture()
    ingest(env, fx)
    con = db(env)
    blobs = BlobStore(env.paths.archive_dir)
    sha, ext = con.execute("""SELECT b.sha256, b.ext FROM media md JOIN messages m USING (msg_key)
                              JOIN blobs b ON b.sha256 = md.sha256 WHERE m.chat_jid LIKE '%@g.us'""").fetchone()
    env.cloud.states[blobs.path(sha, ext)] = CloudState.PENDING
    rep = report(env, fx)
    assert chat(rep, "Group-Charlie")["reasons"] == {"pending upload": 1}
    assert chat(rep, "Contact-Alpha")["safe"]
    env.cloud.states[env.paths.archive_dir / "archive.sqlite"] = CloudState.PENDING
    rep = report(env, fx)
    assert rep["blockers"] and not any(c["safe"] for c in rep["chats"])


def test_report_hash_mismatch_and_eviction(env):
    fx = basic_fixture()
    ingest(env, fx)
    con = db(env)
    blobs = BlobStore(env.paths.archive_dir)
    rows = con.execute("""SELECT b.sha256, b.ext FROM media md JOIN messages m USING (msg_key)
                          JOIN blobs b ON b.sha256 = md.sha256 WHERE m.chat_jid LIKE '%@s.whatsapp.net'""").fetchall()
    bad, evicted = blobs.path(*rows[0]), blobs.path(*rows[1])
    bad.write_bytes(b"corrupted")
    env.cloud.states[evicted] = CloudState.EVICTED
    rep = report(env, fx)
    assert chat(rep, "Contact-Alpha")["reasons"] == {"hash mismatch": 1}
    con.execute("UPDATE blobs SET verified_run = NULL WHERE sha256 = ?", (rows[1][0],))
    con.commit()
    rep = report(env, fx)
    assert chat(rep, "Contact-Alpha")["reasons"] == {"hash mismatch": 1, "unverified": 1}


def test_report_backup_age_warning(env):
    fx = basic_fixture()
    ingest(env, fx)
    assert report(env, fx, hours_after_backup=1)["warnings"] == []
    rep = report(env, fx, hours_after_backup=10)
    assert any("10 hours old" in w for w in rep["warnings"])
    assert "Manage Storage" in to_markdown(rep)


def test_report_flags_stanza_collision(env):
    fx = basic_fixture()
    ingest(env, fx)
    con = db(env)
    m0 = fx.messages[0]
    con.execute("""INSERT INTO messages (msg_key, chat_jid, stanza_id, from_me, first_seen_run, last_seen_run)
                   VALUES ('dupkey', ?, ?, 1, 'x', 'x')""", (fx.chats[1][0], m0.stanza))
    con.commit()
    assert chat(report(env, fx), "Contact-Alpha")["reasons"] == {"stanza collision": 1}


def test_unfinished_run_blocks_safe_list(env):
    env.cloud.default = CloudState.PENDING
    fx = basic_fixture()
    with pytest.raises(UploadTimeout):
        ingest(env, fx, free_space=_tiny_disk(env, fx, room=30), options=Options(margin=0, upload_timeout=0))
    env.cloud.default = CloudState.SYNCED
    rep = report(env, fx)
    assert any("unfinished" in b for b in rep["blockers"])
    assert not any(c["safe"] for c in rep["chats"])


def test_no_message_content_in_reports_stats_or_logs(env, caplog):
    fx = basic_fixture()
    with caplog.at_level(logging.DEBUG):
        stats = ingest(env, fx)
        rep = report(env, fx)
        md, js = save_report(rep, env.paths)
    blob = json.dumps(stats, ensure_ascii=False) + md.read_text() + js.read_text() + caplog.text
    leaked = [s for s in TEXT_SECRETS if s in blob]
    assert not leaked, leaked
    assert "Contact-Alpha" in md.read_text()  # chat names are expected in the report




def test_evict_command_only_touches_verified_uploaded(env):
    from wa_archive.ingest import evict_uploaded
    fx = basic_fixture()
    ingest(env, fx)
    con = db(env)
    blobs = BlobStore(env.paths.archive_dir)
    rows = con.execute("SELECT sha256, ext FROM blobs").fetchall()
    env.cloud.states[blobs.path(*rows[0])] = CloudState.PENDING
    con.execute("UPDATE blobs SET verified_run = NULL WHERE sha256 = ?", (rows[1][0],))
    con.commit()
    n, _ = evict_uploaded(env.paths, env.cloud)
    assert n == len(rows) - 2
    assert blobs.path(*rows[0]) not in env.cloud.evicted and blobs.path(*rows[1]) not in env.cloud.evicted
