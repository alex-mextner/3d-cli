"""dimension_parts.py — name the parts of a model, give each its own colour, build the mesh to paint.

WHAT: `build_model(raw, title, specs)` turns the colour groups read by `dimension_model.py` into a
  `Model`: one `trimesh.Trimesh` of the whole assembly, a part index per face, and a `PartInfo`
  (name, role, displayed colour, where the colour came from) per part.

HOW the parts are found:
  - a file with several named objects (3MF build items / components, OBJ `o`/`g`) -> one part per object
    (an object with several colours is split per colour: "lid / #D2B07A");
  - a file with ONE object (every OpenSCAD export; a one-material OBJ) -> one part per colour group, named
    by its material, or by its hex when the material name is a generated one ("Color 3");
  - geometry without any colour is split into its separate bodies (connected components), so an
    uncoloured assembly still shows its parts apart ("body 1", "body 2", …).

HOW the colours are chosen (deterministic: the same assembly renders the same colours, in any order):
  1. the authored colour (3MF material, OBJ `Kd`, OpenSCAD `color()`) when it is clearly different from
     every colour already taken (CIE76 dE >= MIN_DELTA_E);
  2. else the shared part palette of `lib/parts.py` (`assign_styles`, keyed by part NAME), nudged along the
     hue circle until it clears every taken colour;
  3. a part whose role is `stand_in` is a MUTED stand-in: its palette colour mixed with white (MUTE_MIX), so
     it can never be mistaken for a printed part. `--part SELECTOR=NAME:ROLE` or the 3MF tag `3d:role` sets
     the role; an authored colour never overrides the muting.
  A model with a single part keeps its authored colour, else the classic blue.

INVARIANTS: two parts never share a displayed colour; names are unique (a clash gets " (2)"); `--part`
  selectors that match nothing are an error that lists what exists; stdlib + numpy + scipy + trimesh only.
"""

from __future__ import annotations

import colorsys
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
from dimension_args import parse_part
from dimension_model import RawModel, RenderError, from_hex, to_hex
from parts import (
    META_PRINTABLE,
    META_ROLE,
    Part,
    ReferenceSource,
    assign_styles,
    part_from_metadata,
)

MIN_DELTA_E = 20.0  # CIE76: two colours closer than this are one colour at a glance
MUTE_MIX = 0.55  # share of white mixed into a stand-in's colour
MUTED_PAIR_SCALE = (
    0.7  # two muted colours need only this share of MIN_DELTA_E between them
)
MAX_BODIES = 24  # more separate bodies than this is a noisy mesh, not an assembly
# A body smaller than this share of the model's diagonal is a fragment of its neighbour.
MIN_BODY_DIAGONAL = 0.02
SINGLE_PART_COLOR = "#759ED6"  # the colour every part had before parts had colours
GOLDEN = 0.618033988749895
MAX_HUE_STEPS = 64
# (saturation, value) factors the colour search tries after the palette's own look.
SAT_VAL_LEVELS = ((1.0, 1.0), (0.55, 1.1), (1.0, 0.6), (0.6, 0.8))
UNCOLOURED = "uncoloured"


@dataclass
class PartInfo:
    name: str
    role: str
    color: str  # displayed "#RRGGBB" (already muted for a stand-in)
    authored: str | None  # the colour the file carried
    source: str  # "authored" | "palette" | "default"
    muted: bool
    faces: int

    @property
    def printable(self) -> bool:
        return self.role == "printed"

    def as_dict(self) -> dict[str, Any]:
        """The sidecar `parts` entry."""
        return {
            "name": self.name,
            "role": self.role,
            "printable": self.printable,
            "color": self.color,
            "authored_color": self.authored,
            "color_source": self.source,
            "muted": self.muted,
            "faces": self.faces,
        }


@dataclass
class Model:
    mesh: Any  # trimesh.Trimesh of every part, faces in file order
    face_part: np.ndarray  # (F,) index into parts
    cullable: (
        np.ndarray
    )  # (F,) bool: the face's part is watertight, so its back faces may be skipped
    parts: list[PartInfo]
    colors: np.ndarray  # (P, 3) displayed colours, 0..1
    warnings: list[str]
    # (F,) the closed solid a face belongs to: one per object of a multi-object file, one for a
    # file without object structure (parts of one OpenSCAD union abut: only the whole is closed).
    face_solid: np.ndarray


@dataclass
class _Cand:
    name: str
    object_name: str
    object_id: str
    label: str
    color: str | None
    meta: dict[str, str]
    faces: np.ndarray


@lru_cache(maxsize=4096)
def _lab(hex_color: str) -> np.ndarray:
    rgb = np.array(from_hex(hex_color))
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = (
        np.array(
            [
                [0.4124, 0.3576, 0.1805],
                [0.2126, 0.7152, 0.0722],
                [0.0193, 0.1192, 0.9505],
            ]
        )
        @ lin
        / np.array([0.95047, 1.0, 1.08883])
    )
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def delta_e(a: str, b: str) -> float:
    """CIE76 colour difference of two '#RRGGBB' colours."""
    return float(np.linalg.norm(_lab(a) - _lab(b)))


def mute(color: str) -> str:
    """A stand-in's displayed colour: the colour washed toward white."""
    return to_hex(*(c * (1 - MUTE_MIX) + MUTE_MIX for c in from_hex(color)))


def _group_faces(face_group: np.ndarray, count: int) -> list[np.ndarray]:
    order = np.argsort(face_group, kind="stable")
    bounds = np.searchsorted(face_group[order], np.arange(count + 1))
    return [order[bounds[i] : bounds[i + 1]] for i in range(count)]


def _candidates(raw: RawModel, title: str) -> list[_Cand]:
    faces = _group_faces(raw.face_group, len(raw.groups))
    multi = len({g.object_id for g in raw.groups}) > 1
    per_object = Counter(g.object_id for g in raw.groups)
    out: list[_Cand] = []
    for group, idx in zip(raw.groups, faces, strict=True):
        if len(idx) == 0:
            continue
        if len(raw.groups) == 1:
            name = group.object_name or title
        elif multi:
            base = group.object_name or (
                f"object {group.object_id.rpartition(':')[2]}"
                if group.object_id
                else title
            )
            name = (
                base
                if per_object[group.object_id] == 1
                else f"{base} / {group.label or UNCOLOURED}"
            )
        else:
            name = group.label or UNCOLOURED
        out.append(
            _Cand(
                name,
                group.object_name,
                group.object_id,
                group.label,
                group.color,
                dict(group.meta),
                idx,
            )
        )
    return out


def _bodies(raw: RawModel, idx: np.ndarray) -> list[np.ndarray]:
    """The separate bodies (connected by shared vertices) among faces `idx`, big ones first.

    Fragments under MIN_BODY_DIAGONAL of the whole model join the biggest body; more than MAX_BODIES
    bodies, or a single one, mean "no split" (an empty list).
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    tri = raw.faces[idx]
    points = raw.vertices[tri.ravel()]
    # Coincident vertices (an STL/OBJ without shared indices) are one vertex for connectivity.
    _, inverse = np.unique(np.round(points, 4), axis=0, return_inverse=True)
    inverse = inverse.reshape(-1, 3)
    n = int(inverse.max()) + 1
    rows = np.concatenate([inverse[:, 0], inverse[:, 1], inverse[:, 2]])
    cols = np.concatenate([inverse[:, 1], inverse[:, 2], inverse[:, 0]])
    _, labels = connected_components(
        coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)), directed=False
    )
    face_label = labels[inverse[:, 0]]
    total = float(np.linalg.norm(np.ptp(raw.vertices[raw.faces.ravel()], axis=0)))
    bodies: list[tuple[float, tuple[float, ...], np.ndarray]] = []
    for lab in np.unique(face_label):
        members = idx[face_label == lab]
        pts = raw.vertices[raw.faces[members].ravel()]
        bodies.append(
            (
                float(np.linalg.norm(np.ptp(pts, axis=0))),
                tuple(np.round(pts.min(axis=0), 2)),
                members,
            )
        )
    bodies.sort(key=lambda b: (-b[0], b[1]))
    big = [b for b in bodies if b[0] >= MIN_BODY_DIAGONAL * max(total, 1e-9)]
    if len(big) < 2 or len(big) > MAX_BODIES:
        return []
    merged = [b[2] for b in big]
    for _, _, members in bodies[len(big) :]:
        merged[0] = np.concatenate(
            [merged[0], members]
        )  # a fragment joins the biggest body
    return merged


def _split_uncoloured(raw: RawModel, cands: list[_Cand]) -> list[_Cand]:
    """Split geometry that has no colour and no object structure into its separate bodies."""
    if len({c.object_id for c in cands}) > 1:
        return cands  # the file names its parts; do not second-guess it
    out: list[_Cand] = []
    for cand in cands:
        bodies = _bodies(raw, cand.faces) if cand.color is None else []
        if not bodies:
            out.append(cand)
            continue
        base = "body" if len(cands) == 1 else UNCOLOURED
        out.extend(
            _Cand(
                f"{base} {i}",
                cand.object_name,
                cand.object_id,
                cand.label,
                None,
                cand.meta,
                faces,
            )
            for i, faces in enumerate(bodies, 1)
        )
    return out


def _apply_specs(cands: list[_Cand], specs: Sequence[str]) -> dict[int, str]:
    """Apply `--part SELECTOR=NAME[:ROLE]`; returns candidate index -> forced role.

    A selector is matched against the names the model arrived with (never against a name an earlier
    `--part` just gave), so the result does not depend on the order of the flags.
    """
    keys = [{c.name.lower(), c.object_name.lower(), c.label.lower()} for c in cands]
    roles: dict[int, str] = {}
    for raw_spec in specs:
        spec = parse_part(raw_spec)
        hit = [
            i
            for i, c in enumerate(cands)
            if spec.selector == c.color or spec.selector.lower() in keys[i]
        ]
        if not hit:
            known = ", ".join(
                sorted({c.name for c in cands} | {c.color for c in cands if c.color})
            )
            raise RenderError(
                f"3d dimension: --part {raw_spec!r} matches no part; the model has: {known}"
            )
        for i in hit:
            if spec.name:
                cands[i].name = spec.name
            if spec.role:
                roles[i] = spec.role
    return roles


def _unique(names: list[str]) -> list[str]:
    """Distinct names: the first of a repeated name keeps it, later ones get " (k)" with k free.

    A name the file itself gave ("screw (2)") is never taken by a generated suffix.
    """
    taken = set(names)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
            continue
        k = 2
        while f"{name} ({k})" in taken:
            k += 1
        taken.add(f"{name} ({k})")
        out.append(f"{name} ({k})")
    return out


def _role_of(cand: _Cand, forced: str | None) -> str:
    if forced:
        return forced
    if META_ROLE in cand.meta or META_PRINTABLE in cand.meta:
        return part_from_metadata(cand.name, cand.meta).role
    return "printed"


def _declared(name: str, role: str) -> Part:
    """A `Part` for the palette; a vitamin/reference needs a source, and this one is only being drawn."""
    source = ReferenceSource("unknown", "unknown", "declared for rendering only")
    return Part(name, role, source if role in ("vitamin", "reference") else None)


def _gap(color: str, muted: bool, taken: list[tuple[str, bool]]) -> float:
    """How far `color` is from the nearest taken colour, as seen at full strength.

    Two muted colours are both washed toward white, which shrinks their difference; their gap is divided by
    MUTED_PAIR_SCALE so one threshold (MIN_DELTA_E) fits every pair.
    """
    return min(
        (
            delta_e(color, other) / (MUTED_PAIR_SCALE if muted and other_muted else 1.0)
            for other, other_muted in taken
        ),
        default=math.inf,
    )


def _pick(color: str, taken: list[tuple[str, bool]], muted: bool) -> str:
    """The displayed form of the palette colour `color`, moved until it clears every taken colour.

    Search order: the palette's own saturation/value with its hue stepped round the golden ratio, then
    the same walk at the other SAT_VAL_LEVELS. A crowded model where nothing clears the bar gets the
    candidate farthest from what is taken (two parts still never share a colour).
    """
    h, s, v = colorsys.rgb_to_hsv(*from_hex(color))
    best = mute(color) if muted else color
    best_gap = _gap(best, muted, taken)
    if best_gap >= MIN_DELTA_E:
        return best
    for fs, fv in SAT_VAL_LEVELS:
        hue = h
        for _ in range(MAX_HUE_STEPS):
            hue = (hue + GOLDEN) % 1.0
            cand = to_hex(*colorsys.hsv_to_rgb(hue, min(s * fs, 1.0), min(v * fv, 1.0)))
            cand = mute(cand) if muted else cand
            gap = _gap(cand, muted, taken)
            if gap >= MIN_DELTA_E:
                return cand
            if gap > best_gap:
                best, best_gap = cand, gap
    return best


def _resolve_colours(
    cands: list[_Cand],
    names: list[str],
    roles: list[str],
    reserved: Sequence[str],
) -> list[tuple[str, str | None, str, bool]]:
    """(displayed colour, authored colour, source, muted) per candidate."""
    n = len(cands)
    # A lone part keeps its authored colour (or the classic blue); a lone stand-in, or any part with
    # reserved colours to avoid, goes through the rules below.
    lone = n == 1 and roles[0] != "stand_in" and not reserved
    if lone:
        authored = cands[0].color
        return [
            (
                authored or SINGLE_PART_COLOR,
                authored,
                "authored" if authored else "default",
                False,
            )
        ]
    # The palette colours stand-ins as printed parts and the renderer mutes them afterwards: the
    # ghost look of `parts.py` is too pale to tell two stand-ins apart.
    palette = assign_styles(
        _declared(name, "printed" if role == "stand_in" else role)
        for name, role in zip(names, roles, strict=True)
    )
    order = sorted(range(n), key=lambda i: names[i])
    shown: dict[int, str] = {}
    source: dict[int, str] = {}
    taken: list[tuple[str, bool]] = [(c, False) for c in reserved]
    for i in order:  # pass 1: authored colours claim their place, first (by name) wins
        authored = cands[i].color
        if (
            authored
            and roles[i] != "stand_in"
            and _gap(authored, False, taken) >= MIN_DELTA_E
        ):
            shown[i], source[i] = authored, "authored"
            taken.append((authored, False))
    for i in order:  # pass 2: everything else takes its palette colour, nudged clear of what is taken
        if i in shown:
            continue
        muted = roles[i] == "stand_in"
        shown[i] = _pick(palette[names[i]].color, taken, muted)
        source[i] = "palette"
        taken.append((shown[i], muted))
    return [
        (shown[i], cands[i].color, source[i], roles[i] == "stand_in") for i in range(n)
    ]


def build_model(
    raw: RawModel,
    title: str,
    specs: Sequence[str] = (),
    reserved: Sequence[str] = (),
) -> Model:
    """Parts, roles and colours of `raw`, and the mesh to paint.

    `reserved` colours (e.g. a section's cut-face violet) belong to something else in the picture:
    no part is given a colour close to one.
    """
    import trimesh

    cands = _split_uncoloured(raw, _candidates(raw, title))
    forced = _apply_specs(cands, specs)
    names = _unique([c.name for c in cands])
    roles = [_role_of(c, forced.get(i)) for i, c in enumerate(cands)]
    resolved = _resolve_colours(cands, names, roles, reserved)
    parts = [
        PartInfo(name, role, shown, authored, how, muted, len(c.faces))
        for name, role, c, (shown, authored, how, muted) in zip(
            names, roles, cands, resolved, strict=True
        )
    ]
    face_part = np.zeros(len(raw.faces), dtype=np.int64)
    cullable = np.zeros(len(raw.faces), dtype=bool)
    face_solid = np.zeros(len(raw.faces), dtype=np.int64)
    solids: dict[str, int] = {}
    for i, cand in enumerate(cands):
        face_part[cand.faces] = i
        piece = trimesh.Trimesh(
            vertices=raw.vertices, faces=raw.faces[cand.faces], process=True
        )
        cullable[cand.faces] = bool(piece.is_watertight)
        face_solid[cand.faces] = solids.setdefault(cand.object_id, len(solids))
    mesh = trimesh.Trimesh(vertices=raw.vertices, faces=raw.faces, process=False)
    colors = np.array([from_hex(p.color) for p in parts])
    return Model(
        mesh, face_part, cullable, parts, colors, list(raw.warnings), face_solid
    )
