"""3d gcode-bounds — does every move of a sliced G-code stay on the machine?

WHAT: the move-bounds checks of `3d print-preflight` on their own (issue
  https://github.com/alex-mextner/3d-cli/issues/101): every G0-G3 move, arcs by their real
  axis extremes, inside the printer's X/Y travel (start/end G-code included), and the plate
  inside the G-code's `; printable_area` / outside `; bed_exclude_area`.

WHY a separate command: it is a lint for a freshly sliced file, run before the file goes
  anywhere — it NEVER contacts a printer (limits come from --axis-limits or the
  printer's `axis_limits` in printer-devices.yaml), so agents and CI can run it freely.
  A K1 SE plate faulted with "Y out of range" from a z-hop arc reaching Y -0.84.

INVARIANTS: stdlib-only at import time; the logic lives in printer_ops/move_bounds.py and
  printer_ops/preflight.py (shared with print-preflight), this module only parses argv.
"""
from __future__ import annotations

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d gcode-bounds FILE.gcode [--printer NAME | --axis-limits X0:X1,Y0:Y1] [--json]
  Check that every move stays on the machine. Never contacts a printer.

  axis travel  every G0/G1/G2/G3 (absolute or G91 relative, G92 offsets; arcs G2/G3 by
               their real extreme points from I/J, a full circle when start == end) inside
               the printer's X/Y limits — start/end G-code included. SKIP when unknown.
  print area   moves from the first layer change to the end of the last object/feature
               inside the G-code's `; printable_area`, and no extruding line or arc through
               `; bed_exclude_area`; vendor `;TYPE:Custom` blocks (parking, purging) are exempt
  unverified   WARN for moves only partly checked: arcs by R or in the XZ/YZ plane, relative
               moves from an unknown position (after G28). A known axis is always checked.
  macros       FAIL for macros (any command but moves and stock non-moving ones) that the
               printer's trusted_macros in printer-devices.yaml does not list: their bodies
               are invisible here and may move anywhere

Options:
  --printer NAME       take axis_limits and trusted_macros from that printer in printer-devices.yaml
  --axis-limits SPEC   X/Y travel as X0:X1,Y0:Y1 (or a printer name); wins over --printer
  --json               JSON report on stdout

Exit: 0 PASS / WARN / SKIP, 1 FAIL, 2 usage error, unknown printer or missing file.

Examples:
  3d gcode-bounds print/k1se/base.gcode --printer k1se
  3d gcode-bounds print/k1se/base.gcode --axis-limits -5:229,-0.5:227
  3d gcode-bounds print/u1_trees.gcode
  3d gcode-bounds print/u1_trees.gcode --json | jq '.checks[] | select(.status=="FAIL") | .detail'"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    import json
    from pathlib import Path

    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--printer", "--axis-limits"):
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="gcode-bounds")
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a == "--json":
            opts[a] = "1"
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a}", command="gcode-bounds",
                             remediation=["See `3d gcode-bounds --help`."])
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        raise UsageError(f"expected one G-code file, got {len(pos)}", command="gcode-bounds")
    gcode = Path(pos[0])
    if not gcode.is_file():
        raise InputNotFound(str(gcode), command="gcode-bounds")

    from printer_ops import limits_from_option, trusted_macros_for
    from printer_ops.preflight import FAIL, bounds_report

    spec = opts.get("--axis-limits") or opts.get("--printer")
    if spec is None:
        limits, source = None, "no --printer or --axis-limits given"
    else:
        limits, source = limits_from_option(spec, command="gcode-bounds")
    # the macros of the printer the file is for; --axis-limits NAME only when no --printer
    trusted = trusted_macros_for(opts.get("--printer") or opts.get("--axis-limits"))
    report = bounds_report(gcode, limits, source, trusted)
    print(json.dumps(report.to_json(), indent=1) if "--json" in opts else report.render())
    if report.overall == FAIL:
        raise GateFailure("gcode-bounds FAIL", command="gcode-bounds", silent=True)
    return 0


COMMAND = Command(
    name="gcode-bounds",
    group="SLICING",
    summary="check every G-code move (arcs too) against axis travel and the printable area; offline",
    usage=USAGE,
    run=run,
)
