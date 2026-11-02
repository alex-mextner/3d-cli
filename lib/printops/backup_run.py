"""backup_run.py — one backup run: resolve the target, guard the disk, write the set, prune, report.

ACCESSED VIA: `3d printops backup run` (lib/printops/cli/backup.py); 3d-api's `POST /v1/backup/run`
spawns that CLI as a child process (so the run is niced and memory-isolated from the API).

WHO DECIDES WHAT:
  - WHERE the disk is, WHICH disk it must be (marker id) and which extra files matter: the
    `backup.yaml` next to the PrintOps DB (written by `3d init --env` / `3d printops backup target`).
  - WHEN, which sub-directory and how many sets to keep: Home Assistant
    (integrations/homeassistant/packages/printops_backup.yaml) — passed with every run. A run by hand
    without `--keep` re-uses the last policy a run recorded on the disk, then the built-in default.

LIGHT ON A WEAK HOST: the process lowers its own CPU (nice 19) and I/O (ionice idle) priority, the
SQLite copy runs in small steps with pauses, and compression is gzip level 1.

FAILURE (`run_and_report`): the guard refusing, a full disk, a bad integrity check — anything raises
and `printops.backup_notify` raises the notification + anomaly; the CLI exits non-zero.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printops import backup_notify, backup_store
from printops.backup_policy import Retention
from printops.backup_store import BackupBroken, BackupRefused, Source
from printops.settings import Settings

CONFIG_ENV = "PRINTOPS_BACKUP_CONFIG"
DEFAULT_TARGET = "default"
DEFAULT_SUBDIR = "3d-printops"
DEFAULT_MIN_FREE_GB = 1.0
LAST_FILE = "backup-last.json"
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def valid_subdir(subdir: str) -> bool:
    """A relative path of plain names on the disk: no `.`/`..` component, nothing outside the disk."""
    return bool(SUBDIR_RE.fullmatch(subdir)) and not {".", ".."} & set(
        subdir.split("/")
    )


LOCK = ".3d-backup.lock"


@dataclass(frozen=True)
class Target:
    name: str
    path: Path
    expect_id: str | None = None


@dataclass
class RunRequest:
    target: str = DEFAULT_TARGET
    dest: Path | None = (
        None  # an ad-hoc root instead of a configured target (the marker is still required)
    )
    expect_id: str | None = None
    subdir: str = DEFAULT_SUBDIR
    retention: Retention | None = (
        None  # None: last recorded on the disk, else the default
    )
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GB * (1 << 30))
    spoolman: bool = True
    low_priority: bool = True
    extra_files: list[Path] = field(default_factory=list)


@dataclass
class RunResult:
    status: str
    target: str
    set_name: str
    path: str
    stored_bytes: int
    items: dict[str, dict[str, Any]]
    kept: list[str]
    pruned: list[str]
    retention: dict[str, int]
    free_bytes: int
    seconds: float
    at: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def config_path(env: Mapping[str, str], settings: Settings) -> Path:
    return Path(
        env.get(CONFIG_ENV) or settings.db_path.parent / "backup.yaml"
    ).expanduser()


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BackupRefused(f"{path}: expected a mapping with `targets:`")
    return data


def resolve_target(req: RunRequest, cfg: Mapping[str, Any], cfg_file: Path) -> Target:
    if req.dest is not None:
        return Target("adhoc", req.dest.expanduser(), req.expect_id)
    spec = (cfg.get("targets") or {}).get(req.target)
    if not isinstance(spec, dict) or not spec.get("path"):
        known = ", ".join(sorted(cfg.get("targets") or {})) or "none"
        raise BackupRefused(
            f"backup target {req.target!r} is not configured in {cfg_file} (known: {known}); "
            "configure one: `3d init --env` or `3d printops backup target add`"
        )
    return Target(
        req.target,
        Path(str(spec["path"])).expanduser(),
        req.expect_id or spec.get("expect_id"),
    )


def build_sources(
    req: RunRequest, settings: Settings, env: Mapping[str, str], cfg: Mapping[str, Any]
) -> list[Source]:
    sources = [Source("printops", settings.db_path, True)]
    spoolman = env.get("SPOOLMAN_DB")
    if req.spoolman:
        if not spoolman:
            raise BackupRefused(
                "SPOOLMAN_DB is not set: Spoolman's database would be missing from the backup "
                "(--no-spoolman backs up PrintOps only)"
            )
        sources.append(Source("spoolman", Path(spoolman).expanduser(), True))
    if settings.tokens_file.is_file():
        sources.append(Source("tokens", settings.tokens_file, False))
    extra = [*(Path(str(p)) for p in cfg.get("extra_files") or []), *req.extra_files]
    sources += [
        Source(p.name, p.expanduser(), False) for p in extra if p.expanduser().is_file()
    ]
    missing = [s for s in sources if not s.path.is_file()]
    if missing:
        raise BackupBroken(
            "source missing: " + ", ".join(f"{s.name} ({s.path})" for s in missing)
        )
    return sources


def absent_sources(sources: list[Source], settings: Settings) -> dict[str, str]:
    """Items a run backs up when they exist and must RESTORE as absent when they do not: a missing tokens
    file is the valid deny-all state, so a restore may not leave a stale (authorised) one in place."""
    has_tokens = any(s.name == "tokens" for s in sources)
    return {} if has_tokens else {"tokens": settings.tokens_file.name}


def lower_priority() -> None:
    """nice 19 + idle I/O class for this process (best effort: a container may forbid it)."""
    with contextlib.suppress(OSError):
        os.nice(19)
    if shutil.which("ionice"):
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(os.getpid())],
            check=False,
            capture_output=True,
        )


@contextlib.contextmanager
def exclusive(dest: Path) -> Iterator[None]:
    with (dest / LOCK).open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise BackupRefused(f"another backup run holds {dest / LOCK}") from e
        yield


def run_backup(req: RunRequest, env: Mapping[str, str] | None = None) -> RunResult:
    env = os.environ if env is None else env
    settings = Settings.from_env(env)
    cfg_file = config_path(env, settings)
    cfg = load_config(cfg_file)
    target = resolve_target(req, cfg, cfg_file)
    backup_store.check_disk(target.path, target.expect_id)  # BEFORE anything is written
    if not valid_subdir(req.subdir):
        raise BackupRefused(
            f"bad sub-directory {req.subdir!r}: relative, letters/digits/._- only, no . or .."
        )
    sources = build_sources(req, settings, env, cfg)
    dest = target.path / req.subdir
    started = time.monotonic()
    if req.low_priority:
        lower_priority()
    dest.mkdir(parents=True, exist_ok=True)
    free = backup_store.check_space(dest, sources, req.min_free_bytes)
    with exclusive(dest):
        info = backup_store.write_set(
            dest, sources, target=target.name, absent=absent_sources(sources, settings)
        )
        retention = req.retention or backup_store.read_state(dest) or Retention()
        kept, pruned = backup_store.prune(dest, retention)
        backup_store.write_state(dest, retention)
    return RunResult(
        "ok",
        target.name,
        info.name,
        str(info.path),
        info.stored_bytes,
        (info.manifest or {})["items"],
        kept,
        pruned,
        retention.to_dict(),
        free,
        round(time.monotonic() - started, 2),
        datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    )


def _write_last(env: Mapping[str, str], body: dict[str, Any]) -> None:
    path = Settings.from_env(env).db_path.parent / LAST_FILE
    with contextlib.suppress(OSError):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)


def run_and_report(req: RunRequest, env: Mapping[str, str] | None = None) -> RunResult:
    """run_backup + the signals: raises the original error after notifying."""
    env = os.environ if env is None else env
    ctx = backup_notify.Context(
        req.target if req.dest is None else "adhoc", Settings.from_env(env).db_path, env
    )
    try:
        result = run_backup(req, env)
    except Exception as e:
        _write_last(
            env,
            {
                "status": "failed",
                "target": ctx.target,
                "error": str(e),
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                    timespec="seconds"
                ),
            },
        )
        backup_notify.report_failure(ctx, str(e))
        raise
    _write_last(env, result.to_dict())
    backup_notify.report_recovered(ctx)
    return result
