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
    assert "1.1:jid" in cs["receipt_info_shapes"]["field_shapes"]
    assert "1.2:emoji" in cs["receipt_info_shapes"]["field_shapes"]
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
