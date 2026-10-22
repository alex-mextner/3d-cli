"""orient.py — rank print orientations of a model by overhang, bed contact, height and tip margin.

WHAT: `search_orientations` scores a set of candidate "down" directions (the 6 axis directions, the
  largest convex-hull facets, a Fibonacci-sphere sample) and returns them ranked, with the model's
  current orientation always included and labelled `current`. `apply` puts a candidate's mesh into
  the print frame.
WHY: the Chekhov preparation spent hours choosing the orientation by trial slicing. This is the
  mesh-only version: every candidate costs one column of a single (faces x 3) @ (3 x K) product over
  the face normals, so ~500k faces x ~120 directions takes seconds, with no voxels and no GPU.
HOW: a candidate is the direction `d` (model frame) that ends up facing the bed. A face with outward
  normal `n` is an *overhang* when `n . d > sin(angle_deg)` (it faces down and is steeper than
  `angle_deg` from vertical, a vertical wall is 0 deg, a ceiling 90 deg) unless it lies on the bed.
  A face is *on the bed* when it is an overhang-class face and all three vertices are within
  `bed_tol` mm of the lowest vertex plane. Contact area is the area of those faces; the contact
  hull is the 2-D convex hull of every vertex within `bed_tol` of the lowest plane (so a part
  resting on three pins still has a hull). `com_margin_mm` is the signed distance from the volume
  centre of mass (projected onto the bed) to that hull: positive inside, negative outside or when
  the contact is a line or a point (then the distance to it, negated).
SCORE (lower is better, in [0, 1]): the weighted mean of four terms, each clamped to [0, 1]:
  overhang area / total area; 1 - contact area / (contact_ref_fraction * total area); height /
  bounding-box diagonal; 1 - tip angle / tip_ref_deg where tip angle = atan(margin / CoM height).
  All faces are used (nothing is decimated or subsampled). Ties keep candidate order, which puts the
  current orientation first, so a tie never recommends a pointless rotation.
VERDICT (documented because the pipeline gates on it), first match wins:
  FAIL  the best candidate still has an overhang island (a connected set of overhang faces, joined
        across shared edges) of at least `island_min_mm2`: no orientation avoids supports/adaptation.
  PASS  the current orientation is the best or scores within `score_tol` (10 %) of it.
  WARN  otherwise; the reason says whether a ranked candidate cuts the current overhang area by more
        than `cut_warn` (25 %) or merely scores better.
ASSUMPTIONS: the mesh is welded (shared vertices, as `frame.load_mesh` returns it) and in the print
  frame so that "current" is the identity. Island joining needs the welded adjacency; on an unwelded
  mesh every face is its own island, which can only make the FAIL rule less likely, never wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import ConvexHull, QhullError

from errors import InvalidArgument
from prep.contracts import Verdict
from prep.frame import rotated

#: candidates evaluated per matrix product; bounds the (faces x K) temporaries to ~64 MB at 500k faces
_CHUNK = 16
#: directions closer than this are one candidate (hull-facet merge and de-duplication)
_MERGE_DEG = 3.0
_GOLDEN_ANGLE = float(np.pi * (3.0 - np.sqrt(5.0)))

Vec3 = NDArray[np.float64]


@dataclass(frozen=True)
class OrientWeights:
    """Score weights and the two reference scales that turn areas/angles into [0, 1] terms."""

    overhang: float = 4.0
    contact: float = 1.0
    height: float = 0.5
    stability: float = 1.5
    #: contact area at or above this fraction of the total surface area counts as "enough" bed contact
    contact_ref_fraction: float = 0.1
    #: tip angle at or above this counts as fully stable
    tip_ref_deg: float = 45.0

    def total(self) -> float:
        return self.overhang + self.contact + self.height + self.stability


DEFAULT_WEIGHTS = OrientWeights()


@dataclass
class OrientationCandidate:
    """One scored orientation. `rotation` maps model coordinates to a Z-up frame (then drop to Z = 0)."""

    label: str
    rotation: list[list[float]]
    down: list[float]
    overhang_area_mm2: float
    contact_area_mm2: float
    contact_hull_area_mm2: float
    height_mm: float
    com_margin_mm: float
    tip_angle_deg: float
    score: float
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "rotation": self.rotation,
            "down": self.down,
            "overhang_area_mm2": self.overhang_area_mm2,
            "contact_area_mm2": self.contact_area_mm2,
            "contact_hull_area_mm2": self.contact_hull_area_mm2,
            "height_mm": self.height_mm,
            "com_margin_mm": self.com_margin_mm,
            "tip_angle_deg": self.tip_angle_deg,
            "score": self.score,
            "rank": self.rank,
        }


@dataclass
class OrientationReport:
    """Ranked orientation search result (a `StepResult`)."""

    verdict: Verdict
    reasons: list[str]
    #: the top_k best candidates in rank order, plus `current` when it is not among them
    candidates: list[OrientationCandidate]
    best: OrientationCandidate
    current: OrientationCandidate
    evaluated: int
    angle_deg: float
    total_area_mm2: float
    #: areas (mm2, largest first) of the best candidate's overhang islands >= island_min_mm2
    best_islands_mm2: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "evaluated": self.evaluated,
            "angle_deg": self.angle_deg,
            "total_area_mm2": self.total_area_mm2,
            "best": self.best.to_dict(),
            "current": self.current.to_dict(),
            "best_islands_mm2": list(self.best_islands_mm2),
            "candidates": [c.to_dict() for c in self.candidates],
        }

    def summary(self) -> str:
        b, c = self.best, self.current
        if b.rank == c.rank:
            pick = "current orientation is the best"
        else:
            pick = f"best '{b.label}' (score {b.score:.3f} vs current {c.score:.3f})"
        return (
            f"{self.evaluated} orientations: {pick}; overhang "
            f"{c.overhang_area_mm2:.0f} -> {b.overhang_area_mm2:.0f} mm2, height {b.height_mm:.1f} mm"
        )


def apply(mesh: trimesh.Trimesh, candidate: OrientationCandidate) -> trimesh.Trimesh:
    """Copy of `mesh` in the candidate's orientation, in the print frame (lowest point at Z = 0)."""
    return rotated(mesh, candidate.rotation)


def search_orientations(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    candidates: int = 96,
    top_k: int = 5,
    bed_tol: float = 0.3,
    weights: OrientWeights = DEFAULT_WEIGHTS,
    hull_facets: int = 16,
    island_min_mm2: float = 25.0,
    cut_warn: float = 0.25,
    score_tol: float = 0.10,
) -> OrientationReport:
    """Rank orientations of `mesh`; `candidates` is the Fibonacci-sphere sample size, `hull_facets` the
    number of largest convex-hull facet groups added. See the module header for every definition."""
    if not 0.0 < angle_deg < 90.0:
        raise InvalidArgument(
            "angle_deg", str(angle_deg), ["a number in (0, 90)"], command="prep"
        )
    if candidates < 0 or hull_facets < 0:
        raise InvalidArgument(
            "candidates/hull_facets",
            f"{candidates}/{hull_facets}",
            ["non-negative integers"],
            command="prep",
        )
    if top_k < 1:
        raise InvalidArgument("top_k", str(top_k), ["an integer >= 1"], command="prep")
    if bed_tol <= 0.0:
        raise InvalidArgument(
            "bed_tol", str(bed_tol), ["a positive length in mm"], command="prep"
        )
    if len(mesh.faces) == 0:
        raise InvalidArgument(
            "mesh", "0 faces", ["a mesh with at least one triangle"], command="prep"
        )

    notes: list[str] = []
    labelled = _directions(mesh, candidates, hull_facets, notes)
    dirs = np.array([d for _, d in labelled])
    rots = np.array([_rotation_taking(d) for d in dirs])

    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    areas = np.asarray(mesh.area_faces, dtype=np.float64)
    total_area = float(areas.sum())
    com = _volume_centroid(verts, faces, areas)
    sin_thr = float(np.sin(np.radians(angle_deg)))

    n = len(dirs)
    overhang = np.zeros(n)
    contact = np.zeros(n)
    hull_area = np.zeros(n)
    height = np.zeros(n)
    margin = np.zeros(n)
    tip = np.zeros(n)
    for start in range(0, n, _CHUNK):
        sel = slice(start, min(start + _CHUNK, n))
        cos_down = normals @ dirs[sel].T  # (F, k): n . d for every face and candidate
        heights = (
            verts @ rots[sel, 2, :].T
        )  # (V, k): height above the bed after rotation
        for j, k in enumerate(range(sel.start, sel.stop)):
            hcol = heights[:, j]
            low = float(hcol.min())
            over, bed = _classify(cos_down[:, j], hcol, faces, sin_thr, low + bed_tol)
            overhang[k] = float(areas[over].sum())
            contact[k] = float(areas[bed].sum())
            height[k] = float(hcol.max()) - low
            pts = (verts[hcol <= low + bed_tol] @ rots[k].T)[:, :2]
            com_rot = rots[k] @ com
            hull_area[k], margin[k] = _hull_area_and_margin(pts, com_rot[:2])
            com_h = float(com_rot[2]) - low
            tip[k] = float(np.degrees(np.arctan2(margin[k], max(com_h, 1e-12))))

    diag = float(np.linalg.norm(np.ptp(verts, axis=0)))
    scores = _scores(overhang, contact, height, tip, total_area, diag, weights)
    # round before ranking so float noise cannot reorder exact ties; the stable sort keeps candidate order
    order = np.argsort(np.round(scores, 9), kind="stable")
    rank_of = np.empty(n, dtype=np.int64)
    rank_of[order] = np.arange(1, n + 1)

    def build(k: int) -> OrientationCandidate:
        return OrientationCandidate(
            label=labelled[k][0],
            rotation=[[float(v) for v in row] for row in rots[k]],
            down=[float(v) for v in dirs[k]],
            overhang_area_mm2=float(overhang[k]),
            contact_area_mm2=float(contact[k]),
            contact_hull_area_mm2=float(hull_area[k]),
            height_mm=float(height[k]),
            com_margin_mm=float(margin[k]),
            tip_angle_deg=float(tip[k]),
            score=float(scores[k]),
            rank=int(rank_of[k]),
        )

    best = build(int(order[0]))
    current = build(0)  # candidate 0 is the identity by construction (_directions)
    ranked = [build(int(k)) for k in order[:top_k]]
    if current.rank > top_k:
        ranked.append(current)

    islands = _overhang_islands(
        mesh,
        verts,
        faces,
        normals,
        areas,
        np.asarray(best.rotation),
        sin_thr,
        bed_tol,
        island_min_mm2,
    )
    verdict, reasons = _verdict(
        best, current, ranked, islands, island_min_mm2, cut_warn, score_tol
    )
    return OrientationReport(
        verdict=verdict,
        reasons=notes + reasons,
        candidates=ranked,
        best=best,
        current=current,
        evaluated=n,
        angle_deg=float(angle_deg),
        total_area_mm2=total_area,
        best_islands_mm2=islands,
    )


def _verdict(
    best: OrientationCandidate,
    current: OrientationCandidate,
    ranked: list[OrientationCandidate],
    islands: list[float],
    island_min_mm2: float,
    cut_warn: float,
    score_tol: float,
) -> tuple[Verdict, list[str]]:
    if islands:
        return "FAIL", [
            f"best orientation '{best.label}' still has {len(islands)} overhang island(s) of "
            f">= {island_min_mm2:g} mm2 (largest {islands[0]:.0f} mm2): no orientation avoids supports"
        ]
    if (
        best.rank == current.rank
        or current.score <= best.score * (1.0 + score_tol) + 1e-9
    ):
        return "PASS", [
            "current orientation is the best or within the score tolerance of it"
        ]
    cut = max(
        (
            1.0 - c.overhang_area_mm2 / current.overhang_area_mm2
            for c in ranked
            if current.overhang_area_mm2 > 0.0
        ),
        default=0.0,
    )
    if cut > cut_warn:
        return "WARN", [
            f"'{best.label}' scores {best.score:.3f} vs current {current.score:.3f}; a ranked orientation "
            f"cuts the overhang area by {cut:.0%} ({current.overhang_area_mm2:.0f} mm2 now)"
        ]
    return "WARN", [
        f"'{best.label}' scores {best.score:.3f} vs current {current.score:.3f} (better bed contact/height/stability)"
    ]


def _scores(
    overhang: NDArray[np.float64],
    contact: NDArray[np.float64],
    height: NDArray[np.float64],
    tip: NDArray[np.float64],
    total_area: float,
    diag: float,
    w: OrientWeights,
) -> NDArray[np.float64]:
    c_over = np.clip(overhang / max(total_area, 1e-12), 0.0, 1.0)
    c_contact = 1.0 - np.clip(
        contact / max(w.contact_ref_fraction * total_area, 1e-12), 0.0, 1.0
    )
    c_height = np.clip(height / max(diag, 1e-12), 0.0, 1.0)
    c_stab = 1.0 - np.clip(tip / w.tip_ref_deg, 0.0, 1.0)
    return (
        w.overhang * c_over
        + w.contact * c_contact
        + w.height * c_height
        + w.stability * c_stab
    ) / w.total()


def _classify(
    cos_down: NDArray[np.float64],
    heights: NDArray[np.float64],
    faces: NDArray[np.int64],
    sin_thr: float,
    bed_top: float,
) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    """Indices of overhang faces (off the bed) and of bed-contact faces for one candidate."""
    steep = np.flatnonzero(cos_down > sin_thr)
    on_bed = heights[faces[steep]].max(axis=1) <= bed_top
    return steep[~on_bed], steep[on_bed]


def _overhang_islands(
    mesh: trimesh.Trimesh,
    verts: NDArray[np.float64],
    faces: NDArray[np.int64],
    normals: NDArray[np.float64],
    areas: NDArray[np.float64],
    rot: NDArray[np.float64],
    sin_thr: float,
    bed_tol: float,
    min_area: float,
) -> list[float]:
    """Areas of the connected overhang regions of one orientation that are at least `min_area` (largest first)."""
    hcol = verts @ rot[2]
    over, _ = _classify(
        normals @ (-rot[2]), hcol, faces, sin_thr, float(hcol.min()) + bed_tol
    )
    if len(over) == 0:
        return []
    flag = np.full(len(faces), -1, dtype=np.int64)
    flag[over] = np.arange(len(over))
    pairs = np.asarray(mesh.face_adjacency, dtype=np.int64)
    if len(pairs):
        a, b = flag[pairs[:, 0]], flag[pairs[:, 1]]
        keep = (a >= 0) & (b >= 0)
        a, b = a[keep], b[keep]
    else:
        a = b = np.zeros(0, dtype=np.int64)
    graph = coo_matrix((np.ones(len(a)), (a, b)), shape=(len(over), len(over)))
    count, labels = connected_components(graph, directed=False)
    per_island = np.bincount(labels, weights=areas[over], minlength=count)
    big = np.sort(per_island[per_island >= min_area])[::-1]
    return [float(v) for v in big]


def _rotation_taking(down: Vec3) -> NDArray[np.float64]:
    """Minimal rotation (Rodrigues) that sends the unit vector `down` to (0, 0, -1); identity if equal."""
    target = np.array([0.0, 0.0, -1.0])
    c = float(down @ target)
    if c > 1.0 - 1e-15:
        return np.eye(3)
    if c < -1.0 + 1e-12:
        return np.diag([1.0, -1.0, -1.0])  # 180 deg about X
    v = np.cross(down, target)
    k = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.asarray(np.eye(3) + k + (k @ k) / (1.0 + c), dtype=np.float64)


def _directions(
    mesh: trimesh.Trimesh, fib: int, hull_facets: int, notes: list[str]
) -> list[tuple[str, Vec3]]:
    """Candidate down directions in priority order; index 0 is (0, 0, -1) = the current orientation."""
    out: list[tuple[str, Vec3]] = [("current", np.array([0.0, 0.0, -1.0]))]
    axes = [
        ("flipped (+Z down)", (0.0, 0.0, 1.0)),
        ("-X down", (-1.0, 0.0, 0.0)),
        ("+X down", (1.0, 0.0, 0.0)),
        ("-Y down", (0.0, -1.0, 0.0)),
        ("+Y down", (0.0, 1.0, 0.0)),
    ]
    out.extend((label, np.array(v)) for label, v in axes)
    if hull_facets > 0:
        try:
            for i, (d, area) in enumerate(_hull_directions(mesh, hull_facets)):
                out.append((f"hull facet {i + 1} ({area:.0f} mm2)", d))
        except (QhullError, ValueError) as exc:
            notes.append(
                f"convex hull unavailable ({exc}); hull-facet candidates skipped"
            )
    for i in range(fib):
        z = 1.0 - 2.0 * (i + 0.5) / fib
        r = float(np.sqrt(max(0.0, 1.0 - z * z)))
        out.append(
            (
                f"sphere {i + 1}/{fib}",
                np.array(
                    [r * np.cos(i * _GOLDEN_ANGLE), r * np.sin(i * _GOLDEN_ANGLE), z]
                ),
            )
        )
    cos_merge = float(np.cos(np.radians(_MERGE_DEG)))
    kept: list[tuple[str, Vec3]] = []
    for label, d in out:
        if not kept or all(float(d @ k) < cos_merge for _, k in kept):
            kept.append((label, d))
    return kept


def _hull_directions(mesh: trimesh.Trimesh, count: int) -> list[tuple[Vec3, float]]:
    """Outward normals (area-weighted means) and areas of the `count` largest convex-hull facet groups."""
    hull = mesh.convex_hull
    hn = np.asarray(hull.face_normals, dtype=np.float64)
    ha = np.asarray(hull.area_faces, dtype=np.float64)
    cos_merge = float(np.cos(np.radians(_MERGE_DEG)))
    free = np.ones(len(hn), dtype=bool)
    found: list[tuple[Vec3, float]] = []
    for i in np.argsort(-ha, kind="stable"):
        if len(found) >= count:
            break
        if not free[i]:
            continue
        members = free & (hn @ hn[i] > cos_merge)
        free &= ~members
        mean = (hn[members] * ha[members, None]).sum(axis=0)
        found.append((mean / np.linalg.norm(mean), float(ha[members].sum())))
    return found


def _volume_centroid(
    verts: NDArray[np.float64], faces: NDArray[np.int64], areas: NDArray[np.float64]
) -> Vec3:
    """Volume centroid via signed tetrahedra; area-weighted surface centroid if the mesh has no volume."""
    tri = verts[faces]
    vol6 = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2]))
    total = float(vol6.sum())
    if abs(total) > 1e-9 * max(float(areas.sum()), 1.0) ** 1.5:
        return np.asarray(
            (vol6[:, None] * tri.sum(axis=1)).sum(axis=0) / (4.0 * total),
            dtype=np.float64,
        )
    return np.asarray(
        (tri.mean(axis=1) * areas[:, None]).sum(axis=0)
        / max(float(areas.sum()), 1e-12),
        dtype=np.float64,
    )


def _hull_area_and_margin(
    pts: NDArray[np.float64], p: NDArray[np.float64]
) -> tuple[float, float]:
    """(hull area, signed margin of `p` to the hull of `pts`): margin > 0 inside; a line/point hull gives -distance."""
    if len(pts) >= 3:
        try:
            hull = ConvexHull(pts)
        except QhullError:
            hull = None
        if hull is not None and hull.volume > 1e-9:
            poly = pts[hull.vertices]  # counter-clockwise in 2-D
            edge = np.roll(poly, -1, axis=0) - poly
            outward = (
                np.column_stack([edge[:, 1], -edge[:, 0]])
                / np.linalg.norm(edge, axis=1)[:, None]
            )
            inward = -np.einsum("ij,ij->i", p - poly, outward)
            return float(hull.volume), float(inward.min())
    # a line or a point: the nearest point of the extreme-point segment, negated
    centred = pts - pts.mean(axis=0)
    axis = (
        np.linalg.svd(centred, full_matrices=False)[2][0]
        if len(pts) > 1
        else np.array([1.0, 0.0])
    )
    t = centred @ axis
    a, b = pts[int(t.argmin())], pts[int(t.argmax())]
    ab = b - a
    denom = float(ab @ ab)
    s = 0.0 if denom < 1e-18 else float(np.clip((p - a) @ ab / denom, 0.0, 1.0))
    return 0.0, -float(np.linalg.norm(p - (a + s * ab)))
