"""Viewer API tests on synthetic fixtures."""

import copy
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from wa_archive.cloud import CloudState
from wa_archive.store import Store
from wa_archive.viewer.app import create_app, fts_query

from .fixture import Fixture, Media, Msg, basic_fixture, stanza
from .test_ingest import db, env, ingest, one  # noqa: F401  (env is a fixture)

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def client_for(env, token="secret-token", base=BASE, login=True):
    app = create_app(env.paths, env.cloud, token=token, port=PORT, download_timeout=0)
    c = TestClient(app, base_url=base)
    if login:
        assert c.get(f"/?t={token}").status_code == 200
    return c


@pytest.fixture
def viewer(env):
    fx = basic_fixture()
    ingest(env, fx)
    env.fx = fx
    return client_for(env)


def chat_id(c, name):
    return next(x["id"] for x in c.get("/api/chats?hidden=1").json() if x["name"] == name)


# ------------------------------------------------------------------ access control

def test_requires_launch_secret(env, viewer):
    anon = client_for(env, login=False)
    assert anon.get("/api/chats").status_code == 403
    assert anon.get("/?t=wrong").status_code == 403
    assert anon.get("/blob/" + "0" * 64).status_code == 403


def test_rejects_other_hosts(env, viewer):
    evil = TestClient(create_app(env.paths, env.cloud, token="secret-token", port=PORT),
                      base_url=f"http://evil.example:{PORT}")
    evil.cookies.set("wa_archive_session", "secret-token")
    assert evil.get("/api/chats").status_code == 403


def test_security_headers(viewer):
    r = viewer.get("/api/chats")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_frontend_never_uses_html_injection():
    js = (Path(__file__).parents[1] / "src/wa_archive/viewer/static/app.js").read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert bad not in js


# ------------------------------------------------------------------ chats & messages

def test_chat_list_sorted_by_latest_activity(viewer):
    chats = viewer.get("/api/chats").json()
    assert [c["name"] for c in chats] == ["Group-Charlie", "Contact-Bravo", "Contact-Alpha"]
    assert chats[0]["preview"] == "📷 Photo"


def test_message_pages_and_cursors(viewer):
    cid = chat_id(viewer, "Contact-Alpha")
    newest = viewer.get(f"/api/chats/{cid}/messages?limit=5").json()
    assert len(newest["messages"]) == 5 and newest["has_older"] and not newest["has_newer"]
    ts = [m["ts"] for m in newest["messages"]]
    assert ts == sorted(ts)
    first = newest["messages"][0]
    older = viewer.get(f"/api/chats/{cid}/messages?limit=5&before={first['ts']}:{first['rid']}").json()
    assert older["messages"][-1]["ts"] < first["ts"] and older["has_newer"]
    last = older["messages"][-1]
    newer = viewer.get(f"/api/chats/{cid}/messages?limit=5&after={last['ts']}:{last['rid']}").json()
    assert newer["messages"][0]["key"] == first["key"]
    target = older["messages"][2]["key"]
    around = viewer.get(f"/api/chats/{cid}/messages?limit=6&around={target}").json()
    assert around["target"] == target and target in [m["key"] for m in around["messages"]]
    by_date = viewer.get(f"/api/chats/{cid}/messages?limit=6&around_ts={first['ts']}").json()
    assert by_date["target"] == first["key"]
    assert viewer.get(f"/api/chats/{cid}/messages?before=nonsense").status_code == 400


def test_media_reactions_and_quotes(viewer, env):
    cid = chat_id(viewer, "Contact-Alpha")
    msgs = viewer.get(f"/api/chats/{cid}/messages?limit=200").json()["messages"]
    img = next(m for m in msgs if m.get("media", {}).get("kind") == "image" and m["title"])
    assert img["media"]["status"] == "present" and img["media"]["thumb"]
    video = next(m for m in msgs if m["type"] == 2)
    assert video["media"]["status"] == "missing"
    voice = next(m for m in msgs if m["type"] == 3)
    assert voice["media"]["kind"] == "audio" and voice["media"]["ext"] == "opus"
    reacted = next(m for m in msgs if m["reactions"])
    assert {r["emoji"] for r in reacted["reactions"]} == {"👍", "❤️"}
    assert any(r["who"] == ["You"] for r in reacted["reactions"])
    reply = next(m for m in msgs if m["quote"])
    assert reply["quote"]["key"] == msgs[0]["key"]
    deleted = next(m for m in msgs if m["type"] == 14)
    assert not deleted["revoked"]


def test_edits_and_revokes_are_shown_with_history(env):
    fx = basic_fixture()
    ingest(env, fx)
    fx2 = copy.deepcopy(fx)
    fx2.messages[0].text = "fixture-secret-text edited"
    fx2.messages[1].type, fx2.messages[1].text = 14, None
    ingest(env, fx2)
    c = client_for(env)
    cid = chat_id(c, "Contact-Alpha")
    msgs = c.get(f"/api/chats/{cid}/messages?limit=200").json()["messages"]
    edited = next(m for m in msgs if m["edited"])
    assert edited["text"] == "fixture-secret-text edited"
    versions = c.get(f"/api/messages/{edited['key']}/versions").json()
    assert [v["kind"] for v in versions] == ["original", "edit"]
    assert versions[0]["text"] == fx.messages[0].text
    revoked = next(m for m in msgs if m["revoked"])
    assert revoked["text"] == fx.messages[1].text  # original kept and shown, flagged


# ------------------------------------------------------------------ media serving

def _blob_sha(env, chat_like="%@s.whatsapp.net", ext="jpg"):
    return one(db(env), """SELECT md.sha256 FROM media md JOIN messages m USING (msg_key) JOIN blobs b
                           ON b.sha256 = md.sha256 WHERE m.chat_jid LIKE ? AND b.ext = ? LIMIT 1""", chat_like, ext)


def test_blob_serving_with_range_and_download_name(viewer, env):
    sha = _blob_sha(env)
    full = viewer.get(f"/blob/{sha}")
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    part = viewer.get(f"/blob/{sha}", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == full.content[:4]
    dl = viewer.get(f"/blob/{sha}?name=../../evil name.jpg")
    assert dl.headers["content-disposition"] == 'attachment; filename=".._.._evil name.jpg"'


def test_blob_rejects_bad_ids(viewer):
    assert viewer.get("/blob/../../etc/passwd").status_code == 404
    assert viewer.get("/blob/" + "a" * 64).status_code == 404
    assert viewer.get("/blob/XYZ").status_code == 404


def test_evicted_blob_is_downloaded_on_demand(viewer, env):
    from wa_archive.store import BlobStore
    sha = _blob_sha(env)
    path = BlobStore(env.paths.archive_dir).path(sha, "jpg")
    env.cloud.states[path] = CloudState.EVICTED
    assert viewer.get(f"/blob/{sha}").status_code == 200  # FakeCloud downloads instantly
    env.cloud.states[path] = CloudState.EVICTED
    env.cloud.ensure_local = lambda p, timeout=0, poll=0: False  # iCloud still downloading
    r = viewer.get(f"/blob/{sha}")
    assert r.status_code == 503 and r.headers["retry-after"] == "3"


# ------------------------------------------------------------------ search, calls, recovery

def test_search(viewer):
    r = viewer.get("/api/search?q=caption").json()
    assert len(r["results"]) == 1
    hit = r["results"][0]
    assert hit["chat"] == "Contact-Alpha" and "\x01caption\x02" in hit["snippet"]
    assert viewer.get("/api/search?q=capt*").json()["results"]
    gid = chat_id(viewer, "Group-Charlie")
    assert viewer.get(f"/api/search?q=caption&chat={gid}").json()["results"] == []
    for nasty in ['"', "NEAR(", "*", "a OR", "') DROP TABLE messages; --", "fixture AND NOT"]:
        assert viewer.get("/api/search", params={"q": nasty}).status_code == 200


def test_fts_query_quotes_every_term():
    assert fts_query('hello "wor ld" pre*') == '"hello" "wor" "ld" "pre"*'
    assert fts_query("   ") is None


def test_calls(viewer):
    calls = viewer.get("/api/calls").json()
    assert len(calls) == 2 and calls[0]["with"]


def test_serve_after_restoring_from_archive_folder(env):
    fx = basic_fixture()
    ingest(env, fx)
    shutil.rmtree(env.paths.state_dir)
    assert Store(env.paths, env.cloud).sync() == "pulled"
    c = client_for(env)
    assert len(c.get("/api/chats").json()) == 3
    assert c.get(f"/blob/{_blob_sha(env)}").status_code == 200


# ------------------------------------------------------------------ performance

def big_fixture(n: int) -> Fixture:
    fx = Fixture()
    fx.chats = {1: ("120363000000777@g.us", "Group-Big", 1)}
    fx.members = {1: (1, "64210000101@s.whatsapp.net", "Member-1"), 2: (1, "77770000102@lid", "Member-2")}
    t = 400_000_000.0
    for i in range(1, n + 1):
        t += 90
        media = Media(f"Media/big/{i}.jpg", f"img{i}".encode()) if i % 500 == 0 else None
        fx.messages.append(Msg(i, 1, i % 3 == 0, t, None if media else f"fixture-secret-text {i} word{i % 97}",
                               1 if media else 0, stanza(i), 1 + i % 2, None, media))
    return fx


@pytest.mark.slow
def test_50k_message_chat_is_fast(env):
    fx = big_fixture(50_000)
    ingest(env, fx)
    c = client_for(env)
    cid = chat_id(c, "Group-Big")

    def timed(url):
        t0 = time.perf_counter()
        r = c.get(url)
        assert r.status_code == 200
        return time.perf_counter() - t0, r.json()

    dt_open, page = timed(f"/api/chats/{cid}/messages?limit=80")
    worst = dt_open
    for _ in range(20):  # scroll back through 1,600 messages
        first = page["messages"][0]
        dt, page = timed(f"/api/chats/{cid}/messages?limit=80&before={first['ts']}:{first['rid']}")
        worst = max(worst, dt)
    mid_key = one(db(env), "SELECT msg_key FROM messages ORDER BY ts LIMIT 1 OFFSET 25000")
    dt_jump, around = timed(f"/api/chats/{cid}/messages?limit=80&around={mid_key}")
    dt_search, res = timed("/api/search?q=word42")
    dt_chats, _ = timed("/api/chats")
    assert around["target"] == mid_key and res["results"]
    assert worst < 0.25, worst
    assert dt_jump < 0.25 and dt_search < 0.5 and dt_chats < 0.5, (dt_jump, dt_search, dt_chats)


# ------------------------------------------------------------------ live updates

def test_viewer_picks_up_a_new_ingest_without_restart(env):
    fx = basic_fixture()
    ingest(env, fx)
    app = create_app(env.paths, env.cloud, token="tok", port=PORT, refresh_interval=0)
    c = TestClient(app, base_url=BASE)
    c.get("/?t=tok")
    v1 = c.get("/api/version").json()
    alpha = chat_id(c, "Contact-Alpha")
    before = c.get(f"/api/chats/{alpha}/messages?limit=200").json()["messages"]

    fx2 = copy.deepcopy(fx)
    t = max(m.date for m in fx.messages)
    fx2.messages.append(Msg(900, 1, 0, t + 60, "fixture-secret-text brand new", 0, stanza(900),
                            None, fx.chats[1][0]))
    fx2.chats[9] = ("64210000555@s.whatsapp.net", "Contact-Newcomer", 0)
    fx2.messages.append(Msg(901, 9, 0, t + 120, "fixture-secret-text hello", 0, stanza(901),
                            None, "64210000555@s.whatsapp.net"))
    ingest(env, fx2)  # while the viewer app is still running

    v2 = c.get("/api/version").json()
    assert v2["version"] != v1["version"] and v2["messages"] == v1["messages"] + 2
    assert not v2["ingest_running"]
    chats = c.get("/api/chats").json()
    assert chats[0]["name"] == "Contact-Newcomer"  # new chat, named, sorted first
    last = before[-1]
    newer = c.get(f"/api/chats/{alpha}/messages?after={last['ts']}:{last['rid']}").json()["messages"]
    assert [m["text"] for m in newer] == ["fixture-secret-text brand new"]
    assert c.get("/api/search?q=brand").json()["results"]


def test_version_reports_running_ingest(env):
    from wa_archive.cloud import CloudState
    from wa_archive.ingest import Options, UploadTimeout

    from .test_ingest import _tiny_disk
    env.cloud.default = CloudState.PENDING
    fx = basic_fixture()
    with pytest.raises(UploadTimeout):  # stops between batches, run left 'running'
        ingest(env, fx, free_space=_tiny_disk(env, fx, room=30), options=Options(margin=0, upload_timeout=0))
    c = client_for(env)
    assert c.get("/api/version").json()["ingest_running"] is True


def test_frontend_polls_for_updates():
    js = (Path(__file__).parents[1] / "src/wa_archive/viewer/static/app.js").read_text()
    assert "/api/version" in js and "setInterval(pollVersion" in js
