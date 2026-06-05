"""3d match — forced-monotonic silhouette-match loop (lib/match_loop.py).

match_loop.py shells back out to `bin/3d` for render/score/mesh, so it needs no heavy
deps itself; the original ran it with bare python3. We run it via pyrun with no deps so
the venv/uv/system resolution still applies uniformly.
"""
from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command

USAGE = """3d match <assembly.scad> <reference> [options]
  Forced-monotonic silhouette-match loop (report 3.2/7.4). The LLM critic proposes
  one numeric param delta; an IoU/AE metric + manifold gate dispose. Keep iff the score
  strictly improves AND manifold passes; else revert. Every step logged to a changelog.

Options:
  --rounds N            max rounds (default 8)
  --dry-run            skip the codex critic; synthesise deterministic edits (smoke test)
  --constants FILE      file holding the tunable constants (default: the assembly)
  --params a,b,c        restrict which constants the critic may tune
  --metric iou|ae       primary metric (default iou)
  --no-improve N        stop after N consecutive non-improving rounds (default 4)
  --margin F            strict-improvement margin (default 1e-4)
  --cam ex,..,cz        6-param vector camera for renders
  --size WxH            render size (default 1200x900)
  --ortho              orthographic renders
  --work DIR            work dir (default: <assembly_dir>/match_work)

Examples:
  3d match model.scad ref.jpg --rounds 2 --dry-run
  3d match model.scad ref.jpg --rounds 8 --ortho --cam 130,-600,52,130,0,52"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1
    return exec_tool("", "match_loop.py", argv)


COMMAND = Command(
    name="match",
    group="REFERENCE-MATCH PIPELINE",
    summary="forced-monotonic silhouette-match loop (--dry-run to smoke-test)",
    usage=USAGE,
    run=run,
)
