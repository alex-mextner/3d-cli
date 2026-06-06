from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.paths import config_dir
from errors import GateFailure, UsageError

AUTH_FILE = "auth.json"
HF_WHOAMI = "https://huggingface.co/api/whoami-v2"
HF_TOKEN_URL = "https://huggingface.co/settings/tokens"


@dataclass(frozen=True)
class HFTokenInfo:
    token: str
    username: str | None
    source: str


def auth_path() -> Path:
    return config_dir() / AUTH_FILE


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise UsageError(
            f"auth file is not valid JSON: {path}",
            command="auth",
            remediation=["Fix or remove the file and run `3d auth hf login` again."],
        ) from None
    if not isinstance(data, dict):
        raise UsageError(
            f"auth file must contain a JSON object: {path}",
            command="auth",
            remediation=["Fix or remove the file and run `3d auth hf login` again."],
        )
    return data


def load_hf_token() -> HFTokenInfo | None:
    env_token = os.environ.get("HF_TOKEN", "").strip()
    if env_token:
        return HFTokenInfo(token=env_token, username=None, source="HF_TOKEN")
    path = auth_path()
    if path.is_file():
        _ensure_private_auth_path(path)
    data = _read_json(path)
    hf = data.get("huggingface")
    if not isinstance(hf, dict):
        return None
    token = str(hf.get("token") or "").strip()
    if not token:
        return None
    username = hf.get("username")
    return HFTokenInfo(token=token, username=str(username) if username else None, source=str(auth_path()))


def _ensure_private_auth_path(path: Path) -> None:
    try:
        os.chmod(path.parent, stat.S_IRWXU)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise UsageError(
            f"cannot secure Hugging Face auth file permissions: {path}",
            command="auth",
            remediation=["Fix file ownership/permissions, then run `3d auth hf status` again."],
        ) from exc


def validate_hf_token(token: str) -> str:
    request = urllib.request.Request(HF_WHOAMI, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("whoami response is not a JSON object")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError(
                "Hugging Face token was rejected",
                command="auth",
                remediation=[
                    f"Create a read token at {HF_TOKEN_URL}.",
                    "Then run `3d auth hf login` and paste it when prompted.",
                ],
            ) from None
        raise GateFailure(f"Hugging Face auth check failed: HTTP {exc.code}", command="auth") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise GateFailure(
            f"Hugging Face auth check returned an invalid response: {exc}",
            command="auth",
        ) from exc
    except OSError as exc:
        raise GateFailure(f"Hugging Face auth check failed: {exc}", command="auth") from exc
    username = payload.get("name") or payload.get("fullname") or payload.get("email")
    return str(username or "unknown")


def save_hf_token(token: str, username: str) -> Path:
    path = auth_path()
    data = _read_json(path)
    data["huggingface"] = {"token": token, "username": username}
    _write_auth_data(path, data)
    return path


def _write_auth_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=stat.S_IRWXU, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def delete_hf_token() -> bool:
    path = auth_path()
    data = _read_json(path)
    hf = data.pop("huggingface", None)
    if hf is None:
        return False
    _write_auth_data(path, data)
    return True


def prompt_token(*, json_output: bool = False) -> str:
    output = sys.stderr if json_output else sys.stdout
    print(f"Create a Hugging Face read token: {HF_TOKEN_URL}", file=output)
    print(
        "Paste token. Input is hidden; the token will be stored in ~/.config/3d-cli/auth.json with 0600 permissions.",
        file=output,
    )
    try:
        token = getpass.getpass("HF token: ").strip()
    except EOFError:
        raise UsageError(
            "cannot prompt for Hugging Face token in a non-interactive environment",
            command="auth",
            remediation=["Run `3d auth hf login` in a terminal, or set HF_TOKEN for this process."],
        ) from None
    if not token:
        raise UsageError(
            "empty Hugging Face token",
            command="auth",
            remediation=["Run `3d auth hf login` again and paste a read token."],
        )
    return token
