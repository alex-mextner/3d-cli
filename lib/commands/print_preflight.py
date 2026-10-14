"""3d print-preflight — checks a G-code must pass before it is started on a printer.

WHAT: thumbnails present; every move inside the printer's axis travel and the plate inside
  the printable area (issue #101, the `3d gcode-bounds` checks); with --printer, the printer
  is free and every head the G-code uses is loaded with the colour/material the G-code
  expects (per-head data from Moonraker print_task_config, i.e. the Snapmaker U1); with
  --source-3mf, `3d halves-check` on the source 3MF. One PASS/WARN/FAIL/SKIP line per check.

WHY: `3d printer start` runs exactly this and refuses on FAIL (issue
  https://github.com/alex-mextner/3d-cli/issues/96); running it alone answers "is this file
  ready to go on that printer?" without touching anything. It only READS from the printer.

INVARIANTS: stdlib-only at import time; printer_ops imported lazily inside run().
"""
from __future__ import annotations

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d print-preflight FILE.gcode [--printer NAME] [--axis-limits X0:X1,Y0:Y1|NAME] [--source-3mf FILE.3mf]
                   [--json] [--secs S]
  Read-only checks before a print. `3d printer start` runs the same checks and refuses on FAIL.

  thumbnails   preview images required by the G-code's `; thumbnails =` spec are present
  printer      (--printer) the printer is not printing / paused / busy / unreadable
  slot Tn      (--printer, per-head printers such as the Snapmaker U1) each head the G-code
               uses is loaded, and its colour and material equal `; filament_colour` /
               `; filament_type` for that tool
  halves       (--source-3mf) `3d halves-check FILE.3mf` passes
  axis travel  every G0-G3 move (arcs by their real extremes, start/end G-code included)
               stays inside the printer's X/Y limits; SKIP when the limits are unknown
  print area   plate moves stay inside the G-code's `; printable_area`; no extruding path
               through `; bed_exclude_area` (vendor `;TYPE:Custom` blocks exempt)

Options:
  --printer NAME       a printer from printer-devices.yaml (see `3d printer list`); its state
                       is read (never changed); Moonraker printers also report axis limits
  --axis-limits SPEC   X/Y travel as X0:X1,Y0:Y1, or a printer NAME whose axis_limits come
                       from printer-devices.yaml (no network); overrides --printer's limits
  --source-3mf FILE    the 3MF the G-code was sliced from; runs the halves-check
  --json               JSON report on stdout
  --secs S             Creality: seconds to listen for pushed state (default 3)

Exit: 0 PASS or WARN, 1 FAIL, 2 usage / missing file.

Examples:
  3d print-preflight out/part.gcode
  3d print-preflight out/part.gcode --printer u1
  3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf
  3d print-preflight out/roof.gcode --printer k1se --secs 5
  3d print-preflight out/roof.gcode --axis-limits -5:229,-0.5:227
  3d print-preflight out/roof.gcode --axis-limits k1se
  3d print-preflight out/part.gcode --printer u1 --json | jq '.checks[] | select(.status=="FAIL")'"""


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
        if a in ("--printer", "--source-3mf", "--secs", "--axis-limits"):
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="print-preflight")
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a == "--json":
            opts[a] = "1"
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a}", command="print-preflight",
                             remediation=["See `3d print-preflight --help`."])
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        raise UsageError(f"expected one G-code file, got {len(pos)}", command="print-preflight")
    gcode = Path(pos[0])
    if not gcode.is_file():
        raise InputNotFound(str(gcode), command="print-preflight")
    source = Path(opts["--source-3mf"]) if "--source-3mf" in opts else None
    if source is not None and not source.is_file():
        raise InputNotFound(str(source), command="print-preflight")

    from printer_ops.preflight import FAIL, run_preflight

    from printer_ops import adapter_by_name, limits_from_option

    status = None
    limits, limits_source = None, "no --printer or --axis-limits given"
    if "--axis-limits" in opts:
        limits, limits_source = limits_from_option(opts["--axis-limits"], command="print-preflight")
    if "--printer" in opts:
        adapter = adapter_by_name(opts["--printer"], secs=opts.get("--secs"), command="print-preflight")
        status = adapter.status()
        if "--axis-limits" not in opts:
            limits, limits_source = adapter.axis_limits()

    report = run_preflight(gcode, status=status, source_3mf=source, axis_limits=limits, limits_source=limits_source)
    print(json.dumps(report.to_json(), indent=1) if "--json" in opts else report.render())
    if report.overall == FAIL:
        raise GateFailure("preflight FAIL", command="print-preflight", silent=True)
    return 0


COMMAND = Command(
    name="print-preflight",
    group="SLICING",
    summary="read-only checks before a print: thumbnails, move bounds, loaded colours vs G-code, halves, printer free",
    usage=USAGE,
    run=run,
)
