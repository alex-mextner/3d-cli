"""3d ai — offline AI-assist prompt plumbing.

The roadmap shape is `3d ai <tool> <operator> [args]`, but this skeleton does not call
Claude/Codex/opencode/ollama yet. It validates the request, resolves typed config, and
prints the prompt bundle a future backend adapter will consume after deterministic
preflight evidence is gathered.
"""
from __future__ import annotations

import json
import pathlib

import ai_tools as ai_core
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d ai <tool> <operator> <target> [options]
  Build the offline prompt bundle for an AI-assisted tool run. No backend is invoked
  and no network call is made.

Operators:
  do | review | loop

Options:
  --ref PATH       reference image/mesh for match, fit-camera, critique, etc.
  --backend NAME   backend name to put in the bundle (default claude; accepted: claude, codex, opencode, ollama, mock)
  --model NAME     model name to put in the bundle
  --config PATH    JSON config path (default: ~/.config/3d-cli/ai.json or THREED_AI_CONFIG)
  --context TEXT   extra task context to include in the user prompt
  --json           print the bundle as JSON

Examples:
  3d ai design review bracket.scad --json
  3d ai design review bracket.scad --backend=mock --context "check wall thickness"
  3d ai critique review bracket.scad --ref photo.png --backend opencode"""


def _print_usage() -> None:
    print(USAGE)


def _need_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise UsageError(f"option {flag} needs a value", command="ai")
    return argv[i + 1]


def _existing_path_arg(raw: str) -> pathlib.Path:
    path = pathlib.Path(raw).expanduser()
    if not path.is_file():
        raise InputNotFound(raw, command="ai")
    return path.resolve()


def _target_path_arg(raw: str, *, tool: str, operator: str) -> pathlib.Path:
    path = pathlib.Path(raw).expanduser()
    if tool == "design" and operator == "do":
        if path.is_dir():
            raise UsageError(f"target must be a file path, got directory: {raw}", command="ai")
        return path.resolve(strict=False)
    if not path.is_file():
        raise InputNotFound(raw, command="ai")
    return path.resolve()


def _render_text(bundle: ai_core.PromptBundle) -> str:
    lines = [
        "3d ai — offline prompt bundle",
        f"backend: {bundle.backend}",
        f"model: {bundle.model or '(default)'}",
        f"tool/operator: {bundle.tool} {bundle.operator}",
        f"target: {bundle.target}",
    ]
    if bundle.reference:
        lines.append(f"reference: {bundle.reference}")
    lines.append("")
    lines.append("preflight plan:")
    if bundle.preflight_commands:
        for i, cmd in enumerate(bundle.preflight_commands, start=1):
            lines.append(f"  {i}. {cmd}")
    else:
        lines.append("  (none declared)")
    lines.append("")
    lines.append("system prompt:")
    lines.append(bundle.system_prompt)
    lines.append("")
    lines.append("user prompt:")
    lines.append(bundle.user_prompt)
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    if argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0
    if argv[0] == "bench":
        # `3d ai bench ...` is the same suite runner as the first-class `3d bench`; it does
        # not fit the <tool> <operator> <target> shape, so it forwards to the bench command.
        from commands import bench as bench_cmd

        return bench_cmd.run(argv[1:])
    if len(argv) < 3:
        raise UsageError("ai needs <tool> <operator> <target>", command="ai")

    tool = argv[0]
    operator = argv[1]
    if operator not in ai_core.VALID_OPERATORS:
        raise InvalidArgument("operator", operator, list(ai_core.VALID_OPERATORS), command="ai")
    target = _target_path_arg(argv[2], tool=tool, operator=operator)
    reference: pathlib.Path | None = None
    backend: str | None = None
    model: str | None = None
    config_path: str | None = None
    context: str | None = None
    as_json = False

    i = 3
    while i < len(argv):
        arg = argv[i]
        value: str | None = None
        if arg.startswith("--") and "=" in arg:
            arg, value = arg.split("=", 1)
            if value == "":
                raise UsageError(f"option {arg} needs a value", command="ai")
        if arg == "--ref":
            reference = _existing_path_arg(value if value is not None else _need_value(argv, i, arg))
            i += 1 if value is not None else 2
        elif arg == "--backend":
            backend = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--model":
            model = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--config":
            config_path = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--context":
            context = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--json":
            as_json = True
            i += 1
        else:
            raise UsageError(f"unknown option '{arg}'", command="ai")

    cfg = ai_core.with_cli_overrides(
        ai_core.load_config(config_path),
        backend=backend,
        model=model,
    )
    bundle = ai_core.build_prompt_bundle(
        ai_core.AIRequest(
            tool=tool,
            operator=operator,
            target=target,
            reference=reference,
            context=context,
        ),
        cfg,
    )

    if as_json:
        print(json.dumps(bundle.to_jsonable(), indent=2, sort_keys=True))
    else:
        print(_render_text(bundle))
    return 0


COMMAND = Command(
    name="ai",
    group="META",
    summary="build an offline AI-assist prompt bundle (no backend/network call)",
    usage=USAGE,
    run=run,
)
