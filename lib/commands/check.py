"""3d check — UNIFIED verification command = the acceptance MASTER gate.

No selection flags => ALL applicable gates (manifold + consistency + printability,
+ collision when --collision, + silhouette when --ref). Selectors run a subset; --skip
excludes. Prints a per-gate breakdown + a single verdict; exit 0 (PASS) / 1 (FAIL).

Gate sub-steps run by shelling out to `bin/3d <gate>` (or openscad directly) and parsing
the SAME stdout markers the bash version relied on (`>>> MESH CHECK: FAIL`,
`ModuleNotFoundError` -> SKIP, etc.) — the gate-result protocol is preserved verbatim so
"EXACT behavior" + graceful degradation survive.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

from cli.env import find_magick, require_openscad
from cli.pyrun import tool_argv
from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d check <file.scad> [more parts...] [options]
  Unified verification = acceptance master gate. No selectors => run ALL applicable gates.

Core-gate selectors (any combination runs ONLY those core gates):
  --manifold | --mesh     manifold / watertight gate
  --consistency           assert() consistency gate
  --printability          FDM printability gate (walls/overhangs)
  --skip GATE             exclude a gate (repeatable) — the way to get a subset

Data-driven gates (run whenever their data is supplied, never narrow the core set):
  --collision CFG.json    collision/penetration gate (HARD; runs when CFG given)
  --silhouette / --ref I  silhouette IoU/AE vs reference (ADVISORY; runs when --ref given)

  To run ONLY collision:  3d check asm.scad --collision cfg --skip manifold --skip consistency --skip printability

Other:
  --part FILE             additional part to gate (or just pass extra positional files)
  --ref IMAGE             reference image for the silhouette gate
  --cam ex,..,cz          6-param vector camera for the silhouette render
  --size WxH              silhouette render size (default 1100x480)
  -D k=v                  pass-through define (repeatable)

Exit 0 = PASS (all HARD gates pass), 1 = FAIL.

Examples:
  3d check examples/cube.scad                 # all applicable gates
  3d check examples/cube.scad --mesh          # only the manifold gate
  3d check asm.scad --skip printability
  3d check asm.scad --collision verify/collision.json --ref ref.jpg"""

_DEGRADE_RE = re.compile(r"ModuleNotFoundError|No module named|no python runtime")


def _threed() -> str:
    return os.path.join(os.environ.get("REPO_ROOT") or "", "bin", "3d")


def run(argv: list[str]) -> int:  # noqa: C901  (faithful port of the orchestration)
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("check")

    files: list[str] = []
    defs: list[str] = []
    skip: list[str] = []
    sel: list[str] = []
    coll = ""
    ref = ""
    cam = "125,-330,52,125,28,44"
    silsize = "1100,480"
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a in ("--manifold", "--mesh"):
            sel.append("manifold"); i += 1
        elif a == "--consistency":
            sel.append("consistency"); i += 1
        elif a == "--printability":
            sel.append("printability"); i += 1
        elif a == "--collision":
            coll = argv[i + 1] if i + 1 < n else ""; i += 2
        elif a == "--silhouette":
            i += 1
        elif a == "--skip":
            if i + 1 >= n:
                raise UsageError("--skip needs a gate name", command="check")
            skip.append(argv[i + 1]); i += 2
        elif a == "--part":
            if i + 1 < n:
                files.append(argv[i + 1])
            i += 2
        elif a == "--ref":
            ref = argv[i + 1] if i + 1 < n else ""; i += 2
        elif a == "--cam":
            cam = argv[i + 1] if i + 1 < n else cam; i += 2
        elif a == "--size":
            silsize = (argv[i + 1] if i + 1 < n else silsize).replace("x", ","); i += 2
        elif a == "-D":
            if i + 1 < n:
                defs += ["-D", argv[i + 1]]
            i += 2
        elif a in ("-h", "--help"):
            print(USAGE); return 0
        elif a.startswith("-"):
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="check")
        else:
            files.append(a); i += 1

    if not files:
        raise UsageError("no input file given", command="check")
    for f in files:
        if not os.path.isfile(f):
            raise InputNotFound(f, command="check")
    assembly = files[0]

    def want(gate: str) -> bool:
        if gate in skip:
            return False
        if gate == "collision":
            return bool(coll)
        if gate == "silhouette":
            return bool(ref)
        if not sel:
            return True
        return gate in sel

    tty = sys.stdout.isatty()
    gr = "\033[32m" if tty else ""
    rd = "\033[31m" if tty else ""
    yl = "\033[33m" if tty else ""
    bd = "\033[1m" if tty else ""
    zz = "\033[0m" if tty else ""

    def passg(g: str, msg: str = "") -> None:
        print(f"  {gr}{g:<12} PASS{zz}  {msg}")

    def failg(g: str, msg: str = "") -> None:
        print(f"  {rd}{g:<12} FAIL{zz}  {msg}")

    def skipg(g: str, msg: str = "") -> None:
        print(f"  {yl}{g:<12} SKIP{zz}  {msg}")

    def infog(g: str, msg: str = "") -> None:
        print(f"  {bd}{g:<12} ----{zz}  {msg}")

    work = tempfile.mkdtemp(prefix="3d_check.")
    hard_fail = False
    try:
        print(f"{bd}=== check (acceptance gate) ==={zz}")
        print(f"  files: {' '.join(files)}   logs: {work}")
        if sel:
            print(f"  selected gates: {' '.join(sel)}")
        if skip:
            print(f"  skipped gates:  {' '.join(skip)}")
        print()

        # ---- MANIFOLD (HARD) ----
        if want("manifold"):
            print(f"{bd}[MANIFOLD]{zz} render --render + grep WARNING:/ERROR: + mesh watertight")
            man_bad = 0
            man_n = 0
            man_list: list[str] = []
            man_degraded = False
            for f in files:
                man_n += 1
                stl = os.path.join(work, "man_" + re.sub(r"[/.]", "_", f) + ".stl")
                r = subprocess.run([osc, "--render", "--export-format", "binstl", *defs, "-o", stl, f],
                                   capture_output=True, text=True)
                log = (r.stdout or "") + (r.stderr or "")
                if r.returncode != 0:
                    log += "\nRENDER-ERROR"
                bad_f = False
                if re.search(r"WARNING:|ERROR:|Assertion|RENDER-ERROR", log):
                    bad_f = True
                    print(f"    {rd}x{zz} {f} (openscad warning)")
                    for line in [ln for ln in log.splitlines() if re.search(r"WARNING:|ERROR:|Assertion|RENDER-ERROR", ln)][:3]:
                        print(f"        {line}")
                elif os.path.isfile(stl) and os.path.getsize(stl) > 0:
                    mout = _run_capture(tool_argv("trimesh,manifold3d,numpy,scipy,rtree", "mesh_check.py", [stl]))
                    if _DEGRADE_RE.search(mout):
                        man_degraded = True
                    elif ">>> MESH CHECK: FAIL" in mout:
                        bad_f = True
                        print(f"    {rd}x{zz} {f} (mesh-verified non-manifold)")
                        for line in [ln for ln in mout.splitlines() if "MESH CHECK: FAIL" in ln]:
                            print(f"        {line}")
                else:
                    bad_f = True
                    print(f"    {rd}x{zz} {f} (no mesh produced)")
                if bad_f:
                    man_bad += 1
                    man_list.append(f)
            if man_bad > 0:
                failg("MANIFOLD", f"{man_bad}/{man_n} bad: {' '.join(man_list)}")
                hard_fail = True
            elif man_degraded:
                passg("MANIFOLD", f"{man_n} file(s) clean (grep-only — mesh stack absent)")
            else:
                passg("MANIFOLD", f"{man_n} file(s) clean (mesh-verified)")
            print()

        # ---- CONSISTENCY (HARD) ----
        if want("consistency"):
            print(f"{bd}[CONSISTENCY]{zz} assert() checks (grep ERROR:/Assertion)")
            assert_files = [f for f in files if re.search(r"\bassert\s*\(", _read(f))]
            if not assert_files:
                skipg("CONSISTENCY", "no assert() in inputs (nothing to check)")
            else:
                con_bad = 0
                for f in assert_files:
                    r = subprocess.run([osc, *defs, "-o", os.path.join(work, "con.csg"), f],
                                       capture_output=True, text=True)
                    log = (r.stdout or "") + (r.stderr or "")
                    if re.search(r"ERROR:|Assertion failed", log):
                        con_bad += 1
                        print(f"    {rd}x{zz} {f}")
                        for line in [ln for ln in log.splitlines() if re.search(r"ERROR:|Assertion failed", ln)][:3]:
                            print(f"        {line}")
                if con_bad == 0:
                    passg("CONSISTENCY", f"{len(assert_files)} file(s) with asserts hold")
                else:
                    failg("CONSISTENCY", f"{con_bad}/{len(assert_files)} with failed asserts")
                    hard_fail = True
            print()

        # ---- PRINTABILITY (HARD, degrade->SKIP) ----
        if want("printability"):
            print(f"{bd}[PRINTABILITY]{zz} walls/overhangs/watertight")
            r = subprocess.run([_threed(), "printability", *files, *defs], capture_output=True, text=True)
            log = (r.stdout or "") + (r.stderr or "")
            if _DEGRADE_RE.search(log):
                skipg("PRINTABILITY", "mesh stack unavailable (install trimesh) — not failing check")
            elif r.returncode == 0:
                passg("PRINTABILITY", _lastmatch(log, r">>> PRINTABILITY:").replace(">>> ", ""))
            else:
                failg("PRINTABILITY", _lastmatch(log, r">>> PRINTABILITY:|FAIL"))
                hard_fail = True
            print()

        # ---- COLLISION (HARD, only if configured) ----
        if want("collision"):
            print(f"{bd}[COLLISION]{zz} overlap/penetration (needs --collision cfg.json)")
            if not coll:
                skipg("COLLISION", "not configured (pass --collision cfg.json)")
            elif not os.path.isfile(coll):
                failg("COLLISION", f"config not found: {coll}")
                hard_fail = True
            else:
                r = subprocess.run([_threed(), "collision", coll], capture_output=True, text=True)
                log = (r.stdout or "") + (r.stderr or "")
                v = _lastmatch(log, r"RESULT: (PASS|FAIL)")
                if _DEGRADE_RE.search(log):
                    skipg("COLLISION", "mesh stack unavailable — not failing check")
                elif r.returncode == 0:
                    passg("COLLISION", v or "ok")
                else:
                    failg("COLLISION", v or "see log")
                    hard_fail = True
            print()

        # ---- SILHOUETTE (ADVISORY) ----
        if want("silhouette"):
            print(f"{bd}[SILHOUETTE]{zz} image-space IoU/AE vs reference (advisory)")
            if not ref or not os.path.isfile(ref):
                skipg("SILHOUETTE", "no reference (pass --ref <img>)")
            elif find_magick() is None:
                skipg("SILHOUETTE", "ImageMagick not installed")
            else:
                r = subprocess.run(
                    [_threed(), "score", assembly, ref, "-o", os.path.join(work, "score"),
                     "--cam", cam, "--size", silsize],
                    capture_output=True, text=True,
                )
                log = (r.stdout or "") + (r.stderr or "")
                iou = _lastvalue(log, "IoU")
                ae = _lastvalue(log, "AE")
                if iou:
                    infog("SILHOUETTE", f"IoU={iou} AE={ae or '?'} cam=[{cam}] {silsize} (ref={os.path.basename(ref)})")
                else:
                    skipg("SILHOUETTE", "scoring failed")
            print()

        print(f"{bd}------------------------------------------------{zz}")
        if not hard_fail:
            print(f"{gr}>>> CHECK: PASS{zz}")
            return 0
        print(f"{rd}>>> CHECK: FAIL{zz}")
        raise GateFailure(">>> CHECK: FAIL", command="check", silent=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_capture(argv: list[str]) -> str:
    r = subprocess.run(argv, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _lastmatch(text: str, pattern: str) -> str:
    found = [ln for ln in text.splitlines() if re.search(pattern, ln)]
    return found[-1].strip() if found else ""


def _lastvalue(text: str, key: str) -> str:
    found = [ln for ln in text.splitlines() if ln.startswith(key + "=")]
    return found[-1].split("=", 1)[1].strip() if found else ""


COMMAND = Command(
    name="check",
    group="QA & GATES",
    summary="master acceptance gate: all applicable gates (or a selected subset)",
    usage=USAGE,
    aliases=("acceptance",),
    run=run,
)
