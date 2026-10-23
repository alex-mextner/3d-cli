"""3d retarget — move a slicer project .3mf to another printer in seconds, no re-slice from scratch.

WHAT: takes an Orca / Snapmaker Orca / Bambu Studio project .3mf made for printer A and writes
  a .3mf that is ready for printer B: bed, height, nozzle, start/end G-code and limits from B's
  machine preset, one extruder/filament, only the objects you keep, re-centred (and turned about
  Z if it must be) inside B's bed with a margin. Orientation, supports and tuned settings in the
  file are kept. `--set` / `--preset` adjust settings (e.g. easy-release same-material supports);
  `--gcode` slices the result with the installed slicer and bounds-checks the G-code.

WHY: a ready project for another printer should be patched, not re-derived. The K1 SE spoon cost
  over an hour because the existing U1 project was re-oriented and re-supported from scratch.

Examples:
  3d retarget spoon.3mf --to k1se --keep 1 --preset easy-release-supports -o spoon.k1se.3mf
  3d retarget part.3mf --to "Creality K1 SE 0.4 nozzle" --process "0.20mm Standard @Creality K1 SE" --gcode part.gcode
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from typing import TYPE_CHECKING

from cli.registry import Command
from errors import GateFailure, InvalidArgument, MissingDependency, UsageError

if TYPE_CHECKING:
    from slicing.orca_presets import PresetRef
    from slicing.retarget_3mf import Report

USAGE = """3d retarget <project.3mf> --to PRINTER [options]
  Retarget a slicer project .3mf (Orca / Snapmaker Orca / Bambu Studio) to another printer:
  rewrites the printer settings, collapses extruders/filaments to the target's, keeps only the
  chosen objects, re-centres/rotates them to fit the bed with a margin, validates the result.
  Fails (exit 1) when the footprint cannot fit. Never touches a printer.

Options:
  --to PRINTER          target machine preset: exact name or a fuzzy fragment ("k1se"). Looked up in
                        --preset-dir dirs, the repo profiles/ dir, then each slicer's user + system presets
  -o, --out PATH        output .3mf (default: <project>.<printer>.3mf next to the input)
  --keep N[,N...]       keep only these objects (1-based, the numbering the command prints; default all)
  --margin MM           clearance from every bed edge (default 5)
  --process NAME        also apply a process (print) preset, e.g. "0.20mm Standard @Creality K1 SE"
  --filament NAME       also apply a filament preset, e.g. "Orange PLA @K1SE"
  --preset NAME         named setting set (repeatable); available: easy-release-supports
  --set KEY=VALUE       override one project setting (repeatable, applied last); an unknown KEY is an
                        error. List keys take JSON: --set 'nozzle_temperature=["215"]'
  --preset-dir DIR      extra preset directory (repeatable; holds machine/ process/ filament/ *.json)
  --gcode PATH          also slice the result to PATH with the installed Orca-family slicer (the placed objects as an STL
                        + the retargeted settings as presets), then run `3d gcode-bounds` on it
  --bounds-printer NAME fleet printer for that gcode-bounds run (axis limits, trusted macros), e.g. k1se
  --dry-run             do everything but write the .3mf (and skip --gcode); print the same report
  -v, --verbose         list every changed key, not just the first few per group
  -h, --help            this help

Exit: 0 ok, 1 does not fit / failed validation / slice failed, 2 bad usage or unknown preset.

Examples:
  3d retarget spoon.3mf --to k1se --keep 1 -o spoon.k1se.3mf
  3d retarget spoon.3mf --to k1se --keep 1 --preset easy-release-supports --set support_interface_spacing=0.4
  3d retarget part.3mf --to "Creality K1 SE 0.4 nozzle" --process "0.20mm Standard @Creality K1 SE" --filament "Orange PLA @K1SE"
  3d retarget part.3mf --to k1se --margin 8 --preset-dir ./my-presets
  3d retarget part.3mf --to k1se --keep 1 --gcode part.gcode --bounds-printer k1se
  3d retarget big.3mf --to k1se --dry-run          # exit 1 and the footprint-vs-bed numbers if it cannot fit"""

_SHOWN_PER_GROUP = 6


@dataclass
class _Options:
    src: str = ""
    to: str = ""
    out: str = ""
    keep: str = ""
    margin: float = 5.0
    process: str = ""
    filament: str = ""
    setting_sets: list[str] = field(default_factory=list)
    sets: list[str] = field(default_factory=list)
    preset_dirs: list[str] = field(default_factory=list)
    gcode: str = ""
    bounds_printer: str = ""
    dry_run: bool = False
    verbose: bool = False


_VALUED = {
    "--to": "to",
    "-o": "out",
    "--out": "out",
    "--keep": "keep",
    "--process": "process",
    "--filament": "filament",
    "--gcode": "gcode",
    "--bounds-printer": "bounds_printer",
}
_REPEATED = {"--preset": "setting_sets", "--set": "sets", "--preset-dir": "preset_dirs"}


def _margin(value: str) -> float:
    try:
        margin = float(value)
    except ValueError as exc:
        raise InvalidArgument(
            "--margin", value, ["a number of mm, e.g. 5"], command="retarget"
        ) from exc
    if margin < 0:
        raise InvalidArgument("--margin", value, ["a number >= 0"], command="retarget")
    return margin


def _parse(argv: list[str]) -> _Options:
    opts = _Options()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VALUED or arg in _REPEATED or arg == "--margin":
            if i + 1 >= len(argv):
                raise UsageError(
                    f"{arg} needs a value",
                    command="retarget",
                    remediation=["Run '3d retarget --help'."],
                )
            value = argv[i + 1]
            i += 2
            if arg == "--margin":
                opts.margin = _margin(value)
            elif arg in _VALUED:
                setattr(opts, _VALUED[arg], value)
            else:
                getattr(opts, _REPEATED[arg]).append(value)
            continue
        if arg == "--dry-run":
            opts.dry_run = True
        elif arg in ("-v", "--verbose"):
            opts.verbose = True
        elif arg.startswith("-"):
            raise UsageError(
                f"unknown option {arg}",
                command="retarget",
                remediation=["Run '3d retarget --help'."],
            )
        elif opts.src:
            raise UsageError(
                f"unexpected extra argument {arg!r}",
                command="retarget",
                remediation=["One project .3mf per run."],
            )
        else:
            opts.src = arg
        i += 1
    if not opts.src:
        raise UsageError(
            "missing project .3mf",
            command="retarget",
            remediation=["3d retarget <project.3mf> --to PRINTER"],
        )
    if not opts.to:
        raise UsageError(
            "missing --to PRINTER",
            command="retarget",
            remediation=["Name the target machine preset, e.g. --to k1se"],
        )
    return opts


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "target"


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    opts = _parse(argv)

    # Imported lazily so `3d help` and command discovery stay light.
    from slicing import orca_presets, retarget_3mf as rt

    started = time.perf_counter()
    src = Path(opts.src)
    out = (
        Path(opts.out)
        if opts.out
        else src.with_name(f"{src.stem}.{_slug(opts.to)}.3mf")
    )
    if not opts.dry_run and out.resolve() == src.resolve():
        raise InvalidArgument(
            "--out",
            opts.out,
            ["a path different from the input project"],
            command="retarget",
            extra="The source project is never overwritten; pick another output name.",
        )
    if opts.gcode and not opts.dry_run:
        for protected in (src, out):
            if Path(opts.gcode).resolve() == protected.resolve():
                raise InvalidArgument(
                    "--gcode",
                    opts.gcode,
                    ["a path different from the input and the output project"],
                    command="retarget",
                    extra="The G-code would overwrite a project file; pick another name.",
                )
    for pname in opts.setting_sets:
        if pname not in rt.PRESETS:
            raise InvalidArgument(
                "--preset", pname, sorted(rt.PRESETS), command="retarget"
            )
    overrides = [rt.parse_assignment(s) for s in opts.sets]

    catalog = orca_presets.PresetCatalog([Path(p) for p in opts.preset_dirs])
    queries = {"machine": opts.to, "process": opts.process, "filament": opts.filament}
    matched = {
        kind: catalog.matches(kind, query) for kind, query in queries.items() if query
    }
    refs = {
        kind: (found[0] if found else catalog.find(kind, queries[kind]))
        for kind, found in matched.items()
    }
    others = {
        kind: [r.name for r in found[1:]]
        for kind, found in matched.items()
        if len(found) > 1
    }
    sources = {
        kind: rt.PresetSource(ref.name, catalog.flatten(ref))
        for kind, ref in refs.items()
    }
    machine = sources["machine"]

    parts, report = rt.retarget(
        src,
        machine=machine,
        process=sources.get("process"),
        filament=sources.get("filament"),
        keep=opts.keep,
        margin=opts.margin,
        setting_sets=opts.setting_sets,
        overrides=overrides,
    )
    nozzles = len(machine.settings["nozzle_diameter"])
    if opts.dry_run:
        problems = rt.validate_parts(
            parts, bed=report.bed, margin=opts.margin, nozzles=nozzles
        )
    else:
        problems = _write_validated(out, parts, report, opts.margin, nozzles)
    report.problems = problems

    _print_report(report, refs, opts, others)
    elapsed = time.perf_counter() - started
    if problems:
        print(f"VALIDATION: FAIL ({len(problems)} problem(s))")
        for p in problems:
            print(f"  - {p}")
        raise GateFailure(
            "retargeted 3MF failed validation; no file kept",
            command="retarget",
            silent=True,
        )
    size = "" if opts.dry_run else f" ({out.stat().st_size / 1024:.0f} KiB)"
    print(
        "VALIDATION: PASS (XML/JSON well-formed, required settings present, extruder/filament count, footprint inside bed)"
    )
    print(
        f"RESULT: PASS  {'dry run, nothing written' if opts.dry_run else out}{size}  in {elapsed:.1f} s"
    )

    if opts.gcode and not opts.dry_run:
        return _slice_and_check(
            parts, machine.name, Path(opts.gcode), opts.bounds_printer
        )
    return 0


def _write_validated(
    out: Path, parts: dict[str, bytes], report: Report, margin: float, nozzles: int
) -> list[str]:
    """Write to a staging file beside `out`, re-read and validate THAT, and move it onto `out` only
    if it passes: an existing `out` is never replaced by (or lost to) a failed retarget."""
    from slicing import retarget_3mf as rt

    staging = out.with_name(f".{out.name}.staging")
    try:
        rt.write_package(staging, parts)
        problems = rt.validate_parts(
            rt.read_package(staging), bed=report.bed, margin=margin, nozzles=nozzles
        )
        if not problems:
            staging.replace(out)
    finally:
        staging.unlink(missing_ok=True)
    return problems


# `find_slicer` kinds that take the Orca CLI flags used below ("custom" is an explicit $SLICER).
_ORCA_FAMILY = frozenset({"orca", "bambu", "custom"})


def _find_orca_cli() -> str:
    """The Orca-family slicer binary. `find_slicer` knows OrcaSlicer/Bambu/Prusa names, not the
    Snapmaker Orca launcher, so that one is looked up here."""
    from cli.env import find_slicer

    found = find_slicer()
    if found is not None and found[0] in _ORCA_FAMILY:
        return found[1]
    snapmaker = shutil.which("snapmaker-orca") or shutil.which("snapmaker_orca")
    if snapmaker:
        return snapmaker
    raise MissingDependency(
        "an Orca-family slicer",
        install="install OrcaSlicer or Snapmaker Orca, or point $SLICER at its CLI binary",
        degrades="3d retarget --gcode cannot slice; the .3mf was written and validated",
        command="retarget",
    )


def _slice_and_check(
    parts: dict[str, bytes], machine_name: str, gcode: Path, bounds_printer: str
) -> int:
    """Slice the retargeted project to `gcode` and run `3d gcode-bounds` on it.

    The Snapmaker Orca CLI crashes (exit 127) on project 3MFs but slices an STL with
    `--load-settings`/`--load-filaments`, so the placed objects go in as an STL and the
    retargeted project settings go in as machine / process / filament presets.
    """
    from commands import gcode_bounds
    from commands.slice import _record_warnings
    from slicing import retarget_3mf as rt

    binary = _find_orca_cli()
    presets = rt.split_project_settings(
        json.loads(parts[rt.SETTINGS].decode("utf-8")), machine_name
    )
    gcode.parent.mkdir(parents=True, exist_ok=True)
    print(f"slicing the placed objects with {binary} -> {gcode}")
    with tempfile.TemporaryDirectory(prefix="3dretarget.") as tmp:
        work = Path(tmp)
        paths = {kind: work / f"{kind}.json" for kind in presets}
        for kind, body in presets.items():
            paths[kind].write_text(json.dumps(body, indent=1), encoding="utf-8")
        stl = work / "part.stl"
        stl.write_bytes(rt.export_stl(parts))
        cmd = [
            binary, "--datadir", str(work / "dd"),
            "--load-settings", f"{paths['machine']};{paths['process']}",
            "--load-filaments", str(paths["filament"]),
            "--arrange", "0", "--orient", "0", "--slice", "0",
            "--outputdir", str(work), str(stl),
        ]  # fmt: skip
        env = dict(
            os.environ, APPIMAGE_EXTRACT_AND_RUN="1", QT_QPA_PLATFORM="offscreen"
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        produced = sorted(work.glob("*.gcode"))
        if proc.returncode != 0 or len(produced) != 1:
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-8:])
            raise GateFailure(
                f"slicing failed (exit {proc.returncode}, {len(produced)} G-code files; the .3mf was written and validated)",
                command="retarget",
                remediation=[tail or "no slicer output"],
            )
        shutil.move(produced[0], gcode)
        _record_warnings(str(gcode), proc.stdout + proc.stderr, str(work))
    print(f"sliced OK -> {gcode} ({gcode.stat().st_size} bytes)")
    return gcode_bounds.run(
        [str(gcode), *(["--printer", bounds_printer] if bounds_printer else [])]
    )


def _fmt(value: object, limit: int = 40) -> str:
    text = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if "\n" in text or len(text) > limit:
        return f"<{len(text)} chars>"
    return text if text else "''"


def _print_report(
    report: Report,
    refs: dict[str, PresetRef],
    opts: _Options,
    others: dict[str, list[str]],
) -> None:
    print("=" * 72)
    print(f"retarget: {report.source} -> {report.target}")
    for kind, ref in refs.items():
        print(f"  {kind:<8} preset: {ref.name}  [{ref.origin}]")
        if kind in others:
            also = ", ".join(others[kind][:4]) + (
                ", ..." if len(others[kind]) > 4 else ""
            )
            print(f"           also matched: {also} (pass the exact name to choose)")
    for label in report.objects:
        state = "KEPT" if int(label.split(":")[0]) in report.kept else "dropped"
        print(f"  object   {label}  -> {state}")
    p = report.placement
    w, d = report.footprint
    turn = (
        f"rotated {p.angle_deg:+.1f} deg about Z, "
        if abs(p.angle_deg) > 1e-9
        else "no rotation, "
    )
    print(
        f"  fit      footprint {w:.1f} x {d:.1f} mm in bed {report.bed.width:g} x {report.bed.depth:g} mm "
        f"(margin {report.margin:g}); {turn}moved by ({p.dx:+.1f}, {p.dy:+.1f}) mm; top z {report.zmax:.1f} mm"
    )
    groups: dict[str, list[tuple[str, object, object]]] = {}
    for c in report.changes:
        groups.setdefault(c.origin, []).append((c.key, c.old, c.new))
    print(f"  changed  {len(report.changes)} setting key(s)")
    for origin, rows in groups.items():
        print(f"    {origin}: {len(rows)}")
        shown = (
            rows
            if opts.verbose or len(rows) <= _SHOWN_PER_GROUP
            else rows[:_SHOWN_PER_GROUP]
        )
        for key, old, new in shown:
            print(f"      {key}: {_fmt(old)} -> {_fmt(new)}")
        if len(shown) < len(rows):
            print(
                f"      ... {len(rows) - len(shown)} more (-v lists all): "
                + ", ".join(
                    k for k, _, _ in rows[_SHOWN_PER_GROUP : _SHOWN_PER_GROUP + 8]
                )
                + (", ..." if len(rows) - len(shown) > 8 else "")
            )
    for note in report.notes:
        print(f"  note     {note}")
    print("-" * 72)


COMMAND = Command(
    name="retarget",
    group="SLICING",
    summary="move a slicer project .3mf to another printer (bed, gcode, one extruder, fit) in seconds",
    usage="retarget <project.3mf> --to PRINTER [--keep N] [--preset NAME] [--set K=V] [--gcode OUT]",
    run=run,
)
