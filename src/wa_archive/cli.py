"""wa-archive command line."""

from __future__ import annotations

import argparse
import json
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

    for name in ("ingest", "report", "serve"):
        sub.add_parser(name, help="(later phase)").set_defaults(func=cmd_not_yet)

    # Turn SIGTERM / terminal close into a normal exit so `finally` blocks delete decrypted temp files.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))

    args = ap.parse_args(argv)
    setup_logging(args.debug)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
