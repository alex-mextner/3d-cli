"""3d ollama — validate local Ollama config and print a dry-run request plan."""
from __future__ import annotations

import json

from cli.registry import Command
from errors import UsageError
import ollama

USAGE = """3d ollama --model MODEL --prompt TEXT [options]
  Validate a local Ollama endpoint config and print a dry-run /api/generate request plan.
  This skeleton does not send network requests.

Options:
  --config PATH   read endpoint/model defaults from PATH (default: ~/.config/3d-cli/ollama.json)
  --endpoint URL  local Ollama base URL (default: config, else http://127.0.0.1:11434)
  --model NAME    Ollama model name (default: config)
  --prompt TEXT   prompt to place in the generate request body
  --system TEXT   optional system prompt
  --dry-run       accepted for clarity; the command always prints a plan only

Examples:
  3d ollama --model llama3.2 --prompt "Suggest an OpenSCAD edit" --dry-run
  3d ollama --config ~/.config/3d-cli/ollama.json --prompt "Make it hollow" --dry-run
  3d ollama --endpoint localhost:11434 --model llama3.2 --prompt "Review bracket.scad" > ollama-plan.json"""

_VALUE_OPTIONS = {"--config", "--endpoint", "--model", "--prompt", "--system"}
_BOOLEAN_OPTIONS = {"--dry-run", "-h", "--help"}
_KNOWN_OPTIONS = _VALUE_OPTIONS | _BOOLEAN_OPTIONS


def _need_value(argv: list[str], index: int, flag: str, *, freeform: bool = False) -> str:
    if index + 1 >= len(argv) or not argv[index + 1]:
        raise UsageError(f"{flag} requires a value", command="ollama", remediation=[USAGE])
    value = argv[index + 1]
    if value.startswith("-") and (not freeform or value in _KNOWN_OPTIONS):
        raise UsageError(f"{flag} requires a value", command="ollama", remediation=[USAGE])
    return value


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    config: str | None = None
    endpoint: str | None = None
    model: str | None = None
    prompt: str | None = None
    system: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--config":
            config = _need_value(argv, i, arg)
            i += 2
        elif arg == "--endpoint":
            endpoint = _need_value(argv, i, arg)
            i += 2
        elif arg == "--model":
            model = _need_value(argv, i, arg)
            i += 2
        elif arg == "--prompt":
            prompt = _need_value(argv, i, arg, freeform=True)
            i += 2
        elif arg == "--system":
            system = _need_value(argv, i, arg, freeform=True)
            i += 2
        elif arg == "--dry-run":
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{arg}'", command="ollama")

    if config is not None or endpoint is None or model is None:
        cfg = ollama.load_config(
            config,
            validate_endpoint_value=endpoint is None,
            validate_model_value=model is None,
        )
    else:
        cfg = ollama.OllamaConfig(endpoint=ollama.DEFAULT_ENDPOINT)
    resolved_endpoint = endpoint or cfg.endpoint
    resolved_model = (model or cfg.model or "").strip()
    resolved_prompt = (prompt or "").strip()

    if not resolved_model:
        raise UsageError(
            "missing Ollama model",
            command="ollama",
            remediation=["Pass --model NAME or set {\"model\": \"...\"} in the Ollama config."],
        )
    if not resolved_prompt:
        raise UsageError(
            "missing prompt",
            command="ollama",
            remediation=["Pass --prompt TEXT."],
        )

    plan = ollama.plan_generate_request(
        endpoint=resolved_endpoint,
        model=resolved_model,
        prompt=resolved_prompt,
        system=system.strip() if system else None,
    )
    print(json.dumps({"dry_run": True, "request": plan.as_dict()}, indent=2, sort_keys=True))
    return 0


COMMAND = Command(
    name="ollama",
    group="ENVIRONMENT",
    summary="validate local Ollama config and print a dry-run /api/generate request plan",
    usage=USAGE,
    run=run,
)
