"""cli.py — flags and output of `3d colour-regions <build|shell-volume|classify|transfer|transfer-class|snap|verify>`.

The usage text lives in `commands/colour_regions.py` (stdlib-only, registry contract); this module parses the flags of
each subcommand (`PARSERS`, one factory per subcommand), calls the libraries and prints the result. Structured errors
(`errors.py`) escape to the entry script.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import numpy as np

from colour_regions import CMD
from colour_regions import build as build_mod
from colour_regions import snap as snap_mod
from colour_regions import transfer as transfer_mod
from colour_regions import verify as verify_mod
from colour_regions.labels import mesh_digest, resolve_labels, write_labels
from colour_regions.rules import DEFAULT_TRANSFER_TOL_MM, load_rules
from colour_regions.surface import Surf
from colour_regions.volume import shell_volume
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

DEFAULT_MAX_DIST_MM = 6.0
DEFAULT_ISLAND_FILL_MM2 = 6.0
DEFAULT_MIN_CELL_AREA_MM2 = 0.3


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, command=CMD, remediation=["Run `3d colour-regions --help` for the flags and examples."])


def _pos_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a positive number")
    return value


def _pos_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a positive integer")
    return value


def _parser(sub: str) -> _Parser:
    return _Parser(prog=f"3d {CMD} {sub}", add_help=False, allow_abbrev=False)


def _fullmatch(pattern: str, name: str, flag: str) -> bool:
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error as exc:
        raise InvalidArgument(flag, pattern, [f"a valid regex ({exc})"], command=CMD) from exc


def _names(raw: str) -> list[str]:
    return [n for n in raw.split(",") if n]


def _need_file(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise InputNotFound(str(p), command=CMD)
    return p


def _emit(obj: Any) -> None:
    print(json.dumps(obj, indent=1, default=float))


# ----------------------------------------------------------------------------------------------- parsers
def _p_build() -> _Parser:
    p = _parser("build")
    p.add_argument("mesh")
    p.add_argument("--track", action="append", required=True)
    p.add_argument("--rules", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--island-mm2", type=_pos_float)
    p.add_argument("--transfer-tol", type=_pos_float)
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def _p_shell_volume() -> _Parser:
    p = _parser("shell-volume")
    p.add_argument("mesh")
    p.add_argument("--labels", required=True)
    p.add_argument("--select", action="append", required=True)
    p.add_argument("--depth", type=_pos_float, required=True)
    p.add_argument("--pitch", type=_pos_float, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--min-area", type=_pos_float, default=DEFAULT_MIN_CELL_AREA_MM2)
    p.add_argument("--tol", type=_pos_float, default=DEFAULT_TRANSFER_TOL_MM)
    p.add_argument("--json", action="store_true")
    return p


def _p_classify() -> _Parser:
    p = _parser("classify")
    p.add_argument("skin")
    p.add_argument("--part", action="append", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--shift", default="0,0,0")
    p.add_argument("--tol", type=_pos_float, default=transfer_mod.DEFAULT_PART_TOL_MM)
    p.add_argument("--split-edge", type=_pos_float)
    p.add_argument("--split-box")
    p.add_argument("--json", action="store_true")
    return p


def _p_transfer() -> _Parser:
    p = _parser("transfer")
    p.add_argument("mesh")
    p.add_argument("--labels", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--tol", type=_pos_float, default=DEFAULT_TRANSFER_TOL_MM)
    p.add_argument("--json", action="store_true")
    return p


def _p_transfer_class() -> _Parser:
    p = _parser("transfer-class")
    p.add_argument("mesh")
    p.add_argument("--source", required=True)
    p.add_argument("--positive", required=True)
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--exclude", default="")
    p.add_argument("--max-dist", type=_pos_float, default=DEFAULT_MAX_DIST_MM)
    p.add_argument("--island-mm2", type=_pos_float, default=DEFAULT_ISLAND_FILL_MM2)
    p.add_argument("--fill-box", action="append", default=[])
    p.add_argument("--json", action="store_true")
    return p


def _p_snap() -> _Parser:
    p = _parser("snap")
    p.add_argument("mesh")
    p.add_argument("--labels", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--band", type=_pos_float, required=True)
    p.add_argument("--scale", type=_pos_float, default=snap_mod.DEFAULT_SCALE_MM)
    p.add_argument("--min-crease", type=_pos_float, default=snap_mod.MIN_CREASE_DEG)
    p.add_argument("--fixed", action="append", default=[])
    p.add_argument("--tol", type=_pos_float, default=DEFAULT_TRANSFER_TOL_MM)
    p.add_argument("--json", action="store_true")
    return p


def _p_verify() -> _Parser:
    d = verify_mod.Thresholds()
    p = _parser("verify")
    p.add_argument("mesh")
    p.add_argument("--work", required=True)
    p.add_argument("--regions")
    p.add_argument("--skip-volumes", action="store_true")
    p.add_argument("--waive", default="")
    p.add_argument("--approved-groups", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("--report")
    p.add_argument("--dihedral", type=_pos_float, default=d.dihedral)
    p.add_argument("--crease", type=_pos_float, default=d.crease)
    p.add_argument("--min-share", type=_pos_float, default=d.min_share)
    p.add_argument("--min-group", type=_pos_float, default=d.min_group)
    p.add_argument("--shift-mm", type=_pos_float, default=d.shift_mm)
    p.add_argument("--shift-margin", type=_pos_float, default=d.shift_margin)
    p.add_argument("--chance-margin", type=_pos_float, default=d.chance_margin)
    p.add_argument("--agree", type=_pos_float, default=d.agree)
    p.add_argument("--samples", type=_pos_int, default=verify_mod.DEFAULT_SAMPLES)
    return p


PARSERS: dict[str, Callable[[], _Parser]] = {
    "build": _p_build,
    "shell-volume": _p_shell_volume,
    "classify": _p_classify,
    "transfer": _p_transfer,
    "transfer-class": _p_transfer_class,
    "snap": _p_snap,
    "verify": _p_verify,
}


# ----------------------------------------------------------------------------------------------- handlers
def cmd_build(ns: argparse.Namespace) -> int:
    _need_file(ns.mesh)
    rules = load_rules(ns.rules)
    if ns.island_mm2 is not None:
        rules = dataclasses.replace(rules, island_mm2=ns.island_mm2)
    if ns.transfer_tol is not None:
        rules = dataclasses.replace(rules, transfer_tol_mm=ns.transfer_tol)
    tracks = [build_mod.parse_track(t) for t in ns.track]
    res = build_mod.build(ns.mesh, tracks, rules, ns.out, force=ns.force, verbose=not (ns.quiet or ns.json))
    if ns.json:
        _emit({"out": str(Path(ns.out).resolve()), "regions": res.regions, "groups": res.groups, "labels": res.labels, "stats": res.stats})
    else:
        print(f"regions: {[r['name'] for r in res.regions]} labels: {len(res.labels)}")
        print(f"output: {Path(ns.out) / 'regions.json'}  (patches/*.ply, labels_final.npz, transfer.json)")
    return 0


def cmd_shell_volume(ns: argparse.Namespace) -> int:
    _need_file(ns.mesh)
    s = Surf(ns.mesh)
    lab, names, _ = resolve_labels(ns.labels, s, ns.tol)
    ids = [i for i, n in enumerate(names) if any(_fullmatch(rx, n, "--select") for rx in ns.select)]
    if not ids:
        raise InvalidArgument("--select", " ".join(ns.select), names, command=CMD)
    sel = np.isin(lab, ids) & s.outer
    cells = shell_volume(s, sel, ns.depth, ns.pitch, ns.out, min_area_mm2=ns.min_area)
    info = {
        "out": str(Path(ns.out).resolve()),
        "labels": [names[i] for i in ids],
        "faces": int(sel.sum()),
        "area_mm2": round(float(s.area[sel].sum()), 2),
        "volume_mm3": round(float(cells.volume), 3),
        "watertight": bool(cells.is_watertight),
    }
    if ns.json:
        _emit(info)
    else:
        print(f"{info['out']}: {info['faces']} faces, {info['area_mm2']} mm2 -> closed volume {info['volume_mm3']} mm3")
    return 0


def cmd_classify(ns: argparse.Namespace) -> int:
    _need_file(ns.skin)
    if ns.split_box and not ns.split_edge:
        raise UsageError("--split-box needs --split-edge", command=CMD, remediation=["Add e.g. --split-edge 2."])
    report = transfer_mod.classify_file(
        ns.skin,
        [transfer_mod.parse_part(x) for x in ns.part],
        ns.out,
        tol=ns.tol,
        shift=transfer_mod.parse_vec3(ns.shift, "--shift"),
        split_edge=ns.split_edge,
        split_box=transfer_mod.parse_box(ns.split_box, "--split-box") if ns.split_box else None,
    )
    if ns.json:
        _emit(report)
    else:
        print(f"{ns.out}: {report['labelled_faces']} faces ({report['unlabelled']} unlabelled, {report['split_faces']} added by splitting) {report['counts']}")
    return 0


def cmd_transfer(ns: argparse.Namespace) -> int:
    _need_file(ns.mesh)
    stats = transfer_mod.transfer_file(ns.mesh, ns.labels, ns.out, ns.tol)
    if ns.json:
        _emit(stats)
    else:
        print(f"{ns.out}: {stats['mode']} onto {stats['faces']} faces {stats['labels']}")
    return 0


def cmd_transfer_class(ns: argparse.Namespace) -> int:
    boxes = [transfer_mod.parse_fill_box(raw) for raw in ns.fill_box]
    _need_file(ns.mesh)
    report = transfer_mod.transfer_class_file(
        ns.mesh,
        ns.source,
        ns.out,
        positive=_names(ns.positive),
        exclude=_names(ns.exclude),
        as_name=ns.as_name,
        max_dist=ns.max_dist,
        island_mm2=ns.island_mm2,
        fill_boxes=boxes,
    )
    if ns.json:
        _emit(report)
    else:
        print(
            f"{ns.out}: {report['new_positive_faces']} of {report['new_faces']} faces are {ns.as_name} "
            f"({report['new_positive_area_mm2']} mm2), islands filled {report['filled_islands']}"
        )
    return 0


def cmd_snap(ns: argparse.Namespace) -> int:
    _need_file(ns.mesh)
    s = Surf(ns.mesh)
    lab, names, _ = resolve_labels(ns.labels, s, ns.tol)
    _, cost = snap_mod.crease_cost(s, ns.scale, ns.min_crease)
    fixed = snap_mod.fixed_mask(lab, names, ns.fixed)  # raises on an invalid regex
    new = snap_mod.snap_labels(s, lab, fixed, ns.band, cost)
    write_labels(ns.out, new, names, digest=mesh_digest(s.mesh.triangles))
    report = snap_mod.snap_report(lab, new, names)
    if ns.json:
        _emit(report)
    else:
        print(f"{ns.out}: {report['changed_faces']} faces changed label (band {ns.band} mm)")
    return 0


def cmd_verify(ns: argparse.Namespace) -> int:
    _need_file(ns.mesh)
    th = dataclasses.replace(
        verify_mod.Thresholds(),
        dihedral=ns.dihedral,
        crease=ns.crease,
        min_share=ns.min_share,
        min_group=ns.min_group,
        shift_mm=ns.shift_mm,
        shift_margin=ns.shift_margin,
        chance_margin=ns.chance_margin,
        agree=ns.agree,
    )
    group, names, _ = build_mod.load_groups(ns.work)
    regions = None if ns.skip_volumes else (ns.regions or str(Path(ns.work) / "regions.json"))
    result, lines = verify_mod.verify_work(
        ns.mesh, group, names, th=th, waive=set(_names(ns.waive)), approved=set(_names(ns.approved_groups)), regions=regions, samples=ns.samples
    )
    if ns.report:
        Path(ns.report).write_text(json.dumps(result, indent=1, default=float), encoding="utf-8")
    if ns.json:
        _emit(result)
    else:
        print("\n".join(lines))
    if not result["pass"]:
        raise GateFailure("colour-regions verify: FAIL", command=CMD, silent=True)
    if not ns.json:
        print("colour-regions verify: PASS")
    return 0


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "build": cmd_build,
    "shell-volume": cmd_shell_volume,
    "classify": cmd_classify,
    "transfer": cmd_transfer,
    "transfer-class": cmd_transfer_class,
    "snap": cmd_snap,
    "verify": cmd_verify,
}


#: options whose value is a comma list that may start with a minus sign (argparse would take it for a flag)
LIST_VALUED = ("--shift", "--split-box", "--fill-box")


def glue_list_values(argv: list[str]) -> list[str]:
    """`--split-box -50,50,-20,20,0,40` -> `--split-box=-50,50,-20,20,0,40` so argparse reads the value as a value."""
    out: list[str] = []
    for i, a in enumerate(argv):
        if i > 0 and argv[i - 1] in LIST_VALUED and a.startswith("-"):
            out[-1] = f"{out[-1]}={a}"
        else:
            out.append(a)
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in HANDLERS:
        raise InvalidArgument("subcommand", argv[0] if argv else "", list(HANDLERS), command=CMD)
    return HANDLERS[argv[0]](PARSERS[argv[0]]().parse_args(glue_list_values(argv[1:])))
