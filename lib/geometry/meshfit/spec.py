"""spec.py — load and validate a mesh-deform spec (YAML or JSON).

WHAT: parses the declarative file that drives `3d mesh deform`: seed, named regions (optionally imported
  from a landmarks JSON), keep-out zones and the ordered op list. Unknown keys are errors, not silently
  ignored, so a typo never turns an op into a no-op.

WHY: the spec is the whole contract between a human (or an agent) and the deterministic engine;
  the same spec + the same input mesh must give the same bytes, so everything that could change the
  result — seed, parameters, region geometry — lives in this one file.

Top-level keys: version (1), seed (int), units (mm), keepout_tolerance_mm, landmarks, regions, keepout, ops.
  landmarks: {file: landmarks.json, prefix: lm_}  or a list of those; every landmark with a "box"
             becomes a region `<prefix><name>` (the garage-band landmarks_fur.json layout).
  keepout:   region references that never move (weight 1 = bit-identical, falloff band fades to 0).
  ops:       [{op: <name>, region: <reference>, ...params}], applied in order.
"""

from __future__ import annotations

import json
import os
from typing import Any

from errors import InputNotFound, UsageError

from . import regions as regions_mod

SPEC_VERSION = 1
COMMON_OP_KEYS = {"op", "region", "name"}
OP_KEYS: dict[str, set[str]] = {
    "smooth": {"iterations", "lambda", "method"},
    "inflate": {"amount", "normal_smoothing"},
    "draw": {"amount", "normal_smoothing"},
    "displace": {"amount", "direction"},
    "pinch": {"strength", "toward", "keep_normal", "normal_smoothing"},
    "crease": {
        "strength",
        "depth",
        "sharpness",
        "toward",
        "keep_normal",
        "normal_smoothing",
    },
    "scale_local": {"factor", "pivot"},
    "symmetrize": {"axis", "at", "mode"},
    "grab": {"handles", "anchors", "solver", "iterations", "influence", "handle_min"},
    "fur": {
        "seed",
        "flow",
        "amplitude",
        "amplitude_regions",
        "pitch",
        "length",
        "fine",
        "warp",
        "sharpness",
        "clumps",
        "normal_smoothing",
        "overhang_safe",
    },
    "remesh": {
        "engine",
        "target_edge",
        "max_edge",
        "max_rounds",
        "iterations",
        "feature_deg",
        "max_surface_dist",
    },
    "decimate": {"engine", "tolerance", "target_faces", "aggressiveness"},
    "repair": {"engine"},
    "fit_silhouette": {
        "views",
        "handles",
        "iterations",
        "step_mm",
        "min_step_mm",
        "min_gain",
        "anchors",
        "influence",
    },
}
OP_ALIASES = {"relief": "fur", "move": "grab", "scale": "scale_local"}
TOPOLOGY_OPS = {"remesh", "decimate", "repair"}
TOP_KEYS = {
    "version",
    "seed",
    "units",
    "keepout_tolerance_mm",
    "landmarks",
    "regions",
    "keepout",
    "ops",
    "description",
}


def canonical_op(name: Any) -> str:
    if not isinstance(name, str):
        raise _fail("ops[]", f"op must be a name, got {name!r}")
    key = name.replace("-", "_")
    return OP_ALIASES.get(key, key)


def _fail(where: str, message: str) -> UsageError:
    return UsageError(
        f"spec: {where}: {message}",
        command="mesh deform",
        remediation=[
            f"Ops: {', '.join(sorted(OP_KEYS))} (aliases: {', '.join(f'{a}={b}' for a, b in OP_ALIASES.items())}). See `3d mesh deform --help`."
        ],
    )


def load_spec(path: str) -> dict[str, Any]:
    """Read + validate a spec file; returns the normalised spec (regions merged, op names canonical)."""
    if not os.path.isfile(path):
        raise InputNotFound(path, command="mesh deform")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    import yaml

    try:
        raw = (
            json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
        )
    except (ValueError, yaml.YAMLError) as err:
        raise _fail(path, f"cannot parse: {err}") from err
    return normalise_spec(raw, os.path.dirname(os.path.abspath(path)))


def normalise_spec(raw: Any, base_dir: str = ".") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _fail("<root>", "spec must be a mapping")
    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown:
        raise _fail(
            "<root>", f"unknown keys {unknown}; accepted: {', '.join(sorted(TOP_KEYS))}"
        )
    if int(raw.get("version", SPEC_VERSION)) != SPEC_VERSION:
        raise _fail(
            "version",
            f"only version {SPEC_VERSION} is supported, got {raw['version']!r}",
        )
    spec: dict[str, Any] = {
        "version": SPEC_VERSION,
        "seed": int(raw.get("seed", 0)),
        "keepout_tolerance_mm": float(raw.get("keepout_tolerance_mm", 0.0)),
        "base_dir": base_dir,
        "description": str(raw.get("description", "")),
    }
    if str(raw.get("units", "mm")) != "mm":
        raise _fail("units", "only mm is supported")
    landmark_regions = _landmark_regions(raw.get("landmarks"), base_dir)
    regions = dict(landmark_regions)
    explicit = raw.get("regions", {}) or {}
    if not isinstance(explicit, dict):
        raise _fail("regions", "must be a mapping of name -> definition")
    clash = sorted(set(regions) & set(explicit))
    if clash:
        raise _fail("regions", f"names defined both by landmarks and regions: {clash}")
    regions.update(explicit)
    boxes = {name: d["box"] for name, d in landmark_regions.items()}
    regions = _substitute_boxes(regions, boxes)
    known = set(regions)
    for name, definition in regions.items():
        regions_mod.validate_region(str(name), definition, known)
    spec["regions"] = regions
    keepout = raw.get("keepout", []) or []
    if not isinstance(keepout, list):
        raise _fail("keepout", "must be a list of region references")
    for ref in keepout:
        regions_mod.validate_ref(ref, known, "keepout")
    spec["keepout"] = keepout
    ops = raw.get("ops")
    if not isinstance(ops, list) or not ops:
        raise _fail("ops", "must be a non-empty list")
    spec["ops"] = [
        _validate_op(i, _substitute_boxes(op, boxes), known) for i, op in enumerate(ops)
    ]
    return spec


def _substitute_boxes(obj: Any, boxes: dict[str, Any]) -> Any:
    """Replace `box: <landmark name>` by the landmark's numeric box anywhere in a region / op tree."""
    if isinstance(obj, dict):
        out = {k: _substitute_boxes(v, boxes) for k, v in obj.items()}
        if isinstance(out.get("box"), str):
            if out["box"] not in boxes:
                raise _fail(
                    "box",
                    f"unknown landmark {out['box']!r}; loaded: {', '.join(sorted(boxes)) or 'none'}",
                )
            out["box"] = boxes[out["box"]]
        return out
    if isinstance(obj, list):
        return [_substitute_boxes(v, boxes) for v in obj]
    return obj


def _landmark_regions(entries: Any, base_dir: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if entries is None:
        return out
    for entry in entries if isinstance(entries, list) else [entries]:
        if not isinstance(entry, dict) or "file" not in entry:
            raise _fail("landmarks", "needs {file: landmarks.json, prefix: lm_}")
        path = (
            entry["file"]
            if os.path.isabs(entry["file"])
            else os.path.join(base_dir, entry["file"])
        )
        if not os.path.isfile(path):
            raise InputNotFound(path, command="mesh deform")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        marks = data.get("landmarks", data)
        prefix = str(entry.get("prefix", ""))
        for name in sorted(marks):
            box = marks[name].get("box") if isinstance(marks[name], dict) else None
            if box is not None:
                out[f"{prefix}{name}"] = {"box": box}
    return out


def _validate_op(index: int, op: Any, known: set[str]) -> dict[str, Any]:
    where = f"ops[{index}]"
    if not isinstance(op, dict) or "op" not in op:
        raise _fail(where, "each op is a mapping with an `op:` name")
    name = canonical_op(op["op"])
    if name not in OP_KEYS:
        raise _fail(where, f"unknown op {op['op']!r}")
    extra = sorted(set(op) - COMMON_OP_KEYS - OP_KEYS[name])
    if extra:
        raise _fail(
            f"{where} ({name})",
            f"unknown keys {extra}; accepted: {', '.join(sorted(OP_KEYS[name] | COMMON_OP_KEYS))}",
        )
    regions_mod.validate_ref(op.get("region"), known, f"{where}.region")
    if name == "grab":
        handles = op.get("handles")
        if not isinstance(handles, list) or not handles:
            raise _fail(
                where, "grab needs handles: [{region, translate|rotate|scale}, ...]"
            )
        for h in handles:
            if not isinstance(h, dict) or "region" not in h:
                raise _fail(where, "each handle needs a region")
            regions_mod.validate_ref(h["region"], known, f"{where}.handles")
        for ref in op.get("anchors", []) or []:
            regions_mod.validate_ref(ref, known, f"{where}.anchors")
    if name == "fit_silhouette":
        views, handles = op.get("views"), op.get("handles")
        if (
            not isinstance(views, list)
            or not views
            or not all(isinstance(v, dict) and "mask" in v for v in views)
        ):
            raise _fail(
                where,
                "fit_silhouette needs views: [{mask: ref.png, view: front | ortho | perspective | camera_json}, ...]",
            )
        if (
            not isinstance(handles, list)
            or not handles
            or not all(
                isinstance(h, dict) and "region" in h and "direction" in h
                for h in handles
            )
        ):
            raise _fail(
                where,
                "fit_silhouette needs handles: [{region, direction: [x,y,z], max_move: mm}, ...]",
            )
        for h in handles:
            regions_mod.validate_ref(h["region"], known, f"{where}.handles")
        for ref in op.get("anchors", []) or []:
            regions_mod.validate_ref(ref, known, f"{where}.anchors")
    if name == "displace":
        direction = op.get("direction")
        if not (
            isinstance(direction, list)
            and len(direction) == 3
            and all(isinstance(c, (int, float)) for c in direction)
            and any(direction)
        ):
            raise _fail(
                where, "displace needs direction: [x, y, z] (non-zero), amount: mm"
            )
        if not isinstance(op.get("amount"), (int, float)) or isinstance(
            op.get("amount"), bool
        ):
            raise _fail(where, "displace needs amount: mm (a number)")
    if name == "fur":
        for item in op.get("amplitude_regions", []) or []:
            regions_mod.validate_ref(
                item.get("region"), known, f"{where}.amplitude_regions"
            )
        for item in op.get("clumps", []) or []:
            regions_mod.validate_ref(item.get("region"), known, f"{where}.clumps")
    return {**op, "op": name}
