"""Local, read-only web viewer for the archive.

Bound to 127.0.0.1 only. Every request must carry the per-launch secret (as a
cookie set from the launch URL) and a localhost Host header, which blocks other
local users' browsers, other websites and DNS-rebinding tricks. The archive DB
is opened read-only; media is served from the content-addressed store, with
evicted iCloud files downloaded on demand.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..cloud import CloudState
from ..config import Paths
from ..report import chat_names
from ..store import BlobStore
from ..whatsapp_reader import HIDDEN_KINDS

STATIC = Path(__file__).parent / "static"
COOKIE = "wa_archive_session"
PAGE_MAX = 200
NAME_PRIORITY = {"contact": 0, "push": 1, "member": 2, "chat": 3}
MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        "heic": "image/heic", "mp4": "video/mp4", "mov": "video/quicktime", "m4v": "video/mp4",
        "opus": "audio/ogg", "ogg": "audio/ogg", "m4a": "audio/mp4", "mp3": "audio/mpeg", "aac": "audio/aac",
        "pdf": "application/pdf", "thumb": "image/jpeg", "mmsthumb": "image/jpeg", "favicon": "image/png"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic"}
VIDEO_EXT = {"mp4", "mov", "m4v"}
AUDIO_EXT = {"opus", "ogg", "m4a", "mp3", "aac"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def media_kind(msg_type: int | None, ext: str | None) -> str:
    if msg_type == 15:
        return "sticker"
    if msg_type == 11:
        return "gif"
    if msg_type == 1 or ext in IMAGE_EXT:
        return "image"
    if msg_type == 2 or ext in VIDEO_EXT:
        return "video"
    if msg_type == 3 or ext in AUDIO_EXT:
        return "audio"
    return "document"


def fts_query(q: str) -> str | None:
    """Turn free text into a safe FTS5 query: every term quoted (AND), 'term*' keeps prefix matching."""
    terms = []
    for raw in q.split():
        prefix = raw.endswith("*")
        t = raw.rstrip("*").replace('"', "")
        if t:
            terms.append(f'"{t}"' + ("*" if prefix else ""))
    return " ".join(terms) or None


class Archive:
    """Read-only queries against the local working copy of the archive DB."""

    def __init__(self, paths: Paths, cloud, refresh_interval: float = 2.0):
        self.db_path = paths.work_dir / "archive.sqlite"
        self.blobs = BlobStore(paths.archive_dir)
        self.cloud = cloud
        self.refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._checked = 0.0
        self.loaded_version = self.version()["version"]
        self.refresh()

    def version(self) -> dict:
        """Changes whenever an ingest publishes a batch (generation) or starts/finishes a run."""
        with self.con() as con:
            gen = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
            run = con.execute("SELECT run_id, status, batches_done, backup_date FROM runs "
                              "ORDER BY rowid DESC LIMIT 1").fetchone()
            messages = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        token = f"{gen[0] if gen else 0}:{run['run_id'] if run else ''}:{run['status'] if run else ''}:" \
                f"{run['batches_done'] if run else 0}:{messages}"
        return {"version": token, "messages": messages, "ingest_running": bool(run and run["status"] == "running"),
                "backup_date": run["backup_date"] if run else None}

    def maybe_refresh(self) -> None:
        """Reload cached names/chats if an ingest has changed the archive (checked at most every few seconds)."""
        now = time.monotonic()
        if now - self._checked < self.refresh_interval:
            return
        with self._lock:
            if now - self._checked < self.refresh_interval:
                return
            self._checked = now
            v = self.version()["version"]
            if v != self.loaded_version:
                self.refresh()
                self.loaded_version = v

    @contextmanager
    def con(self):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def refresh(self) -> None:
        """Load the small lookup tables: names, chats and their aliases."""
        with self.con() as con:
            best: dict[str, tuple[int, str]] = {}
            for r in con.execute("SELECT jid, name, source FROM names ORDER BY last_seen_run DESC"):
                p = NAME_PRIORITY.get(r["source"], 9)
                if r["jid"] not in best or p < best[r["jid"]][0]:
                    best[r["jid"]] = (p, r["name"])
            self.person = {jid: name for jid, (_, name) in best.items()}
            self.chat_name = chat_names(con)
            self.aliases: dict[int, list[str]] = defaultdict(list)
            self.chat_of: dict[str, int] = {}
            for r in con.execute("SELECT jid, chat_id FROM chat_aliases"):
                self.aliases[r["chat_id"]].append(r["jid"])
                self.chat_of[r["jid"]] = r["chat_id"]
            self.kind = {r["chat_id"]: r["kind"] for r in con.execute(
                "SELECT chat_id, kind FROM chats WHERE merged_into IS NULL")}

    # ------------------------------------------------------------- names

    def person_name(self, jid: str | None) -> str | None:
        if not jid:
            return None
        if jid in self.person:
            return self.person[jid]
        user, _, domain = jid.partition("@")
        return f"+{user}" if domain == "s.whatsapp.net" else user

    # ------------------------------------------------------------- chats

    def chats(self, include_hidden: bool) -> list[dict]:
        out = []
        with self.con() as con:
            stats = {r["chat_id"]: r for r in con.execute("""
                SELECT a.chat_id, count(*) n, max(m.ts) last_ts, min(m.ts) first_ts
                FROM messages m JOIN chat_aliases a ON a.jid = m.chat_jid GROUP BY a.chat_id""")}
            last = {}
            for r in con.execute("""
                    SELECT a.chat_id, m.type, m.text, m.title, m.from_me, m.revoked_run FROM messages m
                    JOIN chat_aliases a ON a.jid = m.chat_jid
                    JOIN (SELECT a2.chat_id cid, max(m2.ts) mts FROM messages m2
                          JOIN chat_aliases a2 ON a2.jid = m2.chat_jid GROUP BY a2.chat_id) t
                      ON t.cid = a.chat_id AND t.mts = m.ts"""):
                last[r["chat_id"]] = r
        for cid, kind in self.kind.items():
            hidden = kind in HIDDEN_KINDS
            if (hidden and not include_hidden) or cid not in stats:
                continue
            s = stats[cid]
            out.append({"id": cid, "name": self.chat_name.get(cid, "?"), "kind": kind, "hidden": hidden,
                        "count": s["n"], "first_ts": s["first_ts"], "last_ts": s["last_ts"],
                        "preview": preview(last.get(cid))})
        out.sort(key=lambda c: c["last_ts"] or 0, reverse=True)
        return out

    def chat_info(self, cid: int) -> dict | None:
        if cid not in self.kind:
            return None
        return {"id": cid, "name": self.chat_name.get(cid, "?"), "kind": self.kind[cid]}

    # ------------------------------------------------------------- messages

    def page(self, cid: int, *, before: tuple | None = None, after: tuple | None = None,
             around_key: str | None = None, around_ts: float | None = None, limit: int = 80) -> dict:
        jids = self.aliases.get(cid)
        if not jids:
            return {"messages": [], "has_older": False, "has_newer": False}
        limit = max(1, min(limit, PAGE_MAX))
        marks = ",".join("?" * len(jids))
        base = f"SELECT m.rowid AS rid, m.* FROM messages m WHERE m.chat_jid IN ({marks})"
        with self.con() as con:
            def older(cursor, n):
                if cursor is None:
                    rows = con.execute(f"{base} ORDER BY m.ts DESC, m.rowid DESC LIMIT ?", (*jids, n + 1))
                else:
                    rows = con.execute(f"{base} AND (m.ts, m.rowid) < (?, ?) ORDER BY m.ts DESC, m.rowid DESC "
                                       "LIMIT ?", (*jids, *cursor, n + 1))
                rows = rows.fetchall()
                return rows[:n][::-1], len(rows) > n

            def newer(cursor, n, inclusive=False):
                op = ">=" if inclusive else ">"
                rows = con.execute(f"{base} AND (m.ts, m.rowid) {op} (?, ?) ORDER BY m.ts, m.rowid LIMIT ?",
                                   (*jids, *cursor, n + 1)).fetchall()
                return rows[:n], len(rows) > n

            target = None
            if around_key or around_ts is not None:
                if around_key:
                    r = con.execute(f"{base} AND m.msg_key = ?", (*jids, around_key)).fetchone()
                    cursor = (r["ts"], r["rid"]) if r else None
                    target = around_key if r else None
                else:
                    cursor = (around_ts, 0)
                if cursor is None:
                    rows, has_older = older(None, limit)
                    has_newer = False
                else:
                    before_rows, has_older = older(cursor, limit // 2)
                    after_rows, has_newer = newer(cursor, limit - len(before_rows), inclusive=True)
                    rows = before_rows + after_rows
                    if target is None and after_rows:
                        target = after_rows[0]["msg_key"]
            elif after is not None:
                rows, has_newer = newer(after, limit)
                has_older = True
            else:
                rows, has_older = older(before, limit)
                has_newer = before is not None
            messages = self._serialize(con, cid, rows)
        return {"messages": messages, "has_older": has_older, "has_newer": has_newer, "target": target}

    def _serialize(self, con, cid: int, rows: list) -> list[dict]:
        if not rows:
            return []
        keys = [r["msg_key"] for r in rows]
        km = ",".join("?" * len(keys))
        media = {r["msg_key"]: r for r in con.execute(f"""
            SELECT md.*, b.ext, b.size FROM media md LEFT JOIN blobs b ON b.sha256 = md.sha256
            WHERE md.msg_key IN ({km})""", keys)}
        thumbs = {}
        tpaths = [m["thumb_path"] for m in media.values() if m["thumb_path"]]
        if tpaths:
            thumbs = {r["path"]: r["sha256"] for r in con.execute(
                f"SELECT path, sha256 FROM extra_files WHERE path IN ({','.join('?' * len(tpaths))})", tpaths)}
        edits = {}
        edited = [r["msg_key"] for r in rows if r["edited"]]
        if edited:
            for r in con.execute(f"""SELECT msg_key, text, title FROM message_versions WHERE kind = 'edit'
                                     AND msg_key IN ({','.join('?' * len(edited))}) ORDER BY id""", edited):
                edits[r["msg_key"]] = r
        reactions: dict[str, dict] = defaultdict(dict)
        for r in con.execute(f"""SELECT msg_key, reactor_jid, emoji, ts FROM reactions WHERE msg_key IN ({km})
                                 ORDER BY coalesce(ts, 0), rowid""", keys):
            reactions[r["msg_key"]][r["reactor_jid"] or "me"] = r["emoji"]  # latest per person wins
        quotes = {}
        qids = list({r["quoted_stanza"] for r in rows if r["quoted_stanza"]})
        if qids:
            jids = self.aliases[cid]
            for q in con.execute(f"""SELECT m.*, md.sha256, md.status, b.ext FROM messages m
                                     LEFT JOIN media md ON md.msg_key = m.msg_key
                                     LEFT JOIN blobs b ON b.sha256 = md.sha256
                                     WHERE m.stanza_id IN ({','.join('?' * len(qids))})
                                     AND m.chat_jid IN ({','.join('?' * len(jids))})""", (*qids, *jids)):
                quotes[q["stanza_id"]] = {
                    "key": q["msg_key"], "sender": "You" if q["from_me"] else self.person_name(q["sender_jid"]),
                    "text": (q["text"] or q["title"] or "")[:200], "type": q["type"],
                    "media": media_kind(q["type"], q["ext"]) if q["sha256"] else None,
                    "sha": q["sha256"] if q["status"] == "present" and media_kind(q["type"], q["ext"]) in (
                        "image", "sticker") else None}

        out = []
        for r in rows:
            e = edits.get(r["msg_key"])
            md = media.get(r["msg_key"])
            m = {
                "key": r["msg_key"], "ts": r["ts"], "rid": r["rid"], "from_me": bool(r["from_me"]),
                "sender": None if r["from_me"] else self.person_name(r["sender_jid"]),
                "sender_jid": None if r["from_me"] else r["sender_jid"],
                "type": r["type"], "text": e["text"] if e else r["text"], "title": e["title"] if e else r["title"],
                "edited": bool(r["edited"]), "revoked": r["revoked_run"] is not None,
                "revoked_run": r["revoked_run"],
                "quote": quotes.get(r["quoted_stanza"]) or (
                    {"missing": True} if r["quoted_stanza"] else None),
                "reactions": _aggregate(reactions.get(r["msg_key"], {}), self.person_name),
            }
            if md:
                kind = media_kind(r["type"], md["ext"] or _ext(md["orig_path"]))
                aspect = None
                if kind in ("image", "video", "gif"):
                    a = (json.loads(r["raw_json"] or "{}").get("media_item") or {}).get("ZASPECTRATIO")
                    aspect = a if isinstance(a, (int, float)) and 0.2 < a < 5 else None
                m["media"] = {"status": md["status"], "sha": md["sha256"], "kind": kind, "aspect": aspect,
                              "size": md["size"] or md["size_expected"],
                              "ext": md["ext"] or _ext(md["orig_path"]),
                              "thumb": thumbs.get(md["thumb_path"])}
            if r["type"] in (4, 5):
                raw = json.loads(r["raw_json"] or "{}").get("media_item", {})
                if r["type"] == 5:
                    m["location"] = {"lat": raw.get("ZLATITUDE"), "lon": raw.get("ZLONGITUDE")}
                else:
                    m["contact"] = raw.get("ZVCARDNAME")
            out.append(m)
        return out

    def versions(self, key: str) -> list[dict]:
        with self.con() as con:
            orig = con.execute("SELECT text, title, first_seen_run FROM messages WHERE msg_key = ?", (key,)).fetchone()
            if not orig:
                return []
            out = [{"kind": "original", "run": orig["first_seen_run"], "text": orig["text"], "title": orig["title"]}]
            out += [{"kind": r["kind"], "run": r["run_id"], "text": r["text"], "title": r["title"]}
                    for r in con.execute("SELECT * FROM message_versions WHERE msg_key = ? ORDER BY id", (key,))]
        return out

    # ------------------------------------------------------------- search, calls

    def search(self, q: str, chat: int | None, limit: int = 50, offset: int = 0) -> dict:
        query = fts_query(q)
        if not query:
            return {"results": [], "has_more": False}
        limit = max(1, min(limit, 200))
        sql = """SELECT f.msg_key, snippet(fts, 1, char(1), char(2), '…', 14) snip, m.ts, m.chat_jid, m.from_me,
                        m.sender_jid
                 FROM fts f JOIN messages m ON m.msg_key = f.msg_key WHERE fts MATCH ?"""
        args: list = [query]
        if chat is not None:
            jids = self.aliases.get(chat, [])
            sql += f" AND m.chat_jid IN ({','.join('?' * len(jids)) or 'NULL'})"
            args += jids
        sql += " ORDER BY m.ts DESC LIMIT ? OFFSET ?"
        try:
            with self.con() as con:
                rows = con.execute(sql, (*args, limit + 1, offset)).fetchall()
        except sqlite3.OperationalError:
            return {"results": [], "has_more": False, "error": "Couldn't understand that search."}
        seen, results = set(), []
        for r in rows[:limit]:
            if r["msg_key"] in seen:
                continue
            seen.add(r["msg_key"])
            cid = self.chat_of.get(r["chat_jid"])
            results.append({"key": r["msg_key"], "chat_id": cid, "chat": self.chat_name.get(cid, "?"),
                            "ts": r["ts"], "snippet": r["snip"],
                            "sender": "You" if r["from_me"] else self.person_name(r["sender_jid"])})
        return {"results": results, "has_more": len(rows) > limit}

    def calls(self) -> list[dict]:
        with self.con() as con:
            rows = con.execute("SELECT * FROM calls ORDER BY ts DESC").fetchall()
        out = []
        for r in rows:
            raw = json.loads(r["raw_json"] or "{}")
            agg = raw.get("aggregate") or {}
            parts = json.loads(r["participants_json"] or "[]")
            out.append({"ts": r["ts"], "duration": r["duration"],
                        "video": bool(agg.get("ZVIDEO")), "incoming": agg.get("ZINCOMING"),
                        "missed": bool(agg.get("ZMISSED")),
                        "group": self.chat_name.get(self.chat_of.get(r["group_jid"])) if r["group_jid"] else None,
                        "with": [self.person_name(j) for j in parts] or
                                ([self.person_name(r["creator_jid"])] if r["creator_jid"] else [])})
        return out

    # ------------------------------------------------------------- media

    def blob_path(self, sha: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        with self.con() as con:
            r = con.execute("SELECT ext FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        return self.blobs.path(sha, r["ext"]) if r else None


def _ext(path: str | None) -> str | None:
    if path and "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return None


def _aggregate(by_person: dict[str, str], name) -> list[dict]:
    agg: dict[str, dict] = {}
    for who, emoji in by_person.items():
        if not emoji:
            continue  # reaction removed
        a = agg.setdefault(emoji, {"emoji": emoji, "count": 0, "who": []})
        a["count"] += 1
        a["who"].append("You" if who == "me" else name(who))
    return sorted(agg.values(), key=lambda a: -a["count"])


def preview(r) -> str:
    if r is None:
        return ""
    if r["revoked_run"]:
        return "🚫 Deleted for everyone"
    t = r["type"]
    label = {1: "📷 Photo", 2: "🎥 Video", 3: "🎤 Voice message", 4: "👤 Contact", 5: "📍 Location",
             8: "📄 Document", 11: "GIF", 14: "🚫 This message was deleted", 15: "Sticker", 46: "📊 Poll",
             66: "📊 Poll"}.get(t)
    text = r["text"] or r["title"] or ""
    body = f"{label} {text}".strip() if label and t not in (14,) else (text or label or "")
    return ("You: " if r["from_me"] else "") + body[:100]


# ----------------------------------------------------------------- web app

class Guard(BaseHTTPMiddleware):
    """Allow only localhost Host headers and requests holding the launch secret."""

    def __init__(self, app, token: str, hosts: set[str]):
        super().__init__(app)
        self.token, self.hosts = token, hosts

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("host", "") not in self.hosts:
            return PlainTextResponse("Forbidden host.", status_code=403)
        t = request.query_params.get("t")
        if t is not None:
            if not secrets.compare_digest(t, self.token):
                return PlainTextResponse("Invalid link.", status_code=403)
            resp = RedirectResponse(request.url.path or "/", status_code=303)
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="strict", path="/")
            return _secure(resp)
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), self.token):
            return PlainTextResponse("Open the link printed by `wa-archive serve` in the terminal.", status_code=403)
        return _secure(await call_next(request))


def _secure(resp: Response) -> Response:
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _cursor(v: str | None) -> tuple | None:
    if not v:
        return None
    ts, _, rid = v.partition(":")
    return float(ts), int(rid)


def create_app(paths: Paths, cloud, *, token: str, port: int, download_timeout: float = 25,
               refresh_interval: float = 2.0) -> Starlette:
    archive = Archive(paths, cloud, refresh_interval)

    def chats(request: Request):
        archive.maybe_refresh()
        return JSONResponse(archive.chats(request.query_params.get("hidden") == "1"))

    def chat(request: Request):
        archive.maybe_refresh()
        info = archive.chat_info(int(request.path_params["cid"]))
        return JSONResponse(info) if info else JSONResponse({"error": "no such chat"}, status_code=404)

    def messages(request: Request):
        archive.maybe_refresh()
        qp = request.query_params
        try:
            around_ts = float(qp["around_ts"]) if qp.get("around_ts") else None
            page = archive.page(int(request.path_params["cid"]), before=_cursor(qp.get("before")),
                                after=_cursor(qp.get("after")), around_key=qp.get("around"),
                                around_ts=around_ts, limit=int(qp.get("limit", 80)))
        except ValueError:
            return JSONResponse({"error": "bad cursor"}, status_code=400)
        return JSONResponse(page)

    def versions(request: Request):
        return JSONResponse(archive.versions(request.path_params["key"]))

    def search(request: Request):
        archive.maybe_refresh()
        qp = request.query_params
        chat_id = int(qp["chat"]) if qp.get("chat") else None
        return JSONResponse(archive.search(qp.get("q", ""), chat_id, int(qp.get("limit", 50)),
                                           int(qp.get("offset", 0))))

    def calls(request: Request):
        archive.maybe_refresh()
        return JSONResponse(archive.calls())

    def version(request: Request):
        return JSONResponse(archive.version(), headers={"Cache-Control": "no-store"})

    def blob(request: Request):
        path = archive.blob_path(request.path_params["sha"])
        if path is None:
            return PlainTextResponse("Not found.", status_code=404)
        state = cloud.state(path)
        if state == CloudState.MISSING:
            return PlainTextResponse("File missing from archive.", status_code=404)
        if state == CloudState.EVICTED and not cloud.ensure_local(path, timeout=download_timeout, poll=0.5):
            return PlainTextResponse("Downloading from iCloud; try again shortly.", status_code=503,
                                     headers={"Retry-After": "3"})
        ext = path.suffix.lstrip(".").lower()
        headers = {"Cache-Control": "private, max-age=31536000, immutable"}
        name = request.query_params.get("name")
        if name:
            safe = re.sub(r"[^\w .()-]", "_", name)[:120] or "file"
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return FileResponse(path, media_type=MIME.get(ext, "application/octet-stream"), headers=headers)

    def index(request: Request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    def info(request: Request):
        with archive.con() as con:
            run = con.execute("SELECT run_id, backup_date FROM runs WHERE status = 'complete' "
                              "ORDER BY rowid DESC LIMIT 1").fetchone()
        return JSONResponse({"run": run["run_id"] if run else None, "backup_date": run["backup_date"] if run else None,
                             "generated": datetime.now(timezone.utc).isoformat()})

    routes = [
        Route("/", index),
        Route("/api/info", info),
        Route("/api/version", version),
        Route("/api/chats", chats),
        Route("/api/chats/{cid:int}", chat),
        Route("/api/chats/{cid:int}/messages", messages),
        Route("/api/messages/{key}/versions", versions),
        Route("/api/search", search),
        Route("/api/calls", calls),
        Route("/blob/{sha}", blob),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return Starlette(routes=routes, middleware=[Middleware(Guard, token=token, hosts=hosts)])
