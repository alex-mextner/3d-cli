"""3d section — alias for `3d render <file> --section`."""
from __future__ import annotations

from cli.registry import Command
from commands.render import run as render_run

USAGE = """3d section <file.scad> -o out.png [options]
  Alias for: 3d render <file.scad> --section ...
  True cross-section. Generic STL-cut by default (any geometry); --color does the
  per-part coloured ASSEMBLY mode (assembly must honour -D cut=true).

Options:
  -o, --out PATH        output PNG (required)
  --plane YZ|XZ|XY      cut plane (default YZ)
  --keep neg|pos        which half to keep (default neg)
  --color               coloured per-part assembly mode
  --module 'name();'    module to cut (no-mesh-stack fallback only)
  --size WxH            image size (default 1200x900)
  -D k=v                pass-through define (repeatable)"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    inp = argv[0]
    return render_run([inp, "--section", *argv[1:]])


COMMAND = Command(
    name="section",
    group="RENDER & VIEW",
    summary="alias -> render --section (true cross-section)",
    usage=USAGE,
    run=run,
)
