"""3d prep — model (STL/3MF) to a verified, sliced, print-ready plate with a proof pack, in minutes.

WHAT: one cached, incremental flow: mesh diagnose + repair, orientation search, no-support adaptation,
  overhang re-check, fit-envelope table (e.g. phones), stability with external loads, colour regions ->
  filaments/mixes, adaptive layer-height plan, Orca project 3MF, headless slice, G-code gates, proof pack.
WHY: the Chekhov cat-phone-stand preparation took 2 h 22 min of one-off scripts and ended without G-code
  (docs/research/prep-session-ledger-2026-10-20.md); each step here is a tested library call and a cache entry.
INVARIANTS: stdlib-only at import time (registry contract); the work runs in the prep interpreter
  (`cli.pyrun`) under the machine heavy lock (`cli.heavy`, re-entrant) unless --no-lock; exit 1 when any
  step FAILs, 0 for PASS/WARN/SKIP.

Examples:
  3d prep cat.stl --printer u1 --colors colors.yaml --fit phones.json
"""

from __future__ import annotations

from cli import heavy
from cli.pyrun import tool_argv
from cli.registry import Command

DEPS = "numpy,scipy,trimesh,manifold3d,rtree,shapely,pyyaml,pillow,scikit-image"

USAGE = """3d prep MODEL.stl|.3mf [--out DIR] [--printer u1] [--colors colors.yaml] [--fit items.json] [options]
3d prep steps                       list the steps and what each needs
3d prep status OUT_DIR              step table of the last run in OUT_DIR
  Runs, with a per-step cache in OUT_DIR (default ./MODEL-prep):
    diagnose repair orient adapt overhang fit stability colors layers project slice gates proof
  Outputs in OUT_DIR: NAME.print.stl, NAME.3mf, NAME.gcode, proof-report.md + proof/, prep.json.
  Exit 1 when any step FAILs; PASS/WARN/SKIP exit 0. Runs under the heavy lock (3d heavy) unless --no-lock.

Options:
  --spec FILE.yaml        settings file (printer, orient, angle, pitch, adapt, fit, holder, loads, stability,
                          colors, process{preset,filament_preset,walls,infill,layer_min,layer_max,brim,mix_cap}, gcode)
  --printer u1            target printer (default u1)
  --orient auto|keep      auto = rotate to the best candidate when it clearly cuts overhang (default), keep = never
  --angle 45              overhang limit, degrees from vertical (default 45)
  --pitch 0.25            voxel size / layer unit in mm for overhang + adaptation (default 0.25)
  --bridge-max 10         longest unsupported bridge in mm that is only a WARN (longer = FAIL; default 10)
  --no-adapt              report overhangs but do not change the model
  --fit items.json        items the object must hold (phones.json schema: cases + items); HolderSpec in --spec holder:
  --colors colors.yaml    filaments + landmark boxes + regions -> filament or mix per colour part
  --process NAME          Orca process preset (default "0.20mm Standard @Snapmaker U1 (0.4 nozzle)")
  --filament NAME         Orca filament preset for every slot (default: the printer's generic PLA)
  --walls 4 --infill 15   wall loops and sparse infill percent
  --layer-min 0.08 --layer-max 0.32   adaptive layer-height band in mm
  --brim MM               brim width (default 0)
  --only a,b              run just these steps (and what they need); --skip a,b leaves steps out
  --from STEP             forget the cache from STEP onwards and recompute; --force recomputes everything
  --no-slice              stop before slicing (no Orca needed)
  --no-lock               do not take the heavy lock (e.g. already on a dedicated box)
  --json                  machine-readable outcome; --quiet hides progress lines

Examples:
  3d prep cat.stl --printer u1 --colors colors.yaml --fit phones.json
  3d prep cat.stl --out out --spec chekhov.yaml --walls 4 --infill 15 --layer-min 0.08 --layer-max 0.32
  3d prep cat.stl --out out --colors colors.yaml                    # re-run: unchanged steps are cache hits
  3d prep cat.stl --out out --orient keep --angle 50 --no-adapt     # report only
  3d prep cat.stl --out out --only repair,orient,overhang --no-slice
  3d prep cat.stl --out out --from colors                           # redo colours, layers, 3MF, slice, gates
  3d prep cat.stl --out out --no-slice --json | jq '.steps[] | {name, verdict, seconds}'
  3d prep status out
  3d remote run --push cat.stl --pull -- 3d prep cat.stl --out ../out   # on the heavy box"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    lock = "--no-lock" not in argv
    args = [a for a in argv if a != "--no-lock"]
    cmd = tool_argv(DEPS, "prep_cli.py", args)
    if not lock or args[0] in ("steps", "status"):
        import subprocess

        return subprocess.run(cmd, check=False).returncode  # noqa: S603 — argv from cli.pyrun
    return heavy.run_heavy(cmd)


COMMAND = Command(
    name="prep",
    group="GEOMETRY & EXPORT",
    summary="model -> repaired, adapted, checked, sliced, gated plate + proof pack (cached steps)",
    usage=USAGE,
    run=run,
)
