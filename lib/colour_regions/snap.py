"""snap.py — pull label borders onto the strongest crease of the mesh within a band (`3d colour-regions snap`).

WHAT: faces farther than `band_mm` (geodesic) from any label border keep their label as watershed markers; the band
  is flooded by the quantised crease strength of the mesh (`Surf.edge_angle`, smoothed one ring), so every border ends up
  on the strongest crease within +-`band_mm` of where it was. Where the skin has no relief the quantised plateau
  floods at equal speed from both sides and the border stays where it was.
WHY: a coarse outline (a seed, a hand-drawn zone) is rarely exactly on the relief it follows; the border of a colour
  zone that does not sit on a relief edge reads as a drawn-on line. `verify` measures the share of the border on a
  geometric edge; this raises it where the relief allows.
INVARIANTS: labels in `fixed` never change and never move into another label's band; faces of other bodies never
  change; creases weaker than `min_crease` deg count as flat.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

from colour_regions import CMD
from colour_regions.surface import Array, Surf
from errors import InvalidArgument

#: crease strength (deg) below which the surface counts as "no edge"
MIN_CREASE_DEG = 15.0
#: costs are quantised to this step (deg) so plateaus flood from both sides at equal speed and meet in the middle
LEVELS_DEG = 6.0
DEFAULT_SCALE_MM = 0.9


def crease_cost(s: Surf, scale_mm: float, min_crease: float) -> tuple[Array, Array]:
    """(smoothed crease strength per face in deg, flooding cost = strength above `min_crease`)."""
    e = s.smooth_faces(s.edge_angle(scale_mm), 1)
    return e, np.maximum(e - min_crease, 0.0)


def snap_labels(
    s: Surf, face_label: Array, fixed: Array, band_mm: float, cost: Array
) -> NDArray[np.int16]:
    """Snap the borders of `face_label` (id per face, -1 = default) within `band_mm`. `fixed` (bool per face) and the
    faces of other bodies keep their labels and act as walls: nothing floods into them or out of them."""
    z = np.asarray(face_label).astype(np.int64) + 1  # 0 = default, i + 1 = label i
    a = s.adj.tocoo()
    border = np.zeros(s.nf, bool)
    border[a.row[z[a.row] != z[a.col]]] = True
    dist = s.dist_to_faces(border & s.outer, limit=band_mm)
    wall = fixed | ~s.outer
    free = np.isfinite(dist) & ~wall
    default = int(z.max()) + 1  # the default colour floods as a label of its own
    mk = np.where(z == 0, default, z)
    mk[wall | free] = 0  # markers: every face outside the band
    new = s.watershed(mk, cost, allowed=free, levels=LEVELS_DEG)
    out = np.where(new == default, 0, new)
    out = np.where(new == 0, z, out) - 1  # a face the flood never reached keeps its label
    out[wall] = np.asarray(face_label)[wall]
    return out.astype(np.int16)


def fixed_mask(face_label: Array, names: Sequence[str], patterns: Sequence[str]) -> NDArray[np.bool_]:
    """Faces whose label name fully matches one of the regexes."""
    try:
        ids = [i for i, n in enumerate(names) if any(re.fullmatch(p, n) for p in patterns)]
    except re.error as exc:
        raise InvalidArgument("--fixed", ", ".join(patterns), [f"a valid regex ({exc})"], command=CMD) from exc
    return np.isin(face_label, ids)


def snap_report(before: Array, after: Array, names: Sequence[str]) -> dict[str, Any]:
    """Faces per label before and after, and how many faces changed label."""
    return {
        "changed_faces": int((before != after).sum()),
        "labels": {n: {"before": int((before == i).sum()), "after": int((after == i).sum())} for i, n in enumerate(names)},
    }
