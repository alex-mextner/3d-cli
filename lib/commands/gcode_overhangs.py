"""3d gcode-overhangs — extrusion that nothing holds up, found in the sliced G-code itself.

WHAT: the `overhangs` gate of `3d print-preflight` on its own: per object and layer, the
  extrusion the layer below does not carry (45-degree reach), that no short straight bridge
  reaches and that is wider than an edge step-out; FAIL when one object's islands add up to
  more than --max-unsupported mm2, WARN for smaller islands and edge step-outs. Writes a
  top-down map of the islands (PNG).

WHY: with supports off the slicer says nothing about overhangs; a K1 overlay plate with a
  non-flat bottom printed pieces of its underside in the air while every other gate was green.

The rasterisation (numpy/scipy) lives in lib/gcode_overhangs.py and runs via cli.pyrun;
this module stays stdlib-only.
"""

from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d gcode-overhangs FILE.gcode [--max-unsupported MM2] [--bridge-max MM] [--ledge-max MM]
                    [--min-area MM2] [--png PATH | --no-png] [--json]
  Find extrusion printed on nothing, layer by layer, in a sliced G-code. Offline; never
  contacts a printer. `3d print-preflight` runs it as its `overhangs` gate.

  objects     EXCLUDE_OBJECT / Bambu `; start printing object` (M624) / M486 / `; printing
              object` labels; unlabeled = one "plate"
  carried     inside the layer below (its own lines plus support/raft, enclosed gaps up to
              --bridge-max wide filled) grown by dz * tan 45 + half a line width; the bed
              carries the first layer
  bridged     an uncarried run on a row, column or diagonal no longer than --bridge-max with
              carried material at both ends (a ceiling over a pocket, a bridge between walls)
  step-out    uncarried but within --ledge-max of the reach, or no wider than --ledge-max:
              an overhang perimeter at an edge (WARN only)
  island      the rest, pieces over --min-area mm2 (smaller = grass tips, raster noise)

  FAIL  an object's islands add up to more than --max-unsupported mm2
  WARN  smaller islands, or edge step-outs
  SKIP  no extrusion, or spiral (vase) mode

Options:
  --max-unsupported MM2  islands allowed per object before FAIL (default 100)
  --bridge-max MM        longest span counted as a bridge, widest gap filled (default 15)
  --ledge-max MM         widest edge step-out counted as an overhang perimeter (default 0.6)
  --min-area MM2         smallest island reported (default 2)
  --png PATH             write the island map there (default: ~/.cache/3d-cli/overhangs/)
  --no-png               no map
  --json                 JSON report on stdout

Exit: 0 PASS / WARN / SKIP, 1 FAIL, 2 usage / missing file.

Examples:
  3d gcode-overhangs print/k1se/k1_ovl1_v3.gcode
  3d gcode-overhangs plate.gcode --max-unsupported 50 --png /tmp/plate-overhangs.png
  3d gcode-overhangs plate.gcode --bridge-max 25
  3d gcode-overhangs plate.gcode --json | jq '.objects[] | {name, status, unsupported_mm2}'"""

DEPS = "numpy,scipy"
_NUMBERS = ("--max-unsupported", "--bridge-max", "--ledge-max", "--min-area")
_CMD = "gcode-overhangs"


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    import os

    files: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in (*_NUMBERS, "--png"):
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command=_CMD)
            if a != "--png":
                try:
                    number = float(argv[i + 1])
                except ValueError:
                    number = -1.0
                if not number >= 0 or (a == "--bridge-max" and number == 0):
                    accepted = "a number > 0, e.g. 15" if a == "--bridge-max" else "a number >= 0, e.g. 100"
                    raise InvalidArgument(a, argv[i + 1], [accepted], command=_CMD)
            i += 2
            continue
        if a.startswith("--") and a not in ("--json", "--no-png"):
            raise UsageError(f"unknown option {a}", command=_CMD, remediation=["See `3d gcode-overhangs --help`."])
        if not a.startswith("--"):
            files.append(a)
        i += 1
    if len(files) != 1:
        raise UsageError(f"expected one G-code file, got {len(files)}", command=_CMD)
    if not os.path.isfile(files[0]):
        raise InputNotFound(files[0], command=_CMD)
    return exec_tool(DEPS, "gcode_overhangs.py", argv)


COMMAND = Command(
    name="gcode-overhangs",
    group="SLICING",
    summary="find extrusion printed on nothing in a sliced G-code (the slicer is silent with supports off)",
    usage="gcode-overhangs FILE.gcode [--max-unsupported MM2] [--bridge-max MM] [--ledge-max MM] [--min-area MM2] [--json]",
    run=run,
)
