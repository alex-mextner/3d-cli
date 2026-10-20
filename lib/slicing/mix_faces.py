"""mix_faces.py — a part printed in a mixed filament must show its visible face vertically.

WHAT: reads an Orca-family project 3MF (Snapmaker Orca `mixed_filament_definitions`, per-part
  extruders in Metadata/model_settings.config, meshes with their build and component transforms)
  and, for every part whose extruder is a mixed (virtual) filament — a manual layer pattern or a
  Local-Z ratio row — sums its triangle area in bed coordinates by the face normal:
    top    n_z >= cos 30°        faces within 30° of horizontal, looking up
    side   |n_z| <= sin 30°      faces within 30° of vertical
    slope  the rest looking up   terraces: each step shows one layer
  Faces looking down sit on the bed or under the part and are not visible.
RULE: FAIL when top > side / 2; WARN when slope > side / 2; otherwise PASS. From any one
  direction a viewer sees at most about half of a part's walls but all of its top, so a part
  whose top outweighs that half shows its colour mainly on the top.
WHY: Full Spectrum prints ONE filament per layer and alternates filaments from layer to layer, so
  only walls mix in the eye; a horizontal top shows the last layer, one plain filament (SPEC 58 of
  babushkin-dom; docs/full-spectrum/README.md there). On 2026-10-17 a tile printed flat in mix 431
  came out ΔE 16.6 from the mockup (tg#22816, issue #411).
USED BY: `3d mix check FILE.3mf` (before slicing) and the `mix face` line of `3d print-preflight`
  (project 3MF next to the G-code). Stdlib only: print-preflight imports it in-process.
"""

from __future__ import annotations

import builtins
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from slicing.head_sequence import read_recipes

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
TOP_NZ = math.cos(math.radians(30))
SIDE_NZ = math.sin(math.radians(30))

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_UNITS = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}
_MAX_DEPTH = 16
_NOT_PRINTED = ("negative_part", "support_blocker", "support_enforcer")

# row-major 3x4: r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz
Matrix = tuple[float, ...]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)


class ProjectError(ValueError):
    """The input is not a readable Orca-family project 3MF."""


@dataclass
class PartFaces:
    object: str
    part: str
    extruder: int
    recipe: str  # the manual layer pattern, or "local-z" for a ratio row
    top: float = 0.0  # mm²
    side: float = 0.0
    slope: float = 0.0
    status: str = PASS
    detail: str = ""
    object_id: int = 0  # 1-based position among the build items (the `object_id=` key of Metadata/layer_heights_profile.txt)
    object_height: float = 0.0  # mm, of the whole object (all its parts)
    z_lo: float = 0.0  # mm above the object's lowest point
    z_hi: float = 0.0
    layer_height: float = 0.0  # the object's own layer height override (0 = the project's)
    first_layer: float = 0.0  # the object's own first layer override (0 = the project's)

    def to_dict(self) -> dict[str, builtins.object]:
        return {
            "object": self.object,
            "part": self.part,
            "extruder": self.extruder,
            "recipe": self.recipe,
            "top_mm2": round(self.top, 1),
            "side_mm2": round(self.side, 1),
            "slope_mm2": round(self.slope, 1),
            "status": self.status,
            "detail": self.detail,
            "z_lo_mm": round(self.z_lo, 3),
            "z_hi_mm": round(self.z_hi, 3),
        }


@dataclass
class MixFaceReport:
    parts: list[PartFaces] = field(default_factory=list)
    mixed_rows: int = 0
    objects: int = 0  # build items, mixed or not

    @property
    def status(self) -> str:
        states = {p.status for p in self.parts}
        if not self.parts:
            return SKIP
        return FAIL if FAIL in states else WARN if WARN in states else PASS

    @property
    def failures(self) -> int:
        return sum(p.status == FAIL for p in self.parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": "mix-face",
            "status": self.status,
            "mixed_rows": self.mixed_rows,
            "failures": self.failures,
            "parts": [p.to_dict() for p in self.parts],
        }


def classify(top: float, side: float, slope: float) -> tuple[str, str]:
    """The rule of the module docstring -> (status, why)."""
    half = side / 2
    if top > half:
        return FAIL, (
            f"visible face horizontal: top {top:.0f} mm² > half the walls {half:.0f} mm²; "
            "stand the part up so its coloured face is vertical (a layer mix reads only on walls)"
        )
    if slope > half:
        return WARN, (
            f"mostly sloped faces looking up: {slope:.0f} mm² > half the walls {half:.0f} mm²; "
            "each terrace shows one layer"
        )
    return PASS, f"walls {side:.0f} mm², top {top:.0f} mm²"


# -- project metadata -------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObjectSettings:
    name: str
    extruder: int
    parts: dict[str, tuple[str, int]]  # part id -> (name, extruder; 0 = the object's)
    subtypes: dict[str, str] = field(default_factory=dict)  # part id -> Orca part subtype
    layer_height: float = 0.0  # the object's own layer_height override; 0 = the project's
    first_layer: float = 0.0  # the object's own initial_layer_print_height; 0 = the project's


def _extruder(el: ET.Element) -> int:
    e = el.find("metadata[@key='extruder']")
    raw = e.get("value", "0") if e is not None else "0"
    return int(raw) if raw.strip().isdigit() else 0


def _metadata_number(el: ET.Element, key: str) -> float:
    e = el.find(f"metadata[@key='{key}']")
    try:
        v = float(e.get("value", "0")) if e is not None else 0.0
    except ValueError:
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def read_settings(zf: zipfile.ZipFile) -> dict[str, _ObjectSettings]:
    """model_settings.config: object id -> name, extruder, its parts' extruders and subtypes, and
    the object's own layer height overrides."""
    root = ET.fromstring(zf.read("Metadata/model_settings.config"))
    out: dict[str, _ObjectSettings] = {}
    for obj in root.findall("object"):
        name_el = obj.find("metadata[@key='name']")
        name = (
            name_el.get("value", "")
            if name_el is not None
            else f"object {obj.get('id')}"
        )
        parts: dict[str, tuple[str, int]] = {}
        subtypes: dict[str, str] = {}
        for part in obj.findall("part"):
            pn = part.find("metadata[@key='name']")
            parts[part.get("id", "")] = (
                pn.get("value", "") if pn is not None else "",
                _extruder(part),
            )
            subtypes[part.get("id", "")] = part.get("subtype", "normal_part")
        out[obj.get("id", "")] = _ObjectSettings(
            name,
            _extruder(obj) or 1,
            parts,
            subtypes,
            _metadata_number(obj, "layer_height"),
            _metadata_number(obj, "initial_layer_print_height"),
        )
    return out


# -- geometry ---------------------------------------------------------------------------------


def _matrix(attr: str | None) -> Matrix:
    """3MF `transform` (column-major 4x3: m00 m01 m02 m10 … m32) -> row-major 3x4."""
    if not attr:
        return _IDENTITY
    v = [float(t) for t in attr.split()]
    if len(v) != 12 or not all(math.isfinite(x) for x in v):
        raise ProjectError(f"3MF transform {attr!r} must have 12 finite numbers")
    # 3MF applies a row vector: p' = p · M, M rows = (m00 m01 m02) … translation row (m30 m31 m32)
    return (v[0], v[3], v[6], v[9], v[1], v[4], v[7], v[10], v[2], v[5], v[8], v[11])


def _compose(a: Matrix, b: Matrix) -> Matrix:
    """a ∘ b: apply b first, then a."""
    out: list[float] = []
    for r in range(3):
        ar = a[4 * r : 4 * r + 4]
        for c in range(4):
            s = sum(ar[k] * b[4 * k + c] for k in range(3))
            out.append(s + (ar[3] if c == 3 else 0.0))
    return tuple(out)


@dataclass
class _Areas:
    top: float = 0.0
    side: float = 0.0
    slope: float = 0.0
    triangles: int = 0
    z_min: float = math.inf  # bed coordinates, mm
    z_max: float = -math.inf


def _mesh_areas(mesh: ET.Element, m: Matrix, scale: float, acc: _Areas) -> None:
    vs = mesh.find(f"{{{_CORE}}}vertices")
    ts = mesh.find(f"{{{_CORE}}}triangles")
    if vs is None or ts is None:
        return
    pts = []
    for v in vs:
        x, y, z = (float(v.get(k, "0")) for k in "xyz")
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ProjectError(f"vertex {v.attrib} is not finite")
        pts.append(
            (
                (m[0] * x + m[1] * y + m[2] * z + m[3]) * scale,
                (m[4] * x + m[5] * y + m[6] * z + m[7]) * scale,
                (m[8] * x + m[9] * y + m[10] * z + m[11]) * scale,
            )
        )
        acc.z_min = min(acc.z_min, pts[-1][2])
        acc.z_max = max(acc.z_max, pts[-1][2])
    for t in ts:
        try:
            idx = [int(t.get(k, "-1")) for k in ("v1", "v2", "v3")]
        except ValueError as exc:
            raise ProjectError(
                f"triangle {t.attrib} has a non-integer vertex index"
            ) from exc
        if any(not 0 <= i < len(pts) for i in idx):
            raise ProjectError(f"triangle {t.attrib} refers to a missing vertex")
        a, b, c = (pts[i] for i in idx)
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        wx, wy, wz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        if norm == 0.0:
            continue
        area, cz = norm / 2, nz / norm
        acc.triangles += 1
        if cz >= TOP_NZ:
            acc.top += area
        elif abs(cz) <= SIDE_NZ:
            acc.side += area
        elif cz > 0:
            acc.slope += area


class _Model:
    """The 3MF's model parts (main model + production-extension sub-models), parsed on demand."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.roots: dict[str, ET.Element] = {}
        self.objects: dict[tuple[str, str], ET.Element] = {}

    def root(self, part: str) -> ET.Element:
        part = part.lstrip("/")
        if part not in self.roots:
            try:
                self.roots[part] = ET.fromstring(self.zf.read(part))
            except KeyError as exc:
                raise ProjectError(f"missing model part {part}") from exc
            for o in self.roots[part].iter(f"{{{_CORE}}}object"):
                self.objects[(part, o.get("id", ""))] = o
        return self.roots[part]

    def object(self, part: str, oid: str) -> ET.Element:
        part = part.lstrip("/")
        self.root(part)
        o = self.objects.get((part, oid))
        if o is None:
            raise ProjectError(f"object id {oid} not found in {part}")
        return o

    def areas(
        self, part: str, oid: str, m: Matrix, acc: _Areas, depth: int = 0
    ) -> None:
        if depth > _MAX_DEPTH:
            raise ProjectError("component nesting too deep (cycle?)")
        o = self.object(part, oid)
        mesh = o.find(f"{{{_CORE}}}mesh")
        if mesh is not None:
            unit = _UNITS.get(self.root(part).get("unit", "millimeter"), 1.0)
            _mesh_areas(mesh, m, unit, acc)
        comps = o.find(f"{{{_CORE}}}components")
        for c in comps if comps is not None else []:
            sub = c.get(f"{{{_PROD}}}path") or part
            self.areas(
                sub,
                c.get("objectid", ""),
                _compose(m, _matrix(c.get("transform"))),
                acc,
                depth + 1,
            )


def check(project_3mf: str) -> MixFaceReport:
    """Raises ProjectError for a non-project input or a project without geometry."""
    try:
        with zipfile.ZipFile(project_3mf) as zf:
            patterns, _raft = read_recipes(zf)
            recipes = {fid: pat or "local-z" for fid, pat in patterns.items()}
            settings = read_settings(zf)
            report = MixFaceReport(mixed_rows=len(recipes))
            if not recipes:
                return report
            model = _Model(zf)
            report.parts, report.objects = _parts(model, settings, recipes)
    except (
        zipfile.BadZipFile,
        KeyError,
        ValueError,
        ET.ParseError,
        TypeError,
        OSError,
    ) as exc:
        if isinstance(exc, ProjectError):
            raise ProjectError(f"{project_3mf}: {exc}") from exc
        raise ProjectError(
            f"{project_3mf}: not a readable Orca-family project 3MF "
            f"(needs 3D/3dmodel.model + Metadata/project_settings.config + model_settings.config): {exc}"
        ) from exc
    return report


def _parts(
    model: _Model, settings: dict[str, _ObjectSettings], recipes: dict[int, str]
) -> tuple[list[PartFaces], int]:
    main = "3D/3dmodel.model"
    build = model.root(main).find(f"{{{_CORE}}}build")
    if build is None or not len(build):
        raise ProjectError("3D/3dmodel.model has no build items (no geometry to check)")
    out: list[PartFaces] = []
    for position, item in enumerate(build, start=1):
        part = item.get(f"{{{_PROD}}}path") or main
        oid = item.get("objectid", "")
        obj = model.object(part, oid)
        st = settings.get(oid) or _ObjectSettings(
            obj.get("name") or f"object {oid}", 1, {}
        )
        m = _matrix(item.get("transform"))
        comps = obj.find(f"{{{_CORE}}}components")
        volumes = (
            [
                (
                    c.get(f"{{{_PROD}}}path") or part,
                    c.get("objectid", ""),
                    _compose(m, _matrix(c.get("transform"))),
                )
                for c in comps
            ]
            if comps is not None and len(comps)
            else [(part, oid, m)]
        )
        if not _has_mixed(st, recipes):
            continue  # nothing to measure, and no mesh to read
        mixed: list[tuple[PartFaces, _Areas]] = []
        z_lo, z_hi = math.inf, -math.inf  # the whole object, mixed or not
        for vpart, vid, vm in volumes:
            pname, pex = st.parts.get(vid, ("", 0))
            ex = pex or st.extruder
            subtype = st.subtypes.get(vid, "normal_part")
            if subtype in _NOT_PRINTED:
                continue  # negative parts and support blockers/enforcers add no material
            acc = _Areas()
            model.areas(vpart, vid, vm, acc)
            if acc.triangles and subtype == "normal_part":
                # a modifier shapes settings inside the object, it does not make it taller
                z_lo, z_hi = min(z_lo, acc.z_min), max(z_hi, acc.z_max)
            if ex not in recipes:
                continue
            if acc.triangles == 0:
                raise ProjectError(
                    f"{st.name}: part {pname or vid} in mixed filament {ex} has no triangles"
                )
            pf = PartFaces(
                st.name,
                pname or f"volume {vid}",
                ex,
                recipes[ex],
                acc.top,
                acc.side,
                acc.slope,
                object_id=position,
                layer_height=st.layer_height,
                first_layer=st.first_layer,
            )
            pf.status, pf.detail = classify(acc.top, acc.side, acc.slope)
            mixed.append((pf, acc))
        for pf, acc in mixed:
            pf.object_height = z_hi - z_lo
            pf.z_lo, pf.z_hi = acc.z_min - z_lo, acc.z_max - z_lo
            out.append(pf)
    return out, len(build)


def _has_mixed(st: _ObjectSettings, recipes: dict[int, str]) -> bool:
    """Does any part of this object print in a mixed filament?"""
    return st.extruder in recipes or any(
        (pex or st.extruder) in recipes for _name, pex in st.parts.values()
    )


def format_lines(report: MixFaceReport) -> list[str]:
    if not report.parts:
        return [
            f"no part prints in a mixed filament ({report.mixed_rows} mixed row(s))"
        ]
    return [
        f"{p.status:<4} {p.object} / {p.part}: filament {p.extruder} ({p.recipe}) — {p.detail}"
        for p in report.parts
    ]


_WS = re.compile(r"\s+")


def preflight_check(project_3mf: str) -> dict[str, object]:
    """print-preflight entry: {status PASS/WARN/FAIL/SKIP, detail, lines}."""
    try:
        report = check(project_3mf)
    except ProjectError as exc:
        return {"status": SKIP, "detail": _WS.sub(" ", str(exc)), "lines": []}
    if not report.parts:
        return {
            "status": SKIP,
            "detail": f"no part prints in a mixed filament ({report.mixed_rows} mixed row(s))",
            "lines": [],
        }
    flagged = [
        line
        for p, line in zip(report.parts, format_lines(report), strict=True)
        if p.status != PASS
    ]
    n = len(report.parts)
    if report.status == FAIL:
        detail = f"{report.failures} of {n} mixed part(s) show their colour on a horizontal face"
    elif report.status == WARN:
        detail = f"{len(flagged)} of {n} mixed part(s) mostly sloped (terraces show single layers)"
    else:
        detail = f"{n} mixed part(s) show their colour on walls"
    return {"status": report.status, "detail": detail, "lines": flagged}
