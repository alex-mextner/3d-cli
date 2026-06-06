from __future__ import annotations

from typing import Any

import pytest

from commands.auth import run
from errors import UsageError


def test_auth_no_args() -> None:
    assert run([]) == 1


def test_auth_help() -> None:
    assert run(["--help"]) == 0
    assert run(["hf", "login", "--help"]) == 0


def test_auth_unknown_provider() -> None:
    with pytest.raises(UsageError):
        run(["github", "status"])


def test_auth_hf_status_missing(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert run(["hf", "status"]) == 1
    assert "missing" in capsys.readouterr().out


def test_auth_hf_status_env_token(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    assert run(["hf", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    assert '"source": "HF_TOKEN"' in out


def test_auth_hf_logout_removes_stored_token(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    from hf_auth import auth_path, save_hf_token

    save_hf_token("hf_fake", "tester")
    assert auth_path().is_file()
    assert run(["hf", "logout", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"removed": true' in out
    assert run(["hf", "status"]) == 1


def test_auth_hf_login_validates_and_saves_token(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HF_TOKEN", raising=False)

    monkeypatch.setattr("commands.auth.prompt_token", lambda *, json_output=False: "hf_saved")
    monkeypatch.setattr("commands.auth.validate_hf_token", lambda token: "tester")

    assert run(["hf", "login", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out

    from hf_auth import load_hf_token

    info = load_hf_token()
    assert info is not None
    assert info.token == "hf_saved"
    assert info.username == "tester"


def test_auth_hf_complete_is_reserved() -> None:
    with pytest.raises(UsageError, match="OAuth device-flow"):
        run(["hf", "complete", "CODE"])
