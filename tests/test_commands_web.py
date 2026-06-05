"""Unit tests for commands.web — dashboard launcher."""
from __future__ import annotations

from typing import Any

import pytest

from commands.web import run
from errors import InvalidArgument, MissingDependency, UsageError


def test_web_no_args(monkeypatch: Any, tmp_path: Any) -> None:
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    monkeypatch.setattr("web.webconfig.load_or_create", lambda default_root: type("C", (), {"project_root": str(tmp_path), "port": 8733, "host": "127.0.0.1"})())
    monkeypatch.setattr("web.webconfig.config_path", lambda: tmp_path / "web.json")
    monkeypatch.setattr("web.server.create_app", lambda cfg: type("App", (), {})())
    assert run([]) == 0


def test_web_help() -> None:
    assert run(["--help"]) == 0


def test_web_missing_fastapi(monkeypatch: Any) -> None:
    def fake_import(name, *a, **kw):
        if name == "fastapi":
            raise ImportError("no fastapi")
        return __import__(name, *a, **kw)
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(MissingDependency):
        run(["--root", "."])


def test_web_bad_port(monkeypatch: Any) -> None:
    with pytest.raises(InvalidArgument):
        run(["--port", "abc"])


def test_web_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["--bogus"])


def test_web_root_and_host(monkeypatch: Any, tmp_path: Any) -> None:
    # Mock everything so we don't actually start uvicorn
    monkeypatch.setattr("web.webconfig.load_or_create", lambda default_root: type("C", (), {"project_root": str(tmp_path), "port": 8733, "host": "127.0.0.1"})())
    monkeypatch.setattr("web.webconfig.config_path", lambda: tmp_path / "web.json")
    monkeypatch.setattr("web.server.create_app", lambda cfg: type("App", (), {})())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    assert run(["--root", str(tmp_path), "--host", "0.0.0.0"]) == 0


def test_web_config_override(monkeypatch: Any, tmp_path: Any) -> None:
    cfg_file = tmp_path / "web.json"
    cfg_file.write_text("{}")
    monkeypatch.setattr("web.webconfig.load_or_create", lambda default_root: type("C", (), {"project_root": str(tmp_path), "port": 8733, "host": "127.0.0.1"})())
    monkeypatch.setattr("web.webconfig.config_path", lambda: cfg_file)
    monkeypatch.setattr("web.server.create_app", lambda cfg: type("App", (), {})())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    assert run(["--config", str(cfg_file)]) == 0


def test_web_open_browser(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr("web.webconfig.load_or_create", lambda default_root: type("C", (), {"project_root": str(tmp_path), "port": 8733, "host": "127.0.0.1"})())
    monkeypatch.setattr("web.webconfig.config_path", lambda: tmp_path / "web.json")
    monkeypatch.setattr("web.server.create_app", lambda cfg: type("App", (), {})())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    import threading
    monkeypatch.setattr(threading, "Timer", lambda d, f: type("T", (), {"start": lambda self: None})())
    assert run(["--open"]) == 0
