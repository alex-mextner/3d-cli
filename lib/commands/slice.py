"""3d slice — slice a model to G-code via the installed slicer (Orca > Bambu > Prusa)."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

from cli.env import detect_os, find_slicer
from cli.registry import Command
from commands.export import run as export_run
from errors import InputNotFound, MissingDependency, UsageError

USAGE = """3d slice <model.stl|.3mf|.scad> [options]
  Slice a model to G-code with the installed slicer (OrcaSlicer > Bambu Studio >
  PrusaSlicer; auto-detected on PATH and macOS app bundles). A .scad input is
  exported to STL first (via '3d export').

Options:
  -o, --out PATH      output .gcode (default: <model>.gcode). Orca/Bambu write into
                      its parent dir; the result is moved to this path.
  --profile FILE      a profile/config file: .ini (Prusa) or .json (Orca/Bambu),
                      repeatable via comma for Orca/Bambu ("machine.json,process.json")
  --check             sliceability GATE: slice, report OK/FAIL + est. time/filament,
                      then DISCARD the produced G-code. Nonzero exit on failure.
  --printer NAME      printer/machine preset (best-effort, slicer-flag UNVERIFIED).
  -D k=v              pass-through define for .scad export (repeatable)

Env: SLICER=/path/to/binary forces a specific slicer.

Examples:
  3d slice part.stl -o part.gcode
  3d slice part.scad --check
  3d slice part.3mf --profile "machine.json,process.json" """


def _slice_prusa(binary: str, work_input: str, out: str, profile: str, printer: str, log: str) -> int:
    args = [binary, "-g", "--output", out]
    if profile:
        for p in profile.split(","):
            if p:
                args += ["--load", p]
    if printer:
        args += ["--load", printer]  # best-effort
    args.append(work_input)
    with open(log, "w") as lf:
        return subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT).returncode


def _slice_orca(binary: str, work_input: str, outdir: str, profile: str, printer: str, log: str) -> int:
    args = [binary, "--slice", "0", "--outputdir", outdir]
    loads = profile
    if printer:
        loads = (loads + "," if loads else "") + printer
    if loads:
        args += ["--load-settings", loads.replace(",", ";")]
    args.append(work_input)
    with open(log, "w") as lf:
        return subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT).returncode


def run(argv: list[str]) -> int:  # noqa: C901
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    inp = argv[0]
    rest = argv[1:]
    out = ""
    printer = ""
    profile = ""
    check = False
    defs: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            out = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--printer":
            printer = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--profile":
            profile = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--check":
            check = True
            i += 1
        elif a == "-D":
            if i + 1 < n:
                defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="slice")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="slice")

    sl = find_slicer()
    if sl is None:
        os_name = detect_os()
        hints = {
            "macos": "brew install --cask orcaslicer",
            "linux-apt": "OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases",
            "linux-dnf": "OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases",
            "linux-pacman": "OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases",
        }
        raise MissingDependency(
            "a slicer (OrcaSlicer / Bambu Studio / PrusaSlicer)",
            install=hints.get(os_name, "install a slicer (OrcaSlicer / Bambu Studio / PrusaSlicer)"),
            degrades="3d slice cannot run",
            command="slice",
        )
    slicer_kind, slicer_bin = sl

    tmpdir = tempfile.mkdtemp(prefix="3dslice.")
    log = tempfile.mktemp(prefix="3dslicelog.")
    try:
        ext = inp.rsplit(".", 1)[-1].lower() if "." in inp else ""
        work_input = inp
        if ext == "scad":
            tmp_stl = os.path.join(tmpdir, os.path.splitext(os.path.basename(inp))[0] + ".stl")
            print(f"slice: .scad input — exporting STL via '3d export' -> {tmp_stl}")
            rc = export_run([inp, "-o", tmp_stl, *defs])
            if rc != 0:
                sys.stderr.write("slice: STL export failed — aborting\n")
                return 1
            work_input = tmp_stl

        final_out = out or (inp.rsplit(".", 1)[0] + ".gcode")
        # Always slice into the (empty) scratch dir, then move on success.
        outdir = tmpdir
        prusa_out = os.path.join(tmpdir, "out.gcode")

        print("================================================================")
        print(f"slice: {os.path.basename(inp)}  via {slicer_kind}  ({slicer_bin})")
        note = "   (--check: gate only, G-code discarded)" if check else ""
        extra = (f"   printer={printer}" if printer else "") + (f"   profile={profile}" if profile else "")
        print(f"  -> {final_out}{note}{extra}")
        print("================================================================")

        if slicer_kind == "prusa":
            rc = _slice_prusa(slicer_bin, work_input, prusa_out, profile, printer, log)
        elif slicer_kind in ("orca", "bambu"):
            rc = _slice_orca(slicer_bin, work_input, outdir, profile, printer, log)
        elif slicer_kind == "custom":
            rc = _slice_prusa(slicer_bin, work_input, prusa_out, profile, printer, log)
            if rc != 0:
                rc = _slice_orca(slicer_bin, work_input, outdir, profile, printer, log)
        else:
            sys.stderr.write(f"slice: unknown slicer kind '{slicer_kind}'\n")
            rc = 2

        produced = ""
        if rc == 0:
            if os.path.isfile(prusa_out) and os.path.getsize(prusa_out) > 0:
                produced = prusa_out
            else:
                cands = [
                    p for p in (
                        glob.glob(os.path.join(tmpdir, "*.gcode"))
                        + glob.glob(os.path.join(tmpdir, "*.gcode.3mf"))
                    )
                    if os.path.getsize(p) > 0
                ]
                if cands:
                    produced = cands[0]

        log_text = ""
        try:
            with open(log) as lf:
                log_text = lf.read()
        except OSError:
            pass
        print("--- slicer log (tail) ---")
        for line in log_text.splitlines()[-20:]:
            print(f"  {line}")
        print("-------------------------")

        est_time = _grep1(log_text, r"estimated printing time[^0-9]*[0-9hms: ]+")
        est_fil = _grep1(log_text, r"filament used[^0-9]*[0-9.]+ ?[gm]+")

        if rc == 0 and produced and os.path.getsize(produced) > 0:
            size = os.path.getsize(produced)
            if check:
                print(f"STATUS: PASS — sliceable ({size} bytes produced, discarded; --check is a gate)")
            else:
                os.makedirs(os.path.dirname(final_out) or ".", exist_ok=True)
                shutil.move(produced, final_out)
                print(f"STATUS: PASS — sliced OK -> {final_out} ({size} bytes)")
            if est_time:
                print(f"  {est_time}")
            if est_fil:
                print(f"  {est_fil}")
            print("================================================================")
            return 0
        else:
            print(f"STATUS: FAIL — slicer did not produce G-code (rc={rc})")
            print("  (check the profile/printer preset; some slicers REQUIRE --load <profile>)")
            print("================================================================")
            return 1
    finally:
        try:
            os.remove(log)
        except OSError:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def _grep1(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0) if m else ""


COMMAND = Command(
    name="slice",
    group="SLICING",
    summary="slice to G-code (Orca > Bambu > Prusa); --check = sliceability gate",
    usage=USAGE,
    run=run,
)
