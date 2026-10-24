"""transfer.py — carry an approved classification of one mesh onto another: `classify`, `transfer`, `transfer-class`.

WHAT:
  `classify_by_parts`  label every face of a SKIN mesh by which part meshes it lies on (a build's coloured volumes ->
                       the classification of its skin); faces much larger than the cut pieces can first be split.
  `transfer_file`      every label of a mesh, nearest face, onto another mesh (the generic transfer).
  `transfer_class`     one approved class (e.g. "white") of a source mesh onto a newer mesh by nearest face, with a
                       class that is NOT a source (unknown), a distance cap, and island hygiene that never alters
                       the approved border itself.
WHY: when a model is re-sculpted, the colour borders the owner already approved must stay as approved; the new mesh only
  needs the class of the nearest approved skin face. Re-deriving the borders from the new relief moves them.
INVARIANTS:
  * `transfer_class` labels the positive class as one label (`as_name`, id 0) and everything else -1 (default colour);
    new faces farther than `max_dist` from every source face get no label (new relief, the new track's own labels decide).
  * Island fill never overrides the approved class: an island is flipped only when it is surrounded by the other class
    AND the nearest approved faces agree with the surround (the approved build has no such island there). Islands of a
    class that the approved build also has stay.
PAST BUGS: giant approved faces were classified as a whole by their centroid -> wedges in a large region (fixed:
  `--split-edge` splits faces, the part test uses the split pieces); black islands on a region the approved build
  painted solid (fixed: `--fill-box`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from colour_regions import CMD
from colour_regions.labels import (
    TRANSFER_SPACING_MM,
    load_source,
    mesh_digest,
    resolve_labels,
    write_labels,
)
from colour_regions.surface import Array, Mask, Surf, load_mesh, sample_surface, submesh
from errors import InvalidArgument, UsageError

DEFAULT_PART_TOL_MM = 0.06
#: part faces within this distance (mm) of the split box take part in the surface test
SURFACE_MARGIN_MM = 3.0
MATCH_MODES = ("centroid", "surface")


# ---------------------------------------------------------------------------------------------- classify
def parse_part(raw: str) -> tuple[str, Path]:
    """`NAME=file.stl` -> (name, path)."""
    name, sep, path = raw.partition("=")
    if not sep or not name or not path:
        raise UsageError(
            f"--part {raw!r} is not NAME=part.stl",
            command=CMD,
            remediation=["e.g. --part other=pad.stl --part white=white_a.stl --part white=white_b.stl"],
        )
    return name, Path(path).expanduser()


def parse_vec3(raw: str, flag: str) -> tuple[float, float, float]:
    try:
        x, y, z = (float(v) for v in raw.split(","))
    except ValueError as exc:
        raise InvalidArgument(flag, raw, ["X,Y,Z in mm, e.g. 135,134.5,0"], command=CMD) from exc
    return x, y, z


Box = tuple[float, float, float, float, float, float]


def parse_box(raw: str, flag: str) -> Box:
    try:
        v = [float(x) for x in raw.split(",")]
    except ValueError:
        v = []
    if len(v) != 6 or not (v[0] < v[1] and v[2] < v[3] and v[4] < v[5]):
        raise InvalidArgument(flag, raw, ["x0,x1,y0,y1,z0,z1 in mm with min < max on every axis"], command=CMD)
    return v[0], v[1], v[2], v[3], v[4], v[5]


def in_box(points: Array, box: Box) -> Mask:
    lo, hi = np.array(box[0::2]), np.array(box[1::2])
    return np.asarray(np.all((points > lo) & (points < hi), axis=1))


def refine(
    m: trimesh.Trimesh, max_edge: float, box: Box | None, max_iter: int = 16
) -> tuple[trimesh.Trimesh, NDArray[np.int64]]:
    """Conforming midpoint split: every face whose centroid lies in `box` (anywhere when None) and has an edge longer
    than `max_edge` is split on those edges, repeatedly, until none is left; a face outside the box whose edge is split by
    its neighbour is split too, so the mesh stays closed (its children are tested again). The surface does not change,
    only its triangulation; the original vertices keep their indices. -> (refined mesh, parent face of every new face)."""
    v = np.asarray(m.vertices, float).copy()
    f = np.asarray(m.faces, np.int64).copy()
    lo_b, hi_b = (np.full(3, -np.inf), np.full(3, np.inf)) if box is None else (np.array(box[0::2]), np.array(box[1::2]))
    parent = np.arange(len(f))
    for it in range(max_iter + 1):
        tri = v[f]
        edge_len = np.linalg.norm(tri[:, [1, 2, 0]] - tri, axis=2)  # edge i: f[i] -> f[i+1]
        c = tri.mean(1)
        zone = ((c > lo_b) & (c < hi_b)).all(1)
        long_ = (edge_len > max_edge) & zone[:, None]
        if not long_.any():
            break
        if it == max_iter:
            raise UsageError(
                f"splitting to {max_edge} mm did not converge in {max_iter} rounds",
                command=CMD,
                remediation=["Raise --split-edge or narrow --split-box."],
            )
        nv = len(v)
        a, b = f, f[:, [1, 2, 0]]
        key = np.minimum(a, b) * nv + np.maximum(a, b)
        split_keys = np.unique(key[long_])
        mark = np.isin(key, split_keys)  # every face edge that is split (also those of neighbours outside the box)
        mid = (nv + np.searchsorted(split_keys, key)).astype(np.int64)  # valid where mark
        ma, mb = split_keys // nv, split_keys % nv
        v = np.vstack([v, 0.5 * (v[ma] + v[mb])])
        n = mark.sum(1)
        keep = n == 0
        out_f, out_p = [f[keep]], [parent[keep]]
        idx = np.nonzero(n == 1)[0]  # 1 split edge: rotate so it is edge 0
        if len(idx):
            e = np.argmax(mark[idx], axis=1)
            v0, v1, v2 = f[idx, e], f[idx, (e + 1) % 3], f[idx, (e + 2) % 3]
            mm = mid[idx, e]
            out_f.append(np.concatenate([np.stack([v0, mm, v2], 1), np.stack([mm, v1, v2], 1)]))
            out_p.append(np.tile(parent[idx], 2))
        idx = np.nonzero(n == 2)[0]  # 2 split edges: the unsplit edge becomes edge 2 (v2 -> v0)
        if len(idx):
            u = np.argmin(mark[idx], axis=1)
            v0, v1, v2 = f[idx, (u + 1) % 3], f[idx, (u + 2) % 3], f[idx, u]
            m01, m12 = mid[idx, (u + 1) % 3], mid[idx, (u + 2) % 3]
            first = np.linalg.norm(v[v0] - v[m12], axis=1) <= np.linalg.norm(v[m01] - v[v2], axis=1)
            ta = np.stack([m01, v1, m12], 1)
            tb = np.where(first[:, None], np.stack([v0, m01, m12], 1), np.stack([v0, m01, v2], 1))
            tc = np.where(first[:, None], np.stack([v0, m12, v2], 1), np.stack([m01, m12, v2], 1))
            out_f.append(np.concatenate([ta, tb, tc]))
            out_p.append(np.tile(parent[idx], 3))
        idx = np.nonzero(n == 3)[0]
        if len(idx):
            v0, v1, v2 = f[idx, 0], f[idx, 1], f[idx, 2]
            m01, m12, m20 = mid[idx, 0], mid[idx, 1], mid[idx, 2]
            out_f.append(
                np.concatenate(
                    [np.stack([v0, m01, m20], 1), np.stack([m01, v1, m12], 1), np.stack([m20, m12, v2], 1), np.stack([m01, m12, m20], 1)]
                )
            )
            out_p.append(np.tile(parent[idx], 4))
        f = np.concatenate([x for x in out_f if len(x)])
        parent = np.concatenate([x for x in out_p if len(x)])
    return trimesh.Trimesh(v, f, process=False), parent


def parse_fill_box(raw: str) -> tuple[Box, float]:
    """`x0,x1,y0,y1,z0,z1:AREA_MM2` -> (box, area)."""
    spec, sep, area = raw.rpartition(":")
    try:
        limit = float(area)
    except ValueError:
        limit = -1.0
    if not sep or limit <= 0:
        raise InvalidArgument("--fill-box", raw, ["x0,x1,y0,y1,z0,z1:AREA_MM2"], command=CMD)
    return parse_box(spec, "--fill-box"), limit


@dataclass(frozen=True)
class Classified:
    face_label: NDArray[np.int16]
    names: list[str]
    mesh: trimesh.Trimesh  # the (possibly split) skin the labels belong to
    n_split: int  # faces added by splitting


def _surface_distance(
    parts: Sequence[tuple[str, trimesh.Trimesh]], name: str, shift: Array, box: Box, points: Array
) -> Array:
    """Exact distance from `points` to the SURFACE of the parts called `name` (their faces within SURFACE_MARGIN_MM of
    `box` are used; inf when there are none)."""
    lo, hi = np.array(box[0::2]) - SURFACE_MARGIN_MM, np.array(box[1::2]) + SURFACE_MARGIN_MM
    pieces: list[trimesh.Trimesh] = []
    for n, m in parts:
        if n != name:
            continue
        c = np.asarray(m.triangles_center) - shift
        near = np.nonzero(np.all((c > lo) & (c < hi), axis=1))[0]
        if len(near):
            pieces.append(trimesh.Trimesh(np.asarray(m.vertices) - shift, np.asarray(m.faces)[near], process=False))
    if not pieces:
        return np.full(len(points), np.inf)
    _, dist, _ = trimesh.proximity.closest_point(trimesh.util.concatenate(pieces), points)
    return np.asarray(dist)


def classify_by_parts(
    skin: trimesh.Trimesh,
    parts: Sequence[tuple[str, trimesh.Trimesh]],
    tol: float,
    shift: tuple[float, float, float] = (0.0, 0.0, 0.0),
    split_edge: float | None = None,
    split_box: Box | None = None,
    surface_parts: Sequence[str] | None = None,
) -> Classified:
    """A skin face takes the name of the FIRST part (list order) one of whose face centroids lies within `tol` of the
    face centroid; -1 when no part does. Part meshes are in another frame by `shift` (subtracted).
    With `split_edge` the skin is refined first (`refine`, inside `split_box`, the whole skin when None): every new face
    inherits the label of the face it comes from, and inside the box the faces are re-judged for the parts in
    `surface_parts` (default: all): a face is of such a part when its centroid lies within `tol` of the part SURFACE
    (and no earlier listed part claimed it), whatever its parent's centroid said. That is what a giant face a finely
    cut part was cut out of needs; the other parts keep the parent's decision."""
    names: list[str] = []
    for n, _ in parts:
        if n not in names:
            names.append(n)
    judged = set(names if surface_parts is None else surface_parts)
    unknown = sorted(judged - set(names))
    if unknown:
        raise InvalidArgument("--surface-parts", ",".join(unknown), names, command=CMD)
    if surface_parts is not None and not split_edge:
        raise UsageError("--surface-parts needs --split-edge", command=CMD, remediation=["Add e.g. --split-edge 0.5."])
    off = np.asarray(shift)
    c0 = np.asarray(skin.triangles_center)
    near0: dict[str, Array] = {}  # per part: which original faces have a part face centroid within `tol`
    for name in names:
        pts = np.vstack([np.asarray(m.triangles_center) - off for n, m in parts if n == name])
        near0[name] = np.asarray(cKDTree(pts).query(c0, workers=-1)[0] <= tol)
    if not split_edge:
        lab0 = np.full(len(c0), -1, np.int16)
        for k, name in enumerate(names):
            lab0[near0[name] & (lab0 < 0)] = k  # an earlier part wins
        return Classified(lab0, names, skin, 0)
    fine, parent = refine(skin, split_edge, split_box)
    c = np.asarray(fine.triangles_center)
    if split_box is not None:
        box: Box = split_box
    else:
        lo, hi = c.min(axis=0) - 1.0, c.max(axis=0) + 1.0
        box = (float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]), float(lo[2]), float(hi[2]))
    inside = in_box(c, box)
    zi = np.nonzero(inside)[0]
    lab = np.full(len(c), -1, np.int16)
    for k, name in enumerate(names):
        claim = near0[name][parent]  # the parent's centroid decision
        if name in judged and len(zi):
            claim[zi] = _surface_distance(parts, name, off, box, c[zi]) <= tol  # replaced by the surface test in the box
        lab[claim & (lab < 0)] = k  # an earlier part wins
    return Classified(lab, names, fine, len(fine.faces) - len(skin.faces))


def write_ply_f64(m: trimesh.Trimesh, path: Path) -> None:
    """Binary PLY with double-precision vertices (trimesh's own writer stores float32): a refined skin that is read back
    for a nearest-face transfer must keep the centroids it was classified with, or faces equidistant from two source
    faces fall on the other side of the tie."""
    v = np.ascontiguousarray(np.asarray(m.vertices, "<f8"))
    f = np.asarray(m.faces, "<i4")
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(v)}\nproperty double x\nproperty double y\nproperty double z\n"
        f"element face {len(f)}\nproperty list uchar int vertex_indices\nend_header\n"
    )
    faces = np.zeros(len(f), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    faces["n"], faces["i"] = 3, f
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(v.tobytes())
        fh.write(faces.tobytes())


def classify_file(
    skin_path: str | Path,
    parts: Sequence[tuple[str, Path]],
    out: str | Path,
    *,
    tol: float,
    shift: tuple[float, float, float],
    split_edge: float | None,
    split_box: Box | None,
    surface_parts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`classify`: write the label file of `skin_path` by its parts; returns the report."""
    skin = load_mesh(skin_path)  # vertices merged: the refinement needs the neighbours to share vertices
    loaded: list[tuple[str, trimesh.Trimesh]] = []
    for name, p in parts:
        if not p.is_file():
            raise UsageError(f"part mesh not found: {p}", command=CMD)
        loaded.append((name, load_mesh(p, process=False)))
    res = classify_by_parts(skin, loaded, tol, shift, split_edge, split_box, surface_parts)
    out_p = Path(out)
    mesh_rel: str | None = None
    if split_edge:  # the split reorders the faces: the labels belong to the written mesh, even when nothing was long enough
        mesh_rel = out_p.with_suffix(".mesh.ply").name
        write_ply_f64(res.mesh, out_p.parent / mesh_rel)
    write_labels(out_p, res.face_label, res.names, digest=mesh_digest(res.mesh.triangles), mesh=mesh_rel)
    counts = {n: int((res.face_label == i).sum()) for i, n in enumerate(res.names)}
    return {
        "skin_faces": int(len(skin.faces)),
        "labelled_faces": int(len(res.face_label)),
        "split_faces": res.n_split,
        "counts": counts,
        "unlabelled": int((res.face_label < 0).sum()),
        "mesh": mesh_rel,
    }


# ---------------------------------------------------------------------------------------------- transfer
def transfer_file(
    mesh_path: str | Path, spec: str, out: str | Path, tol: float | None
) -> dict[str, Any]:
    """`transfer`: every label of `spec` (labels[@mesh]) onto the faces of `mesh_path`."""
    s = Surf(mesh_path)
    lab, names, stats = resolve_labels(spec, s, tol)
    write_labels(out, lab, names, digest=mesh_digest(s.mesh.triangles))
    stats["labels"] = {n: int((lab == i).sum()) for i, n in enumerate(names)}
    return stats


def _ids(names: list[str], wanted: Sequence[str], flag: str) -> list[int]:
    missing = [w for w in wanted if w not in names]
    if missing:
        raise InvalidArgument(flag, ",".join(missing), names, command=CMD)
    return [names.index(w) for w in wanted]


def fill_islands(
    s: Surf, lab: Array, appr_pos: Mask, d: Array, min_area: float, max_dist: float
) -> tuple[NDArray[np.int16], dict[str, int]]:
    """lab: 0 = positive, -1 = other. Other-class islands < `min_area` whose every neighbour face is positive, and
    positive islands < `min_area` whose every neighbour is other, are flipped, but ONLY when the nearest approved
    face does not itself have the island (see the module invariants)."""
    out = lab.copy()
    filled = {"other_in_positive": 0, "positive_in_other": 0}
    for cls, other, key in ((-1, 0, "other_in_positive"), (0, -1, "positive_in_other")):
        n, comp = s.components((out == cls) & s.outer)
        has = comp >= 0
        area = np.bincount(comp[has], weights=s.area[has], minlength=n)
        for k in np.nonzero(area < min_area)[0]:
            idx = np.nonzero(comp == k)[0]
            ring = s.ring(idx, comp, int(k))
            if len(ring) == 0 or not (out[ring] == other).all():
                continue
            approved_positive = bool(appr_pos[idx].mean() >= 0.5)
            if approved_positive == (cls == 0) and bool((d[idx] <= max_dist).all()):
                continue  # the approved build has this island too (its nearest faces are of the island's class): keep
            out[idx] = other
            filled[key] += 1
    return out, filled


def fill_box_islands(
    s: Surf, lab: Array, boxes: Sequence[tuple[Box, float]]
) -> tuple[NDArray[np.int16], int]:
    """Other-class islands under the area of a (box, mm2) pair, centred inside that box and with every neighbour
    positive, become positive. Unlike `fill_islands` this does NOT keep an island because the approved faces agree."""
    out = lab.copy()
    n, comp = s.components((out == -1) & s.outer)
    has = comp >= 0
    area = np.bincount(comp[has], weights=s.area[has], minlength=n)
    done = 0
    for box, limit in boxes:
        for k in np.nonzero((area < limit) & (area > 0))[0]:
            idx = np.nonzero(comp == k)[0]
            if out[idx[0]] != -1 or not in_box(s.c[idx].mean(0)[None, :], box)[0]:
                continue
            ring = s.ring(idx, comp, int(k))
            if len(ring) and (out[ring] == 0).all():
                out[idx] = 0
                done += 1
    return out, done


def transfer_class(
    new: Surf,
    source: trimesh.Trimesh,
    source_labels: Array,
    names: list[str],
    *,
    positive: Sequence[str],
    exclude: Sequence[str],
    max_dist: float,
    island_mm2: float,
    fill_boxes: Sequence[tuple[Box, float]],
    match: str = "centroid",
    spacing: float = TRANSFER_SPACING_MM,
) -> tuple[NDArray[np.int16], dict[str, Any]]:
    """One class of the source mesh onto the faces of `new` -> (labels: 0 = positive, -1 = other; report).
    `match`: `centroid` = the nearest source face by centroid (the default), `surface` = the nearest source surface point."""
    if match not in MATCH_MODES:
        raise InvalidArgument("--match", match, list(MATCH_MODES), command=CMD)
    pos_ids = _ids(names, positive, "--positive")
    exc_ids = _ids(names, exclude, "--exclude")
    usable = ~np.isin(source_labels, exc_ids)
    if not usable.any():
        raise UsageError("every source face is excluded", command=CMD, remediation=["Check --exclude."])
    sub = submesh(source, np.nonzero(usable)[0])
    is_pos_face = np.isin(source_labels[usable], pos_ids)
    if match == "centroid":  # the nearest approved FACE (by centroid), what the approved build's borders were made of
        d, j = cKDTree(np.asarray(sub.triangles_center)).query(new.c, workers=-1)
        appr_pos = is_pos_face[j]
    else:  # the nearest approved SURFACE point: independent of how the approved faces are sized
        pts, fid = sample_surface(sub, spacing)
        d, j = cKDTree(pts).query(new.c, workers=-1)
        appr_pos = is_pos_face[fid[j]]
    lab = np.where(appr_pos, 0, -1).astype(np.int16)
    beyond = d > max_dist
    lab[beyond] = -1
    lab, filled = fill_islands(new, lab, appr_pos, d, island_mm2, max_dist)
    lab, n_box = fill_box_islands(new, lab, fill_boxes)
    filled["box_islands"] = n_box
    report: dict[str, Any] = {
        "source_faces": int(len(source.faces)),
        "source_positive_faces": int(is_pos_face.sum()),
        "source_excluded_faces": int((~usable).sum()),
        "source_positive_area_mm2": round(float(sub.area_faces[is_pos_face].sum()), 1),
        "new_faces": int(new.nf),
        "new_positive_faces": int((lab == 0).sum()),
        "new_positive_area_mm2": round(float(new.area[lab == 0].sum()), 1),
        "dist_p50": round(float(np.percentile(d, 50)), 3),
        "dist_p95": round(float(np.percentile(d, 95)), 3),
        "dist_max": round(float(d.max()), 3),
        "beyond_max_dist": int(beyond.sum()),
        "match": match,
        "filled_islands": filled,
    }
    return lab, report


def transfer_class_file(
    mesh_path: str | Path,
    source_spec: str,
    out: str | Path,
    *,
    positive: Sequence[str],
    exclude: Sequence[str],
    as_name: str,
    max_dist: float,
    island_mm2: float,
    fill_boxes: Sequence[tuple[Box, float]],
    match: str = "centroid",
) -> dict[str, Any]:
    """`transfer-class`: write the one-label file of the positive class on the faces of `mesh_path`."""
    src = load_source(source_spec)
    if src.mesh is None:
        raise UsageError(
            f"{src.path}: the source labels need their mesh",
            command=CMD,
            remediation=["Use --source labels.npz@source_mesh.stl, or a `mesh` key in the label file."],
        )
    lf = src.labels
    new = Surf(mesh_path)
    lab, report = transfer_class(
        new,
        src.mesh,
        lf.face_label,
        lf.names,
        positive=positive,
        exclude=exclude,
        max_dist=max_dist,
        island_mm2=island_mm2,
        fill_boxes=fill_boxes,
        match=match,
    )
    write_labels(out, lab, [as_name], digest=mesh_digest(new.mesh.triangles))
    return report
