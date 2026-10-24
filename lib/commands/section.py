"""3d section — alias for `3d render <file> --section`.

WHAT: cuts a cross-section through a model (.scad, 3MF, OBJ, STL) to reveal internal cavities,
  wall thicknesses, and fit clearances that are hidden in an external view.

WHY: when you are designing an assembly with hidden pockets, channels, or mating parts,
  an external render shows you nothing. A section cut is the only way to verify that the
  internal geometry actually matches your intent before printing.

Examples:
  3d section bracket.scad -o sec.png --plane YZ
  3d section assembly.scad -D standin=true --plane XZ --at 5 -o sec.png
  3d render bracket.scad --section --plane XZ --keep pos

ROADMAP §3: "Sections — colored-only, anchored, multi, auto-framed.
  Always colored. Every section preserves each part's color ON the cut face.
  High-level spec: presets mid-x|mid-y|mid-z, through:<anchor>, and named sections
  from the object model."
"""

from __future__ import annotations

from cli.registry import Command
from commands.render import run as render_run

USAGE = """3d section <file.(scad|3mf|obj|stl)> -o out.png [options]
  Alias for: 3d render <file> --section ...
  Engineering cross-section: the CUT FACES are violet with 45-degree hatch lines, every uncut
  surface keeps the colour of its PART (.scad color(), 3MF, OBJ+MTL, else the part palette), and
  a legend names the parts and the "cut face".

Options:
  -o, --out PATH        output PNG (default <file>.png)
                         Example: 3d section box.3mf -o sec.png
  --plane YZ|XZ|XY      cut plane (default YZ)
                         Example: 3d section box.3mf --plane XZ -o sec.png
  --at MM               plane position along its axis, model mm (default: the middle)
                         Example: 3d section box.3mf --plane XY --at 12.5 -o sec.png
  --keep neg|pos        which half to keep (default neg)
                         Example: 3d section box.3mf --keep pos -o sec.png
  --part SEL=NAME[:ROLE]  name a part / set its role (printed|vitamin|reference|stand_in); SEL is a
                        part name or a colour; a stand_in is drawn muted (repeatable)
                         Example: 3d section asm.scad --part "#2a62c9=PCB:stand_in" -o sec.png
  --size WxH            exact image size (default 1200x900)
                         Example: 3d section box.3mf --size 1600x1200 -o sec.png
  -D k=v                OpenSCAD define for a .scad input (repeatable)
                         Example: 3d section asm.scad -D standin=true -o sec.png
  --color               accepted for old scripts; a section is always per-part coloured"""


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
