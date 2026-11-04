"""`3d printops backup` — daily-schedulable backups of Spoolman + the PrintOps DB, with rotation.

The SCHEDULE is Home Assistant's (integrations/homeassistant/packages/printops_backup.yaml, 04:00);
this module is what it runs (through 3d-api `POST /v1/backup/run`) and what a human runs by hand.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cli import paths
from errors import GateFailure, UsageError

from printops import backup_config, backup_run, backup_setup, backup_store
from printops.backup_discovery import DEFAULT_HINT_GB, describe, discover_backup_targets
from printops.backup_policy import Retention, parse_keep
from printops.backup_store import BackupBroken, BackupRefused, SetInfo
from printops.cli import Sub
from printops.cli._common import Parsed, parse
from printops.settings import Settings

USAGE = """3d printops backup run [--target NAME | --dest DIR] [--subdir D] [--keep SPEC] [--json] ...
3d printops backup ls|verify|prune|restore|target ...
  Consistent, rotated backups of Spoolman's DB, the PrintOps DB and the tokens file, written to a
  DISK that identifies itself: a run refuses (exit 1, nothing written, notification) when the
  disk's marker file `.3d-backup-disk` is missing or has another id — an unmounted disk never fills
  the root filesystem. A set is `3d-backup-<UTC>/` with gzip'd SQLite snapshots (online backup API,
  small steps, nice 19 + ionice idle), MANIFEST.json and SHA256SUMS, verified (integrity_check on
  every decompressed DB) before it is published with one rename.

  run      write a set, then prune: keeps the newest set of each of the last N days/weeks/months
           (default 7 daily + 4 weekly + 6 monthly). Prune touches ONLY directories named
           3d-backup-YYYYMMDDTHHMMSSZ that hold a MANIFEST.json; nothing else on the disk.
  ls       the sets of a target (size, age, whether the policy keeps them)
  verify   [SET|latest] checksums + integrity_check + row counts of a stored set; exit 1 on a problem
  prune    apply the policy now (--keep, else the policy of the last run on that disk; --dry-run)
  restore  [SET|latest] --into DIR: unpack a verified set (flat: spoolman.db, printops.db, tokens.json);
           --layout data writes the compose data-dir layout (spoolman/spoolman.db, api/...) and needs --yes;
           an existing file is moved to <name>.pre-restore first. Stop the stack before restoring into it.
  target   add NAME PATH [--init-marker]   |   ls   configured targets (backup.yaml next to the PrintOps DB)
  selftest run once, then restore that set into a scratch dir and compare integrity + row counts (what `3d init --env` runs)
  discover [--ssh USER@HOST] [--data-dir DIR] [--hint-gb N] [--json]   read-only list of disks that could hold backups

Options:
  --target NAME    configured target (default: default)         --dest DIR   a disk root instead (marker required)
  --expect-id ID   with --dest: the marker id it must carry     --subdir D   directory on the disk (default 3d-printops)
  --keep SPEC      daily=7,weekly=4,monthly=6 (any subset)      --min-free-gb N  refuse below N GB of reserve (default 1)
  --no-spoolman    back up PrintOps only                        --no-nice   keep the normal CPU/I/O priority
  --dry-run        prune: list, remove nothing                  --json      machine-readable output

Examples:
  3d printops backup run --target default --keep daily=14,weekly=8,monthly=12
  3d printops backup ls --target default
  3d printops backup verify latest --target default
  3d printops backup restore latest --target default --into ~/restore-test
  docker exec printops-api 3d printops backup run --target default --json   # what Home Assistant triggers"""

VALUE_OPTS = {
    "--target",
    "--dest",
    "--expect-id",
    "--subdir",
    "--keep",
    "--min-free-gb",
    "--into",
    "--layout",
    "--config",
    "--ssh",
    "--data-dir",
    "--hint-gb",
}
FLAGS = {"--no-spoolman", "--no-nice", "--dry-run", "--json", "--yes", "--init-marker"}


def _request(p: Parsed) -> backup_run.RunRequest:
    req = backup_run.RunRequest(
        target=p.opts.get("--target", backup_run.DEFAULT_TARGET),
        expect_id=p.opts.get("--expect-id"),
        subdir=p.opts.get("--subdir", backup_run.DEFAULT_SUBDIR),
        spoolman="--no-spoolman" not in p.flags,
        low_priority="--no-nice" not in p.flags,
    )
    if not backup_run.valid_subdir(req.subdir):
        raise UsageError(
            f"bad --subdir {req.subdir!r}: plain relative names, no . or ..",
            command="printops backup",
        )
    if "--dest" in p.opts:
        req.dest = Path(p.opts["--dest"])
    if "--keep" in p.opts:
        req.retention = parse_keep(p.opts["--keep"])
    if "--min-free-gb" in p.opts:
        try:
            req.min_free_bytes = int(float(p.opts["--min-free-gb"]) * (1 << 30))
        except ValueError as e:
            raise UsageError(
                f"--min-free-gb needs a number, got {p.opts['--min-free-gb']!r}",
                command="printops backup",
            ) from e
    return req


def _dest(p: Parsed) -> tuple[Path, str]:
    """The directory holding the sets of the chosen target (the disk guard has passed) + the target name."""
    req = _request(p)
    env = os.environ
    settings = Settings.from_env(env)
    cfg_file = backup_run.config_path(env, settings)
    try:
        target = backup_run.resolve_target(
            req, backup_run.load_config(cfg_file), cfg_file
        )
        backup_store.check_disk(target.path, target.expect_id)
    except BackupRefused as e:
        raise GateFailure(str(e), command="printops backup") from e
    return target.path / req.subdir, target.name


def _find_set(dest: Path, ref: str | None) -> SetInfo:
    sets = [s for s in backup_store.list_sets(dest) if s.manifest is not None]
    if ref in (None, "latest"):
        if not sets:
            raise GateFailure(
                f"no backup set in {dest} yet",
                command="printops backup",
                remediation=["Write one: `3d printops backup run`."],
            )
        return sets[0]
    for s in sets:
        if ref in (s.name, s.name.removeprefix("3d-backup-")):
            return s
    raise GateFailure(
        f"no backup set {ref} in {dest}",
        command="printops backup",
        remediation=[f"`3d printops backup ls` lists the sets of {dest}."],
    )


def _print_result(res: backup_run.RunResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res.to_dict(), sort_keys=True))
        return
    print(
        f"ok  {res.set_name}  {res.stored_bytes} bytes stored, {res.seconds}s, target {res.target}"
    )
    for name, item in res.items.items():
        print(
            f"    {name}: {item['raw_bytes']} -> {item['stored_bytes']} bytes  sha256 {item['sha256'][:16]}…"
        )
    print(
        f"    kept {len(res.kept)} set(s) ({Retention(**res.retention).describe()}); pruned {len(res.pruned)}: "
        + (", ".join(res.pruned) or "none")
    )


def _run(p: Parsed) -> int:
    try:
        res = backup_run.run_and_report(_request(p))
    except (BackupRefused, BackupBroken, OSError) as e:
        if "--json" in p.flags:
            print(json.dumps({"status": "failed", "error": str(e)}))
        raise GateFailure(f"backup failed: {e}", command="printops backup run") from e
    _print_result(res, "--json" in p.flags)
    return 0


def _ls(p: Parsed) -> int:
    dest, _ = _dest(p)
    retention = _request(p).retention or backup_store.read_state(dest) or Retention()
    sets = backup_store.list_sets(dest)
    keep, _drop = backup_store.plan_prune(
        [s.name for s in sets if s.manifest is not None], retention
    )
    rows: list[dict[str, Any]] = [
        {
            "set": s.name,
            "created": s.created.isoformat(),
            "stored_bytes": s.stored_bytes,
            "items": sorted((s.manifest or {}).get("items", {})),
            "state": "NO MANIFEST (not touched)"
            if s.manifest is None
            else ("keep" if s.name in keep else "prune"),
        }
        for s in sets
    ]
    if "--json" in p.flags:
        print(
            json.dumps(
                {"dest": str(dest), "retention": retention.to_dict(), "sets": rows},
                indent=2,
            )
        )
        return 0
    print(f"{dest}  (policy {retention.describe()})")
    for r in rows:
        print(
            f"  {r['set']}  {r['stored_bytes']:>12} B  {r['state']:<6} {','.join(r['items'])}"
        )
    print(f"{len(rows)} set(s)")
    return 0


def _verify(p: Parsed) -> int:
    dest, _ = _dest(p)
    info = _find_set(dest, p.pos[1] if len(p.pos) > 1 else None)
    manifest, problems = backup_store.verify_dir(info.path)
    if problems:
        for line in problems:
            print(f"FAIL  {line}")
        raise GateFailure(
            f"{info.name} failed verification ({len(problems)} problem(s))",
            command="printops backup verify",
        )
    items = (manifest or {}).get("items", {})
    print(
        f"ok  {info.name}: {len(items)} item(s), checksums, integrity_check and row counts match"
    )
    return 0


def _prune(p: Parsed) -> int:
    dest, _ = _dest(p)
    retention = _request(p).retention or backup_store.read_state(dest) or Retention()
    try:
        keep, drop = backup_store.prune(dest, retention, dry_run="--dry-run" in p.flags)
    except BackupBroken as e:
        raise GateFailure(str(e), command="printops backup prune") from e
    verb = "would remove" if "--dry-run" in p.flags else "removed"
    print(
        f"policy {retention.describe()}: keep {len(keep)}, {verb} {len(drop)}"
        + "".join(f"\n  {n}" for n in drop)
    )
    return 0


def _restore(p: Parsed) -> int:
    if "--into" not in p.opts:
        raise UsageError("restore needs --into DIR", command="printops backup restore")
    layout = p.opts.get("--layout", "flat")
    if layout not in ("flat", "data"):
        raise UsageError(
            f"--layout is flat or data, got {layout!r}",
            command="printops backup restore",
        )
    if layout == "data" and "--yes" not in p.flags:
        raise UsageError(
            "--layout data writes into a live data dir: stop the stack and pass --yes",
            command="printops backup restore",
        )
    dest, _ = _dest(p)
    info = _find_set(dest, p.pos[1] if len(p.pos) > 1 else None)
    try:
        restored = backup_store.restore_set(
            info, Path(p.opts["--into"]).expanduser(), layout=layout
        )
    except BackupBroken as e:
        raise GateFailure(str(e), command="printops backup restore") from e
    for f in restored.written:
        print(f"restored {f}")
    for name, filename in restored.absent.items():
        print(
            f"{info.name} has no {name} ({filename}): an existing one was moved to {filename}.pre-restore"
        )
    print(
        f"from {info.name}. Start the stack again: `docker compose -p printops -f services/3d-api/compose.yaml up -d`"
    )
    return 0


def _target(p: Parsed) -> int:
    verb = p.pos[1] if len(p.pos) > 1 else "ls"
    cfg_file = (
        Path(p.opts["--config"])
        if "--config" in p.opts
        else backup_run.config_path(os.environ, Settings.from_env())
    )
    if verb == "ls":
        return backup_config.print_targets(cfg_file)
    if verb != "add" or len(p.pos) != 4:
        raise UsageError(
            "usage: backup target ls | backup target add NAME PATH [--init-marker]",
            command="printops backup target",
        )
    try:
        ident = backup_config.add_target(
            cfg_file,
            p.pos[2],
            Path(p.pos[3]).expanduser(),
            init_marker="--init-marker" in p.flags,
        )
    except BackupRefused as e:
        raise GateFailure(str(e), command="printops backup target") from e
    print(f"target {p.pos[2]!r} -> {p.pos[3]} (disk id {ident}) saved in {cfg_file}")
    return 0


def _discover(p: Parsed) -> int:
    """Read-only look at the host's disks (this machine, or --ssh USER@HOST); chooses nothing."""
    shell = (
        backup_setup.ssh_shell(p.opts["--ssh"])
        if "--ssh" in p.opts
        else backup_setup.local_shell()
    )
    hint = p.int_opt("--hint-gb", "backup discover") or DEFAULT_HINT_GB
    data_dir = p.opts.get("--data-dir") or str(paths.data_dir())
    found = discover_backup_targets(shell, data_dir=data_dir, hint_gb=hint)
    if "--json" in p.flags:
        print(
            json.dumps(
                {
                    "data_device": found.data_device,
                    "skipped": found.skipped,
                    "candidates": [
                        {**c.__dict__, "selectable": c.selectable}
                        for c in found.candidates
                    ],
                },
                indent=2,
            )
        )
        return 0
    for c in found.candidates:
        print(("* " if c.selectable else "- ") + describe(c))
        for note in c.notes:
            print(f"    {note}")
    print(
        f"{len([c for c in found.candidates if c.selectable])} selectable (*), {len(found.candidates)} listed, "
        f"{len(found.skipped)} system/pseudo mounts left out. Nothing was changed or chosen."
    )
    return 0


def _selftest(p: Parsed) -> int:
    """One real run, then proof it can be restored: verify + restore into a scratch dir + row counts."""
    _run(p)
    dest, _ = _dest(p)
    info = _find_set(dest, "latest")
    try:
        for line in backup_store.restore_check(info):
            print(f"restore-check  {line}")
    except BackupBroken as e:
        raise GateFailure(str(e), command="printops backup selftest") from e
    print(f"selftest ok: {info.name} restores and matches its manifest")
    return 0


VERBS = {
    "run": _run,
    "ls": _ls,
    "verify": _verify,
    "prune": _prune,
    "restore": _restore,
    "target": _target,
    "selftest": _selftest,
    "discover": _discover,
}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    p = parse(argv, argv[0], values=VALUE_OPTS, flags=FLAGS, n_pos=(1, 4))
    verb = VERBS.get(p.pos[0])
    if verb is None:
        raise UsageError(
            f"unknown backup subcommand {p.pos[0]!r}",
            command="printops backup",
            remediation=[
                f"One of: {', '.join(VERBS)}. See `3d printops backup --help`."
            ],
        )
    return verb(p)


SUB = Sub(
    name="backup",
    summary="rotated Spoolman + PrintOps backups to a verified disk (HA schedules them)",
    usage=USAGE,
    run=run,
)
