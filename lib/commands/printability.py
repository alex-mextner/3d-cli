"""3d printability — wall / min-feature / overhang / orientation gate (FDM, PLA/PETG).

WHAT: checks that a part satisfies hard FDM rules: wall thickness ≥ 1.2 mm, floor ≥ 0.8 mm,
  min feature ≥ 1.0 mm, overhang angle ≤ 45°. Runs per part, prints a per-part breakdown.

WHY: a model can be geometrically perfect (manifold, no collisions) and still fail on the
  printer because a wall is too thin or an overhang is too steep. `printability` is the
  gate that catches the slicer-level failures before you waste filament and hours.

Examples:
  3d printability part.scad           # check one part
  3d printability a.scad b.scad       # check multiple parts
  3d check part.scad --printability   # same gate, run through the umbrella command

ROADMAP §3: "printability — wall / min-feature / overhang / orientation flags (FDM,
  PLA/PETG). Thresholds: wall>=1.2 floor>=0.8 feature>=1.0 overhang<=45deg.
  Exit 0 = all parts clear HARD rules, 1 = a HARD rule failed."
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from cli.env import require_openscad
from cli.pyrun import run_tool
from cli.registry import Command
from errors import UsageError

USAGE = """3d printability <file.scad|.stl> [more parts...] [-D k=v ...]
  Wall thickness / min feature / overhang / orientation flags (FDM, PLA/PETG).
  Thresholds: wall>=1.2  floor>=0.8  feature>=1.0  overhang<=45deg.
  Exit 0 = all parts clear HARD rules, 1 = a HARD rule failed.

Example:
  3d printability part.scad
  3d printability a.scad b.scad
  3d printability part.stl"""

_GEOM_RE = re.compile(r"non-manifold|not.*manifold|self-intersect|degenerate", re.IGNORECASE)


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("printability")

    parts: list[str] = []
    defs: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "-D":
            if i + 1 >= n:
                raise UsageError("option -D needs a value", command="printability")
            defs += ["-D", argv[i + 1]]
            i += 2
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            parts.append(a)
            i += 1
    if not parts:
        raise UsageError("no parts given", command="printability")

    work = tempfile.mkdtemp(prefix="3d_print.")
    print("================================================================")
    print(" printability gate  (Bambu A1 + PLA/PETG, fdm-printability rules)")
    print(" thresholds: wall>=1.2 floor>=0.8 feature>=1.0 overhang<=45deg")
    print("================================================================")

    fail = False
    for f in parts:
        if not os.path.isfile(f):
            sys.stderr.write(f"  ! not found: {f}\n")
            fail = True
            continue
        name = os.path.splitext(os.path.basename(f))[0]
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        print("")
        print(f"---- {name} ----")

        if ext == "scad":
            stl = os.path.join(work, name + ".stl")
            r = subprocess.run([osc, "--export-format", "binstl", *defs, "-o", stl, f],
                               capture_output=True, text=True)
            exp = (r.stdout or "") + (r.stderr or "")
            if not (os.path.isfile(stl) and os.path.getsize(stl) > 0):
                print("  [FAIL] (HARD) export   OpenSCAD produced no STL")
                for line in exp.splitlines()[:8]:
                    print(f"        {line}")
                fail = True
                continue
            if _GEOM_RE.search(exp):
                print("  [WARN] (HARD) export   OpenSCAD geometry warning:")
                for line in exp.splitlines():
                    if _GEOM_RE.search(line):
                        print(f"        {line}")
        else:
            stl = f

        rc = run_tool("trimesh,numpy,rtree,scipy", "printability_mesh.py", [stl, "--name", name])
        if rc != 0:
            fail = True

    print("")
    print("================================================================")
    if not fail:
        print(">>> PRINTABILITY: PASS  (all parts cleared HARD rules)")
    else:
        print(">>> PRINTABILITY: FAIL  (see per-part FAIL lines above)")
    print("================================================================")
    return 1 if fail else 0


COMMAND = Command(
    name="printability",
    group="QA & GATES",
    summary="wall / min-feature / overhang / orientation flags (FDM, PLA/PETG)",
    usage=USAGE,
    run=run,
)
