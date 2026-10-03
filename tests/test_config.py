import json

import pytest

from wa_archive import cli, config
from wa_archive.config import load_paths, load_settings, resolve
from wa_archive.store import ArchiveError, Store

from .fixture import basic_fixture
from .test_ingest import env, ingest  # noqa: F401  (env is a fixture)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WA_ARCHIVE_DIR", raising=False)
    monkeypatch.delenv("WA_ARCHIVE_BACKUP_ROOT", raising=False)
    # Never look at the real default locations from tests.
    monkeypatch.setitem(config.SETTINGS, "archive_dir", ("WA_ARCHIVE_DIR", tmp_path / "default-archive"))
    monkeypatch.setitem(config.SETTINGS, "backup_root", ("WA_ARCHIVE_BACKUP_ROOT", tmp_path / "default-backups"))
    return tmp_path


def test_precedence_env_over_saved_over_default(state, monkeypatch):
    assert resolve("archive_dir") == (state / "default-archive", "default")
    target = state / 'iCloud "quoted" dir'
    target.mkdir()
    cli.main(["config", "set", "archive-dir", str(target)])
    assert load_settings() == {"archive_dir": str(target.resolve())}
    assert resolve("archive_dir") == (target.resolve(), "config")
    assert load_paths().archive_dir == target.resolve()
    monkeypatch.setenv("WA_ARCHIVE_DIR", str(state / "env-dir"))
    assert resolve("archive_dir") == (state / "env-dir", "env")
    monkeypatch.delenv("WA_ARCHIVE_DIR")
    cli.main(["config", "unset", "archive-dir"])
    assert resolve("archive_dir")[1] == "default"


def test_setting_never_orphans_an_existing_archive(state, monkeypatch):
    old, new, other = state / "old", state / "new", state / "other"
    for d in (old, new, other):
        d.mkdir()
    (old / "archive.json").write_text(json.dumps({"generation": 1}))
    cli.main(["config", "set", "archive-dir", str(old)])
    with pytest.raises(SystemExit, match="doesn't move anything"):
        cli.main(["config", "set", "archive-dir", str(new)])
    assert resolve("archive_dir")[0] == old.resolve()
    (other / "archive.json").write_text(json.dumps({"generation": 1}))  # e.g. folder moved in Finder
    cli.main(["config", "set", "archive-dir", str(other)])
    assert resolve("archive_dir")[0] == other.resolve()
    cli.main(["config", "set", "archive-dir", str(new), "--force"])
    assert resolve("archive_dir")[0] == new.resolve()


def test_config_show_reports_sources(state, monkeypatch):
    from rich.console import Console
    rec = Console(record=True, width=300)
    monkeypatch.setattr(cli, "console", rec)
    cli.main(["config"])
    out = rec.export_text()
    assert "archive-dir" in out and "(default)" in out and str(state / "default-archive") in out


def test_missing_published_archive_is_an_error_not_a_fresh_start(env):
    ingest(env, basic_fixture())
    moved = env.paths.archive_dir.with_name("moved")
    env.paths.archive_dir.rename(moved)  # archive folder moved, setting not updated
    with pytest.raises(ArchiveError, match="config set archive-dir"):
        Store(env.paths, env.cloud).sync()
    assert not env.paths.archive_dir.exists()  # nothing was created at the old location
