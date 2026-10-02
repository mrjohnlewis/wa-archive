"""Read-only access to iPhone backups.

`FinderBackup` wraps an encrypted Finder/iTunes backup via iphone_backup_decrypt.
`DirSource` is a plain folder with the same logical layout, used for synthetic
test fixtures. Nothing here ever writes inside a backup folder.
"""

from __future__ import annotations

import contextlib
import io
import plistlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

SHARED_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
WHATSAPP_DOMAIN_LIKE = "%net.whatsapp.WhatsApp%"
CHAT_DB = "ChatStorage.sqlite"
CONTACTS_DB = "ContactsV2.sqlite"
CALLS_DB = "CallHistory.sqlite"
MEDIA_PREFIX = "Message/"  # ZMEDIALOCALPATH is relative to this folder in SHARED_DOMAIN


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    backup_id: str
    device_name: str | None
    product_type: str | None
    ios_version: str | None
    last_backup: datetime | None
    encrypted: bool | None
    whatsapp_version: str | None


@dataclass(frozen=True)
class BackupFile:
    domain: str
    relative_path: str
    size: int


def _read_plist(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return {}


def _whatsapp_version(info: dict) -> str | None:
    app = (info.get("Applications") or {}).get("net.whatsapp.WhatsApp") or {}
    meta = app.get("iTunesMetadata")
    if isinstance(meta, bytes):
        with contextlib.suppress(Exception):
            meta = plistlib.loads(meta)
    if isinstance(meta, dict):
        return meta.get("bundleShortVersionString") or meta.get("bundleVersion")
    return None


def read_info(path: Path) -> BackupInfo:
    """Metadata from the unencrypted Info.plist / Manifest.plist."""
    info = _read_plist(path / "Info.plist")
    manifest = _read_plist(path / "Manifest.plist")
    last = info.get("Last Backup Date")
    if isinstance(last, datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return BackupInfo(
        path=path,
        backup_id=path.name,
        device_name=info.get("Device Name") or info.get("Display Name"),
        product_type=info.get("Product Type"),
        ios_version=info.get("Product Version"),
        last_backup=last if isinstance(last, datetime) else None,
        encrypted=manifest.get("IsEncrypted"),
        whatsapp_version=_whatsapp_version(info),
    )


def list_backups(root: Path) -> list[BackupInfo]:
    if not root.is_dir():
        return []
    found = [read_info(p) for p in root.iterdir()
             if p.is_dir() and (p / "Manifest.plist").exists()]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(found, key=lambda b: b.last_backup or epoch, reverse=True)


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        with contextlib.suppress(OSError):
            st = p.lstat()
            total += st.st_blocks * 512
    return total


class BackupSource(Protocol):
    info: BackupInfo

    def files(self, domain_like: str = WHATSAPP_DOMAIN_LIKE) -> Iterator[BackupFile]: ...

    def extract(self, domain: str, relative_path: str, dest: Path) -> None: ...

    def close(self) -> None: ...


class FinderBackup:
    """Encrypted Finder backup. Opening derives keys from the password (slow, ~10-30 s)."""

    def __init__(self, path: Path, password: str, tmp_dir: Path):
        from iphone_backup_decrypt import EncryptedBackup
        from iphone_backup_decrypt.exceptions import IncorrectPassphraseError

        self.info = read_info(path)
        if not self.info.encrypted:
            raise SystemExit("This backup is not encrypted. Enable 'Encrypt local backup' in Finder and back up again.")
        # The library puts its decrypted Manifest.db in tempfile's default dir;
        # point that at our private temp folder for the duration.
        self._old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(tmp_dir)
        self._backup = EncryptedBackup(backup_directory=str(path), passphrase=password)
        try:
            self._backup.test_decryption()
        except IncorrectPassphraseError:
            self.close()
            raise SystemExit("Wrong backup password. If it came from Keychain, run: wa-archive password --forget")

    def files(self, domain_like: str = WHATSAPP_DOMAIN_LIKE) -> Iterator[BackupFile]:
        from iphone_backup_decrypt.utils import FilePlist

        with self._backup.manifest_db_cursor() as cur:
            cur.execute("SELECT domain, relativePath, file FROM Files WHERE domain LIKE ? AND flags = 1",
                        (domain_like,))
            for domain, rel, blob in cur:
                yield BackupFile(domain, rel, FilePlist(blob).filesize)

    def extract(self, domain: str, relative_path: str, dest: Path) -> None:
        # The library prints size-mismatch notices to stdout; those name only our temp path.
        with contextlib.redirect_stdout(io.StringIO()):
            self._backup.extract_file(relative_path=relative_path, domain_like=domain,
                                      output_filename=str(dest))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._backup._cleanup()
        tempfile.tempdir = self._old_tempdir


class DirSource:
    """A folder laid out as <root>/files/<domain>/<relative_path>, plus Info.plist. For tests."""

    def __init__(self, path: Path):
        self.path = path
        self.info = read_info(path)

    def files(self, domain_like: str = WHATSAPP_DOMAIN_LIKE) -> Iterator[BackupFile]:
        root = self.path / "files"
        con = sqlite3.connect(":memory:")
        for ddir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not con.execute("SELECT ? LIKE ?", (ddir.name, domain_like)).fetchone()[0]:
                continue
            for f in sorted(ddir.rglob("*")):
                if f.is_file():
                    yield BackupFile(ddir.name, f.relative_to(ddir).as_posix(), f.stat().st_size)

    def extract(self, domain: str, relative_path: str, dest: Path) -> None:
        src = self.path / "files" / domain / relative_path
        if not src.is_file():
            raise FileNotFoundError(relative_path)
        shutil.copyfile(src, dest)

    def close(self) -> None:
        pass


def open_source(path: Path, tmp_dir: Path, password_fn) -> BackupSource:
    if (path / "files").is_dir() and not (path / "Manifest.db").exists():
        return DirSource(path)
    return FinderBackup(path, password_fn(), tmp_dir)
