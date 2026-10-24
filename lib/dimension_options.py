"""dimension_options.py — vertical axis, separated-part groups: the geometry choices behind `3d dimension` options.

WHAT: `reorient(model, up)` turns a model whose vertical axis is X or Y so that its vertical axis is Z
  (a cyclic axis permutation: a proper rotation, no mirroring) and says which ORIGINAL axis each new axis
  is, so labels keep the model's own letters. `separated_groups(model)` finds the parts that do not touch
  any other part (an exploded assembly, a plate of several parts).

WHY: views are drawn for a Z-up model: a mole modelled Y-up got a "TOP" that was its front, and one
  overall X across two halves on a plate is a meaningless number (#595, #596).

INVARIANTS: pure numpy; deterministic (groups sorted by name, then position).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from dimension_parts import Model

# new axis i is the ORIGINAL axis PERM[up][i]; the cycle keeps handedness.
PERM = {"Z": (0, 1, 2), "Y": (2, 0, 1), "X": (1, 2, 0)}
TOUCH_TOLERANCE = (
    0.01  # share of the model's diagonal: boxes this close count as touching
)
MAX_GROUPS = 8
PART_TOLERANCE_MM = (
    0.5  # an endpoint within this of a part's bounding box is on that part
)


def letters(up: str) -> tuple[str, str, str]:
    """Original-axis letter of each (new) axis, e.g. ("Z", "X", "Y") for a Y-up model."""
    a, b, c = (("X", "Y", "Z")[i] for i in PERM[up])
    return a, b, c


def reorient(model: Model, up: str) -> Model:
    """`model` with its `up` axis turned into Z."""
    if up == "Z":
        return model
    import trimesh

    perm = list(PERM[up])
    old = model.mesh
    mesh = trimesh.Trimesh(
        vertices=np.asarray(old.vertices)[:, perm], faces=old.faces, process=False
    )
    return replace(model, mesh=mesh)


@dataclass(frozen=True)
class Group:
    name: str  # the largest part of the group
    bounds: np.ndarray  # (2, 3)


def part_bounds(model: Model) -> list[np.ndarray]:
    tris = np.asarray(model.mesh.triangles, dtype=float)
    out = []
    for i in range(len(model.parts)):
        pts = tris[model.face_part == i].reshape(-1, 3)
        out.append(np.array([pts.min(axis=0), pts.max(axis=0)]))
    return out


def separated_groups(model: Model) -> list[Group]:
    """Groups of touching parts; more than one group means exploded / spread out on a plate."""
    boxes = part_bounds(model)
    n = len(boxes)
    if n < 2:
        return []
    diag = float(np.linalg.norm(np.ptp(np.asarray(model.mesh.vertices), axis=0)))
    tol = TOUCH_TOLERANCE * diag
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = boxes[i], boxes[j]
            if np.all(a[0] <= b[1] + tol) and np.all(b[0] <= a[1] + tol):
                parent[find(i)] = find(j)
    members: dict[int, list[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)
    if len(members) < 2 or len(members) > MAX_GROUPS:
        return []
    groups = []
    for ids in members.values():
        biggest = max(ids, key=lambda i: (model.parts[i].faces, model.parts[i].name))
        lo = np.min([boxes[i][0] for i in ids], axis=0)
        hi = np.max([boxes[i][1] for i in ids], axis=0)
        groups.append(Group(model.parts[biggest].name, np.array([lo, hi])))
    groups.sort(key=lambda g: (g.name, tuple(g.bounds[0])))
    return groups


def parts_at(bounds: list[np.ndarray], point: np.ndarray) -> set[int]:
    """Indices of the parts whose bounding box (grown by PART_TOLERANCE_MM) holds `point`."""
    return {
        i
        for i, box in enumerate(bounds)
        if np.all(point >= box[0] - PART_TOLERANCE_MM)
        and np.all(point <= box[1] + PART_TOLERANCE_MM)
    }


def _names(model: Model, indices: set[int]) -> str:
    return " / ".join(sorted(model.parts[i].name for i in indices))


class SpanError(ValueError):
    """A dimension that would run between separate parts; the message names both and the allowed forms."""


def classify_dimension(
    model: Model,
    bounds: list[np.ndarray],
    name: str,
    p1: np.ndarray,
    p2: np.ndarray,
    gap: tuple[str, str] | None,
    separated: bool,
) -> dict[str, object]:
    """Check an explicit dimension against the part layout; returns its sidecar fields.

    In a separated layout (an exploded assembly, a plate of parts that do not touch) both endpoints must
    sit on ONE part; a measurement between two parts is allowed only when declared `kind: gap` between
    the two NAMED parts, with one endpoint on each. Raises SpanError otherwise.
    """
    end1, end2 = parts_at(bounds, p1), parts_at(bounds, p2)
    if gap is not None:
        by_name = {p.name: i for i, p in enumerate(model.parts)}
        missing = [n for n in gap if n not in by_name]
        if missing:
            raise SpanError(
                f"gap dimension {name!r}: no part named {', '.join(map(repr, missing))}; "
                f"the model has: {', '.join(sorted(by_name))}"
            )
        a, b = by_name[gap[0]], by_name[gap[1]]
        if not ((a in end1 and b in end2) or (b in end1 and a in end2)):
            raise SpanError(
                f"gap dimension {name!r} must run from part {gap[0]!r} to part {gap[1]!r} "
                f"(within {PART_TOLERANCE_MM} mm of their bounding boxes), but its ends are on "
                f"{_names(model, end1) or 'no part'} and {_names(model, end2) or 'no part'}"
            )
        return {"parts": list(gap), "spans_separated": True}
    if not separated:
        return {
            "parts": sorted(model.parts[i].name for i in end1 & end2),
            "spans_separated": False,
        }
    for label, end in (("first", end1), ("second", end2)):
        if not end:
            raise SpanError(
                f"dimension {name!r}: its {label} endpoint is on no part "
                f"(more than {PART_TOLERANCE_MM} mm from every part's bounding box)"
            )
    common = end1 & end2
    if not common:
        raise SpanError(
            f"dimension {name!r} runs from {_names(model, end1)} to {_names(model, end2)}: separate parts "
            "of an exploded / multi-part layout, so the number would measure the gap between them. "
            "Keep both endpoints on ONE part, or declare the mating gap in --dims-from as "
            f'`kind: gap` with `parts: ["{_names(model, end1)}", "{_names(model, end2)}"]`'
        )
    return {
        "parts": sorted(model.parts[i].name for i in common),
        "spans_separated": False,
    }


OCCLUSION_TOLERANCE_MM = (
    0.5  # a surface this close in front of a point does not hide it
)


def visible_from(tris: np.ndarray, point: np.ndarray, toward_eye: np.ndarray) -> bool:
    """Is `point` seen from an orthographic eye in direction `toward_eye` (a unit vector)?

    A ray from the point toward the eye (Moeller-Trumbore against every triangle) that meets a surface
    more than OCCLUSION_TOLERANCE_MM away means something is in front: the point is hidden. A point on
    a visible surface (its own triangle is at t ~ 0) or in the air is visible.
    """
    v0 = tris[:, 0]
    e1, e2 = tris[:, 1] - v0, tris[:, 2] - v0
    h = np.cross(toward_eye, e2)
    a = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(a) > 1e-12
    f = np.divide(1.0, a, out=np.zeros_like(a), where=ok)
    s = point - v0
    u = f * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, e1)
    v = f * (q @ toward_eye)
    t = f * np.einsum("ij,ij->i", e2, q)
    hit = (
        ok
        & (u >= -1e-9)
        & (v >= -1e-9)
        & (u + v <= 1 + 1e-9)
        & (t > OCCLUSION_TOLERANCE_MM)
    )
    return not bool(hit.any())
