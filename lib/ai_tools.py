"""Offline AI-assist plumbing for `3d ai`.

This module deliberately does not call an AI backend. It builds the typed request/config
objects and the prompt bundle that a future backend adapter can consume after the
deterministic preflight commands have produced evidence.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
from dataclasses import dataclass, field
from typing import Any

from cli import paths
from errors import InvalidArgument, ThreeDError, UsageError

DEFAULT_BACKEND = "claude"
DEFAULT_TEMPERATURE = 0.0
VALID_BACKENDS = ("claude", "codex", "opencode", "ollama", "mock")
VALID_OPERATORS = ("do", "review", "loop")

DEFAULT_TOOL_PREFLIGHT: dict[str, tuple[str, ...]] = {
    "axis": (
        "render {target} --multi",
        "params {target} --json",
    ),
    "bench": (),
    "critique": (
        "render {target} --multi",
        "check {target}",
    ),
    "design": (
        "params {target} --json",
        "check {target}",
    ),
    "fit-camera": (
        "fit-camera {target} {reference}",
        "score {target} {reference}",
    ),
    "match": (
        "fit-camera {target} {reference}",
        "score {target} {reference}",
        "render {target} --multi",
    ),
    "printability": (
        "check {target} --printability",
    ),
    "strength": (
        "check {target}",
        "render {target} --multi",
    ),
}
DEFAULT_REFERENCE_PREFLIGHT: dict[str, tuple[str, ...]] = {
    "critique": (
        "score {target} {reference}",
    ),
}

DEFAULT_SYSTEM_PROMPT = """You are assisting with an OpenSCAD-first FDM modeling task.
Use only the deterministic evidence listed in the preflight plan. Prefer concrete
dimensions, named `3d` commands, and edits that can be verified by the gates."""


class AIConfigError(ThreeDError):
    """The AI config file exists but is malformed. Exit 2."""

    exit_code = 2


@dataclass(frozen=True, slots=True)
class ToolPromptConfig:
    """Per-tool prompt settings loaded from `~/.config/3d-cli/ai.json`."""

    system: str | None = None
    preflight: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AIConfig:
    """Resolved AI-assist configuration. This is offline plumbing, not credentials."""

    path: pathlib.Path | None = None
    backend: str = DEFAULT_BACKEND
    model: str | None = None
    temperature: float = DEFAULT_TEMPERATURE
    tool_overrides: dict[str, ToolPromptConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AIRequest:
    """One `3d ai <tool> <operator>` request before backend execution exists."""

    tool: str
    operator: str
    target: pathlib.Path
    reference: pathlib.Path | None = None
    context: str | None = None


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Everything a backend adapter would need, minus any network/process call."""

    tool: str
    operator: str
    backend: str
    model: str | None
    temperature: float
    target: pathlib.Path
    reference: pathlib.Path | None
    preflight_commands: list[str]
    system_prompt: str
    user_prompt: str
    network_call: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "operator": self.operator,
            "backend": self.backend,
            "model": self.model,
            "temperature": self.temperature,
            "target": str(self.target),
            "reference": str(self.reference) if self.reference else None,
            "preflight_commands": list(self.preflight_commands),
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "prompt": {
                "system": self.system_prompt,
                "user": self.user_prompt,
            },
            "network_call": self.network_call,
        }


def default_config_path() -> pathlib.Path:
    override = os.environ.get("THREED_AI_CONFIG")
    if override:
        return pathlib.Path(override).expanduser()
    return paths.config_dir() / "ai.json"


def _as_mapping(value: Any, *, what: str, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AIConfigError(
            f"{what} in {path} must be a JSON object",
            command="ai",
            remediation=["Use a JSON mapping, for example: {\"backend\": \"opencode\"}."],
        )
    return value


def _optional_str(value: Any, *, key: str, path: pathlib.Path) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise AIConfigError(f"`{key}` in {path} must be a string", command="ai")


def _load_tool_overrides(raw: Any, *, path: pathlib.Path) -> dict[str, ToolPromptConfig]:
    if raw is None:
        return {}
    tools = _as_mapping(raw, what="`tools`", path=path)
    out: dict[str, ToolPromptConfig] = {}
    for name, value in tools.items():
        item = _as_mapping(value, what=f"`tools.{name}`", path=path)
        system = _optional_str(item.get("system"), key=f"tools.{name}.system", path=path)
        preflight_raw = item.get("preflight")
        preflight: tuple[str, ...] | None = None
        if preflight_raw is not None:
            if not isinstance(preflight_raw, list) or not all(isinstance(v, str) for v in preflight_raw):
                raise AIConfigError(
                    f"`tools.{name}.preflight` in {path} must be a list of strings",
                    command="ai",
                )
            preflight = tuple(preflight_raw)
        out[str(name)] = ToolPromptConfig(system=system, preflight=preflight)
    return out


def _validate_backend(backend: str) -> str:
    if backend not in VALID_BACKENDS:
        raise InvalidArgument("backend", backend, list(VALID_BACKENDS), command="ai")
    return backend


def _validate_operator(operator: str) -> str:
    if operator not in VALID_OPERATORS:
        raise InvalidArgument("operator", operator, list(VALID_OPERATORS), command="ai")
    return operator


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    require_exists: bool | None = None,
) -> AIConfig:
    config_env = os.environ.get("THREED_AI_CONFIG")
    cfg_path = pathlib.Path(path).expanduser() if path is not None else default_config_path()
    must_exist = require_exists if require_exists is not None else path is not None or config_env is not None
    if not cfg_path.exists():
        if must_exist:
            raise AIConfigError(
                f"config file not found: {cfg_path}",
                command="ai",
                remediation=["Check the path or omit `--config` to use defaults."],
            )
        return AIConfig(path=cfg_path)
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AIConfigError(
            f"could not parse {cfg_path}: {exc.msg}",
            command="ai",
            remediation=["Fix the JSON syntax or pass a different file with `--config PATH`."],
        ) from exc
    except OSError as exc:
        raise AIConfigError(f"could not read {cfg_path}: {exc}", command="ai") from exc

    data = _as_mapping(raw, what="AI config", path=cfg_path)
    backend = _validate_backend(str(data.get("backend", DEFAULT_BACKEND)))
    model = _optional_str(data.get("model"), key="model", path=cfg_path)
    try:
        temperature = float(data.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError) as exc:
        raise AIConfigError(f"`temperature` in {cfg_path} must be a number", command="ai") from exc
    return AIConfig(
        path=cfg_path,
        backend=backend,
        model=model,
        temperature=temperature,
        tool_overrides=_load_tool_overrides(data.get("tools"), path=cfg_path),
    )


def with_cli_overrides(
    cfg: AIConfig,
    *,
    backend: str | None = None,
    model: str | None = None,
) -> AIConfig:
    return AIConfig(
        path=cfg.path,
        backend=_validate_backend(backend) if backend else cfg.backend,
        model=model if model is not None else cfg.model,
        temperature=cfg.temperature,
        tool_overrides=dict(cfg.tool_overrides),
    )


def _tool_config(tool: str, cfg: AIConfig) -> ToolPromptConfig:
    override = cfg.tool_overrides.get(tool)
    if override:
        return override
    if tool not in DEFAULT_TOOL_PREFLIGHT:
        raise InvalidArgument(
            "tool",
            tool,
            sorted(DEFAULT_TOOL_PREFLIGHT),
            command="ai",
            extra="Add a tools.<name> entry to the AI config before using a custom tool.",
        )
    return ToolPromptConfig(system=None, preflight=DEFAULT_TOOL_PREFLIGHT[tool])


def _format_command(template: str, req: AIRequest) -> str | None:
    if "{reference}" in template and req.reference is None:
        raise UsageError(
            f"tool {req.tool!r} needs --ref PATH for reference-based preflight evidence",
            command="ai",
        )
    values = {
        "tool": req.tool,
        "operator": req.operator,
        "target": shlex.quote(str(req.target)),
        "reference": shlex.quote(str(req.reference)) if req.reference else "",
    }
    rendered = _format_template(template, values, label="preflight").strip()
    if not rendered:
        return None
    return rendered if rendered.startswith("3d ") else f"3d {rendered}"


def _format_template(template: str, values: dict[str, str], *, label: str) -> str:
    try:
        return template.format(**values)
    except (AttributeError, KeyError, IndexError, ValueError) as exc:
        accepted = ", ".join(sorted(values))
        raise AIConfigError(
            f"invalid {label} template: {exc}",
            command="ai",
            remediation=[
                f"Use only these placeholders: {accepted}.",
                "Escape literal braces as `{{` and `}}`.",
            ],
        ) from exc


def _system_prompt(template: str | None, req: AIRequest) -> str:
    values = {
        "tool": req.tool,
        "operator": req.operator,
        "target": str(req.target),
        "reference": str(req.reference) if req.reference else "",
    }
    return _format_template(template or DEFAULT_SYSTEM_PROMPT, values, label="system prompt")


def _user_prompt(req: AIRequest, preflight: list[str]) -> str:
    lines = [
        f"Task: {req.tool} / {req.operator}",
        f"Target: {req.target}",
    ]
    if req.reference:
        lines.append(f"Reference: {req.reference}")
    if req.context:
        lines.append(f"Additional context: {req.context}")
    lines.append("")
    lines.append("Deterministic preflight plan:")
    if preflight:
        for i, cmd in enumerate(preflight, start=1):
            lines.append(f"{i}. {cmd}")
    else:
        lines.append("none declared for this tool yet")
    lines.append("")
    lines.append("No network call has been made. This is an offline prompt bundle for the future backend adapter.")
    return "\n".join(lines)


def build_prompt_bundle(req: AIRequest, cfg: AIConfig) -> PromptBundle:
    _validate_operator(req.operator)
    tool_cfg = _tool_config(req.tool, cfg)
    explicit_preflight = req.tool in cfg.tool_overrides and cfg.tool_overrides[req.tool].preflight is not None
    templates = (
        DEFAULT_TOOL_PREFLIGHT.get(req.tool, ())
        if tool_cfg.preflight is None
        else tool_cfg.preflight
    )
    if not explicit_preflight and req.tool == "design" and req.operator == "do" and not req.target.is_file():
        templates = ()
    if not explicit_preflight and req.reference is not None:
        templates = templates + DEFAULT_REFERENCE_PREFLIGHT.get(req.tool, ())
    preflight = [cmd for cmd in (_format_command(t, req) for t in templates) if cmd]
    return PromptBundle(
        tool=req.tool,
        operator=req.operator,
        backend=cfg.backend,
        model=cfg.model,
        temperature=cfg.temperature,
        target=req.target,
        reference=req.reference,
        preflight_commands=preflight,
        system_prompt=_system_prompt(tool_cfg.system, req),
        user_prompt=_user_prompt(req, preflight),
    )
