"""backup_config.py — the `backup.yaml` that names the backup disk(s): `targets: {NAME: {path, expect_id}}`.

ACCESSED VIA: `3d printops backup target add|ls` (run where the PrintOps data lives: on the host, or
`docker exec printops-api …` for the container), `printops.backup_setup` (what `3d init --env` calls).

WHAT: a TARGET is a disk root as seen by the process that runs the backup (`/backup` inside the
3d-api container, a mount point on a host) plus the disk id its `.3d-backup-disk` marker must carry.
Schedule, sub-directory and retention are NOT here: Home Assistant sends them with every run
(printops.backup_run). Adding a target may write the marker to the disk — the one deliberate write,
and only with `--init-marker`; discovery never writes.

INVARIANTS: the file is replaced atomically; it holds no secret, only a disk id. An existing marker
is never rewritten (its id is what configured targets expect).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from printops import backup_store
from printops.backup_policy import TARGET_NAME_RE
from printops.backup_store import BackupRefused


def _dump(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        "# Written by `3d printops backup target add`; see docs/commands/printops.md#backup\n"
        + yaml.safe_dump(data, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def add_target(
    cfg_file: Path, name: str, root: Path, *, init_marker: bool = False, note: str = ""
) -> str:
    """Register `root` as target `name`; returns the disk id. Writes the marker only with init_marker."""
    from printops.backup_run import load_config

    if not TARGET_NAME_RE.fullmatch(name):
        raise BackupRefused(
            f"target name {name!r}: ASCII letters, digits, - and _ only (max 64)"
        )
    if not root.is_dir():
        raise BackupRefused(f"{root} is not a directory (is the disk mounted?)")
    marker = backup_store.read_marker(root)
    if marker is None:
        if not init_marker:
            raise BackupRefused(
                f"{root} has no {backup_store.MARKER}: pass --init-marker if THIS is the backup "
                "disk (it writes that one small file)"
            )
        ident = backup_store.write_marker(
            root, note or f"backup disk for target {name}"
        )
    else:
        ident = str(marker["id"])
    cfg = load_config(cfg_file)
    cfg.setdefault("targets", {})[name] = {"path": str(root), "expect_id": ident}
    _dump(cfg_file, cfg)
    return ident


def target_state(spec: dict[str, Any]) -> str:
    root = Path(spec["path"])
    if not root.is_dir():
        return "disk missing"
    marker = backup_store.read_marker(root)
    if marker is None:
        return "no marker"
    return "OK" if marker["id"] == spec.get("expect_id") else "WRONG DISK (id differs)"


def print_targets(cfg_file: Path) -> int:
    from printops.backup_run import load_config

    targets = load_config(cfg_file).get("targets") or {}
    if not targets:
        print(f"no backup target in {cfg_file}")
        return 0
    for name, spec in sorted(targets.items()):
        root = Path(spec["path"])
        free = (
            f", {shutil.disk_usage(root).free // (1 << 30)} GiB free"
            if root.is_dir()
            else ""
        )
        print(f"{name}: {root}  [{target_state(spec)}]{free}")
    return 0
