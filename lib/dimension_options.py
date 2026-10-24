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


def _part_bounds(model: Model) -> list[np.ndarray]:
    tris = np.asarray(model.mesh.triangles, dtype=float)
    out = []
    for i in range(len(model.parts)):
        pts = tris[model.face_part == i].reshape(-1, 3)
        out.append(np.array([pts.min(axis=0), pts.max(axis=0)]))
    return out


def separated_groups(model: Model) -> list[Group]:
    """Groups of touching parts; more than one group means exploded / spread out on a plate."""
    boxes = _part_bounds(model)
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
