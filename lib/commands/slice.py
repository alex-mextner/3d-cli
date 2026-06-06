"""3d slice — slice a model to G-code via the installed slicer.

WHAT: sends a model (.stl, .3mf, or .scad) to the installed slicer (OrcaSlicer > Bambu
  Studio > PrusaSlicer, auto-detected) and produces G-code. Optionally runs a dry-run
  sliceability gate that verifies without keeping the G-code.

WHY: slicing is the bridge between the designed model and the physical printer. The
  slicer converts the mesh into toolpaths, temperatures, and speeds. `3d slice` wraps
  that process with automatic slicer detection, profile validation, and a --dry-run
  gate so you can verify a model is sliceable before committing filament and time.

Examples:
  3d slice part.stl -o part.gcode
  3d slice part.scad --dry-run
  3d slice part.3mf --printer bambu-a1 --material pla --dry-run
  3d slice part.3mf --profile "machine.json,process.json,filament.json"
  3d slice --list-profiles

ROADMAP §4: "3d slice <stl|3mf|scad> [-o] [--printer] [--profile]. Always runs the
  sliceability check as a gate. Rename --check → --dry-run (slice to temp, verify only,
  keep no g-code). Map material + printer (by name, from the registries §2a) → slicer
  machine/process/filament profiles."
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, MissingDependency, UsageError

PROFILE_ACCEPTED = (
    ".ini profile/config exported from PrusaSlicer",
    ".json profile/config exported from OrcaSlicer or Bambu Studio",
    "comma-separated machine/process/filament files, e.g. machine.json,process.json,filament.json",
)

PROFILE_EXPORT_STEPS = (
    "Open OrcaSlicer/Bambu Studio/PrusaSlicer, select the printer, process, and filament/material presets, "
    "then use the GUI's export config/export preset action and pass the exported .json/.ini file(s) to --profile."
)

AUTO_PROFILE_PRINTERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bambu-a1": (("bambu", "a1"), ("mini",)),
    "bambu-lab-a1": (("bambu", "a1"), ("mini",)),
}
AUTO_PROFILE_MATERIALS = {"pla": ("pla",), "petg": ("petg",)}
AUTO_PROFILE_ALL_MATERIAL_TERMS = ("pla", "petg", "abs", "asa", "tpu", "pc", "pa", "nylon", "hips", "pva")
AUTO_PROFILE_ACCEPTED = (
    "--printer bambu-a1|\"Bambu Lab A1\" --material pla with discoverable machine/process/filament profiles",
    "--printer bambu-a1|\"Bambu Lab A1\" --material petg with discoverable machine/process/filament profiles",
    "--profile machine.json,process.json,filament.json",
)

USAGE = """3d slice <model.stl|.3mf|.scad> [options]
  Slice a model to G-code with the installed slicer (OrcaSlicer > Bambu Studio >
  PrusaSlicer; auto-detected on PATH and macOS app bundles). A .scad input is
  exported to STL first (via '3d export').

Options:
  -o, --out PATH        output .gcode (default: <model>.gcode). The slicer writes
                        into a scratch dir first; on success, 3d moves the result here.
  --dry-run             sliceability gate: slice to temp, verify G-code was produced,
                        report OK/FAIL + est. time/filament, then discard the G-code.
  --check               deprecated compatibility alias for --dry-run.
  --list-profiles       list project/slicer profile files 3d can see, then exit.
  --profile FILES       slicer config/profile file(s). Use .ini for PrusaSlicer or
                        .json for OrcaSlicer/Bambu Studio. Comma-separated files are
                        allowed for machine/process/filament profiles.
                        machine = printer geometry/firmware; process = layer height,
                        speeds, supports, infill; filament = material temperatures/flow.
  --printer NAME        printer/machine preset name (best-effort, slicer-flag
                        UNVERIFIED). Prefer explicit --profile files for repeatability.
  --material NAME       material for default profile auto-pick. Supported today:
                        pla, petg with --printer bambu-a1. Defaults to pla when
                        --printer bambu-a1 is used without --profile.
  -D k=v                pass-through define for .scad export (repeatable)

Profile export/remediation:
  In the slicer GUI, choose the printer, process, and filament/material presets,
  then use export config/export preset. Pass the exported .ini/.json file(s) to
  --profile, e.g. --profile machine.json,process.json,filament.json.

Env: SLICER=/path/to/binary forces a specific slicer.

Examples:
  3d slice part.stl -o part.gcode
  3d slice part.scad --dry-run
  3d slice part.3mf --printer bambu-a1 --material pla --dry-run
  3d slice part.3mf --profile "machine.json,process.json,filament.json"
  3d slice --list-profiles"""


@dataclass(frozen=True)
class _Options:
    inp: str
    out: str
    printer: str
    profile: str
    material: str
    dry_run: bool
    defs: list[str]


def _slice_prusa(binary: str, work_input: str, out: str, profile: str, printer: str, log: str) -> int:
    args = [binary, "-g", "--output", out]
    if profile:
        for p in profile.split(","):
            if p:
                args += ["--load", p]
    if printer:
        args += ["--load", printer]  # best-effort; slicer-specific preset naming varies.
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


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv == ["--list-profiles"]:
        _print_profiles()
        return 0

    opts = _parse(argv)
    _validate_input(opts.inp)
    if opts.profile:
        _validate_profiles(opts.profile)

    sl = _find_slicer()
    if sl is None:
        raise MissingDependency(
            "a slicer (OrcaSlicer / Bambu Studio / PrusaSlicer)",
            install=_install_cmd("slicer"),
            degrades="3d slice cannot run, including --dry-run sliceability verification",
            command="slice",
        )
    slicer_kind, slicer_bin = sl
    profile = opts.profile or _resolve_profile(opts)
    _validate_profiles(profile)

    tmpdir = tempfile.mkdtemp(prefix="3dslice.")
    log = tempfile.mktemp(prefix="3dslicelog.")
    try:
        work_input = _prepare_input(opts.inp, opts.defs, tmpdir)
        final_out = opts.out or (opts.inp.rsplit(".", 1)[0] + ".gcode")
        prusa_out = os.path.join(tmpdir, "out.gcode")

        print("================================================================")
        print(f"slice: {os.path.basename(opts.inp)}  via {slicer_kind}  ({slicer_bin})")
        note = "   (--dry-run: verify only, G-code discarded)" if opts.dry_run else ""
        extra = (f"   printer={opts.printer}" if opts.printer else "") + (
            f"   material={opts.material}" if opts.material else ""
        ) + (
            f"   profile={profile}" if profile else ""
        )
        print(f"  -> {final_out}{note}{extra}")
        print("================================================================")

        rc = _run_slicer(slicer_kind, slicer_bin, work_input, tmpdir, prusa_out, opts, profile, log)
        produced = _find_produced_gcode(tmpdir, prusa_out) if rc == 0 else ""
        log_text = _read_log(log)
        _print_log_tail(log_text)

        est_time = _grep1(log_text, r"estimated printing time[^0-9]*[0-9hms: ]+")
        est_fil = _grep1(log_text, r"filament used[^0-9]*[0-9.]+ ?[gm]+")

        if rc == 0 and produced and os.path.getsize(produced) > 0:
            size = os.path.getsize(produced)
            if opts.dry_run:
                print(f"STATUS: PASS - sliceable ({size} bytes produced, discarded; --dry-run gate)")
            else:
                os.makedirs(os.path.dirname(final_out) or ".", exist_ok=True)
                shutil.move(produced, final_out)
                print(f"STATUS: PASS - sliced OK -> {final_out} ({size} bytes)")
            if est_time:
                print(f"  {est_time}")
            if est_fil:
                print(f"  {est_fil}")
            print("================================================================")
            return 0

        print(f"STATUS: FAIL - slicer did not produce G-code (rc={rc})")
        print("  Check --profile/--printer. Many slicers require machine/process/filament profiles.")
        print(f"  {PROFILE_EXPORT_STEPS}")
        print("================================================================")
        return 1
    finally:
        try:
            os.remove(log)
        except OSError:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def _parse(argv: list[str]) -> _Options:
    inp = argv[0]
    if inp.startswith("-"):
        print(USAGE)
        raise UsageError(f"missing model before option '{inp}'", command="slice")

    out = ""
    printer = ""
    profile = ""
    material = ""
    dry_run = False
    defs: list[str] = []
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-o", "--out"):
            out = _value(rest, i, a)
            i += 2
        elif a == "--printer":
            printer = _value(rest, i, a)
            i += 2
        elif a == "--material":
            material = _value(rest, i, a).lower()
            i += 2
        elif a == "--profile":
            profile = _normalize_profiles(_value(rest, i, a))
            i += 2
        elif a == "--dry-run":
            dry_run = True
            i += 1
        elif a == "--check":
            sys.stderr.write("slice: --check is deprecated; use --dry-run\n")
            dry_run = True
            i += 1
        elif a == "--list-profiles":
            raise UsageError("--list-profiles is a standalone command: 3d slice --list-profiles", command="slice")
        elif a == "-D":
            defs += ["-D", _value(rest, i, a)]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="slice")
    return _Options(inp=inp, out=out, printer=printer, profile=profile, material=material, dry_run=dry_run, defs=defs)


def _value(args: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(args) or args[index + 1].startswith("-"):
        raise UsageError(f"{flag} requires a value", command="slice")
    return args[index + 1]


def _validate_input(inp: str) -> None:
    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="slice")


def _validate_profiles(profile: str) -> None:
    if not profile:
        return
    for raw in profile.split(","):
        p = raw.strip()
        if not p:
            raise InvalidArgument("--profile", raw, PROFILE_ACCEPTED, command="slice", extra=PROFILE_EXPORT_STEPS)
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".ini", ".json"):
            raise InvalidArgument("--profile", p, PROFILE_ACCEPTED, command="slice", extra=PROFILE_EXPORT_STEPS)
        if not os.path.isfile(p):
            raise InvalidArgument("--profile", p, PROFILE_ACCEPTED, command="slice", extra=PROFILE_EXPORT_STEPS)


def _normalize_profiles(profile: str) -> str:
    if not profile:
        return profile
    return ",".join(part.strip() for part in profile.split(","))


def _normalize_selector(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _resolve_profile(opts: _Options) -> str:
    if opts.profile:
        return opts.profile
    printer_key = _normalize_selector(opts.printer)
    if not printer_key:
        if opts.material:
            raise UsageError(
                "--material currently drives profile auto-pick and needs --printer bambu-a1",
                command="slice",
                remediation=[
                    "Pass --printer bambu-a1 --material pla|petg, or pass explicit --profile files."
                ],
            )
        return ""
    if printer_key not in AUTO_PROFILE_PRINTERS:
        if opts.material:
            raise InvalidArgument(
                "--printer",
                opts.printer,
                sorted(AUTO_PROFILE_PRINTERS),
                command="slice",
                extra="--material auto-pick is supported only with --printer bambu-a1; otherwise pass explicit --profile files.",
            )
        return ""

    material_key = _normalize_selector(opts.material or "pla")
    if material_key not in AUTO_PROFILE_MATERIALS:
        raise InvalidArgument("--material", opts.material, sorted(AUTO_PROFILE_MATERIALS), command="slice")

    selected = _auto_pick_profiles(printer_key, material_key)
    if selected:
        print(f"slice: auto-picked {printer_key}/{material_key} profiles: {selected}")
        return selected

    raise InvalidArgument(
        "--profile",
        f"auto for --printer {opts.printer} --material {material_key}",
        AUTO_PROFILE_ACCEPTED,
        command="slice",
        extra=(
            f"{PROFILE_EXPORT_STEPS} Put the machine/process/filament .json/.ini files in ./profiles/ "
            "or pass them explicitly with --profile."
        ),
    )


def _auto_pick_profiles(printer_key: str, material_key: str) -> str:
    profiles = _discover_profiles()
    if not profiles:
        return ""
    printer_terms, excluded_terms = AUTO_PROFILE_PRINTERS[printer_key]
    material_terms = AUTO_PROFILE_MATERIALS[material_key]
    picks = {
        "machine": _best_profile(profiles, "machine", printer_terms, excluded_terms, material_terms),
        "process": _best_profile(profiles, "process", printer_terms, excluded_terms, material_terms),
        "filament": _best_profile(profiles, "filament", printer_terms, excluded_terms, material_terms),
    }
    if any(path is None for path in picks.values()):
        return ""
    picked = [path for path in picks.values() if path is not None]
    if len(set(picked)) != len(picked):
        return ""
    return ",".join(picks[role] or "" for role in ("machine", "process", "filament"))


def _best_profile(
    profiles: list[str],
    role: str,
    printer_terms: tuple[str, ...],
    excluded_terms: tuple[str, ...],
    material_terms: tuple[str, ...],
) -> str | None:
    scored: list[tuple[int, str]] = []
    for path in profiles:
        score = _profile_score(path, role, printer_terms, excluded_terms, material_terms)
        if score > 0:
            scored.append((score, path))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


def _profile_score(
    path: str,
    role: str,
    printer_terms: tuple[str, ...],
    excluded_terms: tuple[str, ...],
    material_terms: tuple[str, ...],
) -> int:
    haystack = _profile_haystack(path)
    if any(term in haystack for term in excluded_terms):
        return 0
    score = 0
    if role == "machine":
        if not all(term in haystack for term in printer_terms):
            return 0
        if any(term in haystack for term in ("filament", "material", "process")):
            return 0
        score += 30
        score += 10 if any(term in haystack for term in ("machine", "printer", "nozzle")) else 0
    elif role == "process":
        if not _matches_process_printer(haystack, printer_terms):
            return 0
        if any(term not in material_terms and term in haystack for term in AUTO_PROFILE_ALL_MATERIAL_TERMS):
            return 0
        if any(term in haystack for term in ("filament", "material", "machine", "printer", "nozzle")):
            return 0
        score += 30
        score += 15 if any(term in haystack for term in ("process", "print", "quality", "standard", "layer")) else 0
        score += 8 if any(term in haystack for term in material_terms) else 0
        score += 5 if "0-2" in haystack or "0-20" in haystack or "0-20mm" in haystack else 0
    elif role == "filament":
        if not any(term in haystack for term in material_terms):
            return 0
        if any(term in haystack for term in ("machine", "printer", "process")):
            return 0
        score += 25
        score += 10 if any(term in haystack for term in ("filament", "material")) else 0
    else:
        return 0
    return score


def _profile_haystack(path: str) -> str:
    parts = _profile_haystack_parts(path)
    if parts:
        parts[-1] = os.path.splitext(parts[-1])[0]
    return re.sub(r"[^a-z0-9]+", "-", " ".join(parts).lower())


def _profile_haystack_parts(path: str) -> list[str]:
    parts = [part for part in os.path.normpath(path).split(os.sep) if part]
    lowered = [part.lower() for part in parts]
    for marker in ("profiles", "slicer_profiles", "slicer"):
        if marker in lowered:
            return parts[lowered.index(marker) + 1 :]
    return parts[-3:]


def _matches_process_printer(haystack: str, printer_terms: tuple[str, ...]) -> bool:
    if all(term in haystack for term in printer_terms):
        return True
    return "bbl" in haystack and "a1" in haystack


def _prepare_input(inp: str, defs: list[str], tmpdir: str) -> str:
    ext = inp.rsplit(".", 1)[-1].lower() if "." in inp else ""
    if ext != "scad":
        return inp

    tmp_stl = os.path.join(tmpdir, os.path.splitext(os.path.basename(inp))[0] + ".stl")
    print(f"slice: .scad input - exporting STL via '3d export' -> {tmp_stl}")
    rc = _prepare_export(inp, tmp_stl, defs)
    if rc != 0:
        sys.stderr.write("slice: STL export failed - aborting\n")
        raise GateFailure("STL export failed before slicing", command="slice")
    return tmp_stl


def _prepare_export(inp: str, out: str, defs: list[str]) -> int:
    from commands.export import run as export_run

    return export_run([inp, "-o", out, *defs])


def _run_slicer(
    slicer_kind: str,
    slicer_bin: str,
    work_input: str,
    outdir: str,
    prusa_out: str,
    opts: _Options,
    profile: str,
    log: str,
) -> int:
    auto_profile = profile and not opts.profile and _normalize_selector(opts.printer) in AUTO_PROFILE_PRINTERS
    if auto_profile and _is_prusa_like_slicer(slicer_kind, slicer_bin):
        raise InvalidArgument(
            "--printer",
            opts.printer,
            AUTO_PROFILE_ACCEPTED,
            command="slice",
            extra="Bambu A1 profile auto-pick requires OrcaSlicer or Bambu Studio; with PrusaSlicer pass explicit .ini profiles via --profile.",
        )
    printer_arg = "" if auto_profile else opts.printer
    if slicer_kind == "prusa":
        return _slice_prusa(slicer_bin, work_input, prusa_out, profile, printer_arg, log)
    if slicer_kind in ("orca", "bambu"):
        return _slice_orca(slicer_bin, work_input, outdir, profile, printer_arg, log)
    if slicer_kind == "custom":
        rc = _slice_prusa(slicer_bin, work_input, prusa_out, profile, printer_arg, log)
        if rc == 0:
            return rc
        return _slice_orca(slicer_bin, work_input, outdir, profile, printer_arg, log)
    raise InvalidArgument("slicer kind", slicer_kind, ["orca", "bambu", "prusa", "custom"], command="slice")


def _is_prusa_like_slicer(slicer_kind: str, slicer_bin: str) -> bool:
    return slicer_kind == "prusa" or "prusa" in os.path.basename(slicer_bin).lower()


def _find_produced_gcode(tmpdir: str, prusa_out: str) -> str:
    if os.path.isfile(prusa_out) and os.path.getsize(prusa_out) > 0:
        return prusa_out
    cands = [
        p
        for p in (glob.glob(os.path.join(tmpdir, "*.gcode")) + glob.glob(os.path.join(tmpdir, "*.gcode.3mf")))
        if os.path.getsize(p) > 0
    ]
    return cands[0] if cands else ""


def _read_log(log: str) -> str:
    try:
        with open(log) as lf:
            return lf.read()
    except OSError:
        return ""


def _print_log_tail(log_text: str) -> None:
    print("--- slicer log (tail) ---")
    for line in log_text.splitlines()[-20:]:
        print(f"  {line}")
    print("-------------------------")


def _grep1(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0) if m else ""


def _find_slicer() -> tuple[str, str] | None:
    from cli.env import find_slicer

    return find_slicer()


def _install_cmd(tool: str) -> str:
    from cli.env import install_cmd

    return install_cmd(tool)


def _print_profiles() -> None:
    profiles = _discover_profiles()
    print("3d slice profiles")
    print("  Profiles are slicer config files: machine/process/filament.")
    print("  machine=printer geometry/firmware; process=layer height/speeds/supports/infill;")
    print("  filament=material temperatures/flow.")
    if not profiles:
        print("No slicer profile files found.")
        print(f"  {PROFILE_EXPORT_STEPS}")
        print("  Put exported .ini/.json files in ./profiles/ or pass their paths to --profile.")
        print("  For Bambu A1, 3d can auto-pick: --printer bambu-a1 --material pla|petg.")
        return

    print("Available .ini/.json profile files:")
    cwd = os.getcwd()
    for p in profiles:
        shown = os.path.relpath(p, cwd) if _is_inside(p, cwd) else p
        print(f"  {shown}")
    print("Use comma-separated machine/process/filament files with --profile when your slicer needs them.")
    print("Bambu A1 shortcut: --printer bambu-a1 --material pla|petg auto-picks matching profiles.")


def _discover_profiles() -> list[str]:
    roots = _profile_roots()
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in _walk_profile_files(root):
            key = os.path.abspath(path)
            if key not in seen:
                seen.add(key)
                found.append(key)
    found.sort()
    return found


def _profile_roots() -> list[str]:
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    roots = [
        os.path.join(cwd, "profiles"),
        os.path.join(cwd, "slicer_profiles"),
        os.path.join(cwd, ".3d", "profiles"),
        os.path.join(cwd, "config", "profiles"),
        os.path.join(cwd, "config", "slicer"),
        os.path.join(home, "Library", "Application Support", "OrcaSlicer"),
        os.path.join(home, "Library", "Application Support", "BambuStudio"),
        os.path.join(home, "Library", "Application Support", "PrusaSlicer"),
        os.path.join(home, ".config", "OrcaSlicer"),
        os.path.join(home, ".config", "BambuStudio"),
        os.path.join(home, ".config", "PrusaSlicer"),
        "/Applications/OrcaSlicer.app/Contents/Resources/profiles",
        "/Applications/BambuStudio.app/Contents/Resources/profiles",
        "/Applications/Bambu Studio.app/Contents/Resources/profiles",
        "/Applications/PrusaSlicer.app/Contents/Resources/profiles",
    ]
    sl = _find_slicer()
    if sl is not None:
        _, slicer_bin = sl
        bin_dir = os.path.dirname(os.path.abspath(slicer_bin))
        roots.extend(
            [
                os.path.join(bin_dir, "profiles"),
                os.path.join(os.path.dirname(bin_dir), "Resources", "profiles"),
                os.path.join(os.path.dirname(os.path.dirname(bin_dir)), "Resources", "profiles"),
            ]
        )
    return roots


def _walk_profile_files(root: str) -> list[str]:
    out: list[str] = []
    max_depth = 4
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("cache", "logs", "__pycache__")]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in (".ini", ".json"):
                out.append(os.path.join(cur, fn))
                if len(out) >= 200:
                    return out
    return out


def _is_inside(path: str, root: str) -> bool:
    try:
        os.path.relpath(path, root)
    except ValueError:
        return False
    return not os.path.relpath(path, root).startswith("..")


COMMAND = Command(
    name="slice",
    group="SLICING",
    summary="slice to G-code (Orca > Bambu > Prusa); --dry-run verifies without keeping G-code",
    usage=USAGE,
    run=run,
)
