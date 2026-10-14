"""values.py — `apply_values`: set option values in a repo preset, the write path calibrations
(issue https://github.com/alex-mextner/3d-cli/issues/168) use for calibrated flow ratio and
pressure advance. It edits only `profiles/<slicer>/<kind>/[base/]<name>.json`; `3d profiles sync`
then pushes the change into the slicer.

Values are stored the way the slicer serializes them (ConfigBase::save_to_json): a string per
scalar option and a list of strings per vector option (one per extruder). A scalar given for an
option the preset already holds as a list is written to every element; a NEW vector option must
be passed as a list, since a scalar is stored as a scalar string.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from errors import InputNotFound, InvalidArgument
from profiles import preset, slicers

Scalar = str | int | float | bool
Value = Scalar | Sequence[Scalar]
FIXED_KEYS = frozenset({"name", "from", "inherits", "version"})


def _scalar(value: Scalar, key: str) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidArgument(
                flag=key,
                got=repr(value),
                accepted=["a finite number"],
                command="profiles",
            )
        text = repr(value)
        return f"{value:.10f}".rstrip("0").rstrip(".") if "e" in text else text
    if isinstance(value, str):
        return value
    raise InvalidArgument(
        flag=key,
        got=repr(value),
        accepted=["str", "int", "float", "bool", "a list of those"],
        command="profiles",
    )


def _stored(key: str, value: Value, current: object) -> str | list[str]:
    if isinstance(value, (str, int, float, bool)):
        text = _scalar(value, key)
        return [text] * max(len(current), 1) if isinstance(current, list) else text
    return [_scalar(v, key) for v in value]


def preset_path(
    slicer: str, kind: str, name: str, *, profiles_dir: Path | None = None
) -> Path:
    if slicer not in slicers.SLICERS:
        raise InvalidArgument(
            flag="slicer",
            got=slicer,
            accepted=sorted(slicers.SLICERS),
            command="profiles",
        )
    if kind not in slicers.KINDS:
        raise InvalidArgument(
            flag="kind", got=kind, accepted=list(slicers.KINDS), command="profiles"
        )
    root = (profiles_dir or slicers.profiles_root()) / slicer / kind
    for path in (root / f"{name}.json", root / "base" / f"{name}.json"):
        if path.is_file():
            return path
    raise InputNotFound(str(root / f"{name}.json"), command="profiles")


def apply_values(
    slicer: str,
    kind: str,
    name: str,
    values: Mapping[str, Value],
    *,
    profiles_dir: Path | None = None,
    dry_run: bool = False,
) -> str:
    """Set `values` in the repo preset `<slicer>/<kind>/<name>`; returns the unified diff
    ("" when nothing changed). The file keeps the slicer's indent and key order."""
    bad = sorted(set(values) & (preset.LOCAL_KEYS | FIXED_KEYS))
    if bad:
        raise InvalidArgument(
            flag="key",
            got=", ".join(bad),
            accepted=["any option except the preset identity and connection keys"],
            command="profiles",
            extra="name/from/inherits/version identify the preset; print_host and printhost_* stay in the slicer",
        )
    path = preset_path(slicer, kind, name, profiles_dir=profiles_dir)
    raw = path.read_bytes()
    obj = preset.load(raw, where=str(path))
    updates = {key: _stored(key, value, obj.get(key)) for key, value in values.items()}
    if all(key in obj and obj[key] == stored for key, stored in updates.items()):
        return ""
    new = preset.dump(preset.with_keys(obj, updates), preset.indent_of(raw))
    rel = path.relative_to(
        (profiles_dir or slicers.profiles_root()) / slicer
    ).as_posix()
    diff = "".join(
        difflib.unified_diff(
            raw.decode("utf-8").splitlines(keepends=True),
            new.decode("utf-8").splitlines(keepends=True),
            f"a/{slicer}/{rel}",
            f"b/{slicer}/{rel}",
        )
    )
    if not dry_run:
        tmp = path.with_name(f".{path.name}.3d-tmp")
        tmp.write_bytes(new)
        tmp.replace(path)
    return diff
