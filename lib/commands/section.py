"""3d section — alias for `3d render <file> --section`.

WHAT: cuts a true cross-section through a .scad model to reveal internal cavities,
  wall thicknesses, and fit clearances that are hidden in an external view.

WHY: when you are designing an assembly with hidden pockets, channels, or mating parts,
  an external render shows you nothing. A section cut is the only way to verify that the
  internal geometry actually matches your intent before printing.

Examples:
  3d section bracket.scad -o sec.png --plane YZ
  3d section assembly.scad --color --plane YZ -o sec.png
  3d render bracket.scad --section --plane XZ --keep pos

ROADMAP §3: "Sections — colored-only, anchored, multi, auto-framed.
  Always colored. Every section preserves each part's color ON the cut face.
  High-level spec: presets mid-x|mid-y|mid-z, through:<anchor>, and named sections
  from the object model."
"""
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
