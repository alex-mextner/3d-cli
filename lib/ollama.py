"""Small Ollama planning helpers for the `3d ollama` command.

This module validates local Ollama endpoint configuration and builds request plans. It
does not open sockets; callers can test and import it without requiring Ollama to run.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from cli.paths import config_dir
from errors import InvalidArgument, UsageError

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
GENERATE_PATH = "/api/generate"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class OllamaConfig:
    endpoint: str
    model: str | None = None


@dataclass(frozen=True)
class RequestPlan:
    method: str
    url: str
    body: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "url": self.url, "body": self.body}


def config_path(override: str | pathlib.Path | None = None) -> pathlib.Path:
    if override is not None:
        return pathlib.Path(override).expanduser().resolve()
    return config_dir() / "ollama.json"


def validate_endpoint(endpoint: str, *, flag: str = "--endpoint") -> str:
    raw = endpoint.strip()
    if not raw:
        raise InvalidArgument(
            flag,
            endpoint,
            ["a local Ollama endpoint, e.g. http://127.0.0.1:11434"],
            command="ollama",
            extra="Set the endpoint in ~/.config/3d-cli/ollama.json or pass --endpoint.",
        )

    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlparse(candidate)
    hostname = parsed.hostname
    try:
        parsed.port
    except ValueError as e:
        raise InvalidArgument(
            flag,
            endpoint,
            ["a valid local Ollama endpoint, e.g. http://127.0.0.1:11434"],
            command="ollama",
        ) from e
    if parsed.netloc.endswith(":"):
        raise InvalidArgument(
            flag,
            endpoint,
            ["a valid local Ollama endpoint, e.g. http://127.0.0.1:11434"],
            command="ollama",
        )
    if parsed.scheme not in ("http", "https") or hostname not in LOCAL_HOSTS:
        raise InvalidArgument(
            flag,
            endpoint,
            ["a local Ollama endpoint, e.g. http://127.0.0.1:11434", "http://localhost:11434"],
            command="ollama",
            extra="Remote Ollama endpoints are not accepted by this command skeleton.",
        )
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        raise InvalidArgument(
            flag,
            endpoint,
            ["a base URL without credentials, query, or fragment"],
            command="ollama",
        )
    path = parsed.path.rstrip("/")
    if path:
        raise InvalidArgument(
            flag,
            endpoint,
            ["a base URL only, e.g. http://127.0.0.1:11434"],
            command="ollama",
        )

    netloc = parsed.netloc
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def load_config(
    path: str | pathlib.Path | None = None,
    *,
    validate_endpoint_value: bool = True,
    validate_model_value: bool = True,
) -> OllamaConfig:
    cfg_path = config_path(path)
    if not cfg_path.exists():
        if path is not None:
            raise UsageError(
                f"config file not found: {cfg_path}",
                command="ollama",
                remediation=["Check the path or omit --config to use the default Ollama config."],
            )
        return OllamaConfig(endpoint=DEFAULT_ENDPOINT)

    try:
        config_text = cfg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(
            f"could not read config file: {cfg_path}",
            command="ollama",
            remediation=["Check that --config points to a readable UTF-8 JSON file."],
        ) from e
    try:
        raw = json.loads(config_text)
    except json.JSONDecodeError as e:
        raise UsageError(
            f"invalid JSON in {cfg_path}",
            command="ollama",
            remediation=["Fix the JSON file or pass --config PATH to use a different config."],
        ) from e

    if not isinstance(raw, dict):
        raise UsageError(
            f"config must be a JSON object: {cfg_path}",
            command="ollama",
            remediation=['Use keys like {"endpoint": "http://127.0.0.1:11434", "model": "llama3.2"}.'],
        )

    endpoint_raw = raw.get("endpoint", DEFAULT_ENDPOINT)
    if validate_endpoint_value and not isinstance(endpoint_raw, str):
        raise InvalidArgument(
            "endpoint",
            repr(endpoint_raw),
            ["a local Ollama endpoint string"],
            command="ollama",
        )
    model_raw = raw.get("model")
    if validate_model_value and model_raw is not None and not isinstance(model_raw, str):
        raise InvalidArgument("model", repr(model_raw), ["an Ollama model name string"], command="ollama")
    model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None
    if validate_endpoint_value:
        endpoint = validate_endpoint(str(endpoint_raw), flag="endpoint")
    else:
        endpoint = endpoint_raw.strip() if isinstance(endpoint_raw, str) and endpoint_raw.strip() else DEFAULT_ENDPOINT
    return OllamaConfig(endpoint=endpoint, model=model)


def plan_generate_request(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    system: str | None = None,
) -> RequestPlan:
    normalized_endpoint = validate_endpoint(endpoint)
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        body["system"] = system
    return RequestPlan(
        method="POST",
        url=f"{normalized_endpoint}{GENERATE_PATH}",
        body=body,
    )
