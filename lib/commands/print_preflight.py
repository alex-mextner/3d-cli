"""3d print-preflight — checks a G-code must pass before it is started on a printer.

WHAT: every offline G-code gate, then the printer-side checks: thumbnails present; every move
  inside the printer's axis travel and the plate inside the printable area (issue #101, the
  `3d gcode-bounds` checks); no object prints a layer on nothing (`3d gcode-overhangs`); every
  slicer warning for this exact G-code recorded and resolved or accepted with a reason
  (`3d slice-warnings`, NAME.slice-warnings.json); each object prints on its recipe's heads
  layer by layer (issue #103, needs the project 3MF: --source-3mf or NAME.3mf next to the
  G-code); the declared visible sides are clean (`3d visible-faces --gcode`, with --visible);
  with --printer, the printer is free and every head the G-code uses is loaded with the
  colour/material the G-code expects (per-head data from Moonraker print_task_config, i.e. the
  Snapmaker U1); with --source-3mf, `3d halves-check` on the source 3MF. One
  PASS/WARN/FAIL/SKIP line per check; a SKIP names the missing input.

WHY: `3d printer start` runs exactly this and refuses on FAIL (issue
  https://github.com/alex-mextner/3d-cli/issues/96); running it alone answers "is this file
  ready to go on that printer?" without touching anything. It only READS from the printer.

INVARIANTS: stdlib-only at import time; printer_ops imported lazily inside run().
"""
from __future__ import annotations

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d print-preflight FILE.gcode [--printer NAME] [--axis-limits X0:X1,Y0:Y1|NAME] [--source-3mf FILE.3mf]
                   [--visible [OBJ:]SPEC]... [--bridge-max MM] [--json] [--secs S]
  Read-only checks before a print. `3d printer start` runs the same checks and refuses on FAIL.
  Every G-code gate runs; one whose input is missing is SKIP and says what to pass.

  thumbnails   preview images required by the G-code's `; thumbnails =` spec are present
  printer      (--printer) the printer is not printing / paused / busy / unreadable
  slot Tn      (--printer, per-head printers such as the Snapmaker U1) each head the G-code
               uses is loaded, and its colour and material equal `; filament_colour` /
               `; filament_type` for that tool
  axis travel  every G0-G3 move (arcs by their real extremes, start/end G-code included)
               stays inside the printer's X/Y limits; SKIP when the limits are unknown
  print area   plate moves stay inside the G-code's `; printable_area`; no extruding path
               through `; bed_exclude_area` (vendor `;TYPE:Custom` blocks exempt; Bambu:
               everything outside its object blocks, wipe tower and features)
  macros       every macro the G-code calls is in the printer's trusted_macros
  overhangs    `3d gcode-overhangs`: no object prints more than 100 mm2 on nothing (FAIL);
               small ceilings and edge step-outs are WARN; bridges up to --bridge-max, else the
               printer's bridge_max_mm (--printer / --axis-limits NAME), else 15 mm
  slicer warn  NAME.slice-warnings.json (`3d slice-warnings record`) exists, matches the
               G-code's SHA-256, and every warning in it is accepted with a reason
  head seq     every object prints each layer on the heads its filaments call for (#103);
               needs the project 3MF: --source-3mf, else NAME.3mf next to NAME.gcode
  visible      (--visible) `3d visible-faces --gcode`: no support / sag on visible sides
  halves       (--source-3mf) `3d halves-check FILE.3mf` passes

Options:
  --printer NAME          a printer from printer-devices.yaml (see `3d printer list`); its state
                          is read (never changed); Moonraker printers also report axis limits
  --axis-limits SPEC      X/Y travel as X0:X1,Y0:Y1, or a printer NAME whose axis_limits come
                          from printer-devices.yaml (no network); overrides --printer's limits
  --source-3mf FILE       the project 3MF the G-code was sliced from: head-sequence + halves-check
  --visible [OBJ:]SPEC    visible side for `3d visible-faces --gcode` (x<V, y>V, all; repeatable)
  --bridge-max MM         longest span the overhangs gate counts as a bridge; overrides the
                          printer's bridge_max_mm in printer-devices.yaml (default 15)
  --json                  JSON report on stdout
  --secs S                Creality: seconds to listen for pushed state (default 3)

Exit: 0 PASS or WARN, 1 FAIL, 2 usage / missing file.

Examples:
  3d print-preflight out/part.gcode
  3d print-preflight out/part.gcode --printer u1
  3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf
  3d print-preflight out/roof.gcode --printer k1se --secs 5
  3d print-preflight out/roof.gcode --axis-limits -5:229,-0.5:227
  3d print-preflight print/k1se/k1_base_4_v3.gcode --axis-limits k1se
  3d print-preflight out/walls.gcode --visible 'WALL*:y<104.8'
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
    visible: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--printer", "--source-3mf", "--secs", "--axis-limits", "--visible", "--bridge-max"):
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="print-preflight")
            if a == "--visible":
                visible.append(argv[i + 1])
            else:
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

    from printer_ops import adapter_by_name, bridge_max_for, device_for, limits_from_option, trusted_macros_for
    from printer_ops.devices import get_device

    printer = opts.get("--printer")
    profile = get_device(printer, offline_ok=True) if printer is not None else None
    if profile is not None and profile.access == "offline":
        # an offline fleet printer is never contacted: its static profile stands in for --printer,
        # an explicit --axis-limits replaces only its limits
        del opts["--printer"]
        opts.setdefault("--axis-limits", profile.name)
    else:
        profile = None

    status = None
    device = profile or (None if "--printer" in opts else device_for(opts.get("--axis-limits")))
    limits, limits_source = None, "no --printer or --axis-limits given"
    trusted = profile.trusted_macros if profile else \
        frozenset() if "--printer" in opts else trusted_macros_for(opts.get("--axis-limits"))
    if "--axis-limits" in opts:
        limits, limits_source = limits_from_option(opts["--axis-limits"], command="print-preflight")
    if "--printer" in opts:
        adapter = adapter_by_name(opts["--printer"], secs=opts.get("--secs"), command="print-preflight")
        trusted = adapter.trusted_macros  # this printer's macros only, whatever --axis-limits names
        device = adapter.device
        status = adapter.status()
        if "--axis-limits" not in opts:
            limits, limits_source = adapter.axis_limits()

    bridge, bridge_source = bridge_max_for(opts.get("--bridge-max"), device, command="print-preflight")
    report = run_preflight(gcode, status=status, source_3mf=source, axis_limits=limits, limits_source=limits_source,
                           trusted_macros=trusted, visible=visible, bridge_max=bridge, bridge_source=bridge_source)
    print(json.dumps(report.to_json(), indent=1) if "--json" in opts else report.render())
    if report.overall == FAIL:
        raise GateFailure("preflight FAIL", command="print-preflight", silent=True)
    return 0


COMMAND = Command(
    name="print-preflight",
    group="SLICING",
    summary="read-only checks before a print: every G-code gate (bounds, overhangs, slicer warnings, heads, "
    "thumbnails) + printer state",
    usage=USAGE,
    run=run,
)
