"""3d match — forced-monotonic silhouette-match loop.

WHAT: an automated optimization loop that proposes a single parameter change, re-renders,
  scores the silhouette against a reference, and accepts the change ONLY if the score
  strictly improves AND all hard gates pass. Otherwise it reverts and tries again.

WHY: unconstrained self-judged LLM loops oscillate without bound (the FlipFlop effect:
  ~46% flips, ~17% accuracy drop). Forced-monotonic acceptance turns "an LLM fiddling
  with numbers" into a convergent optimizer — the single most important rule for making
  AI-assisted modeling actually reach a target.

Examples:
  3d match model.scad ref.jpg --rounds 2 --dry-run   # smoke test, no real critic
  3d match model.scad ref.jpg --rounds 8 --ortho
  3d match model.scad ref.jpg --params width,height --no-improve 4

ROADMAP §13.1: "Forced-monotonic acceptance: the loop applies ONE critic-proposed edit,
  re-scores, and accepts only on strict metric improvement AND all hard gates PASS;
  otherwise it reverts + resamples. A changelog of attempts is fed back so a failed move
  is never retried. This single rule turns an LLM fiddling with numbers into a convergent
  optimiser."

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
  --dry-run            skip the AI critic; synthesise deterministic edits (smoke test)
  --backend NAME        AI critic backend: claude|codex|opencode|ollama|mock
                        (default: ai.json `backend`, else first available)
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
  3d match model.scad ref.jpg --rounds 8 --backend codex
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
