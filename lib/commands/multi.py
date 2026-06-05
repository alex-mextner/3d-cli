"""3d multi — alias for `3d render <file> --multi [outdir]`."""
from __future__ import annotations

from cli.registry import Command
from commands.render import run as render_run

USAGE = """3d multi <file.scad> [outdir] [--render] [--size WxH] [-D k=v]...
  Alias for: 3d render <file.scad> --multi [outdir] ...
  Renders front/back/left/right/top/iso (default outdir: previews/).

Options:
  [outdir]              output directory (default: previews/)
  --render              force CGAL render mode (slower but exact). Use this when
                         the preview mode produces visual artifacts or when you need
                         the exact geometry for a downstream gate.
  --size WxH            render resolution (default 800x600)
  -D k=v                pass an OpenSCAD variable define (repeatable)

Examples:
  3d multi bracket.scad previews/
  3d multi bracket.scad --render --size 1200x900
  3d multi bracket.scad -D 'depth=40'"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    inp = argv[0]
    rest = argv[1:]
    outdir: list[str] = []
    if rest and not rest[0].startswith("-"):
        outdir = [rest[0]]
        rest = rest[1:]
    return render_run([inp, "--multi", *outdir, *rest])


COMMAND = Command(
    name="multi",
    group="RENDER & VIEW",
    summary="alias -> render --multi (all standard angles)",
    usage=USAGE,
    run=run,
)
