"""wa-archive command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import password as pw
from .backup import BackupInfo, dir_size, list_backups, open_source, read_info
from .config import load_paths, private_dir, sweep_tmp
from .privacy import setup_logging

console = Console(emoji=False, highlight=False)


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def age(dt: datetime | None) -> str:
    if not dt:
        return "?"
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return f"{hours:.1f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def pick_backup(arg: str | None) -> BackupInfo:
    paths = load_paths()
    if arg and Path(arg).expanduser().is_dir():
        return read_info(Path(arg).expanduser())
    backups = list_backups(paths.backup_root)
    if arg:
        backups = [b for b in backups if b.backup_id.startswith(arg)]
    if not backups:
        raise SystemExit(f"No matching backup found in {paths.backup_root}")
    if len(backups) > 1 and arg:
        raise SystemExit("Backup id prefix is ambiguous; give more characters.")
    return backups[0]


# ---------------------------------------------------------------- commands

def cmd_backups(args) -> None:
    paths = load_paths()
    backups = list_backups(paths.backup_root)
    if not backups:
        console.print(f"No backups in {paths.backup_root}")
        return
    console.print(f"iPhone backups in {escape(str(paths.backup_root))} (newest first):\n")
    for i, b in enumerate(backups):
        when = b.last_backup.astimezone().strftime("%Y-%m-%d %H:%M %Z") if b.last_backup else "?"
        console.print(f"[bold]{escape(b.backup_id)}[/]" + ("  (default)" if i == 0 else ""))
        console.print(f"  device:      {escape(b.device_name or '?')}  ({escape(b.product_type or '?')}, "
                      f"iOS {escape(b.ios_version or '?')})")
        console.print(f"  last backup: {when}  ({age(b.last_backup)})")
        console.print(f"  encrypted:   {b.encrypted}    WhatsApp: {escape(b.whatsapp_version or 'not found')}")
        if not args.no_size:
            console.print(f"  size:        {human(dir_size(b.path))}")
        console.print()
    console.print("Nothing was modified.")


def cmd_spike(args) -> None:
    from .spike import run_spike

    paths = load_paths()
    info = pick_backup(args.backup)
    tmp = private_dir(paths.tmp_dir)
    if n := sweep_tmp(tmp):
        console.print(f"Removed {n} leftover temp item(s) from an interrupted earlier run.")
    console.print(f"Backup [bold]{info.backup_id}[/] ({info.product_type}, iOS {info.ios_version}, "
                  f"backed up {age(info.last_backup)})")
    timings = {}
    t0 = time.time()
    source = open_source(info.path, tmp, pw.get_password)
    timings["unlock_keybag_and_manifest"] = round(time.time() - t0, 1)
    try:
        with console.status("Analysing (counts and structure only)..."):
            report = run_spike(source, tmp, shutil.disk_usage(Path.home()).free,
                               do_crosscheck=args.crosscheck, timings=timings)
    finally:
        source.close()
    render(report)
    out = private_dir(paths.state_dir / "spike") / f"spike-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    console.print(f"\nSaved (counts only) to {out}")


def cmd_password(args) -> None:
    if args.save:
        pw.save_password()
        console.print("Saved in your login Keychain (service 'wa-archive').")
    elif args.forget:
        console.print("Removed." if pw.forget_password() else "Nothing was saved.")
    else:
        console.print("A password is saved in Keychain." if pw.saved_password()
                      else "No password saved; you'll be prompted each run.")


def cmd_not_yet(args) -> None:
    raise SystemExit(f"'{args.command}' arrives in a later phase.")


def _caffeinate() -> None:
    """Re-exec under `caffeinate -i` so the Mac doesn't idle-sleep during long uploads."""
    if os.environ.get("WA_ARCHIVE_CAFFEINATED") or sys.platform != "darwin" or not shutil.which("caffeinate"):
        return
    env = dict(os.environ, WA_ARCHIVE_CAFFEINATED="1")
    os.execvpe("caffeinate", ["caffeinate", "-i", sys.executable, "-m", "wa_archive", *sys.argv[1:]], env)


def cmd_ingest(args) -> None:
    from .cloud import Cloud
    from .ingest import Options, run_ingest
    from .store import ArchiveError

    if not args.no_caffeinate and not args.dry_run:
        _caffeinate()
    paths = load_paths()
    info = pick_backup(args.backup)
    console.print(f"Backup [bold]{info.backup_id}[/] ({escape(info.product_type or '?')}, iOS "
                  f"{escape(info.ios_version or '?')}, backed up {age(info.last_backup)})")
    console.print(f"Archive: {escape(str(paths.archive_dir))}")
    opts = Options(margin=int(args.margin_gb * 1024**3), upload_timeout=args.upload_timeout_hours * 3600,
                   dry_run=args.dry_run)
    t0 = time.time()
    try:
        stats = run_ingest(paths, lambda: open_source(info.path, private_dir(paths.tmp_dir), pw.get_password),
                           Cloud(), options=opts, progress=lambda m: console.print(f"  {escape(m)}"))
    except ArchiveError as e:
        raise SystemExit(f"Stopped: {e}")
    console.rule("ingest summary" + (" (dry run, nothing written)" if stats.get("dry_run") else ""))
    for k, v in stats.items():
        console.print(f"{k}: {_fmt(k, v)}")
    console.print(f"took {time.time() - t0:.0f} s")
    if not stats.get("dry_run"):
        console.print("\nNext: [bold]wa-archive report[/] (use --wait-upload to wait for iCloud).")


def cmd_report(args) -> None:
    from .cloud import Cloud
    from .store import ArchiveError, run_lock

    paths = load_paths()
    try:
        with run_lock(paths):  # don't race a running ingest
            rep = _build_report(args, paths, Cloud())
    except ArchiveError as e:
        raise SystemExit(f"Stopped: {e}")
    if "error" in rep:
        raise SystemExit(rep["error"])
    from .report import save_report, status_label
    render_report(rep, show_all=args.all, status_label=status_label)
    if not args.quick:
        md, _ = save_report(rep, paths)
        console.print(f"\nSaved: {escape(str(md))} (+ .json)")


def _build_report(args, paths, cloud) -> dict:
    from rich.progress import Progress

    from .report import build_report, wait_for_uploads
    from .store import Store

    if args.wait_upload:
        store = Store(paths, cloud)
        store.sync()
        con = store.open()
        try:
            with Progress(console=console, transient=True) as prog:
                task = prog.add_task("Checking iCloud upload status", total=None)
                scanned = lambda i, n: prog.update(task, completed=i, total=n)  # noqa: E731
                ok = wait_for_uploads(store, con, cloud, args.wait_upload * 3600, scanned=scanned,
                                      say=lambda m: prog.update(task, description=escape(m)))
        finally:
            con.close()
        if not ok:
            console.print("[yellow]Uploads still pending after waiting; the report will show them.[/]")
    with Progress(console=console, transient=True) as prog:
        task = prog.add_task("Checking and re-hashing archived media", total=None)
        return build_report(paths, cloud, quick=args.quick, max_age_hours=args.max_age_hours,
                            progress=lambda i, n: prog.update(task, completed=i, total=n))


def cmd_evict(args) -> None:
    from .cloud import Cloud
    from .ingest import evict_uploaded
    from .store import ArchiveError

    try:
        n, size = evict_uploaded(load_paths(), Cloud())
    except ArchiveError as e:
        raise SystemExit(f"Stopped: {e}")
    console.print(f"Evicted {n:,} verified, uploaded media file(s) ({human(size)}) from this Mac. "
                  "They stay in iCloud and download again on demand.")


def render_report(rep: dict, show_all: bool, status_label) -> None:
    from .report import human

    age_h = rep["backup_age_hours"]
    stale = age_h is None or age_h > 6
    console.rule("[bold]WhatsApp archive report[/]")
    console.print(f"[bold {'red' if stale else 'green'}]Backup taken {escape(str(rep['backup_date']))} "
                  f"({age_h} h ago)[/]   run {escape(rep['run_id'])}")
    console.print("Media received after this backup is NOT archived, and Manage Storage clears it anyway.")
    for b in rep["blockers"]:
        console.print(f"[bold red]BLOCKER:[/] {escape(b)}")
    for w in rep["warnings"]:
        console.print(f"[bold red]WARNING:[/] {escape(w)}")
    t = rep["totals"]
    console.print(f"\n{t['chats']} chats · {t['messages']:,} messages ({t['messages_new']:,} new) · "
                  f"{t['media_present']:,} media archived ({t['media_new']:,} new) · {t['media_missing']:,} missing · "
                  f"{human(t['archived_bytes'])} archived · {rep['blobs_rehashed']:,} files re-hashed")
    if t["blob_problems"]:
        console.print(f"[red]File problems:[/] {t['blob_problems']}")
    table = Table(show_lines=False)
    for col, j in (("Chat", "left"), ("Kind", "left"), ("Msgs", "right"), ("New", "right"), ("From", "left"),
                   ("To", "left"), ("Media ok/miss/new", "right"), ("Archived", "right"), ("Status", "left")):
        table.add_column(col, justify=j)
    shown = [c for c in rep["chats"] if show_all or (not c["hidden"] and (c["media_present"] or c["media_missing"]
                                                                          or c["messages_new"]))]
    for c in shown:
        label = status_label(c)
        table.add_row(escape(c["name"]), c["kind"], f"{c['messages']:,}", f"{c['messages_new']:,}", c["first"],
                      c["last"], f"{c['media_present']}/{c['media_missing']}/{c['media_new']}",
                      human(c["archived_bytes"]), f"[{'green' if c['safe'] else 'red'}]{escape(label)}[/]")
    console.print(table)
    hidden = [c for c in rep["chats"] if c["hidden"]]
    if hidden and not show_all:
        console.print(f"(+ {len(hidden)} status/channel chats hidden: "
                      f"{sum(c['safe'] for c in hidden)} safe; use --all to show)")
    safe = [c for c in rep["chats"] if c["safe"] and c["media_in_latest_backup"]]
    console.rule(f"[bold green]Safe to clear media ({len(safe)} chats)[/]")
    for c in safe:
        console.print(f"  ✓ {escape(c['name'])}" + ("" if status_label(c) == "safe" else f"  ({status_label(c)})"))
    if not safe:
        console.print("  (none yet)")


def cmd_restore(args) -> None:
    from .cloud import Cloud
    from .store import ArchiveError, Store, run_lock

    paths = load_paths()
    try:
        with run_lock(paths):
            result = Store(paths, Cloud()).sync()
    except ArchiveError as e:
        raise SystemExit(f"Stopped: {e}")
    console.print({"pulled": "Restored the local working copy from the archive (hash and integrity verified).",
                   "in-sync": "Local working copy already matches the archive.",
                   "new": "No archive found yet.",
                   "needs-publish": "Local copy is newer than the archive; run `wa-archive ingest` to publish it."}
                  [result])


# ---------------------------------------------------------------- rendering

def _fmt(key: str, v) -> str:
    if isinstance(v, int) and not isinstance(v, bool):
        if "bytes" in key:
            return f"{human(v)} ({v:,})"
        return str(v) if "year" in key else f"{v:,}"
    return escape(str(v))


def _render_value(key: str, v, indent: int) -> None:
    pad = "  " * indent
    if isinstance(v, dict):
        console.print(f"{pad}[bold]{escape(key)}[/]:" + ("" if v else " (none)"))
        for k2, x in v.items():
            _render_value(k2, x, indent + 1)
    elif isinstance(v, list) and v and isinstance(v[0], dict):
        cols = list(dict.fromkeys(k for row in v for k in row))
        t = Table(title=escape(key), title_justify="left")
        for c in cols:
            t.add_column(c, justify="right")
        for row in v:
            t.add_row(*[_fmt(c, row.get(c, 0)) for c in cols])
        console.print(t)
    elif isinstance(v, list):
        console.print(f"{pad}[bold]{escape(key)}[/]: {escape(', '.join(map(str, v))) or '(none)'}")
    else:
        console.print(f"{pad}{escape(key)}: {_fmt(key, v)}")


def render(report: dict) -> None:
    for section, v in report.items():
        console.rule(section)
        if isinstance(v, dict):
            for k, x in v.items():
                _render_value(k, x, 0)
        else:
            _render_value(section, v, 0)


# ---------------------------------------------------------------- entry

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="wa-archive", description="Archive WhatsApp from iPhone backups.")
    ap.add_argument("--debug", action="store_true", help="show full errors (synthetic data only)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backups", help="list iPhone backups (no password needed)")
    p.add_argument("--no-size", action="store_true", help="skip computing backup sizes")
    p.set_defaults(func=cmd_backups)

    p = sub.add_parser("spike", help="phase-1 check: counts and structure only")
    p.add_argument("--backup", help="backup id (prefix) or folder; default newest")
    p.add_argument("--crosscheck", action="store_true", help="compare counts with wtsexporter")
    p.set_defaults(func=cmd_spike)

    p = sub.add_parser("password", help="manage the Keychain-saved backup password")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--save", action="store_true")
    g.add_argument("--forget", action="store_true")
    p.set_defaults(func=cmd_password)

    p = sub.add_parser("ingest", help="merge the newest backup into the archive")
    p.add_argument("--backup", help="backup id (prefix) or folder; default newest")
    p.add_argument("--dry-run", action="store_true", help="read and plan only; write nothing")
    p.add_argument("--margin-gb", type=float, default=2.0, help="disk space to always keep free (default 2)")
    p.add_argument("--upload-timeout-hours", type=float, default=6.0)
    p.add_argument("--no-caffeinate", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("report", help="per-chat verification and the safe-to-clear list")
    p.add_argument("--quick", action="store_true", help="skip re-hashing (no safe list)")
    p.add_argument("--wait-upload", type=float, metavar="HOURS", default=0,
                   help="first wait up to HOURS for iCloud uploads to finish")
    p.add_argument("--max-age-hours", type=float, default=6.0, help="warn if the backup is older than this")
    p.add_argument("--all", action="store_true", help="include status/channel chats and chats without media")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("evict", help="free Mac disk: evict archived media that is verified and in iCloud")
    p.set_defaults(func=cmd_evict)

    p = sub.add_parser("restore", help="rebuild local state from the archive folder (e.g. on a new Mac)")
    p.set_defaults(func=cmd_restore)

    sub.add_parser("serve", help="(phase 3)").set_defaults(func=cmd_not_yet)

    # Turn SIGTERM / terminal close into a normal exit so `finally` blocks delete decrypted temp files.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))

    args = ap.parse_args(argv)
    setup_logging(args.debug)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
