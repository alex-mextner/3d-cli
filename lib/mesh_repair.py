"""mesh_repair.py - thin adapter between 3d-cli and the public `meshfix` library.

WHAT: repairs a triangle mesh (holes, flipped faces, duplicate / near-duplicate vertices, duplicate
  faces, non-manifold edges, T-junctions, floating islands) with
  https://github.com/alex-mextner/meshfix (C++17, no heavy deps, a stable C ABI) and returns a
  report that proves what changed (watertight before/after, volume before/after, counts).

WHY: preparing a downloaded STL for printing used to mean hand repair in a slicer; this is the one
  engine `3d mesh --repair` and the prep pipeline call (ticket #432).

Python API (what `lib/prep` and `3d mesh --repair` use):
  repair_mesh(vertices, faces, **opts) -> (vertices, faces, report_dict)
  repair_file(src, dst=None, **opts)   -> report_dict      (.stl/.obj/.3mf/.ply/.off in; dst by extension)

`opts` are meshfix.Options fields (flags, weld_tolerance, max_hole_edges, min_component_faces, separation).
report_dict keeps these keys stable for callers: watertight_before / watertight_after, volume_before /
volume_after, faces_before / faces_after, holes_filled (plus every other meshfix report field, and
`engine`).

Where the library comes from (first that works, never touches the network at import time):
  1. `import meshfix`                       an installed package (pip / uv: see MESHFIX_PIP)
  2. libs/meshfix + a cached cmake build     cloned at MESHFIX_TAG and built once into
                                             <cache>/meshfix-<tag>/ (needs git, cmake, a C++17 compiler)
  MESHFIX_LIBRARY=/path/to/libmeshfix.so overrides which shared library the Python package loads.

Stdlib-only at import time; numpy / trimesh are imported inside the functions.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, cast

MESHFIX_TAG = "v0.1.1"
MESHFIX_URL = "https://github.com/alex-mextner/meshfix.git"
MESHFIX_PIP = f"meshfix @ git+https://github.com/alex-mextner/meshfix@{MESHFIX_TAG}"

# Formats the repaired mesh can be written in: .stl/.obj natively, the others through trimesh.
OUTPUT_EXTENSIONS = (".stl", ".obj", ".3mf", ".ply", ".off")

# The report keys prep / callers rely on; kept in one place so tests pin them.
STABLE_REPORT_KEYS = (
    "watertight_before",
    "watertight_after",
    "volume_before",
    "volume_after",
    "faces_before",
    "faces_after",
    "holes_filled",
)

_module: Any = None


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _missing(reason: str) -> Exception:
    from errors import MissingDependency

    return MissingDependency(
        "meshfix",
        install=f'pip install "{MESHFIX_PIP}"   (or: git, cmake and a C++17 compiler for the automatic build)',
        degrades=f"mesh repair (3d mesh --repair, 3d prep) cannot run ({reason})",
        command="mesh",
    )


def _build_dir() -> str:
    from cli import paths

    return os.fspath(paths.cache_dir() / f"meshfix-{MESHFIX_TAG}")


def _shared_library(build: str) -> str | None:
    for name in ("libmeshfix.so", "libmeshfix.dylib", "meshfix.dll"):
        for sub in ("", "Release"):
            path = os.path.join(build, sub, name)
            if os.path.isfile(path):
                return path
    return None


def _run(cmd: list[str], what: str) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-5:]
        raise _missing(f"{what} failed: " + " | ".join(tail))


@contextlib.contextmanager
def _exclusive(path: str) -> Iterator[None]:
    """Hold an advisory lock on `path` so concurrent first uses clone and build once, not in parallel."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        try:
            import fcntl
        except ImportError:  # Windows: no advisory locks, first use is unguarded
            yield
            return
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _ensure_checkout_and_build() -> tuple[str, str]:
    """Clone meshfix at MESHFIX_TAG into libs/ and build the shared library once. Returns (python dir, library)."""
    src = os.path.join(_repo_root(), "libs", "meshfix")
    build = _build_dir()
    lib = _shared_library(build)
    if lib and os.path.isdir(os.path.join(src, "python", "meshfix")):
        return os.path.join(src, "python"), lib
    for tool in ("git", "cmake"):
        if shutil.which(tool) is None:
            raise _missing(f"`{tool}` not on PATH")
    with _exclusive(build + ".lock"):
        # Another process may have finished while this one waited for the lock.
        lib = _shared_library(build)
        if lib and os.path.isdir(os.path.join(src, "python", "meshfix")):
            return os.path.join(src, "python"), lib
        if not os.path.isdir(os.path.join(src, ".git")):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            sys.stderr.write(
                f"3d: first use of mesh repair - fetching meshfix {MESHFIX_TAG} into libs/meshfix (once)...\n"
            )
            part = src + ".part"
            shutil.rmtree(part, ignore_errors=True)
            _run(
                [
                    "git",
                    "-c",
                    "http.connectTimeout=10",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    MESHFIX_TAG,
                    MESHFIX_URL,
                    part,
                ],
                "git clone",
            )
            shutil.rmtree(src, ignore_errors=True)  # a leftover without .git
            os.replace(
                part, src
            )  # a half-finished clone is never mistaken for a checkout
        lib = _shared_library(build)
        if lib:  # built from another worktree's checkout of the same pinned tag: reuse, never re-configure the shared cache
            return os.path.join(src, "python"), lib
        sys.stderr.write("3d: building the meshfix library (once)...\n")
        try:
            _run(
                [
                    "cmake",
                    "-S",
                    src,
                    "-B",
                    build,
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DMESHFIX_BUILD_CLI=OFF",
                    "-DMESHFIX_BUILD_TESTS=OFF",
                    "-DMESHFIX_BUILD_STATIC=OFF",
                ],
                "cmake configure",
            )
            from cli.resources import max_workers

            _run(
                [
                    "cmake",
                    "--build",
                    build,
                    "--config",
                    "Release",
                    "--parallel",
                    str(max_workers()),
                ],
                "cmake build",
            )
        except BaseException:
            shutil.rmtree(
                build, ignore_errors=True
            )  # no half-configured cache for the next attempt to trip over
            raise
        lib = _shared_library(build)
        if lib is None:
            raise _missing("the build produced no shared library")
        return os.path.join(src, "python"), lib


def available() -> bool:
    """True when meshfix can run without a clone or build: importable, or a cached build plus a checkout exist."""
    if _module is not None:
        return True
    if importlib.util.find_spec("meshfix") is not None:
        return True
    return _shared_library(_build_dir()) is not None and os.path.isdir(
        os.path.join(_repo_root(), "libs", "meshfix", "python", "meshfix")
    )


def load_meshfix() -> Any:
    """Return the `meshfix` module, installing/building it on first use. Raises MissingDependency."""
    global _module
    if _module is not None:
        return _module
    try:
        _module = importlib.import_module(
            "meshfix"
        )  # an installed package (wheel or editable)
    except ImportError:
        python_dir, lib = _ensure_checkout_and_build()
        os.environ.setdefault("MESHFIX_LIBRARY", lib)
        sys.path.insert(0, python_dir)
        try:
            _module = importlib.import_module("meshfix")
        except ImportError as exc:  # LibraryNotFound is an ImportError
            raise _missing(str(exc)) from exc
    return _module


def _with_aliases(report: Any, engine: str) -> dict[str, Any]:
    out = report.to_dict()
    out["engine"] = engine
    return out


def repair_mesh(
    vertices: Any, faces: Any, **opts: Any
) -> tuple[Any, Any, dict[str, Any]]:
    """Repair (N,3) vertices / (M,3) faces. Returns (vertices, faces, report_dict); see the module docstring."""
    meshfix = load_meshfix()
    out_v, out_f, report = meshfix.repair(vertices, faces, **opts)
    return out_v, out_f, _with_aliases(report, f"meshfix {meshfix.version()}")


def repair_file(src: str, dst: str | None = None, **opts: Any) -> dict[str, Any]:
    """Repair a mesh file; write the result to dst when given (format by extension). Returns the report dict."""
    if dst is not None and os.path.splitext(dst)[1].lower() not in OUTPUT_EXTENSIONS:
        from errors import InvalidArgument

        raise InvalidArgument(
            "-o",
            dst,
            list(OUTPUT_EXTENSIONS),
            command="mesh",
            extra="the repaired mesh is written by file extension",
        )
    meshfix = load_meshfix()
    engine = f"meshfix {meshfix.version()}"
    in_ext = os.path.splitext(src)[1].lower()
    out_ext = os.path.splitext(dst)[1].lower() if dst else ""
    native = (".stl", ".obj")
    if in_ext in native and (dst is None or out_ext in native):
        return _with_aliases(meshfix.repair_file(src, dst, **opts), engine)

    import trimesh  # 3mf / ply / off: through trimesh, which merges equal vertices like an STL reader

    mesh = cast("trimesh.Trimesh", trimesh.load(src, force="mesh", process=False))
    out_v, out_f, report = meshfix.repair(mesh.vertices, mesh.faces, **opts)
    if dst is not None:
        trimesh.Trimesh(out_v, out_f, process=False).export(dst)
    return _with_aliases(report, engine)


def format_report(report: dict[str, Any]) -> str:
    """The human summary `3d mesh --repair` prints."""
    lines = [
        f"repair ({report['engine']}): faces {report['faces_before']} -> {report['faces_after']}, "
        f"watertight {'yes' if report['watertight_before'] else 'no'} -> {'yes' if report['watertight_after'] else 'no'}, "
        f"volume {report['volume_before']:.4f} -> {report['volume_after']:.4f} mm3",
        f"  holes filled {report['holes_filled']} (+{report['hole_faces_added']} faces), "
        f"non-manifold edges {report['non_manifold_edges_before']} -> {report['non_manifold_edges_after']}, "
        f"faces flipped {report['faces_flipped']}, duplicate faces {report['duplicate_faces_removed']}, "
        f"degenerate faces {report['degenerate_faces_removed']}, T-junctions {report['t_junctions_fixed']}",
        f"  vertices merged {report['vertices_merged']}, small islands removed {report['small_components_removed']}, "
        f"zero-volume shells removed {report['flat_components_removed']}, {report['elapsed_ms'] / 1000:.2f} s",
    ]
    return "\n".join(lines)
