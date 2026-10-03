"""iCloud Drive file status: uploaded? evicted? Uses Foundation's NSURL resource values.

Never reads file contents (a read would make iCloud download an evicted file).
"""

from __future__ import annotations

import os
import time
from enum import Enum
from pathlib import Path

SF_DATALESS = 0x40000000


class CloudState(str, Enum):
    NOT_CLOUD = "not_cloud"        # local file outside iCloud Drive
    PENDING = "pending_upload"     # in iCloud Drive, local, not yet fully uploaded
    SYNCED = "synced"              # local and uploaded
    EVICTED = "evicted"            # uploaded, local copy removed (dataless)
    MISSING = "missing"            # no such file

    @property
    def local(self) -> bool:
        return self in (CloudState.NOT_CLOUD, CloudState.PENDING, CloudState.SYNCED)

    @property
    def uploaded(self) -> bool:
        return self in (CloudState.SYNCED, CloudState.EVICTED)


def _dataless(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_flags & SF_DATALESS)
    except OSError:
        return False


class Cloud:
    """Real implementation (macOS). Falls back to st_flags if pyobjc is unavailable."""

    def __init__(self):
        try:
            import Foundation as F
            self._F = F
        except ImportError:  # pragma: no cover
            self._F = None

    def _value(self, url, key):
        ok, value, _err = url.getResourceValue_forKey_error_(None, key, None)
        return value if ok else None

    def state(self, path: Path) -> CloudState:
        if not os.path.lexists(path) and not _icloud_placeholder(path).exists():
            return CloudState.MISSING
        if self._F is None:
            return CloudState.EVICTED if _dataless(path) else CloudState.NOT_CLOUD
        F = self._F
        url = F.NSURL.fileURLWithPath_(str(path))
        if not self._value(url, F.NSURLIsUbiquitousItemKey):
            return CloudState.EVICTED if _dataless(path) else CloudState.NOT_CLOUD
        status = self._value(url, F.NSURLUbiquitousItemDownloadingStatusKey)
        if status == F.NSURLUbiquitousItemDownloadingStatusNotDownloaded or _dataless(path):
            return CloudState.EVICTED
        return CloudState.SYNCED if self._value(url, F.NSURLUbiquitousItemIsUploadedKey) else CloudState.PENDING

    def evict(self, path: Path) -> bool:
        if self._F is None:
            return False
        url = self._F.NSURL.fileURLWithPath_(str(path))
        ok, _err = self._F.NSFileManager.defaultManager().evictUbiquitousItemAtURL_error_(url, None)
        return bool(ok)

    def start_download(self, path: Path) -> bool:
        if self._F is None:
            return False
        url = self._F.NSURL.fileURLWithPath_(str(path))
        ok, _err = self._F.NSFileManager.defaultManager().startDownloadingUbiquitousItemAtURL_error_(url, None)
        return bool(ok)

    def ensure_local(self, path: Path, timeout: float = 600, poll: float = 2) -> bool:
        """Download an evicted file and wait until it is local."""
        deadline = time.monotonic() + timeout
        if self.state(path) == CloudState.EVICTED:
            self.start_download(path)
        while self.state(path) == CloudState.EVICTED:
            if time.monotonic() > deadline:
                return False
            time.sleep(poll)
        return self.state(path).local


def _icloud_placeholder(path: Path) -> Path:
    """Legacy (pre-Sonoma) eviction placeholder: '.name.icloud' next to the real name."""
    return path.with_name(f".{path.name}.icloud")


def conflict_copies(archive_dir: Path) -> list[Path]:
    """iCloud names conflicting versions like 'archive 2.sqlite'."""
    return sorted(p for p in archive_dir.glob("archive *.*") if p.suffix in (".sqlite", ".json"))


class FakeCloud:
    """Test double: states keyed by path, with a default for unknown files."""

    def __init__(self, default: CloudState = CloudState.SYNCED):
        self.default = default
        self.states: dict[Path, CloudState] = {}
        self.evicted: list[Path] = []

    def state(self, path: Path) -> CloudState:
        if not os.path.lexists(path):
            return CloudState.MISSING
        return self.states.get(Path(path), self.default)

    def evict(self, path: Path) -> bool:
        if self.state(path) != CloudState.SYNCED:
            return False
        self.states[Path(path)] = CloudState.EVICTED
        self.evicted.append(Path(path))
        return True

    def start_download(self, path: Path) -> bool:
        self.states[Path(path)] = CloudState.SYNCED
        return True

    def ensure_local(self, path: Path, timeout: float = 0, poll: float = 0) -> bool:
        if self.state(path) == CloudState.EVICTED:
            self.start_download(path)
        return self.state(path).local
