"""3d 3mf — edit a project 3MF on the fly in a shell pipe: ls, pick, orient, arrange, check.

WHAT: each subcommand reads a 3MF (a path, or `-` / nothing for stdin), changes it in memory
  and writes it to stdout (or -o FILE), so the steps chain:
    3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf orient --face-down top | 3d 3mf arrange --printer k1se \
      | 3d 3mf check --printer k1se | 3d slice - | 3d print-preflight - --printer k1se \
      | 3d printer start k1se - --yes
  The project's presets, filaments, mixed filaments and per-object extruders are kept: only
  the build, the objects and plates and Orca's per-object files change
  (lib/slicing/project_3mf.py).

WHY: reprinting a few parts of a big project otherwise means the slicer GUI — copy the
  project, delete the rest, rotate, re-arrange, export, upload. One pipe does it, with the
  3MF checks and print-preflight in between, and starts only with --yes.

Exit codes: 0 ok (check: PASS/WARN), 1 a gate FAILs or the parts do not fit the bed,
  2 usage / unreadable 3MF / a selector matching nothing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, TextIO

from cli.registry import Command
from errors import GateFailure, InvalidArgument, UsageError

if TYPE_CHECKING:
    from slicing.project_3mf import Bed, Project

COMMAND_NAME = "3mf"

USAGE = """3d 3mf <ls|pick|orient|arrange|check> [FILE|-] [options]
  Edit an Orca/Bambu project 3MF in a shell pipe. FILE `-` (or no FILE) reads stdin; the
  result goes to stdout when it is not a terminal (or -o FILE / -o -). Human output goes to
  stderr while stdout carries the 3MF. Presets, filaments, mixed filaments and per-object
  extruders are kept.

  ls [FILE] [--json]               objects of the build: id, name, plate, extruder, size, position
  pick [FILE] SELECTORS [-o OUT]   keep only the matched objects. SELECTORS: comma-separated (or
                                   several args); each is an object id, a name, or the first
                                   word of a name, with shell globs: 'CN,SW*', 1002, 'wall_?'.
                                   A selector that matches nothing exits 2 listing the names.
  orient [FILE] (--face-down FACE | --rotate X,Y,Z) [--only SELECTORS] [-o OUT]
                                   turn each object (or the --only ones) about its centre and
                                   drop it onto the bed. FACE: top, bottom, front, back, left,
                                   right (that side of the part as it lies now goes down) or
                                   largest (the largest flat face of its convex hull).
                                   --rotate: degrees about X, then Y, then Z.
  arrange [FILE] [--printer NAME | --bed WxD] [--gap MM] [--margin MM] [-o OUT]
                                   pack every object onto plate 1 (the shelf packer of
                                   `3d arrange`), centred; other plates are removed. Bed: --bed,
                                   else the project's printable_area, else --printer's build
                                   volume. Exit 1 naming the parts that do not fit one plate.
  check [FILE] [--printer NAME] [--json] [-o OUT]
                                   printer (project preset vs --printer), plates, bed fit,
                                   height, on bed, overhang (WARN over 100 mm2 at > 45 deg).
                                   In a pipe the 3MF is passed on only when nothing FAILs.

Options:
  -o, --out PATH      write the 3MF to PATH (`-` = stdout). Default: stdout when it is not a
                      terminal; a terminal gets an error instead of binary
  --printer NAME      a fleet printer (`3d printer list`: k1se, u1) or a model from
                      `3d printers list` ("Creality K1 SE"); orient/arrange/check compare it
                      with the project's printer_model and arrange takes its bed from it
  --bed WxD           bed size in mm for arrange, e.g. 220x220
  --gap MM            arrange: clearance between parts (default 6)
  --margin MM         arrange: keep-out from each bed edge (default 8)
  --only SELECTORS    orient: turn only these objects (default: all)
  --json              ls/check: JSON instead of text (check: on stdout unless -o is given)

Examples:
  3d 3mf ls proj.3mf
  3d 3mf pick proj.3mf 'CN,SW*' -o two.3mf
  3d 3mf pick proj.3mf 1002 | 3d 3mf orient --face-down largest -o sw.3mf
  3d 3mf orient two.3mf --rotate 90,0,0 --only CN -o two_turned.3mf
  3d 3mf arrange two.3mf --bed 220x220 --gap 4 --margin 10 -o two_k1.3mf
  3d 3mf check two_k1.3mf --printer k1se --json
  3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf orient --face-down top | 3d 3mf arrange --printer k1se \\
    | 3d 3mf check --printer k1se | 3d slice - | 3d print-preflight - --printer k1se \\
    | 3d printer start k1se - --yes"""

_SUBS = ("ls", "pick", "orient", "arrange", "check")
_FLAGS_WITH_VALUE = {
    "-o",
    "--out",
    "--printer",
    "--bed",
    "--gap",
    "--margin",
    "--only",
    "--face-down",
    "--rotate",
}
_ALLOWED = {
    "ls": {"--json"},
    "pick": {"-o", "--out"},
    "orient": {"-o", "--out", "--face-down", "--rotate", "--only", "--printer"},
    "arrange": {"-o", "--out", "--printer", "--bed", "--gap", "--margin"},
    "check": {"-o", "--out", "--printer", "--json"},
}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if sub not in _SUBS:
        raise InvalidArgument("subcommand", sub, _SUBS, command=COMMAND_NAME)
    if any(a in ("-h", "--help") for a in rest):
        print(USAGE)
        return 0
    pos, opts = _parse(sub, rest)
    from slicing.project_3mf import ProjectError, SelectorError

    try:
        return _DISPATCH[sub](pos, opts)
    except SelectorError as exc:
        raise UsageError(
            str(exc),
            command=COMMAND_NAME,
            remediation=[
                "Objects: " + "; ".join(exc.names),
                "List them: 3d 3mf ls FILE",
            ],
        ) from exc
    except ProjectError as exc:
        raise UsageError(str(exc), command=COMMAND_NAME) from exc


def _parse(sub: str, args: list[str]) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-" or not a.startswith("-"):
            pos.append(a)
            i += 1
            continue
        if a not in _ALLOWED[sub]:
            raise UsageError(
                f"unknown option {a} for `3d 3mf {sub}`",
                command=COMMAND_NAME,
                remediation=["See `3d 3mf --help`."],
            )
        if a in _FLAGS_WITH_VALUE:
            if i + 1 >= len(args):
                raise UsageError(f"{a} needs a value", command=COMMAND_NAME)
            opts["-o" if a == "--out" else a] = args[i + 1]
            i += 2
        else:
            opts[a] = ""
            i += 1
    return pos, opts


def _input(pos: list[str], sub: str, max_pos: int = 1) -> tuple[str, list[str]]:
    """(input path or `-`, remaining positionals). No FILE = stdin."""
    if len(pos) > max_pos:
        raise UsageError(
            f"too many arguments for `3d 3mf {sub}`: {' '.join(pos)}",
            command=COMMAND_NAME,
        )
    return (pos[0], pos[1:]) if len(pos) == max_pos else ("-", pos)


def _load(path: str) -> Project:
    from cli.pipe import read_input
    from slicing.project_3mf import Project

    data = read_input(path, command=COMMAND_NAME, what="3MF")
    return Project(data, label="stdin" if path == "-" else path)


def _log_stream(opts: dict[str, str]) -> TextIO:
    """stderr while stdout carries the 3MF, else stdout."""
    from cli.pipe import stdout_is_stream

    return sys.stderr if stdout_is_stream(opts.get("-o")) else sys.stdout


def _emit(project: Project, opts: dict[str, str], sub: str, summary: str) -> int:
    from cli.pipe import write_output

    where = write_output(
        project.to_bytes(), opts.get("-o"), command=COMMAND_NAME, what="3MF"
    )
    print(
        f"3mf {sub}: {summary} -> {where}",
        file=sys.stderr if where == "stdout" else sys.stdout,
    )
    return 0


def _float(flag: str, raw: str, *, positive: bool = True) -> float:
    try:
        v = float(raw)
    except ValueError:
        v = float("nan")
    if not (v > 0 if positive else v >= 0):
        raise InvalidArgument(
            flag,
            raw,
            ["a positive number in mm" if positive else "a number >= 0 in mm"],
            command=COMMAND_NAME,
        )
    return v


# ---------------------------------------------------------------------------- printers
def _printer_model(name: str) -> tuple[str, list[float]]:
    """(model name, [x, y, z] build volume) of a fleet printer name or a registry model."""
    from printops.fleet import load_fleet
    from registries.printers import get_printer

    fleet = load_fleet()
    model = (
        fleet.printers[name].model
        if fleet is not None and name in fleet.printers
        else name
    )
    return model, list(get_printer(model, command=COMMAND_NAME).bed)


def _require_same_printer(project: Project, model: str, name: str) -> None:
    own = project.printer_model()
    if own is not None and own != model:
        raise GateFailure(
            f"the project is set up for {own}, --printer {name} is a {model}",
            command=COMMAND_NAME,
            remediation=[
                "Switch the printer in the slicer and save the project, or pass the printer it is set up for."
            ],
        )


def _bed(project: Project, opts: dict[str, str]) -> Bed:
    from slicing.project_3mf import Bed

    if "--bed" in opts:
        raw = opts["--bed"].lower().split("x")
        if len(raw) != 2:
            raise InvalidArgument(
                "--bed",
                opts["--bed"],
                ["WxD in mm, e.g. 220x220"],
                command=COMMAND_NAME,
            )
        return Bed(_float("--bed", raw[0]), _float("--bed", raw[1]), None, "--bed")
    own = project.bed()
    if own is not None:
        return own
    if "--printer" in opts:
        model, vol = _printer_model(opts["--printer"])
        return Bed(
            vol[0], vol[1], vol[2] if len(vol) > 2 else None, f"build volume of {model}"
        )
    raise UsageError(
        "no bed size: the 3MF has no printable_area",
        command=COMMAND_NAME,
        remediation=["Pass --printer NAME (e.g. k1se) or --bed WxD (e.g. 220x220)."],
    )


# ---------------------------------------------------------------------------- subcommands
def _cmd_ls(pos: list[str], opts: dict[str, str]) -> int:
    import json

    path, _ = _input(pos, "ls")
    project = _load(path)
    rows: list[dict[str, Any]] = []
    for it in project.items():
        mesh = project.mesh(it)
        size = mesh.hi - mesh.lo
        rows.append(
            {
                "id": it.object_id,
                "name": it.name,
                "plate": it.plate,
                "extruder": it.extruder,
                "size_mm": [round(float(v), 2) + 0.0 for v in size],  # + 0.0: no "-0.0"
                "min_mm": [round(float(v), 2) + 0.0 for v in mesh.lo],
            }
        )
    if "--json" in opts:
        print(
            json.dumps(
                {"printer_model": project.printer_model(), "objects": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        f"{project.label}: {len(rows)} object(s); printer preset: {project.printer_model() or '-'}"
    )
    print(
        f"{'id':>6}  {'plate':>5}  {'ext':>3}  {'size X x Y x Z mm':<22} {'at X, Y, Z':<22} name"
    )
    for r in rows:
        size = " x ".join(f"{v:.1f}" for v in r["size_mm"])
        at = ", ".join(f"{v:.1f}" for v in r["min_mm"])
        print(
            f"{r['id']:>6}  {r['plate'] or '-':>5}  {r['extruder'] or '-':>3}  {size:<22} {at:<22} {r['name']}"
        )
    return 0


def _cmd_pick(pos: list[str], opts: dict[str, str]) -> int:
    from slicing.project_3mf import split_selectors

    if not pos:
        raise UsageError(
            "pick needs SELECTORS, e.g. 3d 3mf pick proj.3mf 'CN,SW*'",
            command=COMMAND_NAME,
        )
    path, sels = (pos[0], pos[1:]) if len(pos) >= 2 else ("-", pos)
    selectors = split_selectors(sels)
    if not selectors:
        raise UsageError("pick needs at least one selector", command=COMMAND_NAME)
    project = _load(path)
    before = len(project.items())
    kept = project.pick(selectors)
    names = ", ".join(dict.fromkeys(it.name for it in kept))
    return _emit(
        project, opts, "pick", f"kept {len(kept)} of {before} object(s): {names}"
    )


def _cmd_orient(pos: list[str], opts: dict[str, str]) -> int:
    from slicing.project_3mf import (
        FACE_CHOICES,
        FACES,
        largest_flat_face,
        matches,
        rotation_down,
        rotation_xyz,
        split_selectors,
    )

    path, _ = _input(pos, "orient")
    face, rotate = opts.get("--face-down"), opts.get("--rotate")
    if (face is None) == (rotate is None):
        raise UsageError(
            "orient needs exactly one of --face-down FACE or --rotate X,Y,Z",
            command=COMMAND_NAME,
        )
    if face is not None and face not in FACE_CHOICES:
        raise InvalidArgument("--face-down", face, FACE_CHOICES, command=COMMAND_NAME)
    angles: list[float] = []
    if rotate is not None:
        try:
            angles = [float(a) for a in rotate.split(",")]
        except ValueError:
            angles = []
        if len(angles) != 3:
            raise InvalidArgument(
                "--rotate",
                rotate,
                ["X,Y,Z in degrees, e.g. 90,0,0"],
                command=COMMAND_NAME,
            )
    project = _load(path)
    if "--printer" in opts:
        model, _vol = _printer_model(opts["--printer"])
        _require_same_printer(project, model, opts["--printer"])
    items = project.items()
    only = split_selectors([opts["--only"]]) if "--only" in opts else []
    for sel in only:
        if not any(matches(it.name, it.object_id, sel) for it in items):
            from slicing.project_3mf import SelectorError

            raise SelectorError(sel, sorted({it.name for it in items}))
    chosen = [
        it
        for it in items
        if not only or any(matches(it.name, it.object_id, s) for s in only)
    ]
    blockers = project.orient_blockers(chosen)
    if blockers:
        raise UsageError(
            "cannot turn objects whose per-object settings live in their own frame: "
            + "; ".join(blockers),
            command=COMMAND_NAME,
            remediation=[
                "Remove those settings in the slicer first, or leave those objects out (--only)."
            ],
        )
    log = _log_stream(opts)
    for it in chosen:
        if rotate is not None:
            rot = rotation_xyz(*angles)
        elif face == "largest":
            rot = rotation_down(largest_flat_face(project.mesh(it)))
        else:
            assert face is not None
            rot = rotation_down(FACES[face])
        mesh = project.orient(it, rot)
        size = " x ".join(f"{v:.1f}" for v in mesh.hi - mesh.lo)
        print(f"  {it.name}: now {size} mm", file=log)
    how = f"face {face} down" if face else f"rotated {rotate}"
    return _emit(project, opts, "orient", f"{len(chosen)} object(s) {how}, on the bed")


def _cmd_arrange(pos: list[str], opts: dict[str, str]) -> int:
    from slicing.project_3mf import ProjectError, arrange

    path, _ = _input(pos, "arrange")
    gap = _float("--gap", opts.get("--gap", "6"), positive=False)
    margin = _float("--margin", opts.get("--margin", "8"), positive=False)
    project = _load(path)
    if "--printer" in opts:
        model, _vol = _printer_model(opts["--printer"])
        _require_same_printer(project, model, opts["--printer"])
    bed = _bed(project, opts)
    try:
        placed = arrange(project, bed, gap=gap, margin=margin)
    except ProjectError as exc:
        raise GateFailure(
            str(exc),
            command=COMMAND_NAME,
            remediation=[
                "Pick fewer parts, lower --gap/--margin, or turn parts (3d 3mf orient)."
            ],
        ) from exc
    log = _log_stream(opts)
    for p in placed:
        print(f"  {p.name}: at X {p.x:.1f}, Y {p.y:.1f}", file=log)
    return _emit(
        project,
        opts,
        "arrange",
        f"{len(placed)} object(s) on one {bed.width:g} x {bed.depth:g} mm plate ({bed.source})",
    )


def _cmd_check(pos: list[str], opts: dict[str, str]) -> int:
    import json

    from cli.pipe import stdout_is_stream
    from slicing.project_3mf import check

    path, _ = _input(pos, "check")
    project = _load(path)
    model = None
    bed = project.bed()
    if "--printer" in opts:
        model, vol = _printer_model(opts["--printer"])
        if bed is None:
            from slicing.project_3mf import Bed

            bed = Bed(
                vol[0],
                vol[1],
                vol[2] if len(vol) > 2 else None,
                f"build volume of {model}",
            )
    report = check(project, bed, printer_model=model)
    out = opts.get("-o")
    passes_on = out is not None or ("--json" not in opts and stdout_is_stream(out))
    log = sys.stderr if passes_on and (out in (None, "-")) else sys.stdout
    if "--json" in opts:
        print(
            json.dumps(
                {"file": project.label, **report.to_dict()},
                ensure_ascii=False,
                indent=2,
            ),
            file=log,
        )
    else:
        print(
            f"3mf check: {project.label}" + (f" (bed: {bed.source})" if bed else ""),
            file=log,
        )
        print(report.render(), file=log)
    if report.overall == "FAIL":
        raise GateFailure(
            "3mf check: FAIL — nothing passed on",
            command=COMMAND_NAME,
            silent=passes_on is False,
        )
    if passes_on:
        from cli.pipe import write_output

        write_output(project.to_bytes(), out, command=COMMAND_NAME, what="3MF")
    return 0


_DISPATCH = {
    "ls": _cmd_ls,
    "pick": _cmd_pick,
    "orient": _cmd_orient,
    "arrange": _cmd_arrange,
    "check": _cmd_check,
}

COMMAND = Command(
    name=COMMAND_NAME,
    group="SLICING",
    summary="edit a project 3MF in a pipe: ls / pick parts / orient (face down) / arrange on a bed / check",
    usage="3mf <ls|pick|orient|arrange|check> [FILE|-] [options]",
    run=run,
)
