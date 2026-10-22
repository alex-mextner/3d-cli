"""Mask inputs shared by the calibrate/register commands: an `instances.json` written by `segment`, a directory of PNGs or one PNG."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import io
from ..errors import BadInput


def _read_instances(path: Path) -> list[dict[str, object]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadInput(f"cannot read instances file {path}: {exc}") from exc
    insts = doc.get("instances") if isinstance(doc, dict) else None
    if not isinstance(insts, list):
        raise BadInput(
            f"{path} has no 'instances' list (is it the instances.json written by `3d segment`?)"
        )
    return insts


def instance_mask(instances_json: str | Path, instance_id: int) -> np.ndarray:
    """The mask of instance `instance_id` from an instances.json (mask paths are relative to the file)."""
    p = Path(instances_json)
    for inst in _read_instances(p):
        if inst.get("id") == instance_id:
            return io.load_mask(p.parent / str(inst["mask"]))
    raise BadInput(f"instance {instance_id} not found in {p}")


def load_masks(spec: str) -> list[np.ndarray]:
    """All masks of `spec`: instances.json (every instance, file order), a directory (every *.png, sorted) or one PNG."""
    p = Path(spec)
    if p.suffix.lower() == ".json":
        return [io.load_mask(p.parent / str(i["mask"])) for i in _read_instances(p)]
    if p.is_dir():
        files = sorted(p.glob("*.png"))
        if not files:
            raise BadInput(f"no *.png masks in {p}")
        return [io.load_mask(f) for f in files]
    return [io.load_mask(p)]
