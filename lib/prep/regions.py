"""regions.py — colour regions of a model: named landmark boxes -> disjoint colour parts -> filament / mix per part.

WHAT: a colour spec (YAML/JSON, `load_colour_spec`) names the loaded filaments, a default colour, optional landmark
  boxes (with L/R mirror generation) and an ordered list of regions (landmarks, boxes, spheres, half-spaces or an STL
  volume), each assigned a pure filament or a layer-cycle `mix`. From it:
    `label_faces`   per-face region by the first matching region (face centroid) + per-region face/area/volume stats;
    `split_parts`   the model cut into DISJOINT watertight volumes with manifold3d booleans, the default colour
                    taking whatever no region claimed (-> `prep.contracts.Part`, ready for a project 3MF);
    `check_mixes`   every `mix` assign against the layer rules (no filament on two adjacent layers of the cycle,
                    cyclic; a mixed layer is at most `mix_cap` thick);
    `plan_colours`  the three together as a `StepResult` (`ColourPlan`).
  Ported from Chekhov: u1/build_3mf.py (`_shape_mesh`, `split_regions`, `resolve_assign`), print/landmarks.py (boxes,
  mirror, PRIORITY, per-face labels), u1/colours.py (the no-adjacent-repeat rule, recipe search).
WHY: the Chekhov colour work took hours because regions were Python dicts edited per attempt; as a spec the same
  regions are reviewable, re-runnable and testable, and the colour/layer rules are checked before slicing, not on the
  G-code afterwards.
INVARIANTS:
  * Priority = list order of `regions`: a point belongs to the FIRST region containing it; `default` (part name
    "base") takes the rest. Labels (centroid test) and parts (booleans) use the same order, so they agree up to
    the facets of the cutter (a sphere region is an icosphere of 4 subdivisions in the booleans, exact in labels).
  * Parts are pairwise disjoint and their volumes sum to the model volume (checked, tolerance `VOLUME_TOL`).
  * Landmark boxes are `[x0, x1, y0, y1, z0, z1]` in the print frame (Z up, XY-centred); `mirror: x` takes the box as
    written for one side and generates `NAME_L` (the -X side) and `NAME_R` (+X side): the box whose centre has the
    larger x is `_R`.
  * Parts carry RESOLVED assigns: `Assign(slot=n)` or `Assign(mix="<slot digits>")`; keys are looked up in
    `filaments` only here, so re-loading the spools edits the spec's `filaments`, never the recipes.
  * Spec errors raise `InvalidArgument` with the accepted values; a bad MODEL (not watertight, volume lost) is a FAIL
    verdict of `plan_colours`.
ASSUMPTIONS:
  * The mesh cut is watertight (manifold3d needs a closed mesh): repair first.
  * Volume shares in `label_faces` are measured on a voxel grid of the mesh (`volume_pitch`) and only when the mesh
    is watertight; otherwise they are None.
  * The layer rules come from `slicing.mix_pattern` / `slicing.mix_model` (epic #410) when importable; until #410
    merges the local fallback applies the same default rules (no filament on adjacent layers of the cycle, cyclic;
    mixed layer <= cap) and is replaced by the real engine automatically. The recipe search (`target:`) needs
    `slicing.mix_model` and raises MissingDependency without it.
Past bugs: GWG repeated is G,W,G,G,W,G — a double-thick G band (the cyclic wrap hides it), so the wrap is always
  checked; a one-colour "mix" is rejected (assign the slot directly).
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import numpy as np
import trimesh
from numpy.typing import NDArray

from errors import GateFailure, InputNotFound, InvalidArgument, MissingDependency
from prep.contracts import Assign, Filament, Part, Verdict
from prep.voxel import Grid, voxelize

CMD_NAME = "prep"
#: name of the part that takes whatever no region claimed
DEFAULT_PART = "base"
#: parts must sum to the model volume within this fraction
VOLUME_TOL = 0.005
#: a boolean piece smaller than this (mm^3) is an empty region
MIN_PIECE_MM3 = 1e-3
#: thickness slack when comparing a layer to the cap (layer_heights_profile.txt rounds to 0.0001 mm)
CAP_SLACK_MM = 5e-4
DEFAULT_MIX_CAP_MM = 0.12

Vec3 = tuple[float, float, float]
Box = tuple[float, float, float, float, float, float]
Mask = NDArray[np.bool_]
Points = NDArray[np.float64]


# ---------------------------------------------------------------------------------------------- shapes
@dataclass(frozen=True)
class BoxShape:
    lo: Vec3
    hi: Vec3


@dataclass(frozen=True)
class SphereShape:
    center: Vec3
    radius: float


@dataclass(frozen=True)
class HalfspaceShape:
    """The side the unit `normal` points to: `normal . p >= offset`."""

    normal: Vec3
    offset: float


@dataclass(frozen=True)
class StlShape:
    """A closed STL volume (absolute path, resolved against the spec's directory at load)."""

    path: str


Shape = BoxShape | SphereShape | HalfspaceShape | StlShape


@dataclass(frozen=True)
class RegionSpec:
    name: str
    shapes: tuple[Shape, ...]  # the region is their union
    assign: Assign
    landmarks: tuple[str, ...] = ()  # names the shapes came from, for the report
    target: str | None = None  # '#rrggbb' when the assign came from the recipe search
    target_delta_e: float | None = None


@dataclass(frozen=True)
class ColourSpec:
    filaments: tuple[Filament, ...]
    default: Assign
    regions: tuple[RegionSpec, ...]
    landmarks: Mapping[str, Box]  # after mirror expansion


# ---------------------------------------------------------------------------------------------- loading
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_TOP_KEYS = ("filaments", "default", "landmarks", "regions")
_FILAMENT_KEYS = ("slot", "key", "name", "hex")
_ASSIGN_KEYS = ("slot", "key", "mix")
_REGION_KEYS = ("name", "landmark", "landmarks", "shape", "assign", "target")
_LANDMARK_KEYS = ("box", "mirror")
_SHAPE_TYPES = ("sphere", "box", "halfspace", "stl")


def _bad(flag: str, got: object, accepted: Sequence[str]) -> InvalidArgument:
    return InvalidArgument(
        f"colour spec {flag}", str(got), list(accepted), command=CMD_NAME
    )


def _only_keys(raw: Mapping[str, Any], accepted: Sequence[str], where: str) -> None:
    for k in raw:
        if k not in accepted:
            raise _bad(f"{where}: field", k, accepted)


def _mapping(raw: object, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise _bad(where, type(raw).__name__, ["a mapping"])
    return raw


def _floats(raw: object, n: int, where: str) -> tuple[float, ...]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, str)
        or len(raw) != n
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in raw)
    ):
        raise _bad(where, raw, [f"a list of {n} numbers"])
    return tuple(float(v) for v in raw)


def load_colour_spec(
    path: str | Path,
    *,
    mix_cap: float = DEFAULT_MIX_CAP_MM,
    max_cycle: int = 3,
) -> ColourSpec:
    """Read a YAML/JSON colour spec. `mix_cap` / `max_cycle` only matter for regions given as `target: '#rrggbb'`,
    whose mix the recipe search (`slicing.mix_model.rank`) picks at that layer height / cycle length."""
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(str(p), command=CMD_NAME)
    suffix = p.suffix.lower()
    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
    elif suffix in (".yaml", ".yml"):
        import yaml  # lazy: keeps `import prep.regions` cheap

        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    else:
        raise InvalidArgument(
            "colour spec file",
            suffix or str(p),
            [".yaml", ".yml", ".json"],
            command=CMD_NAME,
        )
    return parse_colour_spec(
        data, base_dir=p.parent, mix_cap=mix_cap, max_cycle=max_cycle
    )


def parse_colour_spec(
    data: object,
    *,
    base_dir: Path | None = None,
    mix_cap: float = DEFAULT_MIX_CAP_MM,
    max_cycle: int = 3,
) -> ColourSpec:
    """Validate a spec mapping (what the file holds) into a `ColourSpec`; every defect raises InvalidArgument."""
    top = _mapping(data, "document")
    _only_keys(top, _TOP_KEYS, "top level")
    for need in ("filaments", "default"):
        if need not in top:
            raise _bad("top level: missing field", need, _TOP_KEYS)
    filaments = _parse_filaments(top["filaments"])
    default = _parse_assign(top["default"], "default", filaments)
    landmarks = _parse_landmarks(top.get("landmarks") or {})
    regions = _parse_regions(
        top.get("regions") or [],
        landmarks,
        filaments,
        base_dir or Path.cwd(),
        mix_cap,
        max_cycle,
    )
    return ColourSpec(filaments, default, regions, landmarks)


def _parse_filament(item: object, where: str) -> Filament:
    f = _mapping(item, where)
    _only_keys(f, _FILAMENT_KEYS, where)
    slot, key, hexc = f.get("slot"), f.get("key"), f.get("hex")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 9:
        raise _bad(f"{where}.slot", slot, ["an integer 1..9 (one pattern digit)"])
    if not isinstance(key, str) or len(key) != 1 or key.isdigit():
        raise _bad(f"{where}.key", key, ["one non-digit character (K, W, R, G ...)"])
    if not isinstance(hexc, str) or not _HEX.match(hexc):
        raise _bad(f"{where}.hex", hexc, ["#RRGGBB"])
    return Filament(slot, key, str(f.get("name", key)), hexc.lower())


def _parse_filaments(raw: object) -> tuple[Filament, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        raise _bad("filaments", raw, ["a non-empty list of {slot, key, name, hex}"])
    out = [_parse_filament(item, f"filaments[{i}]") for i, item in enumerate(raw)]
    for attr in ("slot", "key"):
        seen = [getattr(f, attr) for f in out]
        dup = sorted({str(v) for v in seen if seen.count(v) > 1})
        if dup:
            raise _bad(
                f"filaments: duplicate {attr}", ", ".join(dup), [f"every {attr} once"]
            )
    return tuple(out)


def _assign_of(k: str, value: Any, where: str, filaments: Sequence[Filament]) -> Assign:
    """The `Assign` for the one given field `k` ('slot', 'key' or 'mix') holding `value`."""
    if k == "slot":
        if not isinstance(value, int) or isinstance(value, bool):
            raise _bad(f"{where}.slot", value, ["an integer slot of a loaded filament"])
        return Assign(slot=value)
    if k == "key":
        if not isinstance(value, str):
            raise _bad(f"{where}.key", value, sorted(f.key for f in filaments))
        return Assign(key=value)
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{where}.mix", value, ["one character per layer, e.g. GWG or 424"])
    return Assign(mix=value.strip())


def _parse_assign(raw: object, where: str, filaments: Sequence[Filament]) -> Assign:
    a = _mapping(raw, where)
    _only_keys(a, _ASSIGN_KEYS, where)
    given = [k for k in _ASSIGN_KEYS if a.get(k) is not None]
    if len(given) != 1:
        raise _bad(
            where, ", ".join(given) or "no field", ["exactly one of slot, key, mix"]
        )
    assign = _assign_of(given[0], a[given[0]], where, filaments)
    resolve_assign(
        assign, filaments, where=where
    )  # unknown keys / slots / one-colour mixes
    return assign


def _expand_mirror(
    name: str, box: Box, b: Sequence[float], mirror: object, where: str
) -> dict[str, Box]:
    """The named boxes a landmark stands for: itself, or its left/right pair when mirrored across X = 0."""
    if mirror is None:
        return {name: box}
    if mirror != "x":
        raise _bad(f"{where}.mirror", mirror, ["x"])
    if box[0] < 0 < box[1]:
        raise _bad(
            f"{where}.box",
            list(b),
            [
                "a box on one side of the X = 0 plane (a box crossing it overlaps its own mirror image)"
            ],
        )
    mirrored: Box = (-box[1], -box[0], box[2], box[3], box[4], box[5])
    right, left = (box, mirrored) if box[0] >= 0 else (mirrored, box)
    return {f"{name}_L": left, f"{name}_R": right}


def _parse_landmarks(raw: object) -> dict[str, Box]:
    lm = _mapping(raw, "landmarks")
    out: dict[str, Box] = {}
    for name, item in lm.items():
        where = f"landmarks.{name}"
        entry = _mapping(item, where)
        _only_keys(entry, _LANDMARK_KEYS, where)
        if "box" not in entry:
            raise _bad(f"{where}: missing field", "box", _LANDMARK_KEYS)
        b = _floats(entry["box"], 6, f"{where}.box")
        if not (b[0] < b[1] and b[2] < b[3] and b[4] < b[5]):
            raise _bad(
                f"{where}.box", list(b), ["[x0, x1, y0, y1, z0, z1] with every lo < hi"]
            )
        box: Box = (b[0], b[1], b[2], b[3], b[4], b[5])
        expanded = _expand_mirror(str(name), box, b, entry.get("mirror"), where)
        for n, bx in expanded.items():
            if n in out:
                raise _bad("landmarks: duplicate name", n, ["unique landmark names"])
            out[n] = bx
    return out


def _shape_sphere(s: Mapping[str, Any], where: str, base_dir: Path) -> Shape:
    _only_keys(s, ("type", "center", "radius"), where)
    radius = s.get("radius")
    if not isinstance(radius, int | float) or isinstance(radius, bool) or radius <= 0:
        raise _bad(f"{where}.radius", radius, ["a positive number (mm)"])
    c = _floats(s.get("center"), 3, f"{where}.center")
    return SphereShape((c[0], c[1], c[2]), float(radius))


def _shape_box(s: Mapping[str, Any], where: str, base_dir: Path) -> Shape:
    _only_keys(s, ("type", "min", "max"), where)
    lo = _floats(s.get("min"), 3, f"{where}.min")
    hi = _floats(s.get("max"), 3, f"{where}.max")
    if not all(a < b for a, b in zip(lo, hi, strict=True)):
        raise _bad(
            f"{where}",
            f"min={list(lo)} max={list(hi)}",
            ["min < max on every axis"],
        )
    return BoxShape((lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2]))


def _shape_halfspace(s: Mapping[str, Any], where: str, base_dir: Path) -> Shape:
    _only_keys(s, ("type", "normal", "offset"), where)
    n = np.array(_floats(s.get("normal"), 3, f"{where}.normal"))
    length = float(np.linalg.norm(n))
    if length == 0:
        raise _bad(f"{where}.normal", list(n), ["a non-zero vector"])
    off = s.get("offset")
    if not isinstance(off, int | float) or isinstance(off, bool):
        raise _bad(f"{where}.offset", off, ["a number (mm along the normal)"])
    u = n / length
    return HalfspaceShape((float(u[0]), float(u[1]), float(u[2])), float(off))


def _shape_stl(s: Mapping[str, Any], where: str, base_dir: Path) -> Shape:
    _only_keys(s, ("type", "path"), where)
    rel = s.get("path")
    if not isinstance(rel, str) or not rel:
        raise _bad(f"{where}.path", rel, ["a path to a closed STL"])
    full = (base_dir / rel).resolve()
    if not full.is_file():
        raise InputNotFound(str(full), command=CMD_NAME)
    return StlShape(str(full))


_SHAPE_PARSERS: dict[str, Callable[[Mapping[str, Any], str, Path], Shape]] = {
    "sphere": _shape_sphere,
    "box": _shape_box,
    "halfspace": _shape_halfspace,
    "stl": _shape_stl,
}


def _parse_shape(raw: object, where: str, base_dir: Path) -> Shape:
    s = _mapping(raw, where)
    kind = s.get("type")
    parse = _SHAPE_PARSERS.get(kind) if isinstance(kind, str) else None
    if parse is None:
        raise _bad(f"{where}.type", kind, _SHAPE_TYPES)
    return parse(s, where, base_dir)


def _region_geometry(
    r: Mapping[str, Any], where: str, landmarks: Mapping[str, Box], base_dir: Path
) -> tuple[list[Shape], tuple[str, ...]]:
    """The shapes of a region and the landmark names it used (none for an inline shape)."""
    geometry = [k for k in ("landmark", "landmarks", "shape") if r.get(k) is not None]
    if len(geometry) != 1:
        raise _bad(
            where,
            ", ".join(geometry) or "no geometry",
            ["exactly one of landmark, landmarks, shape"],
        )
    if geometry[0] == "shape":
        return [_parse_shape(r["shape"], f"{where}.shape", base_dir)], ()
    field = geometry[0]
    return _landmark_shapes(
        r["landmark"] if field == "landmark" else r["landmarks"],
        f"{where}.{field}",
        landmarks,
    )


def _landmark_shapes(
    wanted: object, where: str, landmarks: Mapping[str, Box]
) -> tuple[list[Shape], tuple[str, ...]]:
    """Box shapes of the named landmark(s) plus the names used."""
    used = (
        (wanted,)
        if isinstance(wanted, str)
        else tuple(wanted)
        if isinstance(wanted, Sequence)
        else ()
    )
    if not used or not all(isinstance(u, str) for u in used):
        raise _bad(where, wanted, ["landmark name(s)"])
    shapes: list[Shape] = []
    for u in used:
        if u not in landmarks:
            raise _bad(where, u, sorted(landmarks) or ["(no landmarks defined)"])
        b = landmarks[u]
        shapes.append(BoxShape((b[0], b[2], b[4]), (b[1], b[3], b[5])))
    return shapes, used


def _parse_region(
    i: int,
    item: object,
    names: set[str],
    landmarks: Mapping[str, Box],
    filaments: Sequence[Filament],
    base_dir: Path,
    mix_cap: float,
    max_cycle: int,
) -> RegionSpec:
    r = _mapping(item, f"regions[{i}]")
    name = r.get("name")
    where = f"regions[{name!r}]" if isinstance(name, str) else f"regions[{i}]"
    _only_keys(r, _REGION_KEYS, where)
    if not isinstance(name, str) or not name:
        raise _bad(f"{where}.name", name, ["a non-empty string"])
    if name in names:
        raise _bad(
            "regions: duplicate name",
            name,
            [f"unique names ({DEFAULT_PART!r} is the default part)"],
        )
    names.add(name)
    shapes, used = _region_geometry(r, where, landmarks, base_dir)
    if (r.get("assign") is None) == (r.get("target") is None):
        raise _bad(
            where,
            "both" if r.get("assign") is not None else "neither",
            ["exactly one of assign, target"],
        )
    if r.get("target") is None:
        assign = _parse_assign(r["assign"], f"{where}.assign", filaments)
        return RegionSpec(name, tuple(shapes), assign, used)
    target = r["target"]
    if not isinstance(target, str) or not _HEX.match(target):
        raise _bad(f"{where}.target", target, ["#RRGGBB"])
    assign, delta_e = _search_mix(
        target, filaments, mix_cap=mix_cap, max_cycle=max_cycle
    )
    return RegionSpec(name, tuple(shapes), assign, used, target.lower(), delta_e)


def _parse_regions(
    raw: object,
    landmarks: Mapping[str, Box],
    filaments: Sequence[Filament],
    base_dir: Path,
    mix_cap: float,
    max_cycle: int,
) -> tuple[RegionSpec, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise _bad("regions", raw, ["a list of regions"])
    names: set[str] = {DEFAULT_PART}
    return tuple(
        _parse_region(
            i, item, names, landmarks, filaments, base_dir, mix_cap, max_cycle
        )
        for i, item in enumerate(raw)
    )


# ---------------------------------------------------------------------------------------------- assigns
def _slot_of(ch: str, by_key: Mapping[str, int], slots: set[int], where: str) -> int:
    if ch in by_key:
        return by_key[ch]
    if ch.isdigit() and int(ch) in slots:
        return int(ch)
    known = [*sorted(by_key), *(str(s) for s in sorted(slots))]
    raise _bad(where, ch, known)


def resolve_assign(
    assign: Assign, filaments: Sequence[Filament], *, where: str = "assign"
) -> Assign:
    """`assign` with slot numbers only: `key` -> `slot`, a mix's letters -> slot digits.

    Raises InvalidArgument for a key / slot that no filament has and for a mix of a single colour (that is a
    pure filament: assign the slot directly)."""
    by_key = {f.key: f.slot for f in filaments}
    slots = {f.slot for f in filaments}
    if assign.key is not None:
        return Assign(slot=_slot_of(assign.key, by_key, set(), f"{where}.key"))
    if assign.slot is not None:
        if assign.slot not in slots:
            raise _bad(f"{where}.slot", assign.slot, [str(s) for s in sorted(slots)])
        return Assign(slot=assign.slot)
    assert assign.mix is not None  # Assign guarantees exactly one field
    digits = "".join(
        str(_slot_of(ch, by_key, slots, f"{where}.mix")) for ch in assign.mix
    )
    if len(set(digits)) < 2:
        raise _bad(
            f"{where}.mix",
            assign.mix,
            ["two or more different filaments (one colour is a pure `key`)"],
        )
    return Assign(mix=digits)


def mix_digits(assign: Assign, filaments: Sequence[Filament]) -> str:
    """Slot digits of a `mix` assign ("GWG" with G=4, W=2 -> "424"). A pure assign is not a mix: InvalidArgument."""
    if assign.mix is None:
        raise InvalidArgument("assign", "slot/key", ["a mix assign"], command=CMD_NAME)
    resolved = resolve_assign(assign, filaments)
    assert resolved.mix is not None
    return resolved.mix


def _mix_engine(*modules: str) -> tuple[ModuleType, ...] | None:
    """The #410 modules, or None while they are not on this branch."""
    try:
        return tuple(importlib.import_module(m) for m in modules)
    except ImportError:
        return None


def _search_mix(
    target: str, filaments: Sequence[Filament], *, mix_cap: float, max_cycle: int
) -> tuple[Assign, float]:
    """Best recipe for `target` among the loaded filaments (slicing.mix_model.rank), as a resolved assign."""
    engine = _mix_engine("slicing.mix_model", "slicing.mix_pattern")
    if engine is None:
        raise MissingDependency(
            "slicing.mix_model (epic #410, colour mix recipes)",
            install="use a 3d-cli checkout that includes #410, or give the region `assign: {mix: ...}` explicitly",
            degrades="`target: '#rrggbb'` regions cannot pick a recipe",
            command=CMD_NAME,
        )
    model, pattern = engine
    slots = [model.Slot(f.slot, f.hex, f.name) for f in filaments]
    cands = model.rank(
        target,
        slots,
        max_cycle=max_cycle,
        layer_h=mix_cap,
        rules=pattern.MixRules(layer_cap_mm=mix_cap),
    )
    if not cands:
        raise _bad("target", target, ["a colour some filament mix can reach"])
    best = cands[0]
    digits = str(best.pattern)
    return (
        Assign(slot=int(digits)) if len(digits) == 1 else Assign(mix=digits)
    ), float(best.delta_e)


# ---------------------------------------------------------------------------------------------- geometry
def _shape_mesh(shape: Shape, bounds: NDArray[np.float64]) -> trimesh.Trimesh:
    """Closed cutter mesh of a shape; `bounds` (2x3 of the model) sizes the half-space slab."""
    if isinstance(shape, SphereShape):
        m = trimesh.creation.icosphere(subdivisions=4, radius=shape.radius)
        m.apply_translation(shape.center)
        return m
    if isinstance(shape, BoxShape):
        lo, hi = np.array(shape.lo), np.array(shape.hi)
        m = trimesh.creation.box(extents=hi - lo)
        m.apply_translation((lo + hi) / 2)
        return m
    if isinstance(shape, HalfspaceShape):
        n = np.array(shape.normal)
        # a slab that reaches every model point from the plane: distance of the farthest corner + the offset
        reach = float(np.abs(bounds).max()) * np.sqrt(3) + abs(shape.offset) + 10.0
        size = 2 * reach
        m = trimesh.creation.box(extents=(size, size, size))
        m.apply_translation((0, 0, size / 2))  # the +z half-space slab
        m.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], n))
        m.apply_translation(n * shape.offset)
        return m
    return _load_closed_stl(shape.path)


def _load_closed_stl(path: str) -> trimesh.Trimesh:
    loaded: Any = trimesh.load(path, force="mesh")
    if (
        not isinstance(loaded, trimesh.Trimesh)
        or len(loaded.faces) == 0
        or not loaded.is_watertight
    ):
        raise InvalidArgument(
            "colour spec shape.path",
            path,
            ["a closed (watertight) STL"],
            command=CMD_NAME,
        )
    return loaded


class _Containment:
    """Point-in-shape tests; STL shapes are voxelised once per spec use."""

    def __init__(self) -> None:
        self._stl: dict[str, Grid] = {}

    def __call__(self, shapes: Sequence[Shape], pts: Points) -> Mask:
        out = np.zeros(len(pts), dtype=bool)
        for s in shapes:
            out |= self._one(s, pts)
        return out

    def _one(self, s: Shape, pts: Points) -> Mask:
        if isinstance(s, BoxShape):
            lo, hi = np.array(s.lo), np.array(s.hi)
            inside: Mask = np.all((pts >= lo) & (pts <= hi), axis=1)
            return inside
        if isinstance(s, SphereShape):
            ball: Mask = np.sum((pts - np.array(s.center)) ** 2, axis=1) <= s.radius**2
            return ball
        if isinstance(s, HalfspaceShape):
            side: Mask = pts @ np.array(s.normal) >= s.offset
            return side
        grid = self._stl.get(s.path)
        if grid is None:
            m = _load_closed_stl(s.path)
            pitch = max(0.05, float(np.max(m.extents)) / 300.0)
            grid = self._stl[s.path] = voxelize(m, pitch)
        idx = [grid.index(pts[:, a], a) for a in range(3)]
        ok = np.ones(len(pts), dtype=bool)
        for a in range(3):
            ok &= (idx[a] >= 0) & (idx[a] < grid.occ.shape[a])
        hit = np.zeros(len(pts), dtype=bool)
        hit[ok] = grid.occ[idx[0][ok], idx[1][ok], idx[2][ok]]
        return hit


def _first_match(
    spec: ColourSpec, pts: Points, contains: _Containment
) -> NDArray[np.int32]:
    """Index of the first region containing each point; `len(regions)` (the default part) for the rest."""
    lab = np.full(len(pts), len(spec.regions), dtype=np.int32)
    todo = np.ones(len(pts), dtype=bool)
    for i, region in enumerate(spec.regions):
        hit = np.zeros(len(pts), dtype=bool)
        hit[todo] = contains(region.shapes, pts[todo])
        lab[hit] = i
        todo &= ~hit
    return lab


# ---------------------------------------------------------------------------------------------- labels
@dataclass(frozen=True)
class RegionStat:
    name: str
    faces: int
    area_mm2: float
    volume_mm3: float | None  # None: the mesh is not watertight, no volume to share
    volume_share: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "faces": self.faces,
            "area_mm2": round(self.area_mm2, 3),
            "volume_mm3": None
            if self.volume_mm3 is None
            else round(self.volume_mm3, 3),
            "volume_share": None
            if self.volume_share is None
            else round(self.volume_share, 5),
        }


@dataclass
class RegionLabels:
    """Per-face region of a mesh. `names` = region names in priority order + the default part last;
    `labels[f]` indexes `names`. `empty` lists the regions that own no face."""

    names: tuple[str, ...]
    labels: NDArray[np.int32]
    stats: tuple[RegionStat, ...]
    empty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"regions": [s.to_dict() for s in self.stats], "empty": list(self.empty)}


def _region_volumes(
    mesh: trimesh.Trimesh,
    spec: ColourSpec,
    contains: _Containment,
    n_names: int,
    volume_pitch: float,
) -> NDArray[np.float64] | None:
    """Per-region volume (mm3) from a voxel grid; None when the mesh is open or the pitch is 0."""
    if not (mesh.is_watertight and volume_pitch > 0):
        return None
    grid = voxelize(mesh, volume_pitch)
    cells = np.argwhere(grid.occ)
    centres = np.stack([grid.centres(a)[cells[:, a]] for a in range(3)], axis=1)
    counts = np.bincount(_first_match(spec, centres, contains), minlength=n_names)
    return np.asarray(counts * volume_pitch**3, dtype=np.float64)


def label_faces(
    mesh: trimesh.Trimesh, spec: ColourSpec, *, volume_pitch: float = 0.5
) -> RegionLabels:
    """Assign every face to the first region (priority = list order) containing its centroid; the rest is the
    default part. Volume shares come from a `volume_pitch` mm voxel grid of the mesh when it is watertight."""
    contains = _Containment()
    lab = _first_match(
        spec, np.asarray(mesh.triangles_center, dtype=np.float64), contains
    )
    names = (*(r.name for r in spec.regions), DEFAULT_PART)
    area = np.asarray(mesh.area_faces, dtype=np.float64)
    vol = _region_volumes(mesh, spec, contains, len(names), volume_pitch)
    total = float(vol.sum()) if vol is not None else 0.0
    stats: list[RegionStat] = []
    for i, name in enumerate(names):
        sel = lab == i
        v = None if vol is None else float(vol[i])
        share = None if vol is None or total == 0 else float(vol[i]) / total
        stats.append(RegionStat(name, int(sel.sum()), float(area[sel].sum()), v, share))
    empty = tuple(s.name for s in stats if s.faces == 0 and s.name != DEFAULT_PART)
    return RegionLabels(names, lab, tuple(stats), empty)


# ---------------------------------------------------------------------------------------------- parts
def _bool(
    kind: Literal["difference", "union", "intersection"],
    a: trimesh.Trimesh,
    b: trimesh.Trimesh,
) -> trimesh.Trimesh:
    result: trimesh.Trimesh = trimesh.boolean.boolean_manifold([a, b], kind)
    return result


def _is_empty(m: trimesh.Trimesh) -> bool:
    return bool(m.is_empty or len(m.faces) == 0 or m.volume < MIN_PIECE_MM3)


def _union_cutter(
    shapes: Sequence[Shape], bounds: NDArray[np.float64]
) -> trimesh.Trimesh:
    cutter = _shape_mesh(shapes[0], bounds)
    for s in shapes[1:]:
        cutter = _bool("union", cutter, _shape_mesh(s, bounds))
    return cutter


@dataclass
class _Split:
    parts: list[Part]
    empty: tuple[str, ...]


def _split(mesh: trimesh.Trimesh, spec: ColourSpec) -> _Split:
    rest = mesh
    parts: list[Part] = []
    empty: list[str] = []
    for region in spec.regions:
        cutter = _union_cutter(region.shapes, mesh.bounds)
        piece = _bool("intersection", rest, cutter)
        if _is_empty(piece):
            empty.append(region.name)
            continue
        rest = _bool("difference", rest, cutter)
        parts.append(
            Part(region.name, piece, resolve_assign(region.assign, spec.filaments))
        )
    if not _is_empty(rest):
        parts.append(
            Part(DEFAULT_PART, rest, resolve_assign(spec.default, spec.filaments))
        )
    return _Split(parts, tuple(empty))


def split_parts(mesh: trimesh.Trimesh, spec: ColourSpec) -> list[Part]:
    """The model as disjoint watertight colour volumes: one Part per non-empty region (priority = list order) plus
    the default part "base". Empty regions are dropped (`plan_colours` reports them).

    Raises GateFailure when the mesh is not watertight (manifold3d cannot cut an open surface)."""
    if not mesh.is_watertight:
        raise GateFailure(
            "model is not watertight: repair it before cutting colour regions (manifold3d needs a closed mesh)",
            command=CMD_NAME,
        )
    return _split(mesh, spec).parts


# ---------------------------------------------------------------------------------------------- mix rules
@dataclass(frozen=True)
class MixViolation:
    region: str
    rule: str
    detail: str


@dataclass(frozen=True)
class MixCheck:
    """Layer-rule check of every mix assign. `engine` says which rule implementation judged."""

    checked: tuple[str, ...]  # regions with a mix assign
    violations: tuple[MixViolation, ...]
    engine: str
    max_layer_mm: float  # thickest layer the cycle was judged at (the cap itself when no heights were given)

    @property
    def ok(self) -> bool:
        return not self.violations

    def violations_by_region(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for v in self.violations:
            out.setdefault(v.region, []).append(f"{v.rule}: {v.detail}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "checked": list(self.checked),
            "max_layer_mm": round(self.max_layer_mm, 4),
            "violations": [
                {"region": v.region, "rule": v.rule, "detail": v.detail}
                for v in self.violations
            ],
        }


def _fallback_rules(
    pattern: str, thickness_mm: float, cap_mm: float
) -> list[tuple[str, str]]:
    """The #410 default rules in their minimal form: no filament on two adjacent layers (the repeat seam counts),
    a mixed layer at most `cap_mm` thick."""
    out: list[tuple[str, str]] = []
    n = len(pattern)
    seam = [i for i in range(n) if pattern[i] == pattern[(i + 1) % n]]
    if seam:
        spots = ", ".join(f"{pattern[i]} at layers {i}-{(i + 1) % n}" for i in seam)
        out.append(
            (
                "adjacent-repeat",
                f"the same filament on neighbouring layers of {pattern!r} (cyclic): {spots}; a double-thick band",
            )
        )
    if thickness_mm > cap_mm + CAP_SLACK_MM:
        out.append(
            (
                "layer-too-thick",
                f"mixed layers up to {thickness_mm:.3f} mm exceed the {cap_mm:g} mm cap",
            )
        )
    return out


def _check_mix(
    name: str,
    assign: Assign,
    spec: ColourSpec,
    layer_heights: Sequence[float] | Mapping[str, Sequence[float]] | None,
    mix_cap: float,
    engine: tuple[ModuleType, ...] | None,
    rules: Any,
) -> tuple[float, list[MixViolation]]:
    """Thickest mixed layer judged for one mix assign, and the layer-rule violations found."""
    pattern = mix_digits(assign, spec.filaments)
    heights = (
        layer_heights.get(name, ())
        if isinstance(layer_heights, Mapping)
        else (layer_heights or ())
    )
    thickness = max(heights) if heights else mix_cap
    if len(set(pattern)) < 2:
        return thickness, [
            MixViolation(
                name,
                "not-a-mix",
                f"{pattern!r} is one filament: assign the slot directly",
            )
        ]
    if engine and rules is not None:
        report = engine[0].analyse_pattern(pattern, float(thickness), rules)
        return thickness, [
            MixViolation(name, v.rule, v.detail) for v in report.violations
        ]
    return thickness, [
        MixViolation(name, rule, d)
        for rule, d in _fallback_rules(pattern, thickness, mix_cap)
    ]


def check_mixes(
    spec: ColourSpec,
    layer_heights: Sequence[float] | Mapping[str, Sequence[float]] | None = None,
    *,
    mix_cap: float = DEFAULT_MIX_CAP_MM,
) -> MixCheck:
    """Check every `mix` assign (regions and the default) against the layer rules.

    `layer_heights` are the layer heights printed over the mix: one sequence for all regions, or a mapping
    region name ("base" for the default) -> that region's layers (`LayerPlan.heights_between`). The cycle is judged
    at the thickest of them; None (or no layers) judges it at `mix_cap`, i.e. the pattern rules only.
    Uses `slicing.mix_pattern.analyse_pattern` when importable, else the local fallback (same default rules)."""
    engine = _mix_engine("slicing.mix_pattern")
    rules = engine[0].MixRules(layer_cap_mm=mix_cap) if engine else None
    entries = [
        *((r.name, r.assign) for r in spec.regions),
        (DEFAULT_PART, spec.default),
    ]
    checked: list[str] = []
    violations: list[MixViolation] = []
    worst_mm = 0.0
    for name, assign in entries:
        if assign.mix is None:
            continue
        thickness, found = _check_mix(
            name, assign, spec, layer_heights, mix_cap, engine, rules
        )
        worst_mm = max(worst_mm, thickness)
        checked.append(name)
        violations += found
    return MixCheck(
        tuple(checked),
        tuple(violations),
        "slicing.mix_pattern" if engine else "local-fallback",
        worst_mm or mix_cap,
    )


# ---------------------------------------------------------------------------------------------- the step result
def _assign_dict(a: Assign) -> dict[str, Any]:
    return {
        k: v
        for k, v in (("slot", a.slot), ("key", a.key), ("mix", a.mix))
        if v is not None
    }


@dataclass
class ColourPlan:
    """Result of `plan_colours`: the colour parts, what was dropped, and the layer-rule check of the mixes."""

    verdict: Verdict
    reasons: tuple[str, ...]
    parts: list[Part]
    empty_regions: tuple[str, ...]
    model_volume_mm3: float
    parts_volume_mm3: float
    mix_check: MixCheck | None
    thickness_checked: (
        bool  # False: no layer heights were given, mixes were judged at the cap only
    )

    @property
    def volume_error(self) -> float:
        """|sum(parts) - model| / model."""
        if self.model_volume_mm3 <= 0:
            return 0.0
        return (
            abs(self.parts_volume_mm3 - self.model_volume_mm3) / self.model_volume_mm3
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "parts": [
                {
                    "name": p.name,
                    "assign": _assign_dict(p.assign),
                    "faces": int(len(p.mesh.faces)),
                    "volume_mm3": round(float(p.mesh.volume), 3),
                    "z_range": [
                        round(float(p.mesh.bounds[0][2]), 3),
                        round(float(p.mesh.bounds[1][2]), 3),
                    ],
                }
                for p in self.parts
            ],
            "empty_regions": list(self.empty_regions),
            "model_volume_mm3": round(self.model_volume_mm3, 3),
            "parts_volume_mm3": round(self.parts_volume_mm3, 3),
            "volume_error": round(self.volume_error, 6),
            "thickness_checked": self.thickness_checked,
            "mix_check": None if self.mix_check is None else self.mix_check.to_dict(),
        }

    def summary(self) -> str:
        mixes = len(self.mix_check.checked) if self.mix_check else 0
        text = f"{len(self.parts)} colour parts ({mixes} mixed), volume error {self.volume_error * 100:.2f}%"
        if self.empty_regions:
            text += f", empty: {', '.join(self.empty_regions)}"
        if self.reasons and self.verdict == "FAIL":
            text += f" — {self.reasons[0]}"
        return text


def _unknown_filament(
    name: str, assign: Assign, filaments: Sequence[Filament]
) -> str | None:
    try:
        resolve_assign(assign, filaments, where=f"region {name!r}")
    except InvalidArgument as exc:
        return f"region {name!r} is assigned to an unknown filament: {exc.message}"
    return None


def _unresolvable(spec: ColourSpec) -> list[str]:
    """Reasons for every assign that names a filament the spec does not load (empty when all resolve)."""
    found = (
        _unknown_filament(name, assign, spec.filaments)
        for name, assign in [
            *((r.name, r.assign) for r in spec.regions),
            (DEFAULT_PART, spec.default),
        ]
    )
    return [f for f in found if f is not None]


def _cut_parts(
    mesh: trimesh.Trimesh, spec: ColourSpec, reasons: list[str]
) -> tuple[list[Part], tuple[str, ...], float, float]:
    """Cut the mesh into colour parts; appends FAIL reasons (open mesh, volume mismatch) to `reasons`."""
    if not mesh.is_watertight:
        reasons.append(
            "model is not watertight: repair it before cutting colour regions"
        )
        return [], (), 0.0, 0.0
    split = _split(mesh, spec)
    model_volume = float(mesh.volume)
    parts_volume = float(sum(p.mesh.volume for p in split.parts))
    err = abs(parts_volume - model_volume) / model_volume if model_volume > 0 else 0.0
    if err > VOLUME_TOL:
        reasons.append(
            f"parts add up to {err * 100:.2f}% off the model volume (tolerance {VOLUME_TOL * 100:.1f}%)"
        )
    return split.parts, split.empty, model_volume, parts_volume


def plan_colours(
    mesh: trimesh.Trimesh,
    spec: ColourSpec,
    layer_heights: Sequence[float] | Mapping[str, Sequence[float]] | None = None,
    *,
    mix_cap: float = DEFAULT_MIX_CAP_MM,
) -> ColourPlan:
    """Cut `mesh` into colour parts and check the mixes. FAIL: a region assigned to a filament the spec does not
    load, a mesh that is not watertight, parts that do not add up to the model volume, a mix breaking the layer
    rules. WARN: a region that owns no volume (dropped)."""
    reasons: list[str] = _unresolvable(spec)
    if reasons:
        return ColourPlan(
            "FAIL", tuple(reasons), [], (), 0.0, 0.0, None, layer_heights is not None
        )
    check = check_mixes(spec, layer_heights, mix_cap=mix_cap)
    for region, found in check.violations_by_region().items():
        reasons += [f"mix of {region!r} breaks the layer rules — {f}" for f in found]
    parts, empty, model_volume, parts_volume = _cut_parts(mesh, spec, reasons)
    failed = bool(reasons)
    warned = [
        f"region {n!r} has no volume (no overlap with the remaining model), dropped"
        for n in empty
    ]
    verdict: Verdict = "FAIL" if failed else "WARN" if warned else "PASS"
    return ColourPlan(
        verdict,
        tuple(reasons + warned),
        parts,
        empty,
        model_volume,
        parts_volume,
        check,
        layer_heights is not None,
    )
