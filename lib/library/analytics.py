"""library/analytics.py — deterministic facts about a model's files, for ranking and classification.

WHAT: no LLM, no guessing. `file_facts()` turns a model's file list (names + sizes, as a marketplace
listing gives them) into flags and ratios a small model — or a plain `sort` — can use: is there native CAD
(`step`, `f3d`, `scad`, ...) or only a mesh (`stl`, `3mf`), how many parts, is it an assembly, how big.
`analyze_mesh()` goes one step further for a mesh FILE ON DISK with trimesh: bounding box in mm, volume,
watertight/manifold, triangle and body counts, an estimated print footprint. `list_archive()` lists the members
of a downloaded zip/tar so archives stop hiding their contents. `pick_mesh()` chooses which listed file a
`--deep` run should download (the smallest mesh under a size cap).

ACCESSED VIA: `library.search` (every search result gets `file_facts`; `--deep` adds `analyze_mesh`),
`3d models analyze FILE...` and `3d models download` (analytics of the files just written).

INVARIANTS:
  - Pure and deterministic: the same input gives the same output (dict keys in a fixed order, lists sorted).
  - `cad` is True exactly when a file with a native-CAD extension is in the list (an archive is only looked
    into when its members are given; `archives_unlisted` counts the ones that were not).
  - A value that cannot be known is absent or None, never guessed: `bbox_mm` exists only for a loaded mesh,
    `volume_mm3` only for a watertight one.
  - Stdlib-only at module top (checked by tests/test_imports.py); trimesh is imported inside `analyze_mesh`.
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA = "3d.analytics/1"

# Native CAD: the editable source of a model (parametric or B-rep), as opposed to a mesh export.
CAD_EXTENSIONS = frozenset(
    {
        "step",
        "stp",
        "iges",
        "igs",
        "brep",
        "f3d",
        "f3z",
        "fcstd",
        "scad",
        "blend",
        "sldprt",
        "sldasm",
        "ipt",
        "iam",
        "prt",
        "par",
        "catpart",
        "catproduct",
        "3dm",
        "skp",
        "x_t",
        "x_b",
        "sat",
        "asm",
        "cq",
    }
)
# 2-D drawings are CAD *files* but not 3-D models: a DXF/DWG/SVG never makes a listing "have CAD".
DRAWING_EXTENSIONS = frozenset({"dwg", "dxf", "svg", "ai", "eps"})
MESH_EXTENSIONS = frozenset(
    {
        "stl",
        "3mf",
        "obj",
        "ply",
        "amf",
        "off",
        "glb",
        "gltf",
        "3ds",
        "dae",
        "fbx",
        "wrl",
        "x3d",
        "usdz",
        "usd",
        "usda",
        "usdc",
    }
)
PARAMETRIC_EXTENSIONS = frozenset(
    {"scad", "f3d", "f3z", "fcstd", "blend", "sldprt", "ipt", "cq", "par"}
)
ASSEMBLY_EXTENSIONS = frozenset({"sldasm", "iam", "catproduct", "asm"})
GCODE_EXTENSIONS = frozenset(
    {"gcode", "gco", "bgcode", "sl1", "sl1s", "ctb", "cbddlp", "photon", "goo", "lys"}
)
ARCHIVE_EXTENSIONS = frozenset(
    {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "tbz2", "txz"}
)
IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "mp4", "mov"}
)
DOCUMENT_EXTENSIONS = frozenset(
    {"pdf", "txt", "md", "doc", "docx", "rtf", "odt", "html", "csv", "xlsx"}
)
# Name tokens that say "all parts together" (or one full assembled model) rather than one component.
ASSEMBLY_TOKENS = frozenset(
    {
        "assembly",
        "assembled",
        "asm",
        "complete",
        "combined",
        "merged",
        "whole",
        "allparts",
    }
)
MESH_BINARY_STL_BYTES_PER_TRIANGLE = (
    50  # 50 bytes per facet in binary STL, plus an 84-byte header
)

# size_class thresholds on the total bytes of the mesh files (CAD files when there is no mesh).
SIZE_CLASSES: tuple[tuple[str, int], ...] = (
    ("tiny", 100_000),
    ("small", 2_000_000),
    ("medium", 20_000_000),
    ("large", 100_000_000),
)
SIZE_ORDER = ("tiny", "small", "medium", "large", "huge", "unknown")
# Largest bbox edge (mm) -> physical size class, for a measured mesh.
DIMENSION_CLASSES: tuple[tuple[str, float], ...] = (
    ("tiny", 25.0),
    ("small", 75.0),
    ("medium", 180.0),
    ("large", 256.0),
)

FileEntry = tuple[str, int | None]


def extension(name: str) -> str:
    """Lower-case extension without the dot (`'Part.STL'` -> `'stl'`, `'a.tar.gz'` -> `'gz'`); '' if none."""
    base = name.rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    return ext.lower() if dot and stem else ""


def file_class(name: str) -> str:
    """`cad | mesh | gcode | archive | drawing | image | document | other` for a file name."""
    ext = extension(name)
    if ext in CAD_EXTENSIONS:
        return "cad"
    if ext in MESH_EXTENSIONS:
        return "mesh"
    if ext in GCODE_EXTENSIONS:
        return "gcode"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in DRAWING_EXTENSIONS:
        return "drawing"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def _tokens(name: str) -> list[str]:
    stem = name.rsplit("/", 1)[-1].rpartition(".")[0] or name
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)  # CamelCase -> words
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _part_key(name: str) -> str:
    """Grouping key of a part: lower-case alphanumerics of the stem without version/copy suffixes, so
    `Lid.stl`, `lid.3mf` and `Lid (1).stl` are one part."""
    stem = name.rsplit("/", 1)[-1].rpartition(".")[0] or name
    stem = re.sub(r"[\s_\-]*(?:\(\d+\)|v\d+|copy|final)$", "", stem.lower())
    return re.sub(r"[^a-z0-9]+", "", stem)


def size_class(total_bytes: int | None) -> str:
    if total_bytes is None or total_bytes <= 0:
        return "unknown"
    for label, limit in SIZE_CLASSES:
        if total_bytes < limit:
            return label
    return "huge"


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole > 0 else None


def file_facts(
    files: Iterable[FileEntry], *, files_known: bool = True
) -> dict[str, Any]:
    """Deterministic flags and ratios from `(name, size_bytes_or_None)` entries.

    `files_known=False` says the listing was never fetched (a Thingiverse hit past its request budget):
    every flag is then absent from the facts except `files_known: false`, so "no CAD" and "not looked" differ.
    """
    if not files_known:
        return {"schema": SCHEMA, "files_known": False}
    entries = sorted(((n, s) for n, s in files), key=lambda e: (e[0].lower(), e[0]))
    classes = [(name, size, file_class(name)) for name, size in entries]
    exts = Counter(extension(n) or "(none)" for n, _ in entries)
    cad = sorted({extension(n) for n, _, c in classes if c == "cad"})
    mesh = sorted({extension(n) for n, _, c in classes if c == "mesh"})
    sizes = [s for _, s, _ in classes if s is not None]
    total = sum(sizes)
    mesh_bytes = sum(s for _, s, c in classes if c == "mesh" and s is not None)
    cad_bytes = sum(s for _, s, c in classes if c == "cad" and s is not None)
    mesh_names = [n for n, _, c in classes if c == "mesh"]
    part_names = mesh_names or [n for n, _, c in classes if c == "cad"]
    parts = sorted({k for k in (_part_key(n) for n in part_names) if k})
    assembly_files = sorted(
        n
        for n, _, c in classes
        if c in ("mesh", "cad")
        and (extension(n) in ASSEMBLY_EXTENSIONS or ASSEMBLY_TOKENS & set(_tokens(n)))
    )
    stl_triangles = sum(
        max(0, (s - 84) // MESH_BINARY_STL_BYTES_PER_TRIANGLE)
        for n, s, c in classes
        if c == "mesh" and extension(n) == "stl" and s is not None and s > 84
    )
    sized = mesh_bytes or cad_bytes or total
    return {
        "schema": SCHEMA,
        "files_known": True,
        "file_count": len(entries),
        "total_bytes": total if sizes else None,
        "largest_file_bytes": max(sizes) if sizes else None,
        "formats": dict(sorted(exts.items())),
        "cad": bool(cad),
        "cad_formats": cad,
        "parametric": any(extension(n) in PARAMETRIC_EXTENSIONS for n, _, _ in classes),
        "has_stl": "stl" in mesh,
        "has_3mf": "3mf" in mesh,
        "has_step": bool({"step", "stp"} & set(cad)),
        "has_mesh": bool(mesh),
        "mesh_formats": mesh,
        "has_gcode": any(c == "gcode" for _, _, c in classes),
        "archives_unlisted": sum(1 for _, _, c in classes if c == "archive"),
        "mesh_file_count": len(mesh_names),
        "mesh_bytes": mesh_bytes,
        "cad_bytes": cad_bytes,
        "mesh_ratio": _ratio(mesh_bytes, total),
        "cad_ratio": _ratio(cad_bytes, total),
        "part_count": len(parts),
        "is_multi_part": len(parts) >= 2,
        "is_assembly": bool(assembly_files),
        "assembly_files": assembly_files,
        "size_class": size_class(sized),
        "stl_triangles_est": stl_triangles or None,
    }


def list_archive(path: Path, *, max_members: int = 2000) -> list[FileEntry]:
    """Members of a zip/tar archive as `(name, size)`, directories skipped; [] for any other file or a
    broken archive. Only the member table is read, never the contents."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                return [
                    (i.filename, i.file_size)
                    for i in zf.infolist()[:max_members]
                    if not i.is_dir()
                ]
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                members = [m for m in tf.getmembers()[:max_members] if m.isfile()]
                return [(m.name, m.size) for m in members]
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError):
        return []
    return []


def expand_archives(
    files: Sequence[FileEntry], listed: Mapping[str, Sequence[FileEntry]]
) -> list[FileEntry]:
    """`files` with every archive replaced by its members (`listed[archive name]`, given by the caller
    for archives it has opened); archives it has not opened stay as they are and count as unlisted."""
    out: list[FileEntry] = []
    for name, size in files:
        members = listed.get(name)
        if file_class(name) == "archive" and members is not None:
            out.extend((f"{name}/{m}", s) for m, s in members)
        else:
            out.append((name, size))
    return out


def pick_mesh(
    files: Iterable[FileEntry],
    *,
    max_bytes: int,
    formats: Sequence[str] = ("stl", "3mf", "obj", "ply"),
) -> tuple[str, int] | None:
    """The smallest listed mesh file of `formats` with a known size in 1..max_bytes (ties: by name), or None.
    `--deep` downloads exactly this one."""
    candidates = sorted(
        (s, n)
        for n, s in files
        if s is not None and 0 < s <= max_bytes and extension(n) in formats
    )
    return (candidates[0][1], candidates[0][0]) if candidates else None


def dimension_class(max_extent_mm: float) -> str:
    for label, limit in DIMENSION_CLASSES:
        if max_extent_mm < limit:
            return label
    return "oversize"


_3MF_UNITS_TO_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "meter": 1000.0,
                    "inch": 25.4, "foot": 304.8}
_3MF_MAX_XML_BYTES = 400 * 1024 * 1024


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _transform(raw: str | None) -> list[float] | None:
    """A 3MF `transform` (12 numbers, row-vector convention: v' = v * M + t) or None for the identity."""
    if not raw:
        return None
    values = [float(v) for v in raw.split()]
    if len(values) != 12:
        raise ValueError(f"bad 3MF transform {raw!r}")
    return values


def _load_3mf(path: Path) -> Any:
    """The build items of a 3MF file as one trimesh mesh in millimetres, read with the standard library: trimesh's
    own 3MF reader needs lxml, which the base install does not have. Handles `components` and item/component
    transforms; objects that are not on the build plate (or are `support` type) are ignored. Raises ValueError
    for anything that is not a readable core-spec 3MF."""
    import xml.etree.ElementTree as ET

    import numpy as np
    import trimesh

    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".model")]
            if not names:
                raise ValueError("no .model part in the 3MF archive")
            main = next((n for n in names if n.lower() == "3d/3dmodel.model"), names[0])
            if zf.getinfo(main).file_size > _3MF_MAX_XML_BYTES:
                raise ValueError("3MF model part is larger than 400 MB uncompressed")
            data = zf.read(main)
    except zipfile.BadZipFile as exc:
        raise ValueError("not a 3MF (zip) archive") from exc
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise ValueError("3MF model declares a DOCTYPE/ENTITY; refusing to parse it")
    root = ET.fromstring(data)  # DOCTYPE/ENTITY were refused above
    scale = _3MF_UNITS_TO_MM.get(root.get("unit", "millimeter"), 1.0)
    objects: dict[str, ET.Element] = {}
    build: list[ET.Element] = []
    for node in root.iter():
        if _local(node.tag) == "object" and node.get("id") is not None:
            objects[str(node.get("id"))] = node
        elif _local(node.tag) == "item":
            build.append(node)

    def mesh_of(oid: str, depth: int) -> list[tuple[Any, Any]]:
        node = objects.get(oid)
        if node is None or depth > 16 or node.get("type", "model") in ("support", "surface"):
            return []
        out: list[tuple[Any, Any]] = []
        for child in node:
            if _local(child.tag) == "mesh":
                verts = [(float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
                         for v in child.iter() if _local(v.tag) == "vertex"]
                tris = [(int(t.get("v1", 0)), int(t.get("v2", 0)), int(t.get("v3", 0)))
                        for t in child.iter() if _local(t.tag) == "triangle"]
                if verts and tris:
                    out.append((np.array(verts, dtype=float), np.array(tris, dtype=np.int64)))
            elif _local(child.tag) == "components":
                for comp in child:
                    matrix = _transform(comp.get("transform"))
                    for v, f in mesh_of(str(comp.get("objectid")), depth + 1):
                        out.append((_apply(v, matrix), f))
        return out

    def _apply(vertices: Any, matrix: list[float] | None) -> Any:
        if matrix is None:
            return vertices
        rot = np.array(matrix[:9], dtype=float).reshape(3, 3)
        return vertices @ rot + np.array(matrix[9:], dtype=float)

    parts: list[tuple[Any, Any]] = []
    for item in build:
        matrix = _transform(item.get("transform"))
        for v, f in mesh_of(str(item.get("objectid")), 0):
            parts.append((_apply(v, matrix) * scale, f))
    if not parts:
        raise ValueError("3MF build plate holds no mesh")
    offset, all_v, all_f = 0, [], []
    for v, f in parts:
        all_v.append(v)
        all_f.append(f + offset)
        offset += len(v)
    return trimesh.Trimesh(vertices=np.vstack(all_v), faces=np.vstack(all_f), process=True)


def analyze_mesh(path: Path) -> dict[str, Any]:
    """trimesh facts of one mesh file: `bbox_mm` [x, y, z], `triangles`, `watertight`, `winding_consistent`,
    `manifold`, `volume_mm3` (watertight only), `surface_area_mm2`, `body_count`, `degenerate_faces`,
    `fill_ratio` (volume / bbox volume), `est_print_footprint_mm` and `est_print_height_mm` (lying on its
    smallest extent), `dimension_class`, `units_suspect` (largest edge under 1 mm or over 2 m: probably metres
    or inches). Returns `{"status": "error", "reason": ...}` instead of raising for an unreadable file."""
    try:
        import numpy as np
        import trimesh

        loaded = _load_3mf(path) if extension(path.name) == "3mf" else trimesh.load(str(path), force="mesh")
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"trimesh is not installed ({exc})"}
    except Exception as exc:  # noqa: BLE001 — trimesh raises assorted types on corrupt files
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    faces = getattr(loaded, "faces", None)
    if faces is None or len(faces) == 0:
        return {"status": "error", "reason": "no triangles in the file"}
    mesh: Any = loaded
    extents = [float(e) for e in mesh.extents]
    watertight = bool(mesh.is_watertight)
    winding = bool(mesh.is_winding_consistent)
    volume = round(abs(float(mesh.volume)), 3) if watertight else None
    bbox_volume = extents[0] * extents[1] * extents[2]
    flat = sorted(extents)
    return {
        "status": "ok",
        "bbox_mm": [round(e, 3) for e in extents],
        "triangles": int(len(faces)),
        "vertices": int(len(mesh.vertices)),
        "watertight": watertight,
        "winding_consistent": winding,
        "manifold": watertight and winding,
        "volume_mm3": volume,
        "surface_area_mm2": round(float(mesh.area), 3),
        "body_count": int(mesh.body_count),
        "degenerate_faces": int(np.count_nonzero(mesh.area_faces < 1e-12)),
        "fill_ratio": round(volume / bbox_volume, 4)
        if volume is not None and bbox_volume > 0
        else None,
        "est_print_footprint_mm": [round(flat[2], 3), round(flat[1], 3)],
        "est_print_height_mm": round(flat[0], 3),
        "dimension_class": dimension_class(flat[2]),
        "units_suspect": flat[2] < 1.0 or flat[2] > 2000.0,
    }


MESH_TOOL_DEPS = "trimesh,numpy,scipy"
MESH_TOOL_TIMEOUT_S = 600.0


def _trimesh_importable() -> bool:
    import importlib.util

    return importlib.util.find_spec("trimesh") is not None and importlib.util.find_spec("numpy") is not None


def measure_meshes(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    """`{str(path): analyze_mesh(path)}` for every path. In-process when trimesh and numpy import here (a dev
    checkout, the test suite); otherwise ONE subprocess of `lib/mesh_facts.py` through `cli.pyrun` (`.venv`, else
    `uv run --with ...`), because the `3d` dispatcher runs on the system Python and a command module must never
    import the heavy stack at top level. Without any runtime every path answers `status: unavailable`."""
    if not paths:
        return {}
    if _trimesh_importable():
        return {str(p): analyze_mesh(p) for p in paths}
    import json
    import subprocess

    from cli.pyrun import tool_argv
    from errors import ThreeDError

    def unavailable(reason: str) -> dict[str, dict[str, Any]]:
        return {str(p): {"status": "unavailable", "reason": reason} for p in paths}

    try:
        proc = subprocess.run(tool_argv(MESH_TOOL_DEPS, "mesh_facts.py", [str(p) for p in paths]),
                              capture_output=True, text=True, timeout=MESH_TOOL_TIMEOUT_S, check=False)
    except ThreeDError as exc:
        return unavailable(exc.message[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return unavailable(f"mesh tool failed: {exc}"[:200])
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else {}
    except ValueError:
        parsed = {}
    if proc.returncode != 0 or not isinstance(parsed, dict):
        tail = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        return unavailable(f"mesh tool failed: {tail}"[:200])
    return {str(p): parsed.get(str(p), {"status": "error", "reason": "mesh tool returned nothing"}) for p in paths}


def analyze_path(path: Path) -> dict[str, Any]:
    """Facts of one local file: its class and size, plus `mesh` (analyze_mesh) for a mesh file or `members`
    (file_facts of the contents) for an archive."""
    name = path.name
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "name": name,
        "class": file_class(name),
        "size": path.stat().st_size,
    }
    if doc["class"] == "mesh":
        doc["mesh"] = measure_meshes([path])[str(path)]
    elif doc["class"] == "archive":
        members = list_archive(path)
        doc["members"] = file_facts(
            expand_archives([(name, doc["size"])], {name: members})
            if members
            else [(name, doc["size"])]
        )
        doc["member_count"] = len(members)
    return doc


# --------------------------------------------------------------------------- reference suitability
REFERENCE_MAX_EXTENT_MM = 150.0
REFERENCE_MAX_SIZE_CLASS = "small"


def reference_suitability(
    facts: dict[str, Any],
    *,
    paid: bool,
    license_name: str,
    mesh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether a result can serve as a reference model for a non-printable part (PCB, connector, screw, wire;
    umbrella #570): free, with a licence to record, with a STEP or mesh file, small, and (when a mesh was
    measured) with known dimensions of at most REFERENCE_MAX_EXTENT_MM. `reasons` names every failed test;
    `dims_known` is True only after a measured mesh."""
    reasons: list[str] = []
    if not facts.get("files_known"):
        return {
            "suitable": False,
            "dims_known": False,
            "reasons": ["file list unknown"],
        }
    if paid:
        reasons.append("paid")
    if not license_name:
        reasons.append("no licence listed")
    if not (facts.get("has_step") or facts.get("has_mesh")):
        reasons.append("no STEP or mesh file")
    klass = str(facts.get("size_class", "unknown"))
    if klass == "unknown" or SIZE_ORDER.index(klass) > SIZE_ORDER.index(
        REFERENCE_MAX_SIZE_CLASS
    ):
        reasons.append(
            f"size class {klass} (needs {REFERENCE_MAX_SIZE_CLASS} or smaller)"
        )
    dims_known = bool(mesh and mesh.get("status") == "ok")
    if (
        dims_known
        and mesh is not None
        and max(mesh["bbox_mm"]) > REFERENCE_MAX_EXTENT_MM
    ):
        reasons.append(
            f"largest edge {max(mesh['bbox_mm']):g} mm (needs <= {REFERENCE_MAX_EXTENT_MM:g})"
        )
    return {"suitable": not reasons, "dims_known": dims_known, "reasons": reasons}
