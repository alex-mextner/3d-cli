"""Unit tests for web.webconfig — config load/create."""
from __future__ import annotations

import pathlib
from typing import Any

from web import webconfig


def test_config_dir(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert webconfig.config_dir() == tmp_path / "3d-cli"


def test_config_path_override(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    override = tmp_path / "custom.json"
    monkeypatch.setenv("THREED_WEB_CONFIG", str(override))
    assert webconfig.config_path() == override


def test_config_path_default(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("THREED_WEB_CONFIG", raising=False)
    assert webconfig.config_path() == tmp_path / "3d-cli" / "web.json"


def test_state_dir(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = webconfig.state_dir()
    assert d.is_dir()
    assert d.name == "web-state"


def test_load_or_create_existing(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    p = tmp_path / "web.json"
    p.write_text('{"project_root": "/tmp", "port": 9000, "host": "0.0.0.0"}\n')
    monkeypatch.setenv("THREED_WEB_CONFIG", str(p))
    cfg = webconfig.load_or_create()
    assert cfg.project_root == "/tmp"
    assert cfg.port == 9000
    assert cfg.host == "0.0.0.0"


def test_load_or_create_default_root(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    p = tmp_path / "web.json"
    p.write_text('{}\n')
    monkeypatch.setenv("THREED_WEB_CONFIG", str(p))
    cfg = webconfig.load_or_create(default_root="/projects")
    assert cfg.project_root == "/projects"


def test_load_or_create_missing_file(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    p = tmp_path / "web.json"
    monkeypatch.setenv("THREED_WEB_CONFIG", str(p))
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    cfg = webconfig.load_or_create()
    assert cfg.project_root == str(tmp_path)
    assert cfg.port == 8733
    assert cfg.host == "127.0.0.1"
    assert p.is_file()


def test_load_or_create_bad_json(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    p = tmp_path / "web.json"
    p.write_text("not json")
    monkeypatch.setenv("THREED_WEB_CONFIG", str(p))
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    cfg = webconfig.load_or_create()
    assert cfg.project_root == str(tmp_path)


def test_webconfig_to_dict() -> None:
    cfg = webconfig.WebConfig(project_root="/p", port=1, host="h")
    assert cfg.to_dict() == {"project_root": "/p", "port": 1, "host": "h"}
