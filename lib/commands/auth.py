"""3d auth — configure credentials for optional cloud providers."""
from __future__ import annotations

import json

from cli.registry import Command
from errors import UsageError
from hf_auth import delete_hf_token, load_hf_token, prompt_token, save_hf_token, validate_hf_token

USAGE = """3d auth hf <login|status|logout|complete> [options]
  Configure optional Hugging Face access for ZeroGPU Spaces and gated model weights.

Subcommands:
  hf login              interactively paste a Hugging Face read token; validates and stores it
  hf status             show whether HF_TOKEN or stored credentials are available
  hf logout             remove the stored Hugging Face token (does not unset HF_TOKEN)
  hf complete CODE      reserved for OAuth device-flow completion once 3d-cli has a client_id

Options:
  --json                emit machine-readable JSON for status/login/logout

Examples:
  3d auth hf login
  3d auth hf status --json
  3d auth hf logout"""


def _json_flag(argv: list[str]) -> tuple[list[str], bool]:
    json_output = False
    rest: list[str] = []
    for arg in argv:
        if arg == "--json":
            json_output = True
        else:
            rest.append(arg)
    return rest, json_output


def _print(payload: dict[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    status = payload.get("status")
    detail = payload.get("detail")
    print(f"{status}: {detail}")


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help") or "-h" in argv[1:] or "--help" in argv[1:]:
        print(USAGE)
        return 0
    if argv[0] != "hf":
        raise UsageError("unknown auth provider", command="auth", remediation=["Use `3d auth hf ...`."])
    rest, json_output = _json_flag(argv[1:])
    if not rest:
        print(USAGE)
        return 1
    sub = rest[0]
    extra = rest[1:]
    if sub == "login":
        if extra:
            raise UsageError(f"unknown option '{extra[0]}'", command="auth")
        token = prompt_token(json_output=json_output)
        username = validate_hf_token(token)
        path = save_hf_token(token, username)
        _print({"status": "ok", "detail": f"Hugging Face token saved for {username}", "path": str(path)}, json_output)
        return 0
    if sub == "status":
        if extra:
            raise UsageError(f"unknown option '{extra[0]}'", command="auth")
        info = load_hf_token()
        if info is None:
            _print(
                {
                    "status": "missing",
                    "detail": "No Hugging Face token configured. Public downloads may work with lower quota.",
                },
                json_output,
            )
            return 1
        _print(
            {
                "status": "ok",
                "detail": f"Hugging Face token available from {info.source}",
                "source": info.source,
                "username": info.username,
            },
            json_output,
        )
        return 0
    if sub == "logout":
        if extra:
            raise UsageError(f"unknown option '{extra[0]}'", command="auth")
        removed = delete_hf_token()
        _print(
            {
                "status": "ok",
                "detail": "Stored Hugging Face token removed" if removed else "No stored Hugging Face token found",
                "removed": removed,
            },
            json_output,
        )
        return 0
    if sub == "complete":
        raise UsageError(
            "OAuth device-flow completion is not configured yet",
            command="auth",
            remediation=[
                "Use `3d auth hf login` for now.",
                "Device flow requires a registered Hugging Face OAuth client_id.",
            ],
        )
    raise UsageError(f"unknown auth subcommand '{sub}'", command="auth")


COMMAND = Command(
    name="auth",
    group="ENVIRONMENT",
    summary="configure optional Hugging Face credentials for ZeroGPU and gated weights",
    usage=USAGE,
    run=run,
)
