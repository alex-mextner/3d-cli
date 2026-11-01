"""routes_backup.py — the API Home Assistant's schedule calls (and `3d printops backup` reads).

  POST /v1/backup/run   scope write. {target?, subdir?, keep_daily?, keep_weekly?, keep_monthly?, min_free_gb?}
                        Runs `3d printops backup run … --json` as a CHILD process (niced, own memory) and
                        answers 200 with its JSON: {"status": "ok", "set": …, "pruned": […], …} or, when the run
                        failed, {"status": "failed", "error": "…"} — a failed backup is an ANSWER, not an
                        HTTP error, because Home Assistant's rest_command discards the body of a non-2xx reply and
                        the body is what the notification quotes. Auth/validation errors stay 401/403/422.
  GET  /v1/backup       scope read. The last run's outcome (`backup-last.json` next to the PrintOps DB) and
                        the configured targets with their disk state; never touches the disk's contents.

WHY a child process: the SQLite copy + gzip must run at nice 19 / idle I/O on a weak host, and a
child's exit code and notification path are exactly those of the CLI a human runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from cli.env import repo_root
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from printops import backup_config, backup_run
from printops.api.deps import Principal, get_state, require
from printops.api.state import ApiState

router = APIRouter()
RUN_TIMEOUT_S = 1800


class RunBody(BaseModel):
    target: str = Field(
        backup_run.DEFAULT_TARGET, pattern=r"^[A-Za-z0-9_-]+$", max_length=64
    )
    subdir: str = Field(backup_run.DEFAULT_SUBDIR, max_length=128)

    @field_validator("subdir")
    @classmethod
    def _plain_relative_dir(cls, value: str) -> str:
        if not backup_run.valid_subdir(value):
            raise ValueError(
                "subdir: plain relative names (letters, digits, . _ -), no . or .. component"
            )
        return value

    keep_daily: int = Field(7, ge=0, le=3650)
    keep_weekly: int = Field(4, ge=0, le=520)
    keep_monthly: int = Field(6, ge=0, le=240)
    min_free_gb: float = Field(backup_run.DEFAULT_MIN_FREE_GB, ge=0, le=100000)


def cli_argv() -> list[str]:
    """This install's own bin/3d (REPO_ROOT=/app in the container), never a different `3d` found on PATH."""
    return [sys.executable, str(Path(repo_root()) / "bin" / "3d")]


def argv_for(body: RunBody) -> list[str]:
    return [
        *cli_argv(),
        "printops",
        "backup",
        "run",
        "--target",
        body.target,
        "--subdir",
        body.subdir,
        "--keep",
        f"daily={body.keep_daily},weekly={body.keep_weekly},monthly={body.keep_monthly}",
        "--min-free-gb",
        str(body.min_free_gb),
        "--json",
    ]


def _answer(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The CLI's last JSON line ({status: ok|failed, ...}); anything else is a failed run."""
    for line in reversed(done.stdout.strip().splitlines()):
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("status"):
            return body
    detail = (done.stderr or done.stdout or f"exit {done.returncode}").strip()
    return {"status": "failed", "error": detail[-1500:]}


@router.post("/v1/backup/run")
def run_backup(
    body: RunBody, _who: Annotated[Principal, Depends(require("write"))]
) -> dict[str, Any]:
    try:
        done = subprocess.run(
            argv_for(body),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "error": f"the backup run did not finish within {RUN_TIMEOUT_S}s",
        }
    return _answer(done)


@router.get("/v1/backup")
def backup_status(
    state: Annotated[ApiState, Depends(get_state)],
    _who: Annotated[Principal, Depends(require("read"))],
) -> dict[str, Any]:
    settings = state.settings
    last: Any = None
    with contextlib.suppress(OSError, ValueError):
        last = json.loads(
            (settings.db_path.parent / backup_run.LAST_FILE).read_text(encoding="utf-8")
        )
    cfg = backup_run.load_config(backup_run.config_path(os.environ, settings))
    targets = {
        name: {"path": spec.get("path"), "state": backup_config.target_state(spec)}
        for name, spec in (cfg.get("targets") or {}).items()
    }
    return {"last": last, "targets": targets}
