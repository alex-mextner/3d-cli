"""kinematics.py - validate project joint specs and emit deterministic summaries.

ACCESSED VIA: `3d kinematics <3d.yaml>`. This is the headless core for the first
kinematics roadmap slice: it validates a small, explicit joint schema under
`kinematics.joints` in `3d.yaml` and returns a stable JSON-ready summary. It does not run
simulation or geometry checks yet.

Supported joint schema:

    kinematics:
      joints:
        shoulder:
          type: revolute       # revolute | prismatic | fixed
          parent: base         # must name a part in parts:
          child: arm           # must name a part in parts:
          axis: [0, 0, 1]      # required for revolute/prismatic; normalized in summary
          origin: [0, 0, 0]    # optional, default origin
          limits: [-90, 90]    # required for revolute/prismatic; deg or mm by type

INVARIANTS:
  - No CLI printing or argv parsing here.
  - YAML loading stays delegated to project.load_project(), which lazy-imports pyyaml.
  - Output order is deterministic: parts and joints are sorted by name, JSON keys sort.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

from errors import UsageError
from project import Project, load_project

JOINT_TYPES = ("fixed", "prismatic", "revolute")
MOVING_JOINT_TYPES = ("prismatic", "revolute")


@dataclass(frozen=True, slots=True)
class Joint:
    """One validated joint from `kinematics.joints`."""

    name: str
    type: str
    parent: str
    child: str
    axis: list[float]
    origin: list[float]
    limit_min: float
    limit_max: float
    limit_units: str

    def as_summary(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "child": self.child,
            "limits": {"max": self.limit_max, "min": self.limit_min, "units": self.limit_units},
            "name": self.name,
            "origin": self.origin,
            "parent": self.parent,
            "type": self.type,
        }


def _kin_error(message: str, remediation: list[str] | None = None) -> UsageError:
    return UsageError(message, command="kinematics", remediation=remediation)


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _kin_error(
            f"{label} must be a mapping, got {type(value).__name__}",
            [f"Write `{label}:` as keyed YAML fields, not a scalar or list."],
        )
    return dict(value)


def _joint_items(project: Project) -> list[tuple[str, dict[str, Any]]]:
    kin = project.raw.get("kinematics")
    if kin is None:
        raise _kin_error(
            "missing `kinematics.joints` in 3d.yaml",
            ["Add `kinematics:\n  joints:` with at least one named joint."],
        )
    kin_map = _as_mapping(kin, "kinematics")
    joints = kin_map.get("joints")
    if joints is None:
        raise _kin_error(
            "missing `kinematics.joints` in 3d.yaml",
            ["Add `kinematics:\n  joints:` with at least one named joint."],
        )
    if isinstance(joints, dict):
        return [(str(name), _as_mapping(spec, f"kinematics.joints.{name}")) for name, spec in joints.items()]
    if isinstance(joints, list):
        out: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for idx, raw in enumerate(joints):
            spec = _as_mapping(raw, f"kinematics.joints[{idx}]")
            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                raise _kin_error(
                    f"kinematics.joints[{idx}] is missing a non-empty `name:`",
                    ["Either use a mapping `joints: {name: {...}}` or add `name:` to each list item."],
                )
            if name in seen:
                raise _kin_error(f"duplicate joint name {name!r}", ["Joint names must be unique."])
            seen.add(name)
            out.append((name, spec))
        return out
    raise _kin_error(
        f"kinematics.joints must be a mapping or list, got {type(joints).__name__}",
        ["Use `joints:\n  hinge:\n    type: revolute\n    ...`."],
    )


def _coerce_vector(value: Any, label: str, *, default: list[float] | None = None) -> list[float]:
    if value is None and default is not None:
        return list(default)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _kin_error(f"{label} must be a 3-number vector")
    out: list[float] = []
    for idx, raw in enumerate(value):
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise _kin_error(f"{label}[{idx}] must be numeric, got {raw!r}") from None
        if not math.isfinite(num):
            raise _kin_error(f"{label}[{idx}] must be finite, got {raw!r}")
        out.append(num)
    return out


def _normalize_axis(value: Any, label: str) -> list[float]:
    vec = _coerce_vector(value, label)
    length = math.sqrt(sum(v * v for v in vec))
    if length == 0.0:
        raise _kin_error(f"{label} axis must not be the zero vector")
    return [v / length for v in vec]


def _coerce_limits(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _kin_error(f"{label} limits must be two numbers: [min, max]")
    try:
        lo = float(value[0])
        hi = float(value[1])
    except (TypeError, ValueError):
        raise _kin_error(f"{label} limits must be numeric: [min, max]") from None
    if not math.isfinite(lo) or not math.isfinite(hi):
        raise _kin_error(f"{label} limits must be finite: [min, max]")
    if lo > hi:
        raise _kin_error(f"{label} limits min must be <= max")
    return lo, hi


def _part_ref(spec: dict[str, Any], field: str, joint_name: str, project: Project) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _kin_error(
            f"joint {joint_name!r} is missing `{field}:`",
            [f"Set `{field}: <part-name>` using one of: {', '.join(sorted(project.parts))}."],
        )
    if value not in project.parts:
        raise _kin_error(
            f"joint {joint_name!r}: unknown {field} part {value!r}",
            [f"Accepted part names: {', '.join(sorted(project.parts))}."],
        )
    return value


def _build_joint(name: str, spec: dict[str, Any], project: Project) -> Joint:
    if not name.strip():
        raise _kin_error("joint names must be non-empty")
    typ = spec.get("type")
    if not isinstance(typ, str):
        raise _kin_error(f"joint {name!r} is missing `type:`", [f"Accepted types: {', '.join(JOINT_TYPES)}."])
    if typ not in JOINT_TYPES:
        raise _kin_error(
            f"joint {name!r}: type {typ!r}; accepted: {', '.join(JOINT_TYPES)}",
            [f"Set `type:` to one of {', '.join(JOINT_TYPES)}."],
        )

    parent = _part_ref(spec, "parent", name, project)
    child = _part_ref(spec, "child", name, project)
    if parent == child:
        raise _kin_error(f"joint {name!r}: parent and child must be different parts")

    origin = _coerce_vector(spec.get("origin"), f"joint {name!r} origin", default=[0.0, 0.0, 0.0])
    if typ in MOVING_JOINT_TYPES:
        axis = _normalize_axis(spec.get("axis"), f"joint {name!r}")
        lo, hi = _coerce_limits(spec.get("limits"), f"joint {name!r}")
        units = "deg" if typ == "revolute" else project.units
    else:
        axis = _coerce_vector(spec.get("axis"), f"joint {name!r} axis", default=[0.0, 0.0, 0.0])
        lo, hi = _coerce_limits(spec["limits"], f"joint {name!r}") if "limits" in spec else (0.0, 0.0)
        units = "none"

    return Joint(
        name=name,
        type=typ,
        parent=parent,
        child=child,
        axis=axis,
        origin=origin,
        limit_min=lo,
        limit_max=hi,
        limit_units=units,
    )


def load_joints(path_or_dir: str | os.PathLike[str]) -> tuple[Project, list[Joint]]:
    """Load a project and validate its `kinematics.joints` specs."""
    project = load_project(path_or_dir, command="kinematics", check_files=True)
    joints = [_build_joint(name, spec, project) for name, spec in _joint_items(project)]
    joints.sort(key=lambda joint: joint.name)
    return project, joints


def summarize_project(path_or_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a deterministic, JSON-ready kinematics summary for a project."""
    project, joints = load_joints(path_or_dir)
    return {
        "joint_count": len(joints),
        "joints": [joint.as_summary() for joint in joints],
        "parts": sorted(project.parts),
        "project": project.name,
        "units": project.units,
    }


def summary_json(summary: dict[str, Any]) -> str:
    """Serialize a summary with stable ordering and a trailing newline for CLI output."""
    return json.dumps(summary, allow_nan=False, indent=2, sort_keys=True) + "\n"
