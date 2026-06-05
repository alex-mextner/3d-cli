"""Tests for cli.paths — the single config/data dir source of truth (ROADMAP §23)."""
from __future__ import annotations

import pathlib

from cli import paths


def test_config_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.config_dir() == tmp_path / "cfg" / "3d-cli"


def test_data_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.data_dir() == tmp_path / "data" / "3d-cli"


def test_config_dir_default_is_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.config_dir() == tmp_path / ".config" / "3d-cli"


def test_migrate_legacy_moves_old_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    old = cfg / "3d"
    old.mkdir(parents=True)
    (old / ".bootstrapped").write_text("")
    paths.migrate_legacy_config()
    assert paths.config_dir().is_dir()
    assert (paths.config_dir() / ".bootstrapped").is_file()
    assert not old.exists()


def test_migrate_noop_when_new_exists(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    (cfg / "3d-cli").mkdir(parents=True)
    (cfg / "3d").mkdir(parents=True)  # stale legacy left alone when new already present
    paths.migrate_legacy_config()
    assert (cfg / "3d").is_dir()  # untouched
