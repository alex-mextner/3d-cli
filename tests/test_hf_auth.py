from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from errors import GateFailure, UsageError
from hf_auth import auth_path, delete_hf_token, load_hf_token, prompt_token, save_hf_token, validate_hf_token


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_save_hf_token_uses_config_dir_and_0600(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = save_hf_token("hf_fake", "tester")

    assert path == auth_path()
    assert path.parent == tmp_path / "config" / "3d-cli"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["huggingface"]["username"] == "tester"
    assert data["huggingface"]["token"] == "hf_fake"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_hf_token_does_not_follow_predictable_tmp_symlink(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.txt"
    (path.parent / "auth.tmp").symlink_to(victim)

    save_hf_token("hf_secret", "tester")

    assert not victim.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["huggingface"]["token"] == "hf_secret"


def test_load_hf_token_prefers_env(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    save_hf_token("hf_stored", "tester")
    monkeypatch.setenv("HF_TOKEN", "hf_env")

    info = load_hf_token()

    assert info is not None
    assert info.token == "hf_env"
    assert info.source == "HF_TOKEN"


def test_load_hf_token_repairs_insecure_stored_file_permissions(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    path = save_hf_token("hf_stored", "tester")
    path.chmod(0o644)

    info = load_hf_token()

    assert info is not None
    assert info.token == "hf_stored"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_delete_hf_token_preserves_0600(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = save_hf_token("hf_stored", "tester")
    path.chmod(0o644)

    assert delete_hf_token()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "huggingface" not in json.loads(path.read_text(encoding="utf-8"))


def test_prompt_token_reports_non_interactive_environment(monkeypatch: Any) -> None:
    def fake_getpass(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("hf_auth.getpass.getpass", fake_getpass)
    with pytest.raises(UsageError, match="non-interactive"):
        prompt_token()


@pytest.mark.parametrize("content", ["{", "[]", '"hf_token"'])
def test_load_hf_token_reports_malformed_auth_file(monkeypatch: Any, tmp_path: Path, content: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(UsageError):
        load_hf_token()


def test_validate_hf_token_reports_malformed_whoami_response(monkeypatch: Any) -> None:
    monkeypatch.setattr("hf_auth.urllib.request.urlopen", lambda *_args, **_kwargs: _Response(b"{"))

    with pytest.raises(GateFailure, match="invalid response"):
        validate_hf_token("hf_fake")
