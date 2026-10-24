"""labels.py — per-face label files, nearest-face transfer between meshes, speck hygiene.

WHAT: the label-file contract (read/write), `resolve_labels` (labels of any mesh -> labels of the target mesh:
  identity when it is the same mesh, nearest-face transfer otherwise), `nearest_face_labels` (the transfer itself) and
  `clean_islands` (label islands and holes smaller than a nozzle line are merged into the surround).

LABEL FILE CONTRACT (`.npz`, or `.json` with the same keys):
  face_label   integer id per face of the labelled mesh; -1 = unlabelled (the default colour)
  names        JSON list, label id -> name
  mesh_digest  optional: sha1 of that mesh's float32 face triangles (n, 3, 3) in face order (`mesh_digest()`),
               independent of how vertices are merged or numbered
  mesh         optional: path of the labelled mesh, relative to the label file
  The mesh the labels belong to is, in this order: the `@MESH` of a `labels@MESH` spec, else the `mesh` key, else the
  TARGET mesh itself when the face count matches (and, with a `mesh_digest`, when the digest matches).

INVARIANTS:
  * A transferred label counts only where the label mesh lies within `tol` mm of the target skin: every target face takes
    the label of the label-mesh face whose surface point is nearest, and -1 when that point is farther than `tol`.
  * A label count that does not match its mesh, ids outside `-1..len(names)-1` (checked BEFORE they are narrowed to
    int16, so a wrapped id cannot pass), or a `mesh_digest` that is not the digest of the mesh the labels are used with
    (declared mesh included) is an error (exit 2), never a silent truncation, padding or transfer from the wrong geometry.
ASSUMPTIONS: both meshes are in the same coordinate frame (the transfer is geometric, not topological).
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from colour_regions import CMD
from colour_regions.surface import Array, Surf, load_mesh, sample_surface
from errors import UsageError

#: label surface sampling pitch of the transfer (mm): the distance error is <= ~spacing / 1.4 whatever the triangle size
TRANSFER_SPACING_MM = 0.1
#: a label whose whole area is below this many `island_mm2` is a feature (a toe bean, a slit pupil), not a speck
TINY_LABEL_FACTOR = 4
CLEAN_ROUNDS = 3


@dataclass(frozen=True)
class LabelFile:
    face_label: NDArray[np.int16]
    names: list[str]
    digest: str | None  # the file's `mesh_digest`
    mesh: str | None  # the file's `mesh` key: relative to the file


def mesh_digest(triangles: Array) -> str:
    """sha1 of the float32 array of face triangles (n, 3, 3) in face order."""
    return hashlib.sha1(np.ascontiguousarray(np.asarray(triangles, np.float32)).tobytes()).hexdigest()


def parse_spec(spec: str) -> tuple[Path, Path | None]:
    """`labels.npz` or `labels.npz@mesh.stl` -> (labels path, mesh path or None), both absolute."""
    path, _, mesh = spec.partition("@")
    return Path(path).expanduser().resolve(), (Path(mesh).expanduser().resolve() if mesh else None)


def _names_of(raw: Any, where: str) -> list[str]:
    value = raw
    if isinstance(raw, np.ndarray):
        if raw.ndim == 0:
            value = raw.item()
            if isinstance(value, bytes):
                value = value.decode()
        else:
            value = [str(x) for x in raw]
            if len(value) == 1 and value[0].lstrip().startswith("["):
                value = value[0]
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise UsageError(
            f"{where}: `names` must be a JSON list of label names (id -> name)",
            command=CMD,
            remediation=['Write names as e.g. ["iris", "pupil"]: label id 0 is "iris".'],
        )
    return list(value)


def _checked_ids(raw: Any, names: list[str], where: str) -> NDArray[np.int16]:
    """Integer label ids -> int16, validated in the file's own width first."""
    ids = np.asarray(raw)
    if ids.ndim != 1:
        raise UsageError(f"{where}: `face_label` must be one id per face (1-D), got shape {ids.shape}", command=CMD)
    if ids.size and not np.issubdtype(ids.dtype, np.integer):
        raise UsageError(f"{where}: `face_label` must hold integer ids, got dtype {ids.dtype}", command=CMD)
    if len(names) > np.iinfo(np.int16).max:
        raise UsageError(f"{where}: {len(names)} names do not fit the int16 label ids", command=CMD)
    if ids.size and (ids.max() >= len(names) or ids.min() < -1):
        raise UsageError(
            f"{where}: face_label ids outside -1..{len(names) - 1} ({len(names)} names)",
            command=CMD,
            remediation=["Every id indexes `names`; -1 means unlabelled."],
        )
    return ids.astype(np.int16)


def read_labels(path: str | Path) -> LabelFile:
    """Read a label file (.npz or .json) and validate ids against its names."""
    p = Path(path)
    if not p.is_file():
        raise UsageError(
            f"label file not found: {p}",
            command=CMD,
            remediation=["Pass `labels.npz` or `labels.npz@mesh.stl`; see `3d colour-regions --help` for the format."],
        )
    if p.suffix == ".json":
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UsageError(f"{p}: not valid JSON ({exc})", command=CMD) from exc
        if not isinstance(raw, dict) or "face_label" not in raw or "names" not in raw:
            raise UsageError(f"{p}: a .json label file needs `face_label` and `names`", command=CMD)
        names = _names_of(raw["names"], str(p))
        return LabelFile(_checked_ids(raw["face_label"], names, str(p)), names, raw.get("mesh_digest"), raw.get("mesh"))
    with np.load(p, allow_pickle=False) as z:
        for key in ("face_label", "names"):
            if key not in z.files:
                raise UsageError(f"{p}: label file has no `{key}` array (has: {', '.join(z.files)})", command=CMD)
        names = _names_of(z["names"], str(p))
        return LabelFile(
            _checked_ids(z["face_label"], names, str(p)),
            names,
            str(z["mesh_digest"]) if "mesh_digest" in z.files else None,
            str(z["mesh"]) if "mesh" in z.files else None,
        )


def write_labels(
    path: str | Path,
    face_label: Array,
    names: list[str],
    *,
    digest: str | None = None,
    mesh: str | None = None,
    **extra: Array,
) -> Path:
    """Write a label file (`.json` by suffix, else `.npz`). `extra` arrays are stored next to the contract keys (npz only)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fl = np.asarray(face_label).astype(np.int16)
    if p.suffix == ".json":
        body: dict[str, Any] = {"face_label": fl.tolist(), "names": names}
        if digest:
            body["mesh_digest"] = digest
        if mesh:
            body["mesh"] = mesh
        p.write_text(json.dumps(body), encoding="utf-8")
        return p
    arrays: dict[str, Any] = {"face_label": fl, "names": np.array(json.dumps(names))}
    if digest:
        arrays["mesh_digest"] = np.array(digest)
    if mesh:
        arrays["mesh"] = np.array(mesh)
    arrays.update(extra)
    np.savez(p, **arrays)
    return p


@dataclass(frozen=True)
class LabelSource:
    """The labels of a `labels[@mesh]` spec and the mesh they were made on (None: the target mesh itself)."""

    path: Path
    labels: LabelFile
    mesh: trimesh.Trimesh | None


def load_source(spec: str) -> LabelSource:
    """Read a `labels[@mesh]` spec. A declared label mesh (the `@MESH`, else the file's `mesh` key) is loaded with its
    face order kept and must have as many faces as there are labels and, when the file carries a `mesh_digest`, that digest."""
    path, mesh_path = parse_spec(spec)
    lf = read_labels(path)
    if mesh_path is None and lf.mesh is not None:
        mesh_path = (path.parent / lf.mesh).resolve()
    if mesh_path is None:
        return LabelSource(path, lf, None)
    if not mesh_path.is_file():
        raise UsageError(f"label mesh not found: {mesh_path}", command=CMD)
    lm = load_mesh(mesh_path, process=False)
    if len(lm.faces) != len(lf.face_label):
        raise UsageError(
            f"{path}: {len(lf.face_label)} face labels but {mesh_path} has {len(lm.faces)} faces",
            command=CMD,
            remediation=["The labels must be one id per face of the mesh they were made on."],
        )
    if lf.digest is not None and lf.digest != mesh_digest(lm.triangles):
        raise UsageError(
            f"{path}: mesh_digest differs from the triangles of {mesh_path}: these labels belong to another mesh",
            command=CMD,
            remediation=["Pass the mesh the labels were made on (same face order), not a copy with other geometry."],
        )
    return LabelSource(path, lf, lm)


def _identity_stats(src: LabelSource, s: Surf) -> dict[str, Any]:
    """Labels used face for face on the target: the face count must match, and a `mesh_digest` must be the target's."""
    lf = src.labels
    if len(lf.face_label) != s.nf:
        raise UsageError(
            f"{src.path}: {len(lf.face_label)} face labels but the geometry has {s.nf} faces and no label mesh was given",
            command=CMD,
            remediation=[
                "Use 'labels.npz@mesh.stl' (the mesh the labels were made on) or add a `mesh` key to the label file.",
            ],
        )
    if lf.digest is None:
        print(
            f"  note: {src.path.name} has no mesh_digest, identity transfer is trusted on the face count alone",
            file=sys.stderr,
        )
        return {"mode": "identity", "faces": int(s.nf), "digest_checked": False}
    if lf.digest != mesh_digest(s.mesh.triangles):
        raise UsageError(
            f"{src.path}: mesh_digest differs from the geometry's triangles: these labels belong to another mesh",
            command=CMD,
            remediation=[f"Pass '{src.path.name}@its_mesh.stl' for a nearest-face transfer onto this mesh."],
        )
    return {"mode": "identity", "faces": int(s.nf), "digest_checked": True}


def resolve_labels(
    spec: str, s: Surf, tol: float | None
) -> tuple[NDArray[np.int16], list[str], dict[str, Any]]:
    """Labels of the spec's mesh as one id per face of `s` -> (labels, names, transfer stats)."""
    src = load_source(spec)
    fl = src.labels.face_label
    if src.mesh is None:
        return fl, src.labels.names, _identity_stats(src, s)
    if len(src.mesh.faces) == s.nf and np.abs(np.asarray(src.mesh.triangles) - np.asarray(s.mesh.triangles)).max() < 1e-4:
        return fl, src.labels.names, {"mode": "identity", "faces": int(s.nf)}
    out, stats = nearest_face_labels(src.mesh, fl, s.c, tol)
    return out, src.labels.names, stats


def nearest_face_labels(
    label_mesh: trimesh.Trimesh,
    face_label: Array,
    targets: Array,
    tol: float | None,
    spacing: float = TRANSFER_SPACING_MM,
) -> tuple[NDArray[np.int16], dict[str, Any]]:
    """Nearest-face transfer of `face_label` (one id per face of `label_mesh`) onto the points `targets` (face
    centroids of the target mesh): each takes the label of the label-mesh face whose surface point is nearest;
    -1 where that point is farther than `tol` (no limit when `tol` is None)."""
    pts, fid = sample_surface(label_mesh, spacing)
    d, i = cKDTree(pts).query(targets, workers=-1)
    out = np.asarray(face_label)[fid[i]].astype(np.int16)
    far = np.zeros(len(out), bool) if tol is None else d > tol
    out[far] = -1
    stats = {
        "mode": "nearest-face",
        "label_faces": int(len(label_mesh.faces)),
        "faces": int(len(targets)),
        "dist_p50": float(np.percentile(d, 50)),
        "dist_p95": float(np.percentile(d, 95)),
        "dist_max": float(d.max()),
        "dropped_far": int(far.sum()),
    }
    return out, stats


def clean_islands(s: Surf, group: Array, lab: Array, min_area: float) -> Array:
    """Per-face group id (-1 = default): every connected piece of one id smaller than `min_area` (mm2) takes the id
    most of its border faces have, except pieces of a COLOURED group made of a LABEL whose whole area is below
    `TINY_LABEL_FACTOR` * `min_area` (a toe bean, a tear corner is the feature itself, not a speck). A default-colour
    piece that small is a speck like any other (the behaviour of the proven Chekhov build: protecting those too changes
    ~50 faces of its eye groups). Repeats until stable (`CLEAN_ROUNDS` rounds at most)."""
    group = group.copy()
    has = lab >= 0
    lab_area = np.bincount(lab[has], weights=s.area[has])
    tiny_label = np.zeros(len(lab), bool)
    tiny_label[has] = lab_area[lab[has]] < TINY_LABEL_FACTOR * min_area
    for _ in range(CLEAN_ROUNDS):
        changed = 0
        for g in np.unique(group[s.outer]):
            n, comp_lab = s.components((group == g) & s.outer)
            sel = comp_lab >= 0
            areas = np.bincount(comp_lab[sel], weights=s.area[sel], minlength=n)
            for k in np.nonzero(areas < min_area)[0]:
                comp = np.nonzero(comp_lab == k)[0]
                if g >= 0 and tiny_label[comp].mean() > 0.5:  # (specks of -1 absorbed earlier in the round must not unprotect it)
                    continue
                nb = s.ring(comp, comp_lab, int(k))
                if len(nb) == 0:
                    continue
                vals, cnt = np.unique(group[nb], return_counts=True)
                group[comp] = vals[np.argmax(cnt)]
                changed += 1
        if not changed:
            break
    return group
