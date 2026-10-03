import json
import logging

import pytest
from rich.console import Console

from wa_archive import cli, protobuf
from wa_archive.backup import DirSource, list_backups
from wa_archive.spike import run_spike

from .fixture import SECRETS, basic_fixture, pb_field


@pytest.fixture
def source(tmp_path):
    root = basic_fixture().write(tmp_path / "backup")
    return DirSource(root)


@pytest.fixture
def report(source, tmp_path):
    (tmp_path / "tmp").mkdir()
    return run_spike(source, tmp_path / "tmp", free_bytes=20 * 1024**3)


def test_counts(report):
    cs = report["chatstorage"]
    assert report["integrity_check"] == "ok"
    assert cs["chats"]["total"] == 3
    assert cs["chats"]["by_jid_kind"] == {"phone": 1, "lid": 1, "group": 1}
    assert cs["messages"]["total"] == 32
    assert cs["stanza_ids"]["null_or_empty"] == 1
    assert cs["stanza_ids"]["dup_chat_fromme_stanza"] == {"groups": 0, "rows": 0}
    m = cs["media"]
    assert m["items"] == 8
    assert m["by_message_type"]["2 video"]["missing"] == 1
    assert m["by_message_type"]["1 image"]["present"] == 3
    assert m["missing_expected_bytes_total"] == 5_000_000
    assert m["path_match_style"] == {"Message/+path": 6}
    assert cs["quotes"] == {"quote_refs": 1, "resolved_full_id": 1, "resolved_17char_prefix": 1}
    assert "7.1.2:jid" in cs["receipt_info_shapes"]["field_shapes"]
    assert "7.1.3:emoji" in cs["receipt_info_shapes"]["field_shapes"]
    assert report["contacts"]["ZWAADDRESSBOOKCONTACT.ZLID_non_null"] == 1
    assert report["calls"]["call_id_duplicates"] == 0
    assert report["disk"]["estimated_batches"] == 1


def test_output_contains_no_private_values(report, tmp_path, monkeypatch, caplog):
    rec = Console(record=True, width=200)
    monkeypatch.setattr(cli, "console", rec)
    with caplog.at_level(logging.DEBUG):
        cli.render(report)
    text = rec.export_text() + json.dumps(report, default=str, ensure_ascii=False) + caplog.text
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"spike output leaked fixture values: {leaked}"
    assert "Fixture-Phone" not in json.dumps(report, default=str)  # device name stays out of spike output


def test_backups_listing(tmp_path):
    basic_fixture().write(tmp_path / "root" / "abc123")
    (b,) = list_backups(tmp_path / "root")
    assert b.backup_id == "abc123" and b.ios_version == "26.0" and b.whatsapp_version == "26.1.0"
    assert b.last_backup.year == 2026


def test_protobuf_parse_and_census_never_records_values():
    blob = pb_field(5, "ABCDEF0123456789ABCD") + pb_field(7, 3) + pb_field(9, pb_field(1, "hello world"))
    fields = protobuf.parse(blob)
    assert [f.number for f in fields] == [5, 7, 9]
    assert protobuf.first(fields, 5).value == b"ABCDEF0123456789ABCD"
    from collections import Counter
    c = Counter()
    protobuf.shape_census(blob, c)
    assert c == Counter({"5:id": 1, "7:varint": 1, "9:msg": 1, "9.1:text": 1})
    assert protobuf.try_parse(b"\xff\xff\xff") is None


def test_crosscheck_with_wtsexporter(source, tmp_path):
    pytest.importorskip("Whatsapp_Chat_Exporter")
    (tmp_path / "tmp").mkdir()
    r = run_spike(source, tmp_path / "tmp", free_bytes=20 * 1024**3, do_crosscheck=True)
    cc = r["crosscheck_wtsexporter"]
    assert "error" not in cc, cc
    assert cc["chats_common"] == 3
    assert cc["messages_ours"] == 32


def test_sweep_tmp_removes_leftovers(tmp_path):
    from wa_archive.config import sweep_tmp
    (tmp_path / "spike-old").mkdir()
    (tmp_path / "spike-old" / "ChatStorage.sqlite").write_bytes(b"x")
    (tmp_path / "stray.json").write_text("{}")
    assert sweep_tmp(tmp_path) == 2 and not any(tmp_path.iterdir())


def test_crosscheck_leaves_nothing_behind(source, tmp_path):
    pytest.importorskip("Whatsapp_Chat_Exporter")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    run_spike(source, tmp, free_bytes=0, do_crosscheck=True)
    assert not any(tmp.iterdir())


def test_probes_present(report):
    p = report["chatstorage"]["probes"]
    assert p["media_title_by_type"] == {"1 image": 1, "8 document": 1}
    assert p["unreferenced_after_thumbnails"]["count"] == 1
    assert p["ZXMPPTHUMBPATH_paths"] == {"set": 1, "in_backup_Message/+path": 1, "in_backup_as_is": 0}


def test_finder_backup_close_prevents_double_cleanup(tmp_path, capsys):
    from iphone_backup_decrypt import EncryptedBackup
    from wa_archive.backup import FinderBackup
    eb = EncryptedBackup.__new__(EncryptedBackup)
    folder = tmp_path / "manifest-tmp"
    folder.mkdir()
    eb._temp_manifest_db_conn, eb._temporary_folder = None, str(folder)
    eb._temp_decrypted_manifest_db_path = str(folder / "Manifest.db")
    fb = FinderBackup.__new__(FinderBackup)
    fb._backup, fb._old_tempdir = eb, None
    fb.close()
    assert not folder.exists()
    eb.__del__()  # what the garbage collector does later; must be a silent no-op
    assert "Cleanup failed" not in capsys.readouterr().out


def test_finder_backup_extract_decrypts_via_manifest_index(tmp_path):
    """FinderBackup.extract on a synthetic AES-CBC backup file (same format iOS uses)."""
    import plistlib
    import sqlite3
    from contextlib import contextmanager
    from types import SimpleNamespace

    from Crypto.Cipher import AES

    from wa_archive.backup import SHARED_DOMAIN, FinderBackup

    content = b"synthetic media bytes " * 100
    key = bytes(range(32))
    pad = 16 - len(content) % 16
    enc = AES.new(key, AES.MODE_CBC, iv=b"\0" * 16).encrypt(content + bytes([pad]) * pad)
    file_id = "ab" + "0" * 38
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / file_id).write_bytes(enc)
    plist = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000,
                            "$top": {"root": plistlib.UID(1)},
                            "$objects": ["$null", {"Size": len(content), "ProtectionClass": 3,
                                                   "EncryptionKey": plistlib.UID(2)},
                                         {"NS.data": b"\x03\0\0\0wrapped-key"}]}, fmt=plistlib.FMT_BINARY)
    manifest = sqlite3.connect(":memory:")
    manifest.execute("CREATE TABLE Files (fileID, domain, relativePath, flags, file)")
    manifest.execute("INSERT INTO Files VALUES (?, ?, ?, 1, ?)", (file_id, SHARED_DOMAIN, "Message/Media/x.jpg", plist))

    @contextmanager
    def cursor():
        yield manifest.cursor()

    unwrapped = []
    fb = FinderBackup.__new__(FinderBackup)
    fb._index = None
    fb._backup = SimpleNamespace(
        manifest_db_cursor=cursor, _backup_directory=str(tmp_path),
        keybag=SimpleNamespace(unwrap_key_for_class=lambda cls, wrapped: unwrapped.append((cls, wrapped)) or key))
    assert [(f.relative_path, f.size) for f in fb.files(SHARED_DOMAIN)] == [("Message/Media/x.jpg", len(content))]
    out = tmp_path / "out.bin"
    fb.extract(SHARED_DOMAIN, "Message/Media/x.jpg", out)
    assert out.read_bytes() == content
    assert unwrapped == [(3, b"wrapped-key")]
    with pytest.raises(FileNotFoundError):
        fb.extract(SHARED_DOMAIN, "Message/Media/missing.jpg", out)
