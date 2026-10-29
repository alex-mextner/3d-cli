"""Assembled model for `3d strength`: bodies, mates -> contacts, load cases, fixtures.

Inputs, in the order a user reaches for them:

  part.scad|.stl|.3mf   one body (a multi-object 3MF gives one body per object, in assembly pose)
  3d.yaml | project/    one body per `parts:` entry (its `file` / `module`, at the pose the file defines),
                        `mates:` -> contact pairs, `loads:`/`anchors:` -> load cases
  --loads 3d.yaml       load cases (and mates) from a project for a file that is not part of it

Without `loads:` the `--fixture` flag builds the classic beam fixtures from `--load`/`--axis`.
"""

from __future__ import annotations

import math
import os
import pathlib
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from errors import InvalidArgument, UsageError
from structural.fea import ContactPair, Load, LoadCase, Region, Support

if TYPE_CHECKING:
    import trimesh

AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
FIT_CONTACT = {
    "glue": "tied",
    "press": "tied",
    "tight": "tied",
    "snap": "frictionless",
    "sliding": "frictionless",
    "free_running": "frictionless",
}
CONTACT_KINDS = ("tied", "frictionless")
LOAD_KINDS = ("force", "pressure", "displacement", "gravity", "fixed")
_SUPPORTED_UNITS = ("mm",)


@dataclass
class Body:
    """One solid: a closed surface in mm, its material name and per-part print settings."""

    name: str
    surface: trimesh.Trimesh
    material: str | None = None
    orientation: str | Sequence[float] | None = None
    infill: float | None = None  # fraction (0, 1]
    source: str = ""


@dataclass
class Model:
    bodies: list[Body]
    pairs: list[ContactPair] = field(default_factory=list)
    cases: list[LoadCase] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    project_path: pathlib.Path | None = None
    input_3mf: pathlib.Path | None = None
    default_material: str | None = None
    from_fixture: bool = False


# ---- validation helpers -----------------------------------------------------------------------
def finite_triplet(value: Any, what: str) -> tuple[float, float, float]:
    try:
        vec = [float(v) for v in value]
    except (TypeError, ValueError):
        raise InvalidArgument(
            what, str(value), ["three finite numbers [x, y, z]"], command="strength"
        ) from None
    if len(vec) != 3 or not all(math.isfinite(v) for v in vec):
        raise InvalidArgument(
            what, str(value), ["three finite numbers [x, y, z]"], command="strength"
        )
    return (vec[0], vec[1], vec[2])


def finite_positive(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise InvalidArgument(
            what, str(value), ["a positive finite number"], command="strength"
        ) from None
    if not math.isfinite(x) or x <= 0.0:
        raise InvalidArgument(
            what, str(value), ["a positive finite number"], command="strength"
        )
    return x


def parse_infill(value: Any, what: str = "infill") -> float:
    """'20%', 20, 0.2 -> 0.2. Integers above 1 are percent; 100 % is 1.0."""
    text = str(value).strip()
    pct = text.endswith("%")
    try:
        x = float(text.rstrip("%"))
    except ValueError:
        raise InvalidArgument(
            what, text, ["a percentage such as 20 or 20%"], command="strength"
        ) from None
    if pct or x > 1.0:
        x /= 100.0
    if not math.isfinite(x) or not 0.0 < x <= 1.0:
        raise InvalidArgument(
            what, text, ["a percentage in (0, 100]"], command="strength"
        )
    return x


# ---- mesh loading -----------------------------------------------------------------------------
def as_mesh(obj: Any) -> trimesh.Trimesh:
    """A loaded trimesh object as a single Trimesh (a Scene is flattened)."""
    import trimesh

    if isinstance(obj, trimesh.Trimesh):
        return obj
    if isinstance(obj, trimesh.Scene):
        return cast("trimesh.Trimesh", obj.to_geometry())
    raise InvalidArgument("file", type(obj).__name__, ["a triangle mesh"], command="strength")


def clean_surface(mesh: trimesh.Trimesh, label: str, warnings: list[str]) -> trimesh.Trimesh:
    import trimesh

    if mesh.is_empty or len(mesh.faces) == 0:
        raise InvalidArgument(
            "file",
            label,
            ["a mesh with triangles"],
            command="strength",
            extra="The mesh is empty.",
        )
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    if not mesh.is_watertight:
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fill_holes(mesh)
    if not mesh.is_watertight:
        warnings.append(
            f"{label}: the surface is not watertight; fTetWild closes it by winding number, so check the result"
        )
    if not np.all(np.isfinite(mesh.vertices)):
        raise InvalidArgument(
            "file",
            label,
            ["finite vertex coordinates"],
            command="strength",
            extra="The mesh has NaN/inf vertices.",
        )
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def render_scad(
    path: pathlib.Path, defines: Sequence[str], *, module: str | None = None
) -> pathlib.Path:
    """Render a .scad (optionally one `module()` of it) to a temporary binary STL with OpenSCAD."""
    from cli.env import require_openscad

    openscad = require_openscad("strength")
    out_dir = pathlib.Path(tempfile.mkdtemp(prefix="strength_"))
    source = path
    if module:
        wrapper = out_dir / "module.scad"
        wrapper.write_text(f"use <{path.resolve()}>;\n{module}();\n", encoding="utf-8")
        source = wrapper
    stl = out_dir / "part.stl"
    cmd = [openscad, "--export-format", "binstl"]
    for define in defines:
        cmd += ["-D", define]
    cmd += ["-o", str(stl), str(source)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, env={**os.environ}
        )
    except subprocess.TimeoutExpired as exc:
        raise UsageError(
            f"OpenSCAD timed out rendering {path}", command="strength"
        ) from exc
    if not stl.is_file() or stl.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        raise InvalidArgument(
            "file",
            str(path),
            ["a .scad that renders to solid geometry"],
            command="strength",
            extra=f"OpenSCAD produced no mesh: {tail}",
        )
    return stl


def load_surfaces(
    path: pathlib.Path, defines: Sequence[str], warnings: list[str]
) -> list[tuple[str, trimesh.Trimesh]]:
    """(name, surface) per object of one mesh file."""
    import trimesh

    suffix = path.suffix.lower()
    if suffix == ".scad":
        source = render_scad(path, defines)
        loaded = trimesh.load(source, force="mesh")
        return [(path.stem, clean_surface(as_mesh(loaded), path.name, warnings))]
    loaded = trimesh.load(path)
    if isinstance(loaded, trimesh.Scene):
        out: list[tuple[str, trimesh.Trimesh]] = []
        seen: dict[str, int] = {}
        for node in loaded.graph.nodes_geometry:
            transform, geometry_name = loaded.graph[node]
            geom = loaded.geometry[geometry_name].copy()
            geom.apply_transform(transform)
            base = str(node)
            seen[base] = seen.get(base, 0) + 1
            name = base if seen[base] == 1 else f"{base}_{seen[base]}"
            out.append((name, clean_surface(as_mesh(geom), f"{path.name}:{name}", warnings)))
        if not out:
            raise InvalidArgument(
                "file", str(path), ["a mesh file with geometry"], command="strength"
            )
        return out
    return [(path.stem, clean_surface(as_mesh(loaded), path.name, warnings))]


def apply_crop(body: Body, box: Sequence[float]) -> None:
    """Intersect the body with an axis-aligned box [x0, y0, z0, x1, y1, z1] (a sub-model of a big part)."""
    import trimesh

    lo, hi = np.array(box[:3], dtype=float), np.array(box[3:], dtype=float)
    if np.any(hi <= lo):
        raise InvalidArgument(
            "--crop",
            str(list(box)),
            ["x0,y0,z0,x1,y1,z1 with max > min"],
            command="strength",
        )
    cutter = trimesh.creation.box(extents=hi - lo)
    cutter.apply_translation((hi + lo) / 2.0)
    cropped = trimesh.boolean.intersection([body.surface, cutter], engine="manifold")
    if cropped.is_empty:
        raise InvalidArgument(
            "--crop",
            str(list(box)),
            ["a box that overlaps the part"],
            command="strength",
            extra=f"{body.name} lies outside the crop box (bounds {body.surface.bounds.tolist()}).",
        )
    body.surface = cropped


# ---- anchors / loads from 3d.yaml -------------------------------------------------------------
def _anchor_region(
    project: Any, name: str, radius: float | None, what: str
) -> tuple[Region, Any]:
    anchor = project.anchors.get(name)
    if anchor is None:
        raise InvalidArgument(
            what,
            name,
            sorted(project.anchors) or ["(no anchors declared in 3d.yaml)"],
            command="strength",
            extra="Declare the anchor under `anchors:` with a `pos: [x, y, z]`.",
        )
    pos = finite_triplet(anchor.pos, f"anchors.{name}.pos")
    if radius is None:
        if anchor.area is not None and anchor.area > 0:
            radius = math.sqrt(anchor.area / math.pi)
        else:
            radius = 1.5
    return Region("sphere", center=pos, radius=radius), anchor


def _load_region(project: Any, name: str, spec: Any, radius: float | None) -> Region:
    """Region of a load: `plane: X|Y|Z` + `at: mm` (a planar face), else the facets around its anchor."""
    raw = spec.raw
    plane = raw.get("plane")
    if plane is None:
        return _anchor_region(project, spec.anchor, radius, f"loads.{name}.anchor")[0]
    axis = str(plane).upper()
    if axis not in AXES or raw.get("at") is None:
        raise InvalidArgument(
            f"loads.{name}.plane", str(plane), ["X, Y or Z together with `at: <mm>`"], command="strength"
        )
    try:
        at = float(raw["at"])
    except (TypeError, ValueError):
        at = math.nan
    if not math.isfinite(at):
        raise InvalidArgument(f"loads.{name}.at", str(raw["at"]), ["a finite coordinate in mm"], command="strength")
    tol = finite_positive(raw["tol_mm"], f"loads.{name}.tol_mm") if raw.get("tol_mm") is not None else 0.05
    return Region("slab", axis=AXES[axis], lo=at - tol, hi=at + tol, min_alignment=0.5)


def cases_from_project(project: Any) -> list[LoadCase]:
    """Load cases from `loads:`. `kind: fixed` entries are supports shared by every case; the rest form one case
    each (or one per shared `case:` name)."""
    supports: list[Support] = []
    grouped: dict[str, list[Load]] = {}
    order: list[str] = []
    for name, spec in project.loads.items():
        raw = spec.raw
        kind = str(raw.get("kind", "force"))
        if kind not in LOAD_KINDS:
            raise InvalidArgument(
                f"loads.{name}.kind", kind, LOAD_KINDS, command="strength"
            )
        part = str(raw["part"]) if raw.get("part") is not None else None
        radius = (
            finite_positive(raw["radius_mm"], f"loads.{name}.radius_mm")
            if raw.get("radius_mm") is not None
            else None
        )
        if kind == "gravity":
            vec = finite_triplet(
                raw.get("vector", [0.0, 0.0, -9.81]), f"loads.{name}.vector"
            )
            load = Load(name=name, kind="gravity", region=None, vector=vec)
        else:
            region = _load_region(project, name, spec, radius)
            if kind == "fixed":
                comps = str(raw.get("components", "xyz")).lower()
                if not comps or set(comps) - set("xyz"):
                    raise InvalidArgument(
                        f"loads.{name}.components",
                        comps,
                        ["any of x, y, z, e.g. xyz"],
                        command="strength",
                    )
                supports.append(
                    Support(name=name, region=region, components=comps, part=part)
                )
                continue
            if kind == "pressure":
                if raw.get("pressure_mpa") is None:
                    raise UsageError(
                        f"loads.{name} is a pressure load but has no `pressure_mpa:`",
                        command="strength",
                    )
                load = Load(
                    name=name,
                    kind="pressure",
                    region=region,
                    pressure_mpa=finite_positive(
                        raw["pressure_mpa"], f"loads.{name}.pressure_mpa"
                    ),
                    part=part,
                )
            else:
                if spec.vector is None:
                    raise UsageError(
                        f"loads.{name} has no `vector: [x, y, z]`",
                        command="strength",
                        remediation=["force: newtons; displacement: millimetres."],
                    )
                vec = finite_triplet(spec.vector, f"loads.{name}.vector")
                if kind == "force" and not any(vec):
                    raise InvalidArgument(
                        f"loads.{name}.vector",
                        str(vec),
                        ["a non-zero force vector"],
                        command="strength",
                    )
                comps = str(raw["components"]).lower() if raw.get("components") is not None else "".join(
                    c for c, v in zip("xyz", vec, strict=True) if v != 0.0
                )
                if kind == "displacement" and (not comps or set(comps) - set("xyz")):
                    raise InvalidArgument(
                        f"loads.{name}.components", comps, ["any of x, y, z (default: the non-zero vector entries)"],
                        command="strength",
                    )
                load = Load(name=name, kind=kind, region=region, vector=vec, part=part, components=comps or "xyz")
        case_name = str(raw.get("case", name))
        if case_name not in grouped:
            grouped[case_name] = []
            order.append(case_name)
        grouped[case_name].append(load)
    if not grouped:
        raise UsageError(
            "3d.yaml has no load to analyse (only supports, or an empty `loads:`)",
            command="strength",
            remediation=[
                "Add e.g. `loads: {push: {anchor: tip, vector: [0, 0, -5]}}` next to a `kind: fixed` support."
            ],
        )
    return [
        LoadCase(name=n, loads=tuple(grouped[n]), supports=tuple(supports))
        for n in order
    ]


def pairs_from_project(
    project: Any, body_names: Sequence[str], default_contact: str
) -> tuple[list[ContactPair], list[str]]:
    raw = project.raw.get("mates")
    pairs: list[ContactPair] = []
    notes: list[str] = []
    if raw is None:
        return pairs, notes
    if not isinstance(raw, list):
        raise InvalidArgument(
            "mates",
            type(raw).__name__,
            ["a list of {a, b, fit_type, contact?}"],
            command="strength",
        )
    for i, mate in enumerate(raw):
        if not isinstance(mate, dict) or "a" not in mate or "b" not in mate:
            raise InvalidArgument(
                f"mates[{i}]",
                str(mate),
                ["a mapping with `a:` and `b:` part names"],
                command="strength",
            )
        a, b = str(mate["a"]), str(mate["b"])
        for ref in (a, b):
            if ref not in body_names:
                raise InvalidArgument(
                    f"mates[{i}]", ref, list(body_names), command="strength"
                )
        fit = str(mate["fit_type"]) if mate.get("fit_type") is not None else None
        kind = mate.get("contact")
        if kind is None:
            kind = FIT_CONTACT.get(fit or "", default_contact)
            if fit is not None and fit not in FIT_CONTACT:
                notes.append(
                    f"mate {a}<->{b}: unknown fit_type {fit!r}, contact set to {kind}"
                )
        if kind not in CONTACT_KINDS:
            raise InvalidArgument(
                f"mates[{i}].contact", str(kind), CONTACT_KINDS, command="strength"
            )
        tol = (
            finite_positive(
                mate["contact_tolerance_mm"], f"mates[{i}].contact_tolerance_mm"
            )
            if mate.get("contact_tolerance_mm") is not None
            else None
        )
        pairs.append(
            ContactPair(
                a=a,
                b=b,
                kind=str(kind),
                tolerance_mm=tol,
                origin="3d.yaml mate",
                fit_type=fit,
            )
        )
    return pairs, notes


def auto_pairs(
    bodies: list[Body], declared: list[ContactPair], kind: str, tol_mm: float
) -> list[ContactPair]:
    """Bodies that touch within `tol_mm` and have no declared mate get a default contact."""
    import trimesh

    known = {frozenset((p.a, p.b)) for p in declared}
    found: list[ContactPair] = []
    for i, first in enumerate(bodies):
        for second in bodies[i + 1 :]:
            if frozenset((first.name, second.name)) in known:
                continue
            lo = np.maximum(first.surface.bounds[0], second.surface.bounds[0]) - tol_mm
            hi = np.minimum(first.surface.bounds[1], second.surface.bounds[1]) + tol_mm
            if np.any(lo > hi):
                continue
            points, _ = trimesh.sample.sample_surface(first.surface, 400, seed=3)
            _, dist, _ = trimesh.proximity.closest_point(second.surface, points)
            if float(dist.min()) <= tol_mm:
                found.append(
                    ContactPair(
                        a=first.name,
                        b=second.name,
                        kind=kind,
                        origin="auto: surfaces touch",
                    )
                )
    return found


# ---- flag fixtures ----------------------------------------------------------------------------
def parse_axis(raw: str, flag: str = "--axis") -> tuple[float, float, float]:
    """X|Y|Z with an optional sign: the load acts toward the signed axis (default -axis for bending loads)."""
    text = raw.strip().upper()
    sign = 1.0
    if text and text[0] in "+-":
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:]
    if text not in AXES:
        raise InvalidArgument(flag, raw, [*AXES, "-X", "-Y", "-Z"], command="strength")
    ax = AXES[text]
    return (ax[0] * sign, ax[1] * sign, ax[2] * sign)


def resolve_span(
    surface: trimesh.Trimesh, fixture: str, load_dir: np.ndarray, span_axis: str | None
) -> np.ndarray:
    """Span (beam) axis: the load axis for a compression column, `--span-axis`, else the longest extent off the load axis."""
    if fixture == "compression":
        return np.array(load_dir, dtype=float)
    if span_axis is not None:
        span = np.array(AXES[span_axis.upper()], dtype=float)
        if abs(float(span @ load_dir)) > 0.5:
            raise InvalidArgument(
                "--span-axis", span_axis, ["an axis different from the load axis"], command="strength"
            )
        return span
    extents = surface.extents
    order = sorted(range(3), key=lambda i: -extents[i])
    pick = next(i for i in order if abs(load_dir[i]) < 0.5)
    span = np.zeros(3)
    span[pick] = 1.0
    return span


def _vec(v: np.ndarray) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


def _end_region(surface: trimesh.Trimesh, axis: np.ndarray, *, high: bool, notes: list[str]) -> Region:
    """Planar end cap of `surface` along `axis` (low or high end); falls back to a thin strip for rounded ends."""
    coords = surface.vertices @ axis
    lo, hi = float(coords.min()), float(coords.max())
    span = hi - lo
    tol = 1e-3 * span + 1e-6
    target = hi if high else lo
    aligned = np.abs(surface.face_normals @ axis) >= 0.9
    near = np.abs(surface.triangles_center @ axis - target) <= tol
    if np.any(aligned & near):
        return Region("slab", axis=_vec(axis), lo=target - tol, hi=target + tol, min_alignment=0.9)
    notes.append(
        "the end of the part is not a planar cap: the fixture acts on a 3 % strip at that end, "
        "which stiffens the model slightly"
    )
    strip = 0.03 * span
    if high:
        return Region("slab", axis=_vec(axis), lo=target - strip, hi=target + 1e-6)
    return Region("slab", axis=_vec(axis), lo=target - 1e-6, hi=target + strip)


def fixture_case(
    bodies: list[Body],
    *,
    fixture: str,
    load_n: float,
    axis: tuple[float, float, float],
    span_axis: str | None,
    notes: list[str],
) -> list[LoadCase]:
    """The classic `--fixture cantilever|simple|compression` load case on the union of the bodies."""
    import trimesh

    union = trimesh.util.concatenate([b.surface for b in bodies]) if len(bodies) > 1 else bodies[0].surface
    load_dir = np.array(axis, dtype=float)
    span = resolve_span(union, fixture, load_dir, span_axis)
    span_name = "XYZ"[int(np.argmax(np.abs(span)))]
    if fixture == "compression":
        top = _end_region(union, span, high=True, notes=notes)
        bottom = _end_region(union, span, high=False, notes=notes)
        notes.append(
            f"compression fixture: {span_name}-min end fixed, {load_n:g} N pushes along the {span_name} axis on the {span_name}-max end"
        )
        push = _vec(-span * load_n)
        return [LoadCase("compression", (Load("axial", "force", top, vector=push),), (Support("base", bottom),))]
    coords = union.vertices @ span
    lo, hi = float(coords.min()), float(coords.max())
    length = hi - lo
    force = _vec(load_dir * load_n)
    if fixture == "cantilever":
        root = _end_region(union, span, high=False, notes=notes)
        tip = _end_region(union, span, high=True, notes=notes)
        notes.append(
            f"cantilever fixture: {span_name}-min end fixed, {load_n:g} N toward "
            f"({load_dir[0]:g}, {load_dir[1]:g}, {load_dir[2]:g}) on the {span_name}-max end"
        )
        return [LoadCase("cantilever", (Load("tip", "force", tip, vector=force),), (Support("root", root),))]
    # simple: centre point load on the top, rollers under both ends, and two small pins that only stop rigid motion
    strip = max(0.03 * length, 1e-3)
    mid = (lo + hi) / 2.0
    d_axis = _vec(load_dir)

    def zone(lo_s: float, hi_s: float, sign: float) -> Region:
        return Region(
            "slab", axis=_vec(span), lo=lo_s, hi=hi_s, normal_axis=d_axis, normal_sign=sign, min_alignment=0.5
        )

    third_idx = next(i for i in range(3) if abs(span[i]) < 0.5 and abs(load_dir[i]) < 0.5)
    third_axis = np.zeros(3)
    third_axis[third_idx] = 1.0
    under = float((union.vertices @ load_dir).max())
    third_mid = float(union.vertices[:, third_idx].mean())

    def under_point(s_coord: float) -> tuple[float, float, float]:
        point = span * s_coord + load_dir * under
        point[third_idx] = third_mid
        return _vec(point)

    def roller(s_coord: float) -> Region:
        return Region("line", center=under_point(s_coord), axis=_vec(third_axis))

    d_comp = "xyz"[int(np.argmax(np.abs(load_dir)))]
    third = "xyz"[third_idx]
    notes.append(
        f"simple fixture: three-point bend over the {span_name} span, {load_n:g} N on the top centre; knife-edge rollers "
        f"hold {d_comp} along the underside edge of both ends, single-node pins hold {span_name.lower()} and {third} "
        f"at one end and {third} at the other"
    )
    supports = (
        Support("left-roller", roller(lo), components=d_comp),
        Support("right-roller", roller(hi), components=d_comp),
        Support("left-pin", Region("point", center=under_point(lo)), components=span_name.lower() + third),
        Support("right-pin", Region("point", center=under_point(hi)), components=third),
    )
    centre = Load("centre", "force", zone(mid - strip / 2, mid + strip / 2, -1.0), vector=force)
    return [LoadCase("simple", (centre,), supports)]
