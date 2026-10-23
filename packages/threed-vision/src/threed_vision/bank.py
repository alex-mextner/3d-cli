"""Reference bank: lie-flat thumbnails + silhouettes from meshes, an embedding index on disk, and retrieval.

  build_thumbnails(src, out_dir)   meshes (or a hand-made bank.json) -> thumbnails + silhouettes + `bank.json`
  build_index(bank_json, out_dir)  CLIP + DINOv2 embeddings and silhouette descriptors -> `index.npz` + `index.json`
  query(index_dir, crop, topk)     top-k bank entries for one object (index read from files: no network, no models needed to load)

A bank thumbnail is the object lying flat, seen along its SHORTEST axis with the LONGEST axis vertical in the image (the way a
tool lies on a table), at `mm_per_px` millimetres per pixel, plus `extents_mm` = [L, W, T] sorted descending.

TEXTURE MATTERS. CLIP / DINOv2 compare appearance. Thumbnails rendered from meshes without colour are flat grey-blue shapes and
retrieve badly (spike: top-3 of only 0.30-0.45 against photographed tools). Give the bank meshes that carry colour (vertex / face
colours, textured glb / obj / ply / 3mf) or supply real photo thumbnails through a hand-made `bank.json`. `build_thumbnails`
records `textured` per entry and for the whole bank (`bank.json`: `"textured": false` + `"warning"` when any entry fell back to
shaded grey) and `query(metric="auto")` relies on the silhouette descriptor alone when the bank is mostly grey.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np

from .clip import PAD_RGB, ClipEncoder, DinoEncoder, crop_masked
from .errors import BadInput, MissingDependency

MESH_SUFFIXES = (".stl", ".obj", ".ply", ".glb", ".gltf", ".3mf", ".off")
VIEWS = ("longest-axis", "top", "iso")
METRICS = ("auto", "clip", "dino", "sil", "clip+dino", "all")
BASE_RGB = (150, 170, 200)  # colour of meshes that carry none
BG_RGB = (235, 235, 235)
SIL_SIZE = 96
BANK_VERSION = 1
# An object is embedded as it lies and rotated by 180 degrees: which end is "up" is ambiguous; the better view wins.
VIEWS_PER_OBJECT = 2
MIN_TEXTURED_FRACTION = 0.5  # `metric="auto"`: appearance embeddings only when at least this share of the bank has colour
# Material colours trimesh assigns when a file names none: not evidence that the mesh carries colour.
DEFAULT_MATERIAL_COLORS = {(102, 102, 102), (255, 255, 255), (128, 128, 128)}

# Bank entry names -> the default recognition vocabulary (first matching rule wins; needles match whole `_`-separated words).
# Only a convenience for the 22 default categories; a bank with its own vocabulary ships a `categories.json` {name: category}
# (or hand-writes `category` in bank.json).
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("screwdriver", ("screwdriver",)),
    ("pliers", ("pliers", "plier")),
    ("wire stripper", ("crimper", "stripper")),
    ("roll of tape", ("tape_roll", "insulation_tape", "tape_reel", "roll_of_tape")),
    ("tape measure", ("tape_measure", "measuring_tape")),
    ("wrench", ("wrench", "spanner")),
    ("scissors", ("scissors", "shears")),
    ("tin snips", ("snips",)),
    ("battery", ("battery",)),
    ("bit set", ("bit_set", "bitset")),
    ("cordless drill", ("drill",)),
    ("hammer", ("hammer",)),
    ("caliper", ("caliper", "calliper")),
    ("multimeter", ("multimeter",)),
    ("hex key set", ("hex_key", "allen")),
    ("clamp", ("clamp",)),
    ("saw", ("saw",)),
    ("utility knife", ("knife", "cutter")),
    ("glue", ("glue",)),
    ("power supply", ("power_supply", "psu")),
    ("box", ("charger",)),
)


def category_from_name(name: str) -> str:
    key = "_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + "_"
    for category, needles in CATEGORY_RULES:
        if any(f"_{n}_" in key for n in needles):
            return category
    return "other"


# --- rendering ------------------------------------------------------------------------------------------------


def _camera_basis(forward: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Rows (right, down, forward): x right, y down, z away from the camera (right-handed, OpenCV convention)."""
    f = forward / np.linalg.norm(forward)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    return np.stack([r, d, f])


def view_matrix(extents: np.ndarray, view: str) -> np.ndarray:
    """3x3 rotation mapping centred mesh coordinates to (u right, v down, w away) of the thumbnail view."""
    if view == "longest-axis":
        longest, mid, shortest = (int(i) for i in np.argsort(-extents, kind="stable"))
        eye = np.eye(3)
        m = np.stack([eye[mid], eye[longest], eye[shortest]])
        if np.linalg.det(m) < 0:  # keep it a rotation: never mirror the object
            m[0] = -m[0]
        return m
    if view == "top":
        return _camera_basis(np.array([0.0, 0.0, -1.0]), np.array([0.0, 1.0, 0.0]))
    if view == "iso":
        return _camera_basis(np.array([-1.0, 1.0, -1.0]), np.array([0.0, 0.0, 1.0]))
    raise BadInput(f"unknown view '{view}'", f"one of: {', '.join(VIEWS)}")


@dataclass
class Rendered:
    image: np.ndarray  # (px, px, 3) uint8 RGB
    silhouette: np.ndarray  # (px, px) uint8 0/255
    mm_per_px: float
    extents_mm: list[float]
    textured: bool


def _texture_face_colors(mesh: Any, vis: Any) -> np.ndarray | None:
    """RGB per face sampled from the texture image at the face-centroid UV (None when there is no image or no UV)."""
    material = getattr(vis, "material", None)
    image = getattr(material, "image", None) or getattr(
        material, "baseColorTexture", None
    )
    uv = getattr(vis, "uv", None)
    if image is None or uv is None or len(uv) != len(mesh.vertices):
        return None
    tex = np.asarray(image.convert("RGB"))
    th, tw = tex.shape[:2]
    face_uv = np.asarray(uv, np.float64)[np.asarray(mesh.faces)].mean(axis=1) % 1.0
    xs = np.clip((face_uv[:, 0] * (tw - 1)).round().astype(int), 0, tw - 1)
    ys = np.clip(((1.0 - face_uv[:, 1]) * (th - 1)).round().astype(int), 0, th - 1)
    return np.asarray(tex[ys, xs], np.uint8)


def _material_color(vis: Any) -> tuple[int, int, int] | None:
    """The flat colour a material carries (glTF `baseColorFactor`, OBJ `Kd`), None when it is trimesh's default."""
    color = getattr(getattr(vis, "material", None), "main_color", None)
    if color is None:
        return None
    rgb = tuple(int(c) for c in np.asarray(color)[:3])
    return None if rgb in DEFAULT_MATERIAL_COLORS else (rgb[0], rgb[1], rgb[2])


def _face_colors(mesh: Any) -> tuple[np.ndarray, bool]:
    """(n_faces, 3) uint8 colours and whether the mesh carried any (texture, material, vertex or face colours)."""
    n = len(mesh.faces)
    try:
        vis = mesh.visual
        if getattr(vis, "kind", None) == "texture":
            sampled = _texture_face_colors(mesh, vis)
            if sampled is not None:
                return sampled, True
            flat = _material_color(vis)
            if flat is not None:
                return np.tile(np.array(flat, np.uint8), (n, 1)), True
        elif getattr(vis, "kind", None) in ("vertex", "face") and getattr(
            vis, "defined", False
        ):
            fc = np.asarray(vis.face_colors)[:, :3].astype(np.uint8)
            if fc.shape[0] == n:
                return fc, True
    except (AttributeError, ValueError, TypeError, IndexError):
        pass  # an unreadable visual is "no colour": the entry is flagged textured=false and the bank warns
    return np.tile(np.array(BASE_RGB, np.uint8), (n, 1)), False


def render_mesh(
    mesh: Any, view: str = "longest-axis", px: int = 192, unit_scale: float = 1.0
) -> Rendered:
    """Painter's-algorithm render of a trimesh mesh: face colours (texture-sampled when textured), depth-sorted, shaded by
    |normal . view|. `unit_scale` converts mesh units to millimetres (`mm_per_px` and `extents_mm` are in mm)."""
    import cv2

    if px < 32:
        raise BadInput("--px must be at least 32")
    if unit_scale <= 0:
        raise BadInput("--unit-scale must be positive")
    verts = np.asarray(mesh.vertices, np.float64)
    faces = np.asarray(mesh.faces, np.int64)
    if len(faces) == 0:
        raise BadInput("mesh has no faces")
    extents = np.asarray(mesh.extents, np.float64)
    centre = np.asarray(mesh.bounds, np.float64).mean(0)
    m = view_matrix(extents, view)
    p = (verts - centre) @ m.T
    uv_min, uv_max = p[:, :2].min(0), p[:, :2].max(0)
    span = float(max((uv_max - uv_min).max(), 1e-9))
    scale = px * 0.9 / span
    xy = (p[:, :2] - (uv_min + uv_max) / 2) * scale + px / 2
    normals = np.asarray(mesh.face_normals, np.float64) @ m.T
    shade = np.clip(0.35 + 0.65 * np.abs(normals[:, 2]), 0, 1)
    colors, textured = _face_colors(mesh)
    shaded = (colors.astype(np.float64) * shade[:, None]).astype(np.uint8)
    depth = p[faces, 2].mean(1)
    tris = np.round(xy[faces] * 16).astype(np.int32)  # sub-pixel fixed point (shift=4)
    img = np.full((px, px, 3), BG_RGB, np.uint8)
    sil = np.zeros((px, px), np.uint8)
    for i in np.argsort(-depth, kind="stable"):  # far to near
        cv2.fillConvexPoly(
            img,
            tris[i],
            tuple(int(c) for c in shaded[i]),
            lineType=cv2.LINE_AA,
            shift=4,
        )
        cv2.fillConvexPoly(sil, tris[i], 255, shift=4)
    return Rendered(
        img,
        sil,
        unit_scale / scale,
        [round(float(x) * unit_scale, 1) for x in sorted(extents, reverse=True)],
        textured,
    )


def _load_mesh(path: Path) -> Any:
    try:
        import trimesh
    except ImportError as exc:
        raise MissingDependency(
            "trimesh is not installed (needed to render bank thumbnails)",
            "uv pip install 'packages/threed-vision[bank]'",
        ) from exc
    try:
        mesh = trimesh.load(path, force="mesh")
    except Exception as exc:  # noqa: BLE001 - trimesh raises many types for bad files
        raise BadInput(f"cannot read mesh {path}: {exc}") from exc
    if not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        raise BadInput(f"{path} has no triangles")
    return mesh


def _stem_id(path: Path, taken: set[str]) -> str:
    """A file-stem id whose `ID.png` and `ID_sil.png` collide with no other entry's files (`x.stl` vs `x_sil.stl`)."""
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)
    ident, n = base, 2
    while {f"{ident}.png", f"{ident}_sil.png"} & taken:
        ident, n = f"{base}_{n}", n + 1
    taken.update({f"{ident}.png", f"{ident}_sil.png"})
    return ident


def _read_categories_map(src_dir: Path, explicit: str | Path | None) -> dict[str, str]:
    cand = Path(explicit) if explicit else src_dir / "categories.json"
    if not cand.is_file():
        if explicit:
            raise BadInput(f"categories map not found: {cand}")
        return {}
    try:
        data = json.loads(cand.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BadInput(f"{cand} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise BadInput(f"{cand} must be a JSON object {{name: category}}")
    return data


def build_thumbnails(
    src: str | Path,
    out_dir: str | Path,
    view: str = "longest-axis",
    px: int = 192,
    *,
    categories: str | Path | None = None,
    unit_scale: float = 1.0,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Render every mesh under `src` (or re-base a hand-made `bank.json`) into `out_dir`; returns `out_dir/bank.json`.

    Per entry: `{"id","name","category","thumb","sil","extents_mm","mm_per_px","textured"}` (paths relative to bank.json); the
    document carries `"textured"` (every entry has colour) and a `"warning"` when some entry fell back to shaded grey.
    `categories` is an optional JSON `{name_or_id: category}` (default: `categories.json` next to the meshes, else name rules).
    """
    import cv2

    src_p, out = Path(src), Path(out_dir)
    if view not in VIEWS:
        raise BadInput(f"unknown view '{view}'", f"one of: {', '.join(VIEWS)}")
    if src_p.is_file() and src_p.suffix == ".json":
        out.mkdir(parents=True, exist_ok=True)
        return _rebase_bank(src_p, out)
    if not src_p.is_dir():
        raise BadInput(
            f"bank source not found: {src_p}",
            "a directory of meshes (stl/obj/ply/glb/3mf) or a bank.json",
        )
    meshes = sorted(
        p for p in src_p.rglob("*") if p.suffix.lower() in MESH_SUFFIXES and p.is_file()
    )
    if not meshes:
        raise BadInput(f"no meshes ({', '.join(MESH_SUFFIXES)}) under {src_p}")
    out.mkdir(parents=True, exist_ok=True)
    cmap = _read_categories_map(src_p, categories)
    taken: set[str] = set()
    entries: list[dict[str, Any]] = []
    for path in meshes:
        rendered = render_mesh(_load_mesh(path), view, px, unit_scale)
        ident = _stem_id(path, taken)
        thumb, sil = f"{ident}.png", f"{ident}_sil.png"
        cv2.imwrite(str(out / thumb), cv2.cvtColor(rendered.image, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(out / sil), rendered.silhouette)
        entries.append(
            {
                "id": ident,
                "name": path.stem,
                "category": cmap.get(ident)
                or cmap.get(path.stem)
                or category_from_name(path.stem),
                "thumb": thumb,
                "sil": sil,
                "extents_mm": rendered.extents_mm,
                "mm_per_px": round(rendered.mm_per_px, 5),
                "textured": rendered.textured,
            }
        )
        if progress:
            progress(ident)
    return _write_bank(out, entries, view, px)


def _write_bank(out: Path, entries: list[dict[str, Any]], view: str, px: int) -> Path:
    n_textured = sum(1 for e in entries if e.get("textured"))
    doc: dict[str, Any] = {
        "version": BANK_VERSION,
        "view": view,
        "px": px,
        "textured": n_textured == len(entries),
        "n_textured": n_textured,
        "entries": entries,
    }
    if n_textured < len(entries):
        doc["warning"] = (
            f"{len(entries) - n_textured} of {len(entries)} entries are untextured shaded-grey renders: CLIP/DINOv2 image retrieval "
            "needs colour (spike: grey renders give top-3 of only 0.30-0.45). Use meshes with colours/textures or real photo thumbnails."
        )
    path = out / "bank.json"
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return path


def _inside(base: Path, rel: str, what: str) -> Path:
    """`base / rel`, refusing absolute paths and `..` so a bank.json can neither read nor write outside its directory."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise BadInput(
            f"{what}: path '{rel}' must be relative and stay inside the bank directory"
        )
    return base / p


def _copy_into(src_base: Path, dst_base: Path, rel: str, what: str) -> None:
    """Copy `src_base/rel` to `dst_base/rel` (relative layout kept, so equal file names in different folders never collide;
    an existing copy is replaced so a rebuild never leaves stale thumbnails)."""
    src, dst = _inside(src_base, rel, what), _inside(dst_base, rel, what)
    if not src.is_file():
        raise BadInput(f"{what}: file missing: {src}")
    if src.resolve() != dst.resolve():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _rebase_bank(bank_json: Path, out: Path) -> Path:
    """Copy a hand-made bank (real photo thumbnails + optional silhouettes) next to a normalised bank.json."""
    doc = read_bank(bank_json)
    base = bank_json.parent if bank_json.is_file() else bank_json
    entries = []
    for e in doc["entries"]:
        for key in ("thumb", "sil"):
            if e.get(key):
                _copy_into(base, out, e[key], f"{bank_json}: entry '{e['id']}' {key}")
        entries.append(dict(e))
    return _write_bank(out, entries, doc["view"], doc["px"])


def _check_entry(path: Path, e: dict[str, Any]) -> None:
    """Field types of one bank.json entry (a hand-made file must fail here, as BadInput, not later as a TypeError)."""
    who = f"{path}: entry '{e['id']}'"
    for key in ("id", "thumb", "sil", "name", "category"):
        if key in e and e[key] is not None and not isinstance(e[key], str):
            raise BadInput(f"{who}: '{key}' must be a string")
    if "thumb" not in e:
        raise BadInput(f"{who} has no 'thumb'")
    ext = e.get("extents_mm")
    if ext is not None and not (
        isinstance(ext, list)
        and len(ext) == 3
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ext)
    ):
        raise BadInput(f"{who}: 'extents_mm' must be a list of 3 numbers [L, W, T]")
    mm = e.get("mm_per_px")
    if mm is not None and (
        isinstance(mm, bool) or not isinstance(mm, (int, float)) or mm <= 0
    ):
        raise BadInput(f"{who}: 'mm_per_px' must be a positive number")
    if "textured" in e and not isinstance(e["textured"], bool):
        raise BadInput(f"{who}: 'textured' must be true or false")


def read_bank(bank_json: str | Path) -> dict[str, Any]:
    """Validated bank.json document: every entry has `id` and `thumb`; `name`, `category` and `textured` are defaulted
    (entries of a hand-made bank are assumed to be real photos, i.e. textured)."""
    path = Path(bank_json)
    if path.is_dir():
        path = path / "bank.json"
    if not path.is_file():
        raise BadInput(
            f"bank.json not found: {path}",
            "build one with `3d bank thumbs <mesh-dir> -o DIR`",
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BadInput(f"{path} is not valid JSON: {exc}") from exc
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise BadInput(f"{path} has no entries")
    ids = [e.get("id") if isinstance(e, dict) else None for e in entries]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        raise BadInput(f"{path}: every entry needs a unique 'id'")
    for e in entries:
        _check_entry(path, e)
        if e.get("name") is None:
            e["name"] = e["id"]
        if e.get("category") is None:
            e["category"] = category_from_name(str(e["name"]))
        e.setdefault("textured", True)
    if (
        not isinstance(doc.get("view", "supplied"), str)
        or isinstance(doc.get("px", 0), bool)
        or not isinstance(doc.get("px", 0), int)
    ):
        raise BadInput(f"{path}: 'view' must be a string and 'px' an integer")
    doc.setdefault("view", "supplied")
    doc.setdefault("px", 0)
    return dict(doc)


# --- object views (shared by the index build and by queries) ---------------------------------------------------------


def silhouette_descriptor(mask: np.ndarray, size: int = SIL_SIZE) -> np.ndarray:
    """Mask -> (size, size) bool in its principal-axis frame: longest axis vertical, scaled to fit (aspect preserved).

    Invariant to translation, scale and (up to the sign ambiguity `silhouette_variants` covers) rotation of the mask.
    """
    import cv2

    ys, xs = np.nonzero(mask)
    if xs.size < 3:
        return np.zeros((size, size), bool)
    x0, y0 = int(xs.min()), int(ys.min())
    pts = np.stack([xs - x0, ys - y0], axis=1).astype(np.float64)
    centre = pts.mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov((pts - centre).T))
    major, minor = vecs[:, 1], vecs[:, 0]
    along, across = (pts - centre) @ major, (pts - centre) @ minor
    length = max(float(np.ptp(along)), 1.0)
    # pivot = middle of the object's extent (not its centre of mass), so a lopsided tool (heavy handle, light blade) is not clipped
    pivot = (
        centre
        + major * (along.min() + along.max()) / 2
        + minor * (across.min() + across.max()) / 2
    )
    scale = size * 0.9 / length
    # maps a pixel of the crop to the descriptor frame: x' = minor . (p - pivot), y' = major . (p - pivot), scaled to fit
    a = np.stack([minor, major]) * scale
    offset = size / 2 - a @ pivot
    crop: np.ndarray = (
        mask[y0 : int(ys.max()) + 1, x0 : int(xs.max()) + 1].astype(np.uint8)
    ) * 255
    if scale < 0.5:
        # area-average first: a plain warp would alias thin parts away
        h, w = crop.shape
        pre = scale * 2.0
        # a hair-thin object must keep at least one row / column
        nw, nh = max(1, round(w * pre)), max(1, round(h * pre))
        crop = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
        # the resized crop has other pixel units
        a = a * np.array([w / nw, h / nh])[None, :]
    out = cv2.warpAffine(
        crop, np.hstack([a, offset[:, None]]), (size, size), flags=cv2.INTER_LINEAR
    )
    return np.asarray(
        out > min(127.0, float(out.max()) / 2.0)
    )  # a thin object keeps its half-maximum outline


def silhouette_variants(desc: np.ndarray) -> np.ndarray:
    """The 4 flips of a descriptor (the principal axis sign and the mirror are ambiguous): (4, size, size) bool."""
    return np.stack([desc, desc[::-1], desc[:, ::-1], desc[::-1, ::-1]])


def align_object(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Masked crop rotated so the object's longest axis is vertical (bank thumbnails are in that frame)."""
    import cv2

    crop, (x0, y0, x1, y1) = crop_masked(rgb, mask)
    m = np.ascontiguousarray(mask[y0:y1, x0:x1])
    ys, xs = np.nonzero(m)
    if xs.size < 3:
        return crop
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    _, vecs = np.linalg.eigh(np.cov((pts - pts.mean(0)).T))
    vx, vy = vecs[:, 1]
    # CCW degrees that bring the longest axis to vertical (image y points down, hence -vy)
    angle = 90.0 - float(np.degrees(np.arctan2(-vy, vx)))
    h, w = crop.shape[:2]
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(rot[0, 0]), abs(rot[0, 1])
    nw, nh = int(h * sin + w * cos) + 1, int(h * cos + w * sin) + 1
    rot[0, 2] += nw / 2 - w / 2
    rot[1, 2] += nh / 2 - h / 2
    rgb_r = cv2.warpAffine(
        crop, rot, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=PAD_RGB
    )
    mask_r = (
        cv2.warpAffine(m.astype(np.uint8), rot, (nw, nh), flags=cv2.INTER_NEAREST) > 0
    )
    if not mask_r.any():
        return crop
    return crop_masked(rgb_r, mask_r, pad=0.0, keep_bg=True)[0]


def object_views(
    rgb: np.ndarray, mask: np.ndarray | None, align: bool = True
) -> list[np.ndarray]:
    """Embedding inputs for one object: upright + rotated 180 degrees (`VIEWS_PER_OBJECT`)."""
    if mask is None or not mask.any():
        base = rgb
    elif align:
        base = align_object(rgb, mask)
    else:
        base = crop_masked(rgb, mask)[0]
    return [base, np.ascontiguousarray(base[::-1, ::-1])]


def check_mask(
    mask: np.ndarray, shape: tuple[int, ...], what: str = "mask"
) -> np.ndarray:
    """`mask` as a boolean array, which must be as large as the image `shape[:2]`."""
    m = np.asarray(mask, bool)
    if m.ndim == 2 and not m.any():
        raise BadInput(f"{what} is empty (no foreground pixel)")
    if m.shape != tuple(shape[:2]):
        raise BadInput(
            f"{what} is {m.shape[1]}x{m.shape[0]} but the image is {shape[1]}x{shape[0]}"
            if m.ndim == 2
            else f"{what} must be a 2-D array"
        )
    return m


def _thumb_object(
    thumb_rgb: np.ndarray, sil: np.ndarray | None, what: str
) -> np.ndarray:
    """A bank thumbnail as the same kind of input as a query crop: object tightly cropped on the neutral CLIP background."""
    if sil is None or not (sil > 0).any():
        return thumb_rgb
    # pad=0.0 like `align_object`: query crops and thumbnails must show the object at the same scale
    return crop_masked(
        thumb_rgb, check_mask(sil > 0, thumb_rgb.shape, f"{what} silhouette"), pad=0.0
    )[0]


# --- index ---------------------------------------------------------------------------------------------------------


@dataclass
class BankIndex:
    entries: list[dict[str, Any]]
    clip: np.ndarray  # (N, 512) l2-normalised
    dino: np.ndarray  # (N, 768) l2-normalised
    sil: np.ndarray  # (N, SIL_SIZE, SIL_SIZE) bool, principal-axis frame (all False when an entry has no silhouette)
    meta: dict[str, Any] = field(default_factory=dict)
    root: Path | None = None  # directory the entries' thumbnails are relative to

    @property
    def textured_fraction(self) -> float:
        return sum(1 for e in self.entries if e.get("textured")) / max(
            len(self.entries), 1
        )

    @cached_property
    def sil_flat(self) -> np.ndarray:
        """(N, SIL_SIZE ** 2) float32 silhouettes, converted once per index instead of once per query."""
        return self.sil.reshape(len(self.sil), -1).astype(np.float32)

    @cached_property
    def sil_area(self) -> np.ndarray:
        return np.asarray(self.sil_flat.sum(axis=1))


@contextmanager
def _owned_encoders(
    parts: Sequence[str],
    clip: ClipEncoder | None,
    dino: DinoEncoder | None,
    device: str,
) -> Iterator[tuple[ClipEncoder | None, DinoEncoder | None]]:
    """The encoders `parts` needs: the injected ones, else fresh ones that are unloaded again when the block ends."""
    own: list[ClipEncoder | DinoEncoder] = []
    c = d = None
    try:
        if "clip" in parts:
            c = clip if clip is not None else ClipEncoder(device)
            if clip is None:
                own.append(c)
        if "dino" in parts:
            d = dino if dino is not None else DinoEncoder(device)
            if dino is None:
                own.append(d)
        yield c, d
    finally:
        for enc in own:
            enc.unload()


def _atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    write(tmp)
    os.replace(tmp, path)


def build_index(
    bank_json: str | Path,
    out_dir: str | Path | None = None,
    *,
    clip: ClipEncoder | None = None,
    dino: DinoEncoder | None = None,
    device: str = "auto",
) -> Path:
    """Embed every bank thumbnail (CLIP + DINOv2) and store silhouette descriptors: `index.npz` + `index.json` in `out_dir`
    (default: next to `bank_json`); returns that directory, which is what `query` / `Recogniser.classify(bank=...)` take.

    Needs the clip-b32 and dinov2-s models locally (never downloads). Encoders may be injected to share resident weights.
    Rebuilding replaces the files atomically and stamps both with a build id, so a reader never pairs new embeddings with an
    old entry list.
    """
    import cv2

    bank_path = Path(bank_json)
    if bank_path.is_dir():
        bank_path = bank_path / "bank.json"
    doc = read_bank(bank_path)
    base = bank_path.parent
    out = Path(out_dir) if out_dir else base
    out.mkdir(parents=True, exist_ok=True)
    objects: list[np.ndarray] = []
    sils: list[np.ndarray] = []
    for e in doc["entries"]:
        what = f"{bank_path}: entry '{e['id']}'"
        thumb_path = _inside(base, e["thumb"], what)
        thumb = cv2.imread(str(thumb_path), cv2.IMREAD_COLOR)
        if thumb is None:
            raise BadInput(f"{what}: cannot read thumbnail {thumb_path}")
        thumb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
        sil = None
        if e.get("sil"):
            sil_path = _inside(base, e["sil"], what)
            sil = cv2.imread(str(sil_path), cv2.IMREAD_GRAYSCALE)
            if sil is None:
                raise BadInput(f"{what}: cannot read silhouette {sil_path}")
        objects.append(_thumb_object(thumb, sil, what))
        sils.append(
            silhouette_descriptor(sil > 0)
            if sil is not None
            else np.zeros((SIL_SIZE, SIL_SIZE), bool)
        )
    t0 = time.perf_counter()
    with _owned_encoders(("clip", "dino"), clip, dino, device) as (clip_e, dino_e):
        assert clip_e is not None and dino_e is not None
        clip_emb = clip_e.images(objects)
        dino_emb = dino_e.images(objects)
    elapsed = time.perf_counter() - t0
    entries = [dict(e) for e in doc["entries"]]
    for e in entries:  # the index directory is self-contained: thumbnails are copied next to it (replacing older copies)
        for key in ("thumb", "sil"):
            if e.get(key):
                _copy_into(base, out, e[key], f"{bank_path}: entry '{e['id']}' {key}")
    build_id = uuid.uuid4().hex
    meta = {
        "version": BANK_VERSION,
        "build_id": build_id,
        "view": doc.get("view"),
        "px": doc.get("px"),
        "n": len(entries),
        "n_textured": sum(1 for e in entries if e.get("textured")),
        "models": {"clip": "clip-b32", "dino": "dinov2-s"},
        "embed_seconds": round(elapsed, 3),
        "sil_size": SIL_SIZE,
    }
    if doc.get("warning"):
        meta["warning"] = doc["warning"]
    sil_bits = np.packbits(np.stack(sils).reshape(len(entries), -1), axis=1)

    def write_npz(tmp: Path) -> None:
        with open(tmp, "wb") as fh:
            np.savez(
                fh,
                clip=clip_emb.astype(np.float32),
                dino=dino_emb.astype(np.float32),
                sil=sil_bits,
                build_id=np.array(build_id),
            )

    _atomic_write(out / "index.npz", write_npz)

    def write_json(tmp: Path) -> None:
        tmp.write_text(
            json.dumps({"meta": meta, "entries": entries}, indent=1) + "\n",
            encoding="utf-8",
        )

    _atomic_write(out / "index.json", write_json)
    return out


def load_index(index_dir: str | Path) -> BankIndex:
    """Load `index.npz` + `index.json` (numpy + json only: no network, no models)."""
    d = Path(index_dir)
    if d.is_file():
        d = d.parent
    rebuild = "rebuild it with `3d bank index <bank dir>`"
    try:
        doc = json.loads((d / "index.json").read_text(encoding="utf-8"))
        with np.load(d / "index.npz", allow_pickle=False) as z:
            clip_e, dino_e, sil_bits, npz_build = (
                z["clip"],
                z["dino"],
                z["sil"],
                str(z["build_id"]),
            )
        entries = doc["entries"]
        meta = doc["meta"]
        if (
            not isinstance(meta, dict)
            or not isinstance(entries, list)
            or not all(isinstance(e, dict) for e in entries)
        ):
            raise TypeError("meta must be an object and entries a list of objects")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        zipfile.BadZipFile,
    ) as exc:
        raise BadInput(
            f"cannot load a bank index from {d}: {exc}",
            "build one with `3d bank index <bank dir>`",
        ) from exc
    if meta.get("version") != BANK_VERSION or meta.get("sil_size") != SIL_SIZE:
        raise BadInput(
            f"index in {d} is format v{meta.get('version')} / silhouette {meta.get('sil_size')}, "
            f"this build reads v{BANK_VERSION} / {SIL_SIZE}",
            rebuild,
        )
    if meta.get("build_id") != npz_build:
        raise BadInput(
            f"index.json and index.npz in {d} come from different builds (rebuilt while loading?)",
            rebuild,
        )
    n = len(entries)
    if clip_e.shape[0] != n or dino_e.shape[0] != n or sil_bits.shape[0] != n:
        raise BadInput(f"index in {d} is inconsistent (entries vs embeddings)", rebuild)
    sil = (
        np.unpackbits(sil_bits, axis=1)[:, : SIL_SIZE * SIL_SIZE]
        .reshape(n, SIL_SIZE, SIL_SIZE)
        .astype(bool)
    )
    return BankIndex(entries, clip_e, dino_e, sil, meta, d)


def metric_parts(metric: str) -> tuple[str, ...]:
    """A resolved metric name -> the score families it averages: 'clip+dino' -> ('clip', 'dino'), 'all' -> all three."""
    if metric == "all":
        return ("clip", "dino", "sil")
    parts = tuple(metric.split("+"))
    if any(p not in ("clip", "dino", "sil") for p in parts):
        raise BadInput(f"unknown metric '{metric}'", f"one of: {', '.join(METRICS)}")
    return parts


def resolve_metric(index: BankIndex, metric: str, have_mask: bool) -> str:
    """`auto` -> appearance (clip+dino) for a bank that mostly has colour, else the silhouette (shape is all an untextured bank has)."""
    if metric not in METRICS:
        raise BadInput(f"unknown metric '{metric}'", f"one of: {', '.join(METRICS)}")
    if metric != "auto":
        return metric
    if index.textured_fraction >= MIN_TEXTURED_FRACTION or not have_mask:
        return "clip+dino"
    return "sil"


def _needs_mask() -> BadInput:
    return BadInput(
        "the silhouette metric needs a mask",
        "pass mask=... (CLI: --mask, or --instances with --id)",
    )


def score_index(
    index: BankIndex,
    metric: str,
    clip_q: np.ndarray | None,
    dino_q: np.ndarray | None,
    sil_q: np.ndarray | None,
) -> np.ndarray:
    """(N,) similarity of one object to every bank entry. Query arrays hold the object's variants (axis 0); best variant wins.

    An entry that has no silhouette cannot be matched by shape: its score is NaN and `rank` leaves it out.
    """
    scores: list[np.ndarray] = []
    for part in metric_parts(metric):
        if part == "sil":
            if sil_q is None:
                raise _needs_mask()
            q = sil_q.reshape(len(sil_q), -1).astype(np.float32)
            inter = q @ index.sil_flat.T
            union = q.sum(1)[:, None] + index.sil_area[None, :] - inter
            sil_score = (inter / np.maximum(union, 1.0)).max(axis=0)
            scores.append(np.where(index.sil_area > 0, sil_score, np.nan))
        else:
            emb_q, emb_b = (
                (clip_q, index.clip) if part == "clip" else (dino_q, index.dino)
            )
            if emb_q is None:
                raise BadInput(f"missing {part} query embeddings")
            scores.append((emb_q @ emb_b.T).max(axis=0))
    stacked = np.stack(scores)
    with np.errstate(
        all="ignore"
    ):  # entries without a silhouette: the appearance parts still count, a sil-only metric gives NaN
        return (
            np.asarray(np.nanmean(stacked, axis=0)) if len(scores) > 1 else stacked[0]
        )


def rank(
    index: BankIndex,
    scores: np.ndarray,
    topk: int,
    metric: str,
    categories: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Top-k records for one score vector, optionally restricted to entries whose category is in `categories`."""
    ok = ~np.isnan(scores)
    if categories is not None:
        allowed = set(categories)
        ok &= np.array(
            [e.get("category") in allowed for e in index.entries], dtype=bool
        )
    idx = np.flatnonzero(ok)
    order = idx[np.argsort(-scores[idx], kind="stable")][: max(topk, 0)]
    root = index.root
    return [
        {
            "bank_id": index.entries[i]["id"],
            "name": index.entries[i].get("name"),
            "category": index.entries[i].get("category"),
            "score": round(float(scores[i]), 4),
            "metric": metric,
            "extents_mm": index.entries[i].get("extents_mm"),
            "thumb": str(root / index.entries[i]["thumb"])
            if root is not None
            else index.entries[i]["thumb"],
        }
        for i in order
    ]


def object_embeddings(
    rgb: np.ndarray,
    masks: Sequence[np.ndarray | None],
    metric: str,
    *,
    align: bool = True,
    clip: ClipEncoder | None = None,
    dino: DinoEncoder | None = None,
    device: str = "auto",
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """(n, VIEWS_PER_OBJECT, D) CLIP / DINOv2 embeddings of every object's views; None for a family `metric` does not use."""
    parts = metric_parts(metric)
    views = [v for m in masks for v in object_views(rgb, m, align)]
    clip_q = dino_q = None
    with _owned_encoders(parts, clip, dino, device) as (c, d):
        if c is not None:
            clip_q = c.images(views).reshape(len(masks), VIEWS_PER_OBJECT, -1)
        if d is not None:
            dino_q = d.images(views).reshape(len(masks), VIEWS_PER_OBJECT, -1)
    return clip_q, dino_q


def score_object(
    index: BankIndex,
    metric: str,
    mask: np.ndarray | None,
    clip_q: np.ndarray | None,
    dino_q: np.ndarray | None,
) -> np.ndarray:
    """(N,) scores of one object: its view embeddings (from `object_embeddings`) and, for the silhouette metric, its mask."""
    sil_q = None
    if "sil" in metric_parts(metric):
        if mask is None:
            raise _needs_mask()
        sil_q = silhouette_variants(silhouette_descriptor(mask))
    return score_index(index, metric, clip_q, dino_q, sil_q)


def query(
    index_dir: str | Path | BankIndex,
    crop: np.ndarray,
    topk: int = 5,
    *,
    mask: np.ndarray | None = None,
    metric: str = "auto",
    align: bool = True,
    categories: Sequence[str] | None = None,
    clip: ClipEncoder | None = None,
    dino: DinoEncoder | None = None,
    device: str = "auto",
) -> list[dict[str, Any]]:
    """Top-k bank entries for an object. `crop` is an RGB image; `mask` (same size, bool) marks the object inside it.

    With a mask the object is cut out, rotated so its longest axis is vertical (like the thumbnails) and matched both ways
    up; without one the whole crop is the object (no alignment, no silhouette metric).
    Each record: `{"bank_id","name","category","score","metric","extents_mm","thumb"}`, best first. `categories` restricts
    the search to entries of those categories.
    """
    if crop.ndim != 3 or crop.shape[2] != 3:
        raise BadInput(f"query expects an HxWx3 RGB crop, got shape {crop.shape}")
    index = index_dir if isinstance(index_dir, BankIndex) else load_index(index_dir)
    use = resolve_metric(index, metric, mask is not None)
    obj = None if mask is None else check_mask(mask, crop.shape)
    if obj is None and "sil" in metric_parts(use):
        raise _needs_mask()
    clip_q, dino_q = object_embeddings(
        crop, [obj], use, align=align, clip=clip, dino=dino, device=device
    )
    scores = score_object(
        index,
        use,
        obj,
        None if clip_q is None else clip_q[0],
        None if dino_q is None else dino_q[0],
    )
    return rank(index, scores, topk, use, categories)
