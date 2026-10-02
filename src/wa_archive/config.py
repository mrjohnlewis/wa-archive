"""Paths and settings.

Local state (working DB, temp files) lives outside any synced folder.
The archive lives in iCloud Drive by default. Both can be overridden with
environment variables, which the tests use to stay inside a temp dir.
"""

from __future__ import annotations

import os
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


def load_paths() -> Paths:
    return Paths(
        backup_root=Path(os.environ.get("WA_ARCHIVE_BACKUP_ROOT", DEFAULT_BACKUP_ROOT)),
        state_dir=Path(os.environ.get("WA_ARCHIVE_STATE_DIR", DEFAULT_STATE_DIR)),
        archive_dir=Path(os.environ.get("WA_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR)),
    )


def private_dir(path: Path) -> Path:
    """Create a directory readable only by the current user."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path
