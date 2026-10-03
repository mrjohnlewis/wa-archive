"""Paths and settings.

Local state (working DB, temp files) lives outside any synced folder.
The archive lives in iCloud Drive by default. Precedence for each path:
environment variable (used by the tests) > saved setting in
<state dir>/config.toml (`wa-archive config set ...`) > built-in default.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
DEFAULT_BACKUP_ROOT = HOME / "Library/Application Support/MobileSync/Backup"
DEFAULT_STATE_DIR = HOME / "Library/Application Support/wa-archive"
DEFAULT_ARCHIVE_DIR = HOME / "Library/Mobile Documents/com~apple~CloudDocs/WhatsApp Archive"


@dataclass(frozen=True)
class Paths:
    backup_root: Path
    state_dir: Path
    archive_dir: Path

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    @property
    def work_dir(self) -> Path:
        return self.state_dir / "work"


# Settings users can save: key -> (environment variable, built-in default).
SETTINGS = {
    "archive_dir": ("WA_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR),
    "backup_root": ("WA_ARCHIVE_BACKUP_ROOT", DEFAULT_BACKUP_ROOT),
}


def state_dir() -> Path:
    return Path(os.environ.get("WA_ARCHIVE_STATE_DIR", DEFAULT_STATE_DIR))


def config_file() -> Path:
    return state_dir() / "config.toml"


def load_settings() -> dict[str, str]:
    try:
        with open(config_file(), "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    return {k: v for k, v in data.items() if k in SETTINGS and isinstance(v, str)}


def save_settings(settings: dict[str, str]) -> None:
    private_dir(state_dir())
    tmp = config_file().with_suffix(".tmp")
    # JSON string escaping is valid TOML basic-string escaping.
    tmp.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in sorted(settings.items())))
    os.replace(tmp, config_file())


def resolve(key: str) -> tuple[Path, str]:
    """Effective value of a setting and where it came from ('env', 'config' or 'default')."""
    env, default = SETTINGS[key]
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser(), "env"
    saved = load_settings().get(key)
    if saved:
        return Path(saved).expanduser(), "config"
    return default, "default"


def load_paths() -> Paths:
    return Paths(backup_root=resolve("backup_root")[0], state_dir=state_dir(), archive_dir=resolve("archive_dir")[0])


def private_dir(path: Path) -> Path:
    """Create a directory readable only by the current user."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def sweep_tmp(tmp: Path) -> int:
    """Delete leftovers in our private temp dir (e.g. decrypted DBs from a killed run).

    Only ever called on wa-archive's own tmp dir, never on a backup or the archive.
    """
    n = 0
    for p in tmp.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        n += 1
    return n
