"""spec.py — the declarative assembly spec (`3d.assembly/1`): parts, mates, loads, thresholds.

WHAT: one YAML (or JSON) file next to `3d.yaml`, or embedded in a positioned 3MF, that says WHAT the assembly is:

    schema: 3d.assembly/1
    name: mole
    root: .                          # base dir of relative mesh paths (default: the spec's dir); $VAR allowed
    context: {printer: k1se, material: Rapid PLA+, nozzle: 0.4, layer: 0.2}   # for fits.resolve
    settings: {eps_mm3: 0.01, seat_tol_mm: 0.02, step_mm: 0.1, finger_pad_mm: 12, finger_nail_mm: 6}
    parts:
      back:  {role: printed, mesh: print/back.stl, pose: {translate: [0, 0, -10]},
              print: {face: -Z, settings: {layer_height: 0.2}}}
      board: {role: stand_in, mesh: vitamins/board.stl}
    mates:
      - name: lid
        a: back
        b: front
        kind: snap                    # snap | sliding | tight | press | glue | bolt
        fit: snap                     # a fit type of lib/data/fits.yaml (default: from kind); or explicit numbers
        insertion: {part: front, axis: -Z, distance_mm: 8}   # the way `front` moves to ASSEMBLE
        zone: {center: [0, 20, 5], size: [30, 40, 10]}       # where this mate's interference/clearance is judged
        allow: {max_depth_mm: 0.15, max_volume_mm3: 4}       # extra cap for designed interference
        snap: {part: back, flex_limit_mm: 0.3, hooks: [{name: h1, point: [11.5, 16, 7], deflect: -X, ...}]}
    loads:
      - {name: cable_tug, part: front, point: [0, 40, 3], force_n: 10, direction: +Y}

A pose is a mapping `{translate: [x,y,z], rotate: [rx,ry,rz], matrix: [[4x4]]}` or a list of them applied in order;
inside one mapping `rotate` (degrees, OpenSCAD `rotate([rx,ry,rz])` order) is applied before `translate`.

INVARIANTS:
  - Validation happens here, before any geometry is loaded: every error names its YAML path
    (`mates[1].insertion.axis`) and the accepted values (`errors.InvalidArgument`, exit 2).
  - A part's `role` and `reference_source` go through `lib/parts.py` (`Part`): a vitamin without a source URL +
    licence is refused, `printable` is derived, never stored.
  - Stdlib only (YAML is imported lazily): this module is reachable from the command module's `run()`.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assembly.vec import Vec3, parse_direction, parse_point
from errors import InputNotFound, InvalidArgument
from parts import ROLES, Part, ReferenceSource

SCHEMA = "3d.assembly/1"
# Depth/deflection comparisons are good to the float32 precision of the meshes (~1e-6 mm at part coordinates), so a
# designed 0.15 mm interference measured as 0.1500004 is not "over the 0.15 limit".
DEPTH_EPS_MM = 0.001
CMD = "assembly"
KINDS = ("snap", "sliding", "tight", "press", "glue", "bolt")
# mate kind -> default fit type of the shared table (`bolt` is a clearance hole: sliding clearance)
KIND_DEFAULT_FIT = {
    "snap": "snap",
    "sliding": "sliding",
    "tight": "tight",
    "press": "press",
    "glue": "glue",
    "bolt": "sliding",
}
PROBES = ("finger", "nail", "tool")
PURPOSES = ("release", "press", "pull", "pry", "drive")
RELEASES = ("push", "pull")
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

Pose = tuple[tuple[str, tuple[float, ...]], ...]


# --------------------------------------------------------------------------- value objects
@dataclass(frozen=True)
class Zone:
    """A region of the assembly frame: an axis-aligned box, or a sphere (`center` + `radius`)."""

    lo: Vec3
    hi: Vec3
    sphere: bool = False
    center: Vec3 | None = None
    radius: float | None = None

    def bbox(self, margin: float = 0.0) -> tuple[Vec3, Vec3]:
        lo, hi = self.lo, self.hi
        return (
            (lo[0] - margin, lo[1] - margin, lo[2] - margin),
            (hi[0] + margin, hi[1] + margin, hi[2] + margin),
        )

    def contains(self, point: Sequence[float], margin: float = 0.0) -> bool:
        if self.sphere and self.center is not None and self.radius is not None:
            return math.dist(point, self.center) <= self.radius + margin
        lo, hi = self.bbox(margin)
        return all(lo[i] <= point[i] <= hi[i] for i in range(3))

    def to_dict(self) -> dict[str, Any]:
        if self.sphere and self.center is not None:
            return {"center": list(self.center), "radius": self.radius}
        return {"min": list(self.lo), "max": list(self.hi)}


@dataclass(frozen=True)
class PrintSpec:
    """How a printed part is laid on the bed (all optional; see `assembly.position`)."""

    face: Vec3 | str | None = (
        None  # direction of the part face that goes DOWN onto the bed, or "auto"
    )
    rotate: Vec3 | None = (
        None  # explicit rotation (deg) applied to the assembled mesh before laying flat
    )
    supports: bool | None = None
    settings: Mapping[str, Any] = field(
        default_factory=dict
    )  # slicer keys carried to the project (layer_height, ...)

    def to_dict(self) -> dict[str, Any]:
        return {
            "face": list(self.face) if isinstance(self.face, tuple) else self.face,
            "rotate": list(self.rotate) if self.rotate else None,
            "supports": self.supports,
            "settings": dict(self.settings),
        }


@dataclass(frozen=True)
class PartSpec:
    name: str
    part: Part  # role, printable (derived), reference_source: the shared contract of lib/parts.py
    mesh: str  # path as written (relative to the spec `root`) or `3mf:<object name>`
    pose: Pose = ()
    print: PrintSpec = field(default_factory=PrintSpec)

    @property
    def role(self) -> str:
        return self.part.role

    @property
    def printable(self) -> bool:
        return self.part.printable


@dataclass(frozen=True)
class Insertion:
    """The straight-line path along which `part` moves to ASSEMBLE; the distance is the approach length."""

    part: str
    axis: Vec3
    distance_mm: float
    carry: tuple[str, ...] = ()  # parts that travel rigidly with `part` (a board that sits in the moving half)
    ignore: tuple[str, ...] = ()  # parts not in the way on THIS path (assembled later / already removed)


@dataclass(frozen=True)
class Hinge:
    """A revolute motion of `part` about an axis through `origin`; `range_deg` is the sweep (#61)."""

    part: str
    axis: Vec3
    origin: Vec3
    range_deg: tuple[float, float]
    carry: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Access:
    """Where a finger/tool must get to: `target`, approached from the side `from_dir` (unit, pointing out)."""

    name: str
    target: Vec3 | None  # None = take it from the hook the access belongs to
    from_dir: Vec3 | None  # None = try the axes and the hook's push side
    probe: str = "finger"
    radius_mm: float | None = None  # overrides the probe's default radius
    reach_mm: float | None = None
    purpose: str = "release"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": list(self.target) if self.target else None,
            "from": list(self.from_dir) if self.from_dir else None,
            "probe": self.probe,
            "radius_mm": self.radius_mm,
            "reach_mm": self.reach_mm,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class Hook:
    """One snap hook: where it bites, which way its tip moves to release, which geometry flexes."""

    name: str
    point: Vec3
    deflect: Vec3 | None = (
        None  # unit direction the barb tip moves while it flexes clear
    )
    region: Zone | None = (
        None  # the flexing arm: the part of the carrier mesh inside this zone
    )
    barb_mm: float | None = None
    release: str = "push"  # push: a finger/tool presses the hook to release; pull: the halves are pulled apart
    arm_thickness_mm: float | None = None
    arm_length_mm: float | None = None
    arm_width_mm: float | None = None
    access: tuple[Access, ...] = ()


@dataclass(frozen=True)
class SnapSpec:
    part: str  # the part that carries the hooks
    hooks: tuple[Hook, ...]
    flex_limit_mm: float | None = (
        None  # max tip deflection the arm survives (declared, or from the S4 hook formulas)
    )
    strain_limit: float | None = (
        None  # fraction (0.01 = 1 %): input of the S4 closed-form flex limit
    )
    material: str | None = None
    lead_angle_deg: float | None = None
    retention_angle_deg: float | None = None
    friction: float = 0.4
    modulus_mpa: float | None = None  # Young's modulus along the arm (S4 hook formulas); None = no force estimate


@dataclass(frozen=True)
class Allow:
    """Caps on a mate's DESIGNED interference (press ribs, snap barbs); beyond them an overlap is a defect."""

    max_depth_mm: float | None = None
    max_volume_mm3: float | None = None
    note: str = ""


@dataclass(frozen=True)
class MateSpec:
    name: str
    a: str
    b: str
    kind: str
    fit: str  # fit type of the shared table
    shape: str = "planar"
    clearance_mm: float | None = (
        None  # explicit per-face clearance: replaces the table (provenance says so)
    )
    tol_mm: float | None = None
    interference_mm: float | None = None
    insertion: Insertion | None = None
    hinge: Hinge | None = None
    zone: Zone | None = None
    allow: Allow = field(default_factory=Allow)
    snap: SnapSpec | None = None
    access: tuple[Access, ...] = ()
    seat: bool = False  # the two parts touch BY DESIGN (a seam, a floor): zero gap is not a clearance defect
    note: str = ""

    @property
    def pair(self) -> frozenset[str]:
        return frozenset((self.a, self.b))


@dataclass(frozen=True)
class LoadSpec:
    name: str
    part: str
    point: Vec3
    force_n: float
    direction: Vec3
    note: str = ""


@dataclass(frozen=True)
class Settings:
    eps_mm3: float = 0.01  # overlap smaller than this volume is numerical noise
    seat_tol_mm: float = 0.02  # overlap thinner than this is a seated contact, not a collision (#140: 6 um passes)
    step_mm: float = 0.1  # sweep sampling step along a path
    bisect_mm: float = 0.005  # first-contact refinement
    finger_pad_mm: float = 12.0  # diameter of the finger pad probe
    finger_nail_mm: float = 6.0  # diameter of the nail / fingertip probe
    tool_mm: float = 3.0  # diameter of a driver / prying tool
    pry_blade_mm: float = 0.8  # thickness of the blade a prying slot must admit
    reach_mm: float = 40.0  # how far outside the target a probe is tried
    max_faces: int = (
        400_000  # per-mesh face cap: a bigger mesh is refused, not silently decimated
    )
    pair_pad_mm: float = 1.0  # crop margin around a pair's overlap box

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class Context:
    printer: str | None = None
    material: str | None = None
    nozzle: float | None = None
    layer: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "material": self.material,
            "nozzle": self.nozzle,
            "layer": self.layer,
        }


@dataclass(frozen=True)
class AssemblySpec:
    name: str
    path: Path | None  # the spec file (None: built in memory / read from a 3MF)
    root: Path  # base for relative mesh paths
    parts: Mapping[str, PartSpec]
    mates: tuple[MateSpec, ...]
    loads: tuple[LoadSpec, ...]
    settings: Settings
    context: Context
    raw: Mapping[
        str, Any
    ]  # the document as read (embedded verbatim in a positioned 3MF)
    groups: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )  # rigid sub-assemblies the order planner may move as one
    base: str | None = None  # the part the order planner keeps fixed (default: the biggest printed part)

    def mate(self, name: str) -> MateSpec:
        for mate in self.mates:
            if mate.name == name:
                return mate
        raise InvalidArgument(
            "--mate",
            name,
            [m.name for m in self.mates] or ["(no mates declared)"],
            command=CMD,
        )

    def part_list(self) -> list[Part]:
        return [p.part for p in self.parts.values()]

    def mates_of(self, a: str, b: str) -> list[MateSpec]:
        return [m for m in self.mates if m.pair == frozenset((a, b))]


# --------------------------------------------------------------------------- parsing helpers
def _bad(
    where: str, got: Any, accepted: Sequence[str], extra: str | None = None
) -> InvalidArgument:
    return InvalidArgument(
        where,
        repr(got) if not isinstance(got, str) else got,
        list(accepted),
        command=CMD,
        extra=extra,
    )


def _mapping(
    value: Any, where: str, allowed: Sequence[str] | None = None
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _bad(where, value, ["a mapping"])
    if allowed is not None:
        unknown = [k for k in value if k not in allowed]
        if unknown:
            raise _bad(f"{where}.{unknown[0]}", "unknown key", list(allowed))
    return value


def _number(
    value: Any, where: str, *, minimum: float | None = None, positive: bool = False
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise _bad(where, value, ["a finite number"])
    out = float(value)
    if positive and out <= 0:
        raise _bad(where, value, ["a number > 0"])
    if minimum is not None and out < minimum:
        raise _bad(where, value, [f"a number >= {minimum:g}"])
    return out


def _opt_number(
    raw: Mapping[str, Any], key: str, where: str, **kw: Any
) -> float | None:
    return None if raw.get(key) is None else _number(raw[key], f"{where}.{key}", **kw)


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not IDENT.match(value):
        raise _bad(
            where, value, ["a name of letters, digits, `_`, `-`, `.` (no spaces)"]
        )
    return value


def parse_pose(value: Any, where: str) -> Pose:
    """A pose mapping or a list of them -> ordered ops `(kind, numbers)`; `rotate` goes before `translate`."""
    if value is None:
        return ()
    items = value if isinstance(value, list) else [value]
    ops: list[tuple[str, tuple[float, ...]]] = []
    for i, item in enumerate(items):
        w = f"{where}[{i}]" if isinstance(value, list) else where
        block = _mapping(item, w, ("translate", "rotate", "matrix", "scale"))
        if "matrix" in block:
            if len(block) > 1:
                raise _bad(
                    w, "matrix with other keys", ["matrix alone", "translate/rotate"]
                )
            rows = block["matrix"]
            flat = (
                [x for r in rows for x in r]
                if rows and isinstance(rows[0], list)
                else list(rows)
            )
            if len(flat) not in (12, 16):
                raise _bad(
                    f"{w}.matrix", len(flat), ["4x4 (16 numbers)", "3x4 (12 numbers)"]
                )
            nums = tuple(_number(x, f"{w}.matrix") for x in flat)
            ops.append(
                ("matrix", nums if len(nums) == 16 else (*nums, 0.0, 0.0, 0.0, 1.0))
            )
            continue
        if "scale" in block:
            scale = block["scale"]
            vec = (
                [scale] * 3
                if isinstance(scale, (int, float)) and not isinstance(scale, bool)
                else list(scale)
            )
            ops.append(("scale", parse_point(vec, f"{w}.scale")))
        if "rotate" in block:
            ops.append(("rotate", parse_point(block["rotate"], f"{w}.rotate")))
        if "translate" in block:
            ops.append(("translate", parse_point(block["translate"], f"{w}.translate")))
    return tuple(ops)


def parse_zone(value: Any, where: str) -> Zone:
    block = _mapping(value, where, ("min", "max", "center", "size", "radius"))
    if "radius" in block:
        if "center" not in block:
            raise _bad(f"{where}.center", "missing", ["[x, y, z]"])
        c = parse_point(block["center"], f"{where}.center")
        r = _number(block["radius"], f"{where}.radius", positive=True)
        return Zone(
            (c[0] - r, c[1] - r, c[2] - r), (c[0] + r, c[1] + r, c[2] + r), True, c, r
        )
    if "min" in block and "max" in block:
        lo, hi = (
            parse_point(block["min"], f"{where}.min"),
            parse_point(block["max"], f"{where}.max"),
        )
    elif "center" in block and "size" in block:
        c, s = (
            parse_point(block["center"], f"{where}.center"),
            parse_point(block["size"], f"{where}.size"),
        )
        lo = (c[0] - s[0] / 2, c[1] - s[1] / 2, c[2] - s[2] / 2)
        hi = (c[0] + s[0] / 2, c[1] + s[1] / 2, c[2] + s[2] / 2)
    else:
        raise _bad(
            where, "incomplete", ["{min, max}", "{center, size}", "{center, radius}"]
        )
    if any(h < lo_ for lo_, h in zip(lo, hi, strict=True)):
        raise _bad(where, "max below min", ["min <= max on every axis"])
    return Zone(lo, hi)


def _parse_print(value: Any, where: str) -> PrintSpec:
    if value is None:
        return PrintSpec()
    block = _mapping(value, where, ("face", "rotate", "supports", "settings"))
    face: Vec3 | str | None = None
    if block.get("face") is not None:
        raw = block["face"]
        face = (
            "auto"
            if isinstance(raw, str) and raw.lower() == "auto"
            else parse_direction(raw, f"{where}.face")
        )
    rotate = (
        parse_point(block["rotate"], f"{where}.rotate")
        if block.get("rotate") is not None
        else None
    )
    if face is not None and rotate is not None:
        raise _bad(where, "face and rotate", ["face OR rotate"])
    supports = block.get("supports")
    if supports is not None and not isinstance(supports, bool):
        raise _bad(f"{where}.supports", supports, ["true", "false"])
    settings = _mapping(block.get("settings", {}), f"{where}.settings")
    return PrintSpec(face, rotate, supports, dict(settings))


def _parse_part(name: str, value: Any) -> PartSpec:
    where = f"parts.{name}"
    block = _mapping(
        value, where, ("role", "mesh", "pose", "reference_source", "print")
    )
    role = str(block.get("role", "printed"))
    if role not in ROLES:
        raise _bad(f"{where}.role", role, list(ROLES))
    mesh = block.get("mesh")
    if not isinstance(mesh, str) or not mesh:
        raise _bad(f"{where}.mesh", mesh, ["a mesh path (.stl/.3mf/.obj/.ply)"])
    source = block.get("reference_source")
    ref: ReferenceSource | None = None
    if source is not None:
        src = _mapping(source, f"{where}.reference_source", ("url", "license", "note"))
        ref = ReferenceSource(
            str(src.get("url", "")),
            str(src.get("license", "")),
            str(src.get("note", "")),
        )
    part = Part(
        name, role, ref
    )  # vitamin/reference without {url, license}: InvalidArgument from lib/parts.py
    return PartSpec(
        name,
        part,
        mesh,
        parse_pose(block.get("pose"), f"{where}.pose"),
        _parse_print(block.get("print"), f"{where}.print"),
    )


def _parse_access(value: Any, where: str) -> tuple[Access, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _bad(where, value, ["a list of access entries"])
    out: list[Access] = []
    for i, item in enumerate(value):
        w = f"{where}[{i}]"
        block = _mapping(
            item,
            w,
            ("name", "target", "from", "probe", "radius_mm", "reach_mm", "purpose"),
        )
        probe = str(block.get("probe", "finger"))
        if probe not in PROBES:
            raise _bad(f"{w}.probe", probe, list(PROBES))
        purpose = str(block.get("purpose", "release"))
        if purpose not in PURPOSES:
            raise _bad(f"{w}.purpose", purpose, list(PURPOSES))
        out.append(
            Access(
                _name(block.get("name", f"access{i + 1}"), f"{w}.name"),
                parse_point(block["target"], f"{w}.target")
                if block.get("target") is not None
                else None,
                parse_direction(block["from"], f"{w}.from")
                if block.get("from") is not None
                else None,
                probe,
                _opt_number(block, "radius_mm", w, positive=True),
                _opt_number(block, "reach_mm", w, positive=True),
                purpose,
            )
        )
    return tuple(out)


def _parse_hooks(value: Any, where: str) -> tuple[Hook, ...]:
    if not isinstance(value, list) or not value:
        raise _bad(where, value, ["a non-empty list of hooks"])
    hooks: list[Hook] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        w = f"{where}[{i}]"
        block = _mapping(
            item,
            w,
            (
                "name",
                "point",
                "deflect",
                "region",
                "barb_mm",
                "arm",
                "access",
                "release",
            ),
        )
        release = str(block.get("release", "push"))
        if release not in RELEASES:
            raise _bad(f"{w}.release", release, list(RELEASES))
        name = _name(block.get("name", f"hook{i + 1}"), f"{w}.name")
        if name in seen:
            raise _bad(f"{w}.name", name, ["a unique hook name"])
        seen.add(name)
        arm = _mapping(
            block.get("arm", {}), f"{w}.arm", ("thickness_mm", "length_mm", "width_mm")
        )
        hooks.append(
            Hook(
                name,
                parse_point(block.get("point"), f"{w}.point"),
                parse_direction(block["deflect"], f"{w}.deflect")
                if block.get("deflect") is not None
                else None,
                parse_zone(block["region"], f"{w}.region")
                if block.get("region") is not None
                else None,
                _opt_number(block, "barb_mm", w, positive=True),
                release,
                _opt_number(arm, "thickness_mm", f"{w}.arm", positive=True),
                _opt_number(arm, "length_mm", f"{w}.arm", positive=True),
                _opt_number(arm, "width_mm", f"{w}.arm", positive=True),
                _parse_access(block.get("access"), f"{w}.access"),
            )
        )
    return tuple(hooks)


def _parse_snap(value: Any, where: str) -> SnapSpec:
    block = _mapping(
        value,
        where,
        (
            "part",
            "hooks",
            "flex_limit_mm",
            "strain_limit",
            "material",
            "lead_angle_deg",
            "retention_angle_deg",
            "friction",
            "modulus_mpa",
        ),
    )
    return SnapSpec(
        _name(block.get("part"), f"{where}.part"),
        _parse_hooks(block.get("hooks"), f"{where}.hooks"),
        _opt_number(block, "flex_limit_mm", where, positive=True),
        _opt_number(block, "strain_limit", where, positive=True),
        None if block.get("material") is None else str(block["material"]),
        _opt_number(block, "lead_angle_deg", where, positive=True),
        _opt_number(block, "retention_angle_deg", where, positive=True),
        _number(block.get("friction", 0.4), f"{where}.friction", minimum=0.0),
        _opt_number(block, "modulus_mpa", where, positive=True),
    )


def _parse_carry(
    value: Any, where: str, mover: str, parts: Mapping[str, PartSpec]
) -> tuple[str, ...]:
    """Names of parts that travel rigidly with the mover."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _bad(where, value, ["a list of part names"])
    for name in value:
        if name not in parts or name == mover:
            raise _bad(
                where,
                name,
                sorted(set(parts) - {mover}),
                "carry lists other declared parts.",
            )
    return tuple(str(n) for n in value)


def _parse_mate(index: int, value: Any, parts: Mapping[str, PartSpec]) -> MateSpec:
    where = f"mates[{index}]"
    block = _mapping(
        value,
        where,
        (
            "name",
            "a",
            "b",
            "kind",
            "fit",
            "fit_type",
            "shape",
            "clearance_mm",
            "tol_mm",
            "interference_mm",
            "insertion",
            "hinge",
            "zone",
            "allow",
            "snap",
            "access",
            "seat",
            "note",
        ),  # fmt: skip
    )
    name = _name(block.get("name", f"mate{index + 1}"), f"{where}.name")
    ends: list[str] = []
    for key in ("a", "b"):
        part = block.get(key)
        if part not in parts:
            raise _bad(f"{where}.{key}", part, sorted(parts) or ["(no parts declared)"])
        ends.append(str(part))
    if ends[0] == ends[1]:
        raise _bad(f"{where}.b", ends[1], ["a part different from `a`"])
    fit_given = block.get("fit", block.get("fit_type"))
    kind = block.get("kind")
    if kind is None:
        kind = str(fit_given) if str(fit_given) in KINDS else "sliding"
    if kind not in KINDS:
        raise _bad(f"{where}.kind", kind, list(KINDS))
    fit = str(fit_given) if fit_given is not None else KIND_DEFAULT_FIT[str(kind)]
    insertion: Insertion | None = None
    if block.get("insertion") is not None:
        ins = _mapping(
            block["insertion"],
            f"{where}.insertion",
            ("part", "axis", "distance_mm", "carry", "ignore"),
        )
        mover = str(ins.get("part", ends[1]))
        if mover not in ends:
            raise _bad(
                f"{where}.insertion.part",
                mover,
                ends,
                "The moving part must be one of the mate's two parts.",
            )
        insertion = Insertion(
            mover,
            parse_direction(ins.get("axis"), f"{where}.insertion.axis"),
            _number(
                ins.get("distance_mm", 10.0),
                f"{where}.insertion.distance_mm",
                positive=True,
            ),
            _parse_carry(ins.get("carry"), f"{where}.insertion.carry", mover, parts),
            _parse_carry(ins.get("ignore"), f"{where}.insertion.ignore", mover, parts),
        )
    hinge: Hinge | None = None
    if block.get("hinge") is not None:
        if insertion is not None:
            raise _bad(f"{where}.hinge", "insertion and hinge", ["insertion OR hinge"])
        hg = _mapping(
            block["hinge"],
            f"{where}.hinge",
            ("part", "axis", "origin", "range_deg", "carry", "ignore"),
        )
        mover = str(hg.get("part", ends[1]))
        if mover not in ends:
            raise _bad(f"{where}.hinge.part", mover, ends)
        rng = hg.get("range_deg", [0.0, 90.0])
        if not isinstance(rng, list) or len(rng) != 2:
            raise _bad(f"{where}.hinge.range_deg", rng, ["[min, max] degrees"])
        lo, hi = (
            _number(rng[0], f"{where}.hinge.range_deg"),
            _number(rng[1], f"{where}.hinge.range_deg"),
        )
        if hi <= lo:
            raise _bad(f"{where}.hinge.range_deg", rng, ["max > min"])
        hinge = Hinge(
            mover,
            parse_direction(hg.get("axis"), f"{where}.hinge.axis"),
            parse_point(hg.get("origin", [0, 0, 0]), f"{where}.hinge.origin"),
            (lo, hi),
            _parse_carry(hg.get("carry"), f"{where}.hinge.carry", mover, parts),
            _parse_carry(hg.get("ignore"), f"{where}.hinge.ignore", mover, parts),
        )
    allow = Allow()
    if block.get("allow") is not None:
        al = _mapping(
            block["allow"], f"{where}.allow", ("max_depth_mm", "max_volume_mm3", "note")
        )
        allow = Allow(
            _opt_number(al, "max_depth_mm", f"{where}.allow", minimum=0.0),
            _opt_number(al, "max_volume_mm3", f"{where}.allow", minimum=0.0),
            str(al.get("note", "")),
        )
    snap = (
        _parse_snap(block["snap"], f"{where}.snap")
        if block.get("snap") is not None
        else None
    )
    if snap is not None and snap.part not in ends:
        raise _bad(
            f"{where}.snap.part",
            snap.part,
            ends,
            "The hook carrier must be one of the mate's two parts.",
        )
    if block.get("seat") is not None and not isinstance(block["seat"], bool):
        raise _bad(f"{where}.seat", block["seat"], ["true", "false"])
    return MateSpec(
        name=name,
        a=ends[0],
        b=ends[1],
        kind=str(kind),
        fit=fit,
        shape=str(block.get("shape", "planar")),
        clearance_mm=_opt_number(block, "clearance_mm", where),
        tol_mm=_opt_number(block, "tol_mm", where, minimum=0.0),
        interference_mm=_opt_number(block, "interference_mm", where, minimum=0.0),
        insertion=insertion,
        hinge=hinge,
        zone=parse_zone(block["zone"], f"{where}.zone")
        if block.get("zone") is not None
        else None,
        allow=allow,
        snap=snap,
        access=_parse_access(block.get("access"), f"{where}.access"),
        seat=bool(block.get("seat", False)),
        note=str(block.get("note", "")),
    )


def _parse_loads(value: Any, parts: Mapping[str, PartSpec]) -> tuple[LoadSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _bad("loads", value, ["a list of loads"])
    out: list[LoadSpec] = []
    for i, item in enumerate(value):
        where = f"loads[{i}]"
        block = _mapping(
            item, where, ("name", "part", "point", "force_n", "direction", "note")
        )
        part = block.get("part")
        if part not in parts:
            raise _bad(f"{where}.part", part, sorted(parts) or ["(no parts declared)"])
        out.append(
            LoadSpec(
                _name(block.get("name", f"load{i + 1}"), f"{where}.name"),
                str(part),
                parse_point(block.get("point"), f"{where}.point"),
                _number(block.get("force_n"), f"{where}.force_n", positive=True),
                parse_direction(block.get("direction"), f"{where}.direction"),
                str(block.get("note", "")),
            )
        )
    return tuple(out)


def _parse_settings(value: Any) -> Settings:
    if value is None:
        return Settings()
    block = _mapping(value, "settings", tuple(Settings.__dataclass_fields__))
    kwargs: dict[str, Any] = {}
    for key, raw in block.items():
        kwargs[key] = (
            int(_number(raw, f"settings.{key}", positive=True))
            if key == "max_faces"
            else _number(raw, f"settings.{key}", positive=True)
        )
    return Settings(**kwargs)


def _parse_context(value: Any) -> Context:
    if value is None:
        return Context()
    block = _mapping(value, "context", ("printer", "material", "nozzle", "layer"))
    return Context(
        None if block.get("printer") is None else str(block["printer"]),
        None if block.get("material") is None else str(block["material"]),
        _opt_number(block, "nozzle", "context", positive=True),
        _opt_number(block, "layer", "context", positive=True),
    )


def expand_vars(text: str, where: str) -> str:
    """`~` and `$VAR` / `${VAR}` in a path; an unset variable is an error, never a silent empty string."""
    missing = [
        m
        for m in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text)
        if m not in os.environ
    ]
    if missing:
        raise _bad(
            where,
            text,
            [f"set the environment variable {missing[0]}"],
            "The spec path uses a variable that is not set.",
        )
    return os.path.expanduser(os.path.expandvars(text))


def _parse_groups(
    value: Any, parts: Mapping[str, PartSpec]
) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    block = _mapping(value, "groups")
    out: dict[str, tuple[str, ...]] = {}
    for name, members in block.items():
        if not isinstance(members, list) or len(members) < 2:
            raise _bad(f"groups.{name}", members, ["a list of at least 2 part names"])
        for member in members:
            if member not in parts:
                raise _bad(f"groups.{name}", member, sorted(parts))
        out[_name(name, "groups.<name>")] = tuple(str(m) for m in members)
    return out


def parse_spec(
    doc: Any, *, path: Path | None = None, base_dir: Path | None = None
) -> AssemblySpec:
    """Validate a spec document (already loaded from YAML/JSON) and build the typed `AssemblySpec`."""
    top = _mapping(
        doc,
        "assembly",
        (
            "schema", "name", "root", "context", "settings", "parts", "mates", "loads",
            "groups", "base",
        ),
    )
    schema = top.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise _bad("schema", schema, [SCHEMA])
    raw_parts = _mapping(top.get("parts"), "parts")
    if not raw_parts:
        raise _bad("parts", "empty", ["at least one part"])
    parts = {
        _name(n, "parts.<name>"): _parse_part(str(n), v) for n, v in raw_parts.items()
    }
    mates = tuple(
        _parse_mate(i, m, parts) for i, m in enumerate(top.get("mates") or [])
    )
    names = [m.name for m in mates]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise _bad("mates[].name", sorted(dup)[0], ["unique mate names"])
    for mate in mates:
        if mate.snap is not None:
            hook_names = [h.name for h in mate.snap.hooks]
            if len(set(hook_names)) != len(hook_names):
                raise _bad(
                    f"mates.{mate.name}.snap.hooks",
                    "duplicate names",
                    ["unique hook names"],
                )
    groups = _parse_groups(top.get("groups"), parts)
    base_part = top.get("base")
    if base_part is not None and base_part not in parts:
        raise _bad("base", base_part, sorted(parts))
    base = (
        base_dir
        if base_dir is not None
        else (path.parent if path is not None else Path.cwd())
    )
    root_raw = top.get("root")
    root = (
        (base / expand_vars(str(root_raw), "root")).resolve()
        if root_raw
        else base.resolve()
    )
    return AssemblySpec(
        name=str(top.get("name") or (path.stem if path else "assembly")),
        path=path,
        root=root,
        parts=parts,
        mates=mates,
        loads=_parse_loads(top.get("loads"), parts),
        settings=_parse_settings(top.get("settings")),
        context=_parse_context(top.get("context")),
        raw=dict(top),
        groups=groups,
        base=None if base_part is None else str(base_part),
    )


def load_document(path: Path) -> Any:
    """Read the spec file: JSON by extension, YAML otherwise (pyyaml is imported lazily)."""
    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise _bad(
                str(path), f"invalid JSON: {exc}", ["a valid assembly spec"]
            ) from exc
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _bad(
            str(path), f"invalid YAML: {exc}", ["a valid assembly spec"]
        ) from exc


SPEC_NAMES = ("assembly.yaml", "assembly.yml", "assembly.json")


def find_spec(target: Path) -> Path:
    """A spec file, or a project directory holding `assembly.yaml` (next to its `3d.yaml`)."""
    if target.is_dir():
        for name in SPEC_NAMES:
            if (target / name).is_file():
                return target / name
        raise InputNotFound(str(target / SPEC_NAMES[0]), command=CMD)
    return target


def load_spec(target: str | os.PathLike[str]) -> AssemblySpec:
    path = find_spec(Path(target))
    return parse_spec(load_document(path), path=path.resolve())


def mesh_path(spec: AssemblySpec, part: PartSpec) -> Path:
    """Absolute path of a part's mesh file (`$VAR` and `~` expanded, relative to the spec root)."""
    text = expand_vars(part.mesh, f"parts.{part.name}.mesh")
    p = Path(text)
    return p if p.is_absolute() else (spec.root / p).resolve()
