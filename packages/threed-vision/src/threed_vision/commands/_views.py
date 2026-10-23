"""Shared CLI parsing for `--view MASK:CAM.json` (used by fit-silhouette and view-check; not a command itself)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import BadInput
from ..io import load_mask


def load_camera(path: str | Path) -> dict[str, Any]:
    """CAM.json = {"K": 3x3, "R": 3x3, "t": 3} (world -> camera, mm, OpenCV) or {"K": 3x3, "pose": "identity"}."""
    p = Path(path)
    if not p.is_file():
        raise BadInput(f"camera file not found: {p}")
    try:
        cam = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BadInput(f"camera file {p} is not valid JSON: {exc}") from exc
    if not isinstance(cam, dict) or "K" not in cam:
        raise BadInput(
            f"camera file {p} needs a 'K' (3x3 intrinsics) and either 'R'+'t' or \"pose\": \"identity\""
        )
    if cam.get("pose") == "identity":
        return {"K": cam["K"], "R": np.eye(3).tolist(), "t": [0.0, 0.0, 0.0]}
    if "R" not in cam or "t" not in cam:
        raise BadInput(
            f"camera file {p} needs 'R' and 't' (world->camera) or \"pose\": \"identity\""
        )
    return {"K": cam["K"], "R": cam["R"], "t": cam["t"]}


def load_mask_spec(spec: str) -> np.ndarray:
    """`mask.png` or `instances.json#ID` (an instance of `3d segment`; its `mask` path is relative to the JSON)."""
    path, _, ident = spec.partition("#")
    if not ident:
        return load_mask(path)
    p = Path(path)
    if not p.is_file():
        raise BadInput(f"instances file not found: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        inst = next(i for i in doc["instances"] if str(i["id"]) == ident)
        mask_path = p.parent / inst["mask"]
    except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
        raise BadInput(f"instance {ident!r} (with a 'mask' path) not found in {p}") from exc
    return load_mask(mask_path)


def parse_view(spec: str) -> dict[str, Any]:
    """`MASK:CAM.json` -> {"mask", "K", "R", "t", "name"}; the camera is after the LAST colon."""
    mask_spec, sep, cam_path = spec.rpartition(":")
    if not sep or not mask_spec or not cam_path:
        raise BadInput(
            f"--view expects MASK:CAM.json, got {spec!r}",
            remediation="e.g. --view mask.png:cam.json",
        )
    cam = load_camera(cam_path)
    return {"mask": load_mask_spec(mask_spec), **cam, "name": mask_spec}
