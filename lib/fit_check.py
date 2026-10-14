#!/usr/bin/env python3
"""fit_check.py — does a socket still fit an insert that is already printed?

Parts of one assembly are often printed days apart, on different printers, while the
generator keeps being rebuilt. A rebuild can silently move or resize the socket (pocket,
bow-tie slot, …) for an insert that already exists in plastic; the mismatch then shows only
at assembly, after the big mating part has been printed. This gate locks the insert's
printed size and the designed clearance per side, both in the ASSEMBLY frame:

  size    the insert's bounding box equals the printed size within --size-tol (any axis
          permutation of it with --any-turn); skipped when no printed size is given
  socket  slicing every --step mm from 0.05 mm above the insert's bottom to its top: the
          insert section never overlaps the socket section (overlap area > 1e-4 mm2 = FAIL),
          and wherever socket material lies within 2*gap+1 mm of the insert, the clearance —
          the distance from the insert section to that socket part — stays at the set gap
          +- --gap-tol. No socket at any layer = FAIL. Between the layers the solids are
          intersected too, so a shelf thinner than --step cannot hide an interference.
  depth   the socket depth (its top found to ~0.001 mm) is reported, and FAILs when below
          --min-depth. Tolerances are tested on unrounded measurements.

Sections are taken with manifold3d and read into shapely even-odd (outer rings and holes).
The clearance per layer is the smallest distance from the insert to the socket wall around
it, so --gap is the gap on the tightest side.

Pairs come from the command line (--insert/--socket/--gap …) or from a YAML/JSON spec
(--spec fits.yaml) for many inserts at once; see docs/commands/fit-check.md.

Exit: 0 PASS, 1 FAIL, 2 usage / spec / load error.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any

SIZE_TOL = 0.02
GAP_TOL = 0.03
STEP = 0.25
OVERLAP_AREA = 1e-4  # mm2: a section overlap smaller than this is numeric noise
OVERLAP_VOLUME = 1e-3  # mm3: the same for the solids (touching faces leave slivers)
FIRST_LAYER = 0.05  # mm above the insert's bottom: off its flat bottom face

SPEC_KEYS = {
    "name",
    "insert",
    "socket",
    "gap",
    "printed_size",
    "any_turn",
    "size_tol",
    "gap_tol",
    "step",
    "min_depth",
}
DEFAULT_KEYS = SPEC_KEYS - {"name", "insert", "socket", "printed_size"}


class FitError(ValueError):
    """a fit cannot be checked: bad option, unreadable spec, missing or broken mesh"""


@dataclass
class Fit:
    insert: list[str]
    socket: list[str]
    gap: float
    name: str = ""
    printed_size: tuple[float, float, float] | None = None
    any_turn: bool = False
    size_tol: float = SIZE_TOL
    gap_tol: float = GAP_TOL
    step: float = STEP
    min_depth: float | None = None
    socket_name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = "+".join(_stem(p) for p in self.insert)
        if not self.socket_name:
            self.socket_name = "+".join(_stem(p) for p in self.socket)


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _number(value: Any, what: str, *, positive: bool) -> float:
    if isinstance(value, bool):  # YAML `gap: true` must not become a 1 mm gap
        raise FitError(f"{what} must be a number, got {value!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise FitError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(v) or v < 0 or (positive and v == 0):
        raise FitError(
            f"{what} must be a finite number {'> 0' if positive else '>= 0'}, got {value!r}"
        )
    return v


def parse_size(value: Any, what: str = "printed size") -> tuple[float, float, float]:
    """'46.275,75.446,15.311' / 'X x Y x Z' / [x, y, z] -> three positive mm"""
    parts = (
        value.lower().replace("x", ",").split(",") if isinstance(value, str) else value
    )
    if not isinstance(parts, (list, tuple)) or len(parts) != 3:
        raise FitError(f"{what} needs three numbers X,Y,Z (mm), got {value!r}")
    x, y, z = (_number(p, what, positive=True) for p in parts)
    return x, y, z


def expand(specs: list[str], base: str, what: str) -> list[str]:
    """resolve paths (globs allowed) relative to `base`; every entry must match a file"""
    out: list[str] = []
    for spec in specs:
        path = spec if os.path.isabs(spec) else os.path.join(base, spec)
        hits = (
            sorted(glob.glob(path))
            if glob.has_magic(path)
            else ([path] if os.path.isfile(path) else [])
        )
        if not hits:
            raise FitError(
                f"{what} file not found: {spec}"
                + (f" (relative to {base})" if base not in ("", ".") else "")
            )
        out += [h for h in hits if h not in out]
    return out


def _paths(value: Any, what: str) -> list[str]:
    items = value.split(",") if isinstance(value, str) else value
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(p, str) and p.strip() for p in items)
    ):
        raise FitError(
            f"{what} must be a path, a comma-joined list of paths, or a list of paths, got {value!r}"
        )
    return [p.strip() for p in items]


def load_spec(path: str) -> list[Fit]:
    """read a fits spec (YAML or JSON):

        gap: 0.15                  # optional defaults for every fit: gap, any_turn,
        fits:                      #   size_tol, gap_tol, step, min_depth
          - name: PFL
            insert: out/assembly/PFL__DOOR.stl          # path, comma list or list; globs ok
            socket: [out/assembly/BASE2__*.stl]
            gap: 0.2
            printed_size: [46.275, 75.446, 15.311]
            any_turn: false

    Paths are relative to the spec file's directory."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except OSError as e:
        raise FitError(f"cannot read spec {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise FitError(f"spec {path} is not valid YAML/JSON: {e}") from None
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("fits"), list)
        or not doc["fits"]
    ):
        raise FitError(f"spec {path} needs a non-empty `fits:` list")
    unknown = set(doc) - DEFAULT_KEYS - {"fits"}
    if unknown:
        raise FitError(
            f"spec {path}: unknown top-level key(s) {', '.join(sorted(unknown))} "
            f"(allowed: fits, {', '.join(sorted(DEFAULT_KEYS))})"
        )
    defaults = {k: doc[k] for k in DEFAULT_KEYS if k in doc}
    base = os.path.dirname(os.path.abspath(path))
    fits = []
    for i, entry in enumerate(doc["fits"], 1):
        where = f"spec {path} fit #{i}"
        if not isinstance(entry, dict):
            raise FitError(f"{where} must be a mapping")
        unknown = set(entry) - SPEC_KEYS
        if unknown:
            raise FitError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(SPEC_KEYS))})"
            )
        merged = {**defaults, **entry}
        missing = [k for k in ("insert", "socket", "gap") if k not in merged]
        if missing:
            raise FitError(f"{where} is missing {', '.join(missing)}")
        any_turn = merged.get("any_turn", False)
        if not isinstance(any_turn, bool):
            raise FitError(f"{where}: any_turn must be true or false, got {any_turn!r}")
        if any_turn and merged.get("printed_size") is None:
            raise FitError(
                f"{where}: any_turn needs printed_size (set any_turn: false on this fit)"
            )
        name = merged.get("name", "")
        fits.append(
            make_fit(
                insert=expand(
                    _paths(merged["insert"], f"{where} insert"), base, "insert"
                ),
                socket=expand(
                    _paths(merged["socket"], f"{where} socket"), base, "socket"
                ),
                gap=merged["gap"],
                name=str(name) if name is not None else "",
                printed_size=merged.get("printed_size"),
                any_turn=any_turn,
                size_tol=merged.get("size_tol", SIZE_TOL),
                gap_tol=merged.get("gap_tol", GAP_TOL),
                step=merged.get("step", STEP),
                min_depth=merged.get("min_depth"),
                where=where,
            )
        )
    return fits


def make_fit(
    *,
    insert: list[str],
    socket: list[str],
    gap: Any,
    name: str = "",
    printed_size: Any = None,
    any_turn: bool = False,
    size_tol: Any = SIZE_TOL,
    gap_tol: Any = GAP_TOL,
    step: Any = STEP,
    min_depth: Any = None,
    where: str = "",
) -> Fit:
    """validate the numbers of one fit; `where` prefixes error messages"""
    pre = f"{where}: " if where else ""
    return Fit(
        insert=insert,
        socket=socket,
        name=name,
        gap=_number(gap, f"{pre}gap", positive=True),
        printed_size=None
        if printed_size is None
        else parse_size(printed_size, f"{pre}printed size"),
        any_turn=any_turn,
        size_tol=_number(size_tol, f"{pre}size tolerance", positive=False),
        gap_tol=_number(gap_tol, f"{pre}gap tolerance", positive=False),
        step=_number(step, f"{pre}step", positive=True),
        min_depth=None
        if min_depth is None
        else _number(min_depth, f"{pre}min depth", positive=True),
    )


def load_manifold(path: str) -> Any:
    """mesh file (STL/3MF/OBJ/…; a scene is flattened) -> manifold3d.Manifold"""
    import numpy as np
    import trimesh
    from manifold3d import Manifold, Mesh

    try:
        t = trimesh.load(path, force="mesh", process=False)
    except Exception as e:  # noqa: BLE001 - trimesh raises a zoo of types for unreadable files
        raise FitError(f"cannot load mesh {path}: {e}") from None
    if not isinstance(t, trimesh.Trimesh) or len(t.faces) == 0:
        raise FitError(f"{path} holds no triangles")
    t.merge_vertices()
    m = Manifold(
        Mesh(
            vert_properties=np.asarray(t.vertices, np.float32),
            tri_verts=np.asarray(t.faces, np.uint32),
        )
    )
    if m.is_empty():
        raise FitError(
            f"{path} is not a closed manifold mesh ({m.status().name}); repair it first (3d mesh {path})"
        )
    return m


def union(paths: list[str]) -> Any:
    from manifold3d import Manifold, OpType

    return Manifold.batch_boolean([load_manifold(p) for p in paths], OpType.Add)


def section(manifold: Any, z: float) -> Any:
    """the section of `manifold` at height z as one shapely geometry, rings combined even-odd"""
    from shapely.geometry import Polygon

    out = Polygon()
    for ring in manifold.slice(z).to_polygons():
        if len(ring) > 2:
            out = out.symmetric_difference(
                Polygon(ring).buffer(0)
            )  # outer rings and holes, even-odd
    return out


def size_matches(
    size: tuple[float, float, float],
    printed: tuple[float, float, float],
    tol: float,
    any_turn: bool,
) -> bool:
    turns = set(itertools.permutations(printed)) if any_turn else {printed}
    return any(
        all(abs(s - p) <= tol + 1e-9 for s, p in zip(size, w, strict=True))
        for w in turns
    )


def _fmt_mm(v: float) -> str:
    return f"{v:g}"


def _near_socket(
    insert: Any, socket: Any, z: float, gap: float
) -> tuple[Any, Any, Any]:
    """insert section, socket section, and the socket part within 2*gap+1 mm of the insert at z"""
    insert_sec, socket_sec = section(insert, z), section(socket, z)
    return (
        insert_sec,
        socket_sec,
        socket_sec.intersection(insert_sec.buffer(2 * gap + 1)),
    )


def _socket_top(
    insert: Any, socket: Any, z_in: float, z_out: float, gap: float
) -> float:
    """bisect the height where socket material near the insert ends, between a layer that has
    it (z_in) and one that has not (z_out), to well under the report's 0.01 mm"""
    for _ in range(12):
        mid = (z_in + z_out) / 2
        if _near_socket(insert, socket, mid, gap)[2].is_empty:
            z_out = mid
        else:
            z_in = mid
    return z_in


def check_fit(fit: Fit) -> dict[str, Any]:
    """measure one insert/socket pair; returns the JSON-ready result (ok + errors).
    Tolerances are tested on the unrounded measurements; only the report is rounded."""
    import numpy as np

    insert = union(fit.insert)
    socket = union(fit.socket)
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = insert.bounding_box()
    raw_size = (hi_x - lo_x, hi_y - lo_y, hi_z - lo_z)
    size = tuple(round(s, 3) for s in raw_size)
    errors: list[str] = []
    if fit.printed_size is not None and not size_matches(
        raw_size, fit.printed_size, fit.size_tol, fit.any_turn
    ):
        turn = " in any turn" if fit.any_turn else ""
        errors.append(
            f"size {' x '.join(map(_fmt_mm, size))} mm differs from the printed "
            f"{' x '.join(map(_fmt_mm, fit.printed_size))} mm{turn} by more than ±{_fmt_mm(fit.size_tol)}"
        )
    zs = [float(z) for z in np.arange(lo_z + FIRST_LAYER, hi_z, fit.step)] or [
        (lo_z + hi_z) / 2
    ]
    layers: list[
        dict[str, float]
    ] = []  # every layer with socket material near the insert
    overlap_layers: list[tuple[float, float]] = []
    off_gap_layers: list[dict[str, float]] = []
    last = -1  # index in zs of the highest socket layer
    for i, z in enumerate(zs):
        insert_sec, socket_sec, near = _near_socket(insert, socket, z, fit.gap)
        if near.is_empty:
            continue
        last = i
        gap = float(insert_sec.distance(near))
        layer = {"z": round(z, 2), "gap": round(gap, 3)}
        layers.append(layer)
        overlap = float(insert_sec.intersection(socket_sec).area)
        if overlap > OVERLAP_AREA:
            overlap_layers.append(
                (layer["z"], round(overlap, 4))
            )  # reported once, as an overlap, not as a gap
        elif abs(gap - fit.gap) > fit.gap_tol + 1e-9:
            off_gap_layers.append(layer)
    depth = None
    if layers:
        z_in = zs[last]
        z_out = (
            zs[last + 1] if last + 1 < len(zs) else hi_z - 1e-4
        )  # just under the insert's top face
        if (
            z_out <= z_in
            or not _near_socket(insert, socket, z_out, fit.gap)[2].is_empty
        ):
            depth = max(z_in, z_out) - lo_z  # the socket reaches the insert's top
        else:
            depth = _socket_top(insert, socket, z_in, z_out, fit.gap) - lo_z
    # the layers sample every --step mm; the solids themselves catch an interference between
    # them. Faces in contact (the insert on the socket floor) leave flat zero-volume pieces.
    pieces = [
        p for p in (insert ^ socket).decompose() if p.volume() > OVERLAP_VOLUME / 100
    ]
    overlap_volume = float(sum(p.volume() for p in pieces))
    if overlap_layers:
        shown = ", ".join(f"{z:.2f}" for z, _ in overlap_layers[:5]) + (
            ", …" if len(overlap_layers) > 5 else ""
        )
        n = len(overlap_layers)
        errors.append(
            f"overlaps the socket at z {shown} ({n} layer{'s' if n != 1 else ''}, "
            f"up to {max(a for _, a in overlap_layers):g} mm²)"
        )
    elif overlap_volume > OVERLAP_VOLUME:
        z_lo = min(p.bounding_box()[2] for p in pieces)
        z_hi = max(p.bounding_box()[5] for p in pieces)
        errors.append(
            f"overlaps the socket between the sampled layers, z {z_lo:.2f}–{z_hi:.2f} "
            f"({overlap_volume:.3g} mm³); a smaller --step shows the layers"
        )
    if not layers:
        errors.append(
            f"no socket around it (no socket section within {_fmt_mm(2 * fit.gap + 1)} mm of the insert "
            "at any layer)"
        )
    if off_gap_layers:
        errors.append(
            f"socket gap off {_fmt_mm(fit.gap)}±{_fmt_mm(fit.gap_tol)} mm at {len(off_gap_layers)} of "
            f"{len(layers)} layers: {_layer_list(off_gap_layers)}"
        )
    if fit.min_depth is not None and depth is not None and depth < fit.min_depth:
        errors.append(
            f"socket only {depth:.2f} mm deep, less than the required {_fmt_mm(fit.min_depth)} mm"
        )
    gaps = [g["gap"] for g in layers]
    return {
        "insert": fit.name,
        "socket": fit.socket_name,
        "insert_files": fit.insert,
        "socket_files": fit.socket,
        "status": "FAIL" if errors else "PASS",
        "ok": not errors,
        "insert_size_mm": list(size),
        "printed_size_mm": list(fit.printed_size)
        if fit.printed_size is not None
        else None,
        "any_turn": fit.any_turn,
        "size_tol_mm": fit.size_tol,
        "gap_set_mm": fit.gap,
        "gap_tol_mm": fit.gap_tol,
        "step_mm": fit.step,
        "min_depth_mm": fit.min_depth,
        "gap_mm": min(gaps) if gaps else None,
        "gap_max_mm": max(gaps) if gaps else None,
        "socket_depth_mm": None if depth is None else round(depth, 2),
        "overlap_z_mm": [z for z, _ in overlap_layers],
        "overlap_volume_mm3": round(overlap_volume, 4),
        "layers": layers,
        "errors": errors,
    }


def _layer_list(layers: list[dict[str, float]]) -> str:
    return ", ".join(f"z {g['z']:.2f}: {_fmt_mm(g['gap'])}" for g in layers[:5]) + (
        ", …" if len(layers) > 5 else ""
    )


def _line(r: dict[str, Any]) -> str:
    size = " x ".join(map(_fmt_mm, r["insert_size_mm"]))
    printed = (
        f" (printed {' x '.join(map(_fmt_mm, r['printed_size_mm']))})"
        if r["printed_size_mm"]
        else ""
    )
    if r["gap_mm"] is None:
        gap = "no socket"
    else:
        gap = (
            f"gap {_fmt_mm(r['gap_mm'])}–{_fmt_mm(r['gap_max_mm'])} mm (set {_fmt_mm(r['gap_set_mm'])}"
            f"±{_fmt_mm(r['gap_tol_mm'])}) over {_fmt_mm(r['socket_depth_mm'])} mm"
        )
    return f"fit {r['insert']} ↔ {r['socket']}: {r['status']}: size {size} mm{printed}, {gap}"


def run_fits(fits: list[Fit], as_json: bool) -> int:
    results = []
    for fit in fits:
        try:
            results.append(check_fit(fit))
        except FitError as e:  # noqa: PERF203 — stop at the first fit that cannot be checked
            print(f"error: {e}", file=sys.stderr)
            return 2
        except Exception as e:  # noqa: BLE001 - a geometry crash must not exit 1, which means FAIL
            print(
                f"error: checking {fit.name} ↔ {fit.socket_name} failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return 2
    status = "PASS" if all(r["ok"] for r in results) else "FAIL"
    if as_json:
        print(
            json.dumps(
                {"status": status, "fits": results}, indent=1, ensure_ascii=False
            )
        )
    else:
        for r in results:
            print(_line(r))
            for err in r["errors"]:
                print(f"    FAIL {err}")
        print(
            f">>> FIT-CHECK: {status} ({len(results)} fit{'s' if len(results) != 1 else ''})"
        )
    return 0 if status == "PASS" else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="3d fit-check", description="check an insert against its socket"
    )
    ap.add_argument(
        "--insert",
        action="append",
        help="insert mesh(es), comma-joined or repeated (unioned); globs ok",
    )
    ap.add_argument(
        "--socket",
        action="append",
        help="socket mesh(es), comma-joined or repeated (unioned); globs ok",
    )
    ap.add_argument("--gap", help="designed clearance per side, mm")
    ap.add_argument("--printed-size", help="X,Y,Z of the printed insert, mm")
    ap.add_argument(
        "--any-turn",
        action="store_true",
        help="accept any axis permutation of --printed-size",
    )
    ap.add_argument("--name", default="", help="insert name in the report")
    ap.add_argument(
        "--size-tol", help=f"printed-size tolerance, mm (default {SIZE_TOL})"
    )
    ap.add_argument("--gap-tol", help=f"clearance tolerance, mm (default {GAP_TOL})")
    ap.add_argument("--step", help=f"layer step up the socket, mm (default {STEP})")
    ap.add_argument(
        "--min-depth", help="FAIL when the socket is shallower than this, mm"
    )
    ap.add_argument("--spec", help="YAML/JSON file listing many fits")
    ap.add_argument("--json", action="store_true", help="JSON report on stdout")
    a = ap.parse_args(argv)
    try:
        if a.spec:
            pair_flags = [
                f
                for f, v in (
                    ("--insert", a.insert),
                    ("--socket", a.socket),
                    ("--gap", a.gap),
                    ("--printed-size", a.printed_size),
                    ("--any-turn", a.any_turn),
                    ("--name", a.name),
                    ("--size-tol", a.size_tol),
                    ("--gap-tol", a.gap_tol),
                    ("--step", a.step),
                    ("--min-depth", a.min_depth),
                )
                if v
            ]
            if pair_flags:
                raise FitError(
                    f"--spec takes every fit from the file; drop {', '.join(pair_flags)} "
                    "(put them in the spec, top level for all fits)"
                )
            fits = load_spec(a.spec)
        else:
            missing = [
                f
                for f, v in (
                    ("--insert", a.insert),
                    ("--socket", a.socket),
                    ("--gap", a.gap),
                )
                if not v
            ]
            if missing:
                raise FitError(
                    f"missing {', '.join(missing)} (or give --spec fits.yaml)"
                )
            if a.any_turn and a.printed_size is None:
                raise FitError("--any-turn needs --printed-size")
            fits = [
                make_fit(
                    insert=expand(_paths(",".join(a.insert), "--insert"), "", "insert"),
                    socket=expand(_paths(",".join(a.socket), "--socket"), "", "socket"),
                    gap=a.gap,
                    name=a.name,
                    printed_size=a.printed_size,
                    any_turn=a.any_turn,
                    size_tol=SIZE_TOL if a.size_tol is None else a.size_tol,
                    gap_tol=GAP_TOL if a.gap_tol is None else a.gap_tol,
                    step=STEP if a.step is None else a.step,
                    min_depth=a.min_depth,
                )
            ]
    except FitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return run_fits(fits, a.json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
