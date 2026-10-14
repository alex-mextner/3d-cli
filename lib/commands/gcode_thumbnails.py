"""3d gcode-thumbnails — check / add the preview image a printer screen shows for a G-code.

WHAT: `check` lists the thumbnail blocks in a G-code's head and compares them with the sizes
  its slicer profile asks for (`; thumbnails = 48x48/PNG, 300x300/PNG`); exit 1 when any is
  missing. `add` writes the missing ones right after `; HEADER_BLOCK_END`, in Orca's exact
  block format, from `--image PNG` or from a z-buffered isometric render of the G-code's own
  extrusion moves (tool colours from `filament_colour`).

WHY: the Orca-family CLI (`--slice`) exports G-code without thumbnails — it passes a null
  thumbnail callback to export_gcode — so every headless slice shows a blank preview on the
  Snapmaker U1 / Klipper / Creality K1 screen even though the profile sets `thumbnails`.

ACCESSED VIA: `3d gcode-thumbnails check|add`; printer preflights import
  lib/gcode_thumbnails.py `check()` directly. `check` runs in-process (stdlib only); `add`
  runs the lib tool through cli.pyrun with numpy (+ Pillow for `--image` / JPG output).
"""
from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import UsageError

USAGE = """3d gcode-thumbnails <check|add> <file.gcode>... [options]
  The preview image a printer screen (Snapmaker U1, Klipper/Moonraker, Creality K1) shows
  for a job comes from base64 blocks in the G-code header. Orca's CLI never writes them.

  check FILE...            PASS/FAIL per file: every size in the profile's `; thumbnails =`
                           is present (no spec: at least one thumbnail). Exit 1 on FAIL.
  add FILE...              insert the missing sizes after `; HEADER_BLOCK_END` (before the
                           first move), file rewritten atomically, mtime kept. A file that
                           already has them is left untouched.
    --image PNG            picture to use (e.g. a plate render) instead of drawing the
                           preview from the G-code's extrusion moves
    --size WxH[/FMT]       sizes for a G-code without a `; thumbnails =` spec (repeatable)
    --out FILE             write the result there instead of in place (one input only)
    --preview PNG          also save the full-size G-code render
    --replace              redraw every size, dropping the thumbnails already there

Examples:
  3d gcode-thumbnails check print/u1_trees.gcode
  3d gcode-thumbnails add print/*.gcode
  3d gcode-thumbnails add plate.gcode --image plate.png
  3d gcode-thumbnails add other.gcode --size 300x300/PNG --size 32x32/PNG
"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub not in ("check", "add"):
        raise UsageError(f"unknown subcommand {sub!r}", command="gcode-thumbnails",
                         remediation=["Use `3d gcode-thumbnails check FILE...` or `3d gcode-thumbnails add FILE...`."])
    if not [a for a in rest if not a.startswith("-")]:
        raise UsageError(f"`{sub}` needs at least one G-code file", command="gcode-thumbnails",
                         remediation=[f"3d gcode-thumbnails {sub} plate.gcode"])
    if sub == "check":
        import gcode_thumbnails  # lazy; stdlib-only for check

        return gcode_thumbnails.main(argv)
    # Pillow always: --image needs it to read, and a `/JPG` size in the profile needs it to write.
    return exec_tool("numpy,pillow", "gcode_thumbnails.py", argv)


COMMAND = Command(
    name="gcode-thumbnails",
    group="SLICING",
    summary="check/add the printer-screen preview thumbnails in a G-code (Orca CLI writes none)",
    usage="gcode-thumbnails <check|add> <file.gcode>... [--image PNG] [--size WxH/FMT] [--json]",
    run=run,
)
