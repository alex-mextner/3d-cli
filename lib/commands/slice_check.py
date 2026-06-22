"""3d slice-check — headless "does this 3MF open / how many plates / does it slice?" gate.

WHAT: verifies a 3MF (or STL) WITHOUT a GUI, using an OrcaSlicer-family CLI binary
  (OrcaSlicer > Bambu Studio > the Snapmaker Orca app, auto-detected). It reports three
  things: (1) does the file OPEN (the slicer parses it), (2) how many PLATES it contains,
  and (3) does it SLICE all plates to G-code. Exit 0 only when every requested check
  passes.

WHY: a 3MF can be syntactically a zip yet fail to slice — geometry off the bed, an
  incompatible printer profile, a corrupt mesh, a newer-than-CLI file version. The only
  trustworthy "it will slice on the printer" signal is to actually run the slicer
  headlessly. This wraps that so any 3MF can be verified in CI / from a subagent with no
  GUI, no clicking, and a single exit code.

HOW PLATE COUNT IS DETECTED: an Orca PROJECT 3MF embeds one `Metadata/plate_N.png` per
  plate (and `plate_N.json` for sliced ones); the count of distinct plate indices is the
  plate count. A bare-mesh 3MF (just `3D/3dmodel.model`, e.g. exported by `3d export`)
  carries no plate metadata and is one implicit plate. This is read straight from the zip
  — no slicer needed — so plate count is always available even when slicing is skipped.

HOW SLICE SUCCESS IS DETECTED: the binary is run with `--slice 0` (0 = all plates;
  `--slice N` = plate N) and `--outputdir <dir>`. On success it exits 0 and writes one
  `plate_N.gcode` per plate into the output dir. Success = exit 0 AND at least one
  non-empty `*.gcode` produced (and, for a project 3MF, the produced-plate count is
  compared to the embedded plate count). A non-zero exit or zero G-code = FAIL, with the
  slicer log tail shown.

KNOWN SLICER QUIRKS (verified on this machine, 2026-06):
  - The Snapmaker Orca app (the user's GUI slicer, an Orca fork) opens 3MFs fine via
    `--info` but its CLI SEGFAULTS (SIGSEGV) when it actually slices, because it looks up
    resolved system presets under `Resources/profiles/BBL/machine_full/` which that build
    does not ship. So `slice-check` uses it for the OPEN + PLATE-COUNT checks but routes
    the SLICE step to upstream OrcaSlicer when available.
  - Upstream OrcaSlicer slices a GUI-saved project 3MF headlessly (exit 0 + plate_N.gcode)
    when given a fresh/throwaway `--datadir` so it self-initialises its bundled profiles.
  - A 3MF written by a NEWER Orca than the CLI needs `--allow-newer-file` or it errors on
    a version gate; we always pass it.

Examples:
  3d slice-check model.3mf                 # open + plate count + slice all plates
  3d slice-check model.3mf --no-slice      # open + plate count only (fast, never slices)
  3d slice-check model.3mf --plates 4      # also assert exactly 4 plates
  3d slice-check model.3mf --slicer /path/to/OrcaSlicer
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, MissingDependency, UsageError

# macOS app bundles that ship an Orca-family CLI, in slice-preference order. The user's
# Snapmaker fork is listed for OPEN/INFO use; slicing prefers a non-Snapmaker Orca.
_SLICE_BINARY_BUNDLES = (
    "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
    "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio",
    "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker_Orca",
)
# The Snapmaker fork can OPEN (--info) but cannot SLICE headlessly (segfaults), so it is
# acceptable for open/plate-count but is the LAST resort for the slice step.
_SNAPMAKER_MARK = "Snapmaker_Orca"

USAGE = """3d slice-check <file.3mf|file.stl> [options]
  Headless verification of a 3MF: does it OPEN, how many PLATES, does it SLICE? Uses an
  OrcaSlicer-family CLI (OrcaSlicer > Bambu Studio > Snapmaker Orca, auto-detected on PATH
  and macOS app bundles) with NO GUI. Exit 0 only when every requested check passes.

Checks:
  OPEN   — the slicer parses the file (geometry/manifold via the CLI `--info`).
  PLATES — number of plates, read from the 3MF's embedded plate metadata (a bare-mesh
           3MF with no plate metadata is one implicit plate).
  SLICE  — `--slice 0` (all plates) produces one plate_N.gcode per plate (skipped with
           --no-slice). Success = exit 0 + >=1 non-empty .gcode produced.

Options:
  --no-slice        only OPEN + PLATES (never invokes the slicer's slice path; fast).
  --plates N        assert the file has exactly N plates (FAIL otherwise).
  --slicer PATH     force a specific slicer binary (overrides SLICER env + auto-detect).
  --datadir DIR     slicer config dir for the slice step. Default: a throwaway temp dir so
                    upstream OrcaSlicer self-initialises its bundled profiles.
  --timeout SECS    per-slicer-call timeout (default 420).
  --printer NAME    informational; recorded in the report (profile selection is embedded
                    in a project 3MF).

Env:
  SLICER=/path/to/binary   force a specific slicer (same as --slicer).

Exit codes: 0 = all checks PASS; 1 = a check FAILED; 2 = bad usage/argument;
  127 = no slicer binary found.

Examples:
  3d slice-check model.3mf
  3d slice-check model.3mf --no-slice
  3d slice-check model.3mf --plates 4
  3d slice-check model.3mf --slicer "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer\""""


# ---------------------------------------------------------------------------
# Plate-count detection — pure zip introspection, no slicer needed.
# ---------------------------------------------------------------------------
_PLATE_PNG_RE = re.compile(r"^Metadata/plate_(\d+)\.png$", re.IGNORECASE)
_PLATE_JSON_RE = re.compile(r"^Metadata/plate_(\d+)\.json$", re.IGNORECASE)
# `<plate> ... key="plater_id" value="N" ...` in Metadata/model_settings.config — the
# authoritative plate list, present even before a project is sliced (so it beats the
# plate_N.png thumbnails, which only appear once the project has been sliced/GUI-saved).
_PLATER_ID_RE = re.compile(rb'key="plater_id"\s+value="(\d+)"', re.IGNORECASE)


def detect_plate_count(path: str) -> int:
    """Plate count of a 3MF from embedded metadata; 1 for a bare-mesh/non-3MF file.

    Order of signals (strongest first):
      1. `Metadata/model_settings.config` `<plate>` entries (`plater_id`) — the plate list
         the slicer itself maintains; present even on an unsliced project.
      2. `Metadata/plate_N.png` / `plate_N.json` count (sliced/GUI-saved projects only;
         `plate_no_light_N.png` decoys are ignored).
      3. A file with no plate metadata at all (a bare mesh, an STL, a plain
         `3D/3dmodel.model`-only 3MF) is one implicit plate.
    """
    if not path.lower().endswith(".3mf") or not zipfile.is_zipfile(path):
        return 1
    with zipfile.ZipFile(path) as zf:
        from_settings = _plater_ids_from_settings(zf)
        if from_settings:
            return len(from_settings)
        from_files = _plate_indices_from_filenames(zf)
        return len(from_files) if from_files else 1


def _plater_ids_from_settings(zf: zipfile.ZipFile) -> set[int]:
    try:
        data = zf.read("Metadata/model_settings.config")
    except KeyError:
        return set()
    return {int(m.group(1)) for m in _PLATER_ID_RE.finditer(data)}


def _plate_indices_from_filenames(zf: zipfile.ZipFile) -> set[int]:
    indices: set[int] = set()
    for name in zf.namelist():
        norm = name.replace("\\", "/")
        if "no_light" in norm.lower():
            continue
        m = _PLATE_PNG_RE.match(norm) or _PLATE_JSON_RE.match(norm)
        if m:
            indices.add(int(m.group(1)))
    return indices


# ---------------------------------------------------------------------------
# Slicer binary discovery.
# ---------------------------------------------------------------------------
def _find_slice_binary(forced: str | None) -> str | None:
    """Resolve the slicer binary for the SLICE step (prefers a non-Snapmaker Orca)."""
    if forced:
        return forced if os.access(forced, os.X_OK) else None
    env = os.environ.get("SLICER")
    if env and os.access(env, os.X_OK):
        return env
    for cmd in ("orca-slicer", "OrcaSlicer", "bambu-studio", "BambuStudio"):
        w = shutil.which(cmd)
        if w:
            return w
    for bundle in _SLICE_BINARY_BUNDLES:
        if os.access(bundle, os.X_OK):
            return bundle
    return None


def _find_info_binary(forced: str | None) -> str | None:
    """Binary for the OPEN/--info check. Any Orca-family binary (incl. Snapmaker) works."""
    sl = _find_slice_binary(forced)
    if sl:
        return sl
    # Even if no slice-capable binary, the Snapmaker fork can still --info.
    for bundle in _SLICE_BINARY_BUNDLES:
        if os.access(bundle, os.X_OK):
            return bundle
    return None


# ---------------------------------------------------------------------------
# Open check via `--info`.
# ---------------------------------------------------------------------------
def _check_open(binary: str, inp: str, timeout: int) -> tuple[bool, str]:
    """Run `<binary> --info <file>`; True + the parsed info block if it opens."""
    try:
        r = subprocess.run(
            [binary, "--info", inp],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (False, "info timed out")
    out = (r.stdout or "") + (r.stderr or "")
    # `--info` prints size_x/number_of_parts/manifold for a file it understood. It exits 0
    # even on some soft errors, so require the geometry markers, not just rc==0.
    opened = ("number_of_parts" in out or "number_of_facets" in out) and r.returncode == 0
    info = _extract_info(out)
    return (opened, info)


def _extract_info(out: str) -> str:
    """Compact geometry summary from `--info` (deduped: multi-object files repeat it)."""
    keep = ("size_x", "size_y", "size_z", "number_of_parts", "number_of_facets", "manifold")
    seen: list[str] = []
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in s for k in keep) and s not in seen:
            seen.append(s)
    return "; ".join(seen)


# ---------------------------------------------------------------------------
# Slice check via `--slice 0`.
# ---------------------------------------------------------------------------
def _check_slice(binary: str, inp: str, datadir: str, timeout: int) -> tuple[bool, int, str]:
    """Slice all plates headlessly; return (ok, n_gcode_produced, log_tail)."""
    outdir = tempfile.mkdtemp(prefix="3d_slicecheck_out.")
    log_fd, log_path = tempfile.mkstemp(prefix="3d_slicecheck_log.", suffix=".txt")
    os.close(log_fd)
    args = [
        binary,
        "--datadir", datadir,
        "--allow-newer-file",
        "--slice", "0",
        "--outputdir", outdir,
        inp,
    ]
    rc, timed_out = _run_to_log(args, log_path, timeout)
    gcodes = _produced_gcodes(outdir)
    log_tail = _tail(log_path, 14)
    if timed_out:
        log_tail = f"(timed out after {timeout}s)\n" + log_tail
    ok = (rc == 0) and len(gcodes) > 0
    _cleanup(outdir, log_path)
    return (ok, len(gcodes), log_tail)


def _run_to_log(args: list[str], log_path: str, timeout: int) -> tuple[int, bool]:
    """Run a slicer call, streaming combined output to log_path. (rc, timed_out)."""
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")  # headless: no display needed.
    with open(log_path, "w") as lf:
        try:
            r = subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout, env=env)
            return (r.returncode, False)
        except subprocess.TimeoutExpired:
            return (-1, True)


def _produced_gcodes(outdir: str) -> list[str]:
    out: list[str] = []
    for fn in os.listdir(outdir):
        full = os.path.join(outdir, fn)
        if fn.lower().endswith(".gcode") and os.path.isfile(full) and os.path.getsize(full) > 0:
            out.append(fn)
    return sorted(out)


def _tail(path: str, n: int) -> str:
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    interesting = [ln for ln in lines if "collapsing redundant" not in ln and "load_from_json" not in ln]
    return "\n".join((interesting or lines)[-n:])


def _cleanup(*paths: str) -> None:
    for p in paths:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Option parsing.
# ---------------------------------------------------------------------------
class _Opts:
    def __init__(self) -> None:
        self.inp = ""
        self.no_slice = False
        self.expect_plates: int | None = None
        self.slicer: str | None = None
        self.datadir: str | None = None
        self.timeout = 420
        self.printer = ""


def _parse(argv: list[str]) -> _Opts:
    opts = _Opts()
    if argv[0].startswith("-"):
        raise UsageError(f"missing model file before option '{argv[0]}'", command="slice-check")
    opts.inp = argv[0]
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--no-slice":
            opts.no_slice = True
            i += 1
        elif a == "--plates":
            opts.expect_plates = _int_value(rest, i, a)
            i += 2
        elif a == "--slicer":
            opts.slicer = _value(rest, i, a)
            i += 2
        elif a == "--datadir":
            opts.datadir = _value(rest, i, a)
            i += 2
        elif a == "--timeout":
            opts.timeout = _int_value(rest, i, a)
            i += 2
        elif a == "--printer":
            opts.printer = _value(rest, i, a)
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="slice-check")
    return opts


def _value(args: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(args) or args[index + 1].startswith("-"):
        raise UsageError(f"{flag} requires a value", command="slice-check")
    return args[index + 1]


def _int_value(args: list[str], index: int, flag: str) -> int:
    raw = _value(args, index, flag)
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidArgument(flag, raw, ["a non-negative integer"], command="slice-check") from exc


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1

    opts = _parse(argv)
    if not os.path.isfile(opts.inp):
        raise InputNotFound(opts.inp, command="slice-check")

    binary = _find_info_binary(opts.slicer)
    if binary is None:
        raise MissingDependency(
            "an OrcaSlicer-family slicer (OrcaSlicer / Bambu Studio / Snapmaker Orca)",
            install="brew install --cask orcaslicer",
            degrades="slice-check cannot open or slice the 3MF",
            command="slice-check",
        )

    return _verify(opts, binary)


def _verify(opts: _Opts, binary: str) -> int:
    print("================================================================")
    print(f"slice-check: {os.path.basename(opts.inp)}")
    print(f"  binary: {binary}")
    if opts.printer:
        print(f"  printer (informational): {opts.printer}")
    print("================================================================")

    plates = detect_plate_count(opts.inp)
    failures: list[str] = []

    _report_open(opts, binary, failures)
    _report_plates(opts, plates, failures)
    slice_ran = _report_slice(opts, binary, plates, failures)

    print("================================================================")
    if failures:
        print(f"STATUS: FAIL ({len(failures)} check(s) failed)")
        for f in failures:
            print(f"  - {f}")
        print("================================================================")
        raise GateFailure("; ".join(failures), command="slice-check", silent=True)
    tail = "" if slice_ran else " (slice skipped: --no-slice)"
    print(f"STATUS: PASS - opens OK, {plates} plate(s){tail}")
    print("================================================================")
    return 0


def _report_open(opts: _Opts, binary: str, failures: list[str]) -> None:
    opened, info = _check_open(binary, opts.inp, opts.timeout)
    if opened:
        print(f"  OPEN:   PASS  {info}")
    else:
        print(f"  OPEN:   FAIL  ({info or 'slicer could not parse the file'})")
        failures.append("file did not open in the slicer")


def _report_plates(opts: _Opts, plates: int, failures: list[str]) -> None:
    if opts.expect_plates is None:
        print(f"  PLATES: {plates}")
        return
    if plates == opts.expect_plates:
        print(f"  PLATES: PASS  {plates} (expected {opts.expect_plates})")
    else:
        print(f"  PLATES: FAIL  {plates} (expected {opts.expect_plates})")
        failures.append(f"plate count {plates} != expected {opts.expect_plates}")


def _report_slice(opts: _Opts, binary: str, plates: int, failures: list[str]) -> bool:
    """Run + report the slice check unless --no-slice. Returns True if it ran."""
    if opts.no_slice:
        print("  SLICE:  skipped (--no-slice)")
        return False
    slice_bin = _slice_step_binary(opts, binary)
    datadir = opts.datadir or tempfile.mkdtemp(prefix="3d_slicecheck_dd.")
    own_dd = opts.datadir is None
    try:
        ok, n_gcode, log_tail = _check_slice(slice_bin, opts.inp, datadir, opts.timeout)
    finally:
        if own_dd:
            shutil.rmtree(datadir, ignore_errors=True)
    if ok:
        match = "" if n_gcode == plates else f" (note: {n_gcode} g-code for {plates} plate(s))"
        print(f"  SLICE:  PASS  {n_gcode} plate g-code produced{match}")
    else:
        print(f"  SLICE:  FAIL  ({n_gcode} g-code produced)")
        if log_tail:
            print("  --- slicer log (tail) ---")
            for ln in log_tail.splitlines():
                print(f"    {ln}")
            print("  -------------------------")
        failures.append("slicer did not produce G-code for all plates")
    return True


def _slice_step_binary(opts: _Opts, info_binary: str) -> str:
    """Pick the binary for the SLICE step: avoid the Snapmaker fork if a real Orca exists."""
    if opts.slicer:
        return opts.slicer
    if _SNAPMAKER_MARK in info_binary:
        for bundle in _SLICE_BINARY_BUNDLES:
            if _SNAPMAKER_MARK not in bundle and os.access(bundle, os.X_OK):
                sys.stderr.write(
                    "slice-check: Snapmaker Orca CLI cannot slice headlessly (segfaults); "
                    f"routing the slice step to {bundle}\n"
                )
                return bundle
    return info_binary


COMMAND = Command(
    name="slice-check",
    group="SLICING",
    summary="headless 3MF verify: opens? how many plates? slices? (Orca CLI, no GUI)",
    usage=USAGE,
    run=run,
)
