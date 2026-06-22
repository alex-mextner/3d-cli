"""3d arrange — split parts and pack them onto bed-sized plates.

WHAT: takes a .3mf (parts in assembled positions) or one-or-more .stl files, splits each
  object into CONNECTED bodies, lays every part flat (drops min-Z to 0), shelf-packs the XY
  footprints onto square plates that fit the printer bed, and centers each plate.

  DEFAULT (single mode): writes ONE multi-plate Orca/Bambu PROJECT `<prefix>.3mf` holding
  every plate, with each part assigned to its plate — OrcaSlicer-based slicers (incl. the
  Snapmaker U1's) open it and show all N plates. `--per-plate` writes the legacy output of
  one print-ready `<prefix>_plate1.3mf`, `_plate2.3mf`, ... instead.

  Every written 3MF is reloaded and verified: the single project checks valid zip + the
  required OPC/Bambu entries + object count + plate count + per-object plate assignment +
  per-plate bed fit; per-plate files check object count + bed fit.

WHY: a 3MF whose parts sit in their assembled positions dumps everything onto ONE plate, which
  overflows the bed (default 270x270 mm, Snapmaker U1). `arrange` distributes the parts across
  as many plates as needed so each plate is actually printable — in one file the user opens once.

Exit codes:
  0   success (plates written + verified)
  1   a single part is larger than the bed (named, with its size)
  2   usage / IO error
  127 a python runtime / trimesh missing (via pyrun)

Examples:
  3d arrange assembly.3mf
  3d arrange assembly.3mf --per-plate -o out/tray
  3d arrange assembly.3mf --bed 270 --gap 6 --margin 8 -o sign.3mf
  3d arrange a.stl b.stl c.stl --bed 220
"""
from __future__ import annotations

import os

from cli.pyrun import run_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d arrange <input> [options]
  Split parts into connected bodies, lay them flat, and shelf-pack them onto bed-sized
  plates. By DEFAULT writes ONE multi-plate Orca/Bambu project .3mf holding every plate
  (each part assigned to its plate); --per-plate writes one .3mf per plate instead. Reloads
  the output to verify object count, plate count/assignment, and bed fit.

  <input>  a .3mf, or one-or-more .stl files (parts). Repeat for multiple .stl inputs.

Options:
  --single              write ONE multi-plate project .3mf (DEFAULT)
  --per-plate           write one print-ready .3mf per plate (<PREFIX>_plate1.3mf, ...)
  --bed MM              square bed size in millimeters (default: 270, Snapmaker U1)
  --gap MM              clearance between part footprints (default: 6)
  --margin MM           keep-out margin from each bed edge (default: 8)
  -o, --out PREFIX      output path/prefix. single: <PREFIX>.3mf; per-plate:
                        <PREFIX>_plate1.3mf, _plate2.3mf, ... (default: <first-input>_arranged)
  --json                emit a machine-readable JSON plan + verification instead of a table

Exit 1 if a single part is larger than the usable plate (named, with its size).

Examples:
  3d arrange assembly.3mf
  3d arrange assembly.3mf --per-plate -o out/tray
  3d arrange assembly.3mf --bed 270 --gap 6 --margin 8 -o sign.3mf
  3d arrange a.stl b.stl c.stl --bed 220"""

_PART_EXTS = (".3mf", ".stl")


def _parse_float(flag: str, raw: str) -> float:
    try:
        val = float(raw)
    except ValueError:
        raise InvalidArgument(flag, raw, ["a number in mm"], command="arrange") from None
    if val <= 0:
        raise InvalidArgument(
            flag, raw, ["a positive number in mm"], command="arrange",
            extra=f"{flag} must be greater than zero.",
        )
    return val


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    inputs: list[str] = []
    bed = 270.0
    gap = 6.0
    margin = 8.0
    out = ""
    emit_json = False
    single = True  # default: one multi-plate project file

    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--single":
            single = True
            i += 1
        elif a == "--per-plate":
            single = False
            i += 1
        elif a == "--bed":
            if i + 1 >= n:
                raise UsageError("option --bed needs a value", command="arrange")
            bed = _parse_float("--bed", argv[i + 1])
            i += 2
        elif a == "--gap":
            if i + 1 >= n:
                raise UsageError("option --gap needs a value", command="arrange")
            gap = _parse_float("--gap", argv[i + 1])
            i += 2
        elif a == "--margin":
            if i + 1 >= n:
                raise UsageError("option --margin needs a value", command="arrange")
            margin = _parse_float("--margin", argv[i + 1])
            i += 2
        elif a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError("option -o/--out needs a value", command="arrange")
            out = argv[i + 1]
            i += 2
        elif a == "--json":
            emit_json = True
            i += 1
        elif a.startswith("-"):
            raise UsageError(
                f"unknown option '{a}'",
                command="arrange",
                remediation=["See `3d arrange --help` for accepted options."],
            )
        else:
            inputs.append(a)
            i += 1

    if not inputs:
        raise UsageError(
            "no input given",
            command="arrange",
            remediation=["Pass a .3mf, or one-or-more .stl files.", "Example: 3d arrange assembly.3mf"],
        )
    if 2.0 * margin >= bed:
        raise UsageError(
            f"margin {margin:g} mm leaves no usable area on a {bed:g} mm bed",
            command="arrange",
            remediation=["Lower --margin or raise --bed so 2*margin < bed."],
        )

    for inp in inputs:
        if not os.path.isfile(inp):
            raise InputNotFound(inp, command="arrange")
        ext = os.path.splitext(inp)[1].lower()
        if ext not in _PART_EXTS:
            raise InvalidArgument(
                "<input>", inp, [".3mf", ".stl"], command="arrange",
                extra="Pass a .3mf or .stl part file.",
            )
    if len(inputs) > 1 and any(os.path.splitext(p)[1].lower() == ".3mf" for p in inputs):
        raise UsageError(
            "pass a single .3mf, or one-or-more .stl files (not a mix)",
            command="arrange",
            remediation=["Give one .3mf alone, or several .stl files."],
        )

    tool_args = list(inputs) + [
        "--bed", repr(bed),
        "--gap", repr(gap),
        "--margin", repr(margin),
    ]
    if out:
        tool_args += ["-o", out]
    if emit_json:
        tool_args.append("--json")
    tool_args.append("--single" if single else "--per-plate")

    # trimesh's 3MF export needs networkx + lxml; rtree speeds the connected-body split.
    return run_tool("trimesh,numpy,networkx,lxml,rtree", "arrange_pack.py", tool_args)


COMMAND = Command(
    name="arrange",
    group="GEOMETRY & EXPORT",
    summary="split parts and shelf-pack them onto bed-sized plates (one print-ready 3MF per plate)",
    usage=USAGE,
    run=run,
)
