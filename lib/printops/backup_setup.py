"""backup_setup.py — choose and configure WHERE the Spoolman/PrintOps backups go (`3d init --env`).

ACCESSED VIA: `printops.env_stage`, `3d printops backup discover`.

THREE WAYS (the menu never pre-selects one; `--no-input` needs an explicit `--backup`):
  FOUND   the read-only candidates of `printops.backup_discovery` on the host that runs Spoolman
  MANUAL  the user types a directory on that host
  AGENT   `handoff_prompt`: a self-contained text an AI agent (or the user) follows to prepare the disk,
          mount it, and finish with `3d printops backup selftest` — 3d itself touches nothing

CONFIGURE (`configure_backup`, after the user picked a directory — the only place that writes):
  1. the directory must exist on the host (nothing is created, mounted or formatted);
  2. the 3d-api container gets it bound at /backup (`ctx.redeploy(path)` re-runs the compose step of
     `3d printops deploy` with PRINTOPS_BACKUP_DIR=path — Spoolman's own container is not touched);
  3. inside the container: `3d printops backup target add default /backup --init-marker` (marker file +
     backup.yaml), then `3d printops backup selftest`: one real backup, verified, restored into a scratch
     directory and compared. Only a passing selftest counts as "configured".
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from printops.backup_discovery import (
    Candidate,
    Discovery,
    RunHost,
    describe,
    discover_backup_targets,
)

SUBDIR = "3d-printops"
TARGET = "default"
CONTAINER_ROOT = "/backup"


def local_shell() -> RunHost:
    def run(script: str) -> tuple[int, str]:
        done = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout + (done.stderr if done.returncode else "")

    return run


def ssh_shell(
    host: str,
    opts: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=40"),
) -> RunHost:
    def run(script: str) -> tuple[int, str]:
        done = subprocess.run(
            ["ssh", *opts, host, script], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout + (done.stderr if done.returncode else "")

    return run


@dataclass
class BackupContext:
    run: RunHost  # a shell on the host that runs Spoolman (local bash or ssh)
    where: str  # "this machine" | "USER@HOST"
    data_dir: (
        str  # PRINTOPS_DATA_DIR on that host (the Spoolman data must not be the backup)
    )
    container: str  # the 3d-api container name
    redeploy: Callable[[str], None]  # bind <host dir> as /backup, recreate 3d-api only
    ask: Callable[[str], str]
    say: Callable[[str], None]
    interactive: bool
    hint_gb: int = 512
    ha_candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Configured:
    ok: bool
    path: str = ""
    detail: str = ""


def exec_in_container(ctx: BackupContext, *argv: str) -> tuple[int, str]:
    return ctx.run(
        f"docker exec {shlex.quote(ctx.container)} "
        + " ".join(shlex.quote(a) for a in argv)
    )


def show(ctx: BackupContext, found: Discovery) -> list[Candidate]:
    """Print every candidate; returns the selectable ones in menu order."""
    pickable = [c for c in found.candidates if c.selectable]
    ctx.say(
        f"Backup locations seen on {ctx.where} (read-only look; nothing is selected for you):"
    )
    for c in found.candidates:
        mark = f"{pickable.index(c) + 1})" if c in pickable else " -"
        ctx.say(f"  {mark} {describe(c)}")
        for note in c.notes:
            ctx.say(f"       {note}")
    if found.skipped:
        ctx.say(
            f"  (left out: {len(found.skipped)} system/pseudo mounts such as {', '.join(found.skipped[:3])})"
        )
    if not pickable:
        ctx.say(
            "  nothing usable is mounted. Mount the backup disk first (3d never mounts or formats a disk)."
        )
    return pickable


def discover(ctx: BackupContext) -> Discovery:
    return discover_backup_targets(
        ctx.run, data_dir=ctx.data_dir, hint_gb=ctx.hint_gb, extra=ctx.ha_candidates
    )


def handoff_prompt(ctx: BackupContext, found: Discovery) -> str:
    seen = (
        "\n".join(f"  - {describe(c)}" for c in found.candidates)
        or "  (nothing usable was found)"
    )
    return f"""TASK: set up daily, rotated backups of Spoolman + PrintOps on {ctx.where}. You may use a shell there.

WHAT EXISTS
- Spoolman + the 3d-api run as the docker compose project `printops` (containers `{ctx.container}` and the Spoolman
  one). Spoolman's data is under {ctx.data_dir} (spoolman/spoolman.db, api/printops.db, api/tokens.json).
- The backup runner is `3d printops backup` (inside the container: `docker exec {ctx.container} 3d printops backup ...`).
  It writes verified sets `3d-backup-<UTC>/` into /backup/{SUBDIR} of the container and refuses any disk that has no
  `.3d-backup-disk` marker. Home Assistant (not cron, not systemd) schedules it at 04:00 through 3d-api.
- What a read-only look at the host found (nothing was changed, nothing chosen):
{seen}

DO, IN ORDER (ask the user before anything marked CONFIRM)
1. Pick the backup disk WITH THE USER (about {ctx.hint_gb} GB is what they expect). It must NOT be the disk that holds
   {ctx.data_dir}. CONFIRM the choice, the device and the mount point. If it is not mounted: CONFIRM, then mount it at a
   stable path (fstab entry by UUID, `nofail`). Do NOT format, partition or repair anything unless the user explicitly
   asks for exactly that.
2. Make the directory writable by the user the containers run as (`id -u`/`id -g` of the owner of {ctx.data_dir}).
3. Bind it into the 3d-api container: re-run the deploy with PRINTOPS_BACKUP_DIR=<that directory>
   (`3d printops deploy --backup-dir <dir> ...`, or `docker compose -p printops ... up -d` with that variable).
4. Register the disk: `docker exec {ctx.container} 3d printops backup target add {TARGET} {CONTAINER_ROOT} --init-marker`
5. Prove it: `docker exec {ctx.container} 3d printops backup selftest --target {TARGET}`. It must end with `selftest ok`.
6. Tell the user the schedule lives in Home Assistant (integrations/homeassistant/packages/printops_backup.yaml,
   04:00, 7 daily + 4 weekly + 6 monthly; edit the block marked EDIT HERE). If HA is not set up yet:
   `3d init --env` offers to install that package.

DONE WHEN: `selftest ok` printed, `3d printops backup ls --target {TARGET}` lists the set, and the user knows the disk
path. Report the exact commands you ran. If anything contradicts this text, stop and ask."""


def configure_backup(ctx: BackupContext, host_path: str) -> Configured:
    """Bind `host_path` as the backup disk, register it and prove it with a selftest. The only writing step."""
    rc, out = ctx.run(
        f"test -d {shlex.quote(host_path)} -a -w {shlex.quote(host_path)}"
    )
    if rc != 0:
        return Configured(
            False,
            host_path,
            f"{host_path} is not a writable directory on {ctx.where} "
            "(mount the disk and fix the permissions first; 3d creates and mounts nothing)"
            + out,
        )
    ctx.say(
        f"binding {host_path} into the 3d-api container as {CONTAINER_ROOT} (Spoolman is not restarted) ..."
    )
    ctx.redeploy(host_path)
    rc, out = exec_in_container(
        ctx,
        "3d",
        "printops",
        "backup",
        "target",
        "add",
        TARGET,
        CONTAINER_ROOT,
        "--init-marker",
    )
    if rc != 0:
        return Configured(
            False, host_path, f"registering the disk failed: {out.strip()}"
        )
    ctx.say(out.strip())
    rc, out = exec_in_container(
        ctx,
        "3d",
        "printops",
        "backup",
        "selftest",
        "--target",
        TARGET,
        "--subdir",
        SUBDIR,
        "--no-nice",
    )
    ctx.say(out.strip())
    if rc != 0:
        return Configured(
            False,
            host_path,
            "the test backup / restore check failed (see above); nothing is scheduled",
        )
    return Configured(
        True,
        host_path,
        "test backup written, verified and restored into a scratch directory",
    )
