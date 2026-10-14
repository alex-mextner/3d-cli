"""3d gcode-resume — continue a stopped multi-head print above the last intact layer.

WHAT: from the original sliced G-code, writes a new G-code that starts at a chosen layer of a
  part still on the bed: original header, a resume header (bed/heads reheated, Z lifted above
  everything first, every head purged off the bed, fans/PA/accel/feed restored) and the
  original from that layer on, with per-head retraction kept exact. Also locates a pause by
  byte offset (--where) and prints a per-layer table (--table). Issue
  https://github.com/alex-mextner/3d-cli/issues/138.

WHY: a failed head or a pause is otherwise a lost plate. Z homing is impossible with the part on
  the bed, so the printer's own homing/mesh must be kept (start with CLEAR_PAUSE, then
  SDCARD_PRINT_FILE; never after CANCEL_PRINT, whose M84 drops the homing).

INVARIANTS: stdlib-only at import time; never contacts a printer; printer-specific values come
  from the `resume:` block of --printer in printer-devices.yaml. Logic: lib/gcode_resume.py.
"""
from __future__ import annotations

from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d gcode-resume FILE.gcode --printer NAME (--top-z Z | --layer N) [options]
3d gcode-resume FILE.gcode --where BYTE
3d gcode-resume FILE.gcode --table FIRST:LAST
  Resume a stopped multi-head Orca print above the last intact layer of the part still on the
  bed. Never contacts a printer. Start the output with CLEAR_PAUSE, then SDCARD_PRINT_FILE
  (the printer must still be homed; never after CANCEL_PRINT, whose M84 drops the homing).

Modes:
  --where BYTE         line, layer, active head, temperatures and retraction at a byte offset
                       (Klipper virtual_sdcard.file_position at the pause)
  --table FIRST:LAST   per layer: Z, line, byte, heads, objects, slicer minutes left
  --top-z Z            build: Z of the measured top of the last intact layer; resumes at the
                       first layer above it
  --layer N            build: resume at layer N (instead of --top-z)

Build options:
  --printer NAME       printer from printer-devices.yaml whose `resume:` block gives the purge
                       macro and lengths, clearance and start commands (required to build)
  --paused-at BYTE     file_position at the pause: lifts travel Z above what was printed and
                       refuses a resume layer above the paused one
  --no-tower           drop the prime tower; purge off the bed after every tool change
  --home-xy            add G28 X Y (only if X/Y lost their homing; Z is never homed)
  -o, --out FILE       output (default: <FILE>_resume_L<N>.gcode next to the input)

printer-devices.yaml `resume:` keys (all required): purge_macro, purge_mm, purge_retract_mm,
  no_tower_prime_mm, clearance_mm, tower_margin_mm, filament_diameter_mm, min_print_temp,
  default_accel, start_commands. See `3d printer --help` for the file.

Exit: 0 written / reported, 1 internal check failed, 2 usage error, bad G-code, unknown printer
  or missing file.

Examples:
  3d gcode-resume print/plate.gcode --where 41236998
  3d gcode-resume print/plate.gcode --table 840:860
  3d gcode-resume print/plate.gcode --printer u1 --top-z 170.2 --paused-at 41236998
  3d gcode-resume print/plate.gcode --printer u1 --layer 851 --no-tower -o print/plate_resume.gcode"""

_VALUE_OPTS = ("--where", "--table", "--top-z", "--layer", "--paused-at", "--printer", "-o", "--out")
_FLAGS = ("--no-tower", "--home-xy")


def _number(opts: dict[str, str], key: str, kind: type[int] | type[float]) -> int | float | None:
    if key not in opts:
        return None
    try:
        return kind(opts[key])
    except ValueError as exc:
        raise UsageError(f"{key} wants a{'n integer' if kind is int else ' number'}, got {opts[key]!r}",
                         command="gcode-resume") from exc


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    from pathlib import Path

    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_OPTS:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="gcode-resume")
            opts["--out" if a == "-o" else a] = argv[i + 1]
            i += 2
            continue
        if a in _FLAGS:
            opts[a] = "1"
        elif a.startswith("-"):
            raise UsageError(f"unknown option {a}", command="gcode-resume",
                             remediation=["See `3d gcode-resume --help`."])
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        raise UsageError(f"expected one G-code file, got {len(pos)}", command="gcode-resume")
    gcode = Path(pos[0])
    if not gcode.is_file():
        raise InputNotFound(str(gcode), command="gcode-resume")
    modes = [k for k in ("--where", "--table", "--top-z", "--layer") if k in opts]
    if len(modes) != 1:
        raise UsageError(f"give exactly one of --where, --table, --top-z, --layer (got {', '.join(modes) or 'none'})",
                         command="gcode-resume", remediation=["See `3d gcode-resume --help`."])

    import gcode_resume as gr

    where = _number(opts, "--where", int)
    if where is not None:
        print(gr.where(gr.Walk.read(gcode), int(where)))
        return 0
    if "--table" in opts:
        print(gr.table(gr.Walk.read(gcode), opts["--table"]))
        return 0
    if "--printer" not in opts:
        raise UsageError("building a resume needs --printer NAME (its `resume:` block in printer-devices.yaml)",
                         command="gcode-resume", remediation=["Run `3d printer list` to see configured printers."])
    from printer_ops.devices import get_device

    device = get_device(opts["--printer"])
    if device.resume is None:
        raise UsageError(f"printer {device.name!r} has no `resume:` block in {device.source}",
                         command="gcode-resume", remediation=["Add one; the keys are listed in `3d gcode-resume --help`."])
    top_z, layer = _number(opts, "--top-z", float), _number(opts, "--layer", int)
    paused = _number(opts, "--paused-at", int)
    res = gr.build(gr.Walk.read(gcode), device.resume, name=gcode.name, top_z=top_z,
                   layer=None if layer is None else int(layer), paused_at=None if paused is None else int(paused),
                   no_tower="--no-tower" in opts, home_xy="--home-xy" in opts)
    dest = Path(opts["--out"]) if "--out" in opts else gcode.with_name(f"{gcode.stem}_resume_L{res.layer}.gcode")
    if dest.exists() and dest.samefile(gcode):
        raise UsageError(f"--out {dest} is the input G-code; the original must stay", command="gcode-resume",
                         remediation=["Give another -o path, or omit it for <FILE>_resume_L<N>.gcode."])
    dest.write_bytes(b"\n".join(res.lines))
    print(res.summary(dest))
    return 0


COMMAND = Command(
    name="gcode-resume",
    group="SLICING",
    summary="resume a stopped multi-head print above the last intact layer (no Z homing); offline",
    usage=USAGE,
    run=run,
)
