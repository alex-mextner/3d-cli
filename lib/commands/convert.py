"""3d convert — one command for every model-format conversion the repo's libs support.

IN... OUT: one or more inputs, the LAST argument is the result; the output format comes
from its extension (.stl / .3mf / .usdz / .png; an extensionless name is filled from the
conversion — a single stl/3mf input keeps its format, everything else becomes .3mf).

The flagship route is scad→3mf MULTICOLOUR: every color() group of the scad becomes its
own object in a true Orca/Bambu project 3MF (lib/orca_project_3mf.py) with per-object
`extruder` metadata and its authored colour, so a multi-filament/AMS slicer prints it in
colours instead of one uniform blob. 3mf→3mf accepts the `3d retarget` options (--to
PRINTER, --set, --preset, …) and delegates to that machinery.

Collision contract (lib/convert_plan.py): an output that would overwrite one of its own
inputs never fails — it auto-appends a free suffix (a.stl → a-2.stl); any other existing
target is refused (exit 2) with the exact ready-to-run command that uses a free name,
unless --force; existence is re-checked at write time. Every route writes a staging file
beside the target first and only then commits, so a failed conversion never leaves a
half-written output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

import convert_plan as cp
from cli.env import require_openscad
from cli.pyrun import run_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d convert <IN...> <OUT> [options]
  Convert model files between the formats this repo's libs support. One or more inputs;
  the LAST argument is the result; the output format comes from its extension:

    .stl   binary mesh (several inputs are merged, laid out side by side)
    .3mf   Orca/Bambu PROJECT 3mf. From .scad this is MULTICOLOR-aware: every color()
           group becomes its own object with its own extruder + colour, so an
           AMS / multi-filament slicer prints it in colours
           Example: 3d convert mole.scad mole.3mf
    .usdz  AR Quick Look package, one coloured mesh per colour group
           Example: 3d convert mole.scad mole.usdz
    .png   a render of the model (exactly ONE input)
           Example: 3d convert bracket.scad bracket.png
    (no extension)  filled from the conversion: a single .stl/.3mf input keeps its
           format, everything else becomes .3mf
           Example: 3d convert spoon.3mf spoon-u1 --to u1

  inputs: .scad .stl .3mf .usdz .obj

Options:
  --force              overwrite an existing OUT (default: refuse and print the
                       ready-to-run command with a free name)
                       Example: 3d convert a.scad out.stl --force
  -D k=v               OpenSCAD define for .scad inputs (repeatable)
                       Example: 3d convert model.scad out.stl -D 'depth=40'
  --color r,g,b        diffuse colour 0..1 for single-colour .usdz output
                       (default 0.78,0.74,0.66; authored colours are kept)
  --bed MM             square bed for the .3mf project layout (default 270)
  --to PRINTER         3mf->3mf only: retarget the project to that machine preset
                       (delegated to 3d retarget, with its --set/--preset/--keep/
                       --margin/--process/--filament/--preset-dir/--include-non-
                       printable/-v options passed through)
                       Example: 3d convert spoon.3mf spoon-u1.3mf --to u1

Exit: 0 ok, 1 conversion failed, 2 bad usage / refused overwrite.

Examples:
  3d convert mole.scad mole.3mf                    # multicolour Orca project
  3d convert part.stl part.usdz                    # mesh -> AR Quick Look
  3d convert a.stl b.stl merged.3mf                # merge into one project
  3d convert spoon.3mf spoon-u1.3mf --to u1        # retarget alias
  3d convert model.scad shot.png                   # render as png"""

# Valued retarget flags we pass through (repeatables included; kept in argv order).
_RETARGET_VALUED = (
    "--keep",
    "--margin",
    "--process",
    "--filament",
    "--preset",
    "--set",
    "--preset-dir",
)
_RETARGET_BARE = ("--include-non-printable", "-v", "--verbose")


@dataclass
class _Options:
    positionals: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    force: bool = False
    color: str = "0.78,0.74,0.66"
    bed: str = "270"
    retarget_args: list[str] = field(default_factory=list)


def _needs_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv):
        raise UsageError(f"option {flag} needs a value", command="convert")
    return argv[i + 1]


def _parse(argv: list[str]) -> _Options:
    opts = _Options()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        if arg == "--force":
            opts.force = True
        elif arg == "-D":
            opts.defines.append(_needs_value(argv, i, arg))
            i += 1
        elif arg == "--color":
            opts.color = _needs_value(argv, i, arg)
            i += 1
        elif arg == "--bed":
            opts.bed = _needs_value(argv, i, arg)
            i += 1
        elif arg == "--to":
            opts.retarget_args += ["--to", _needs_value(argv, i, arg)]
            i += 1
        elif arg in _RETARGET_VALUED:
            value = _needs_value(argv, i, arg)
            opts.retarget_args += [arg, value]
            i += 1
        elif arg in _RETARGET_BARE:
            opts.retarget_args.append(arg)
        elif arg.startswith("-"):
            print(USAGE)
            raise UsageError(f"unknown option '{arg}'", command="convert")
        else:
            opts.positionals.append(arg)
        i += 1
    return opts


def _mesh_tool(
    mode: str, inputs: list[str], staged: str, opts: _Options, in_fmts: list[str]
) -> int:
    """The colour-aware mesh worker (lib/convert_mesh.py) through pyrun."""
    deps = "numpy,trimesh"
    if "usdz" in in_fmts or mode == "usdz":
        deps += ",usd-core"  # pxr reads/writes usdz
    argv = [mode, staged, *inputs]
    for define in opts.defines:
        argv += ["-D", define]
    argv += ["--color", opts.color, "--bed", opts.bed]
    return run_tool(deps, "convert_mesh.py", argv)


def _openscad_stl(inp: str, staged: str, opts: _Options) -> int:
    """A single scad → binary STL, straight from OpenSCAD (the `3d export` route)."""
    osc = require_openscad("convert")
    argv = [osc, "--export-format", "binstl"]  # OpenSCAD's .stl default is ASCII
    for define in opts.defines:
        argv += ["-D", define]
    result = subprocess.run([*argv, "-o", staged, inp], capture_output=True, text=True)
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or not os.path.isfile(staged):
        sys.stderr.write(f"convert: openscad failed on {inp}\n")
        for line in text.splitlines():
            sys.stderr.write(f"  {line}\n")
        return 1
    return 0


def _read_3mf(inp: str) -> dict[str, bytes]:
    """Every part of a .3mf zip, without retarget's full-project requirements — a plain
    mesh 3MF is a legal convert input (its geometry goes through the mesh worker)."""
    import zipfile
    from pathlib import Path

    with zipfile.ZipFile(Path(inp)) as zf:
        return {
            info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()
        }


def _is_slicer_project(inp: str) -> bool:
    """True when the .3mf carries Orca's model + model_settings parts."""
    try:
        parts = _read_3mf(inp)
    except (OSError, KeyError, ValueError):
        return False
    return "3D/3dmodel.model" in parts and "Metadata/model_settings.config" in parts


def _project_stl(inp: str, staged: str) -> int:
    """A project .3mf → one binary STL in world/bed coordinates (retarget_3mf)."""
    from slicing.retarget_3mf import export_stl

    data = export_stl(_read_3mf(inp))
    with open(staged, "wb") as handle:
        handle.write(data)
    return 0


def _render_png(inp: str, in_fmt: str, staged: str, opts: _Options) -> int:
    """OUT.png: one render. scad/stl go straight to the renderer; other formats are
    staged through an STL first (the renderer is OpenSCAD-driven)."""
    require_openscad("convert")
    target = inp
    tmp = ""
    if in_fmt not in ("scad", "stl"):
        fd, tmp = tempfile.mkstemp(suffix=".stl")
        os.close(fd)
        if _mesh_tool("stl", [inp], tmp, opts, [in_fmt]) != 0:
            os.unlink(tmp)
            return 1
        target = tmp
    argv = ["single", target, "-o", staged]
    for define in opts.defines:
        argv += ["-D", define]
    rc = run_tool("", "render.py", argv)
    if tmp:
        os.unlink(tmp)
    return rc


def _convert(
    inputs: list[str], in_fmts: list[str], final: str, out_fmt: str, opts: _Options
) -> int:
    staged = cp.staging_path(final)
    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
    try:
        rc = _route(inputs, in_fmts, staged, out_fmt, opts)
        if rc != 0:
            return rc
        cp.commit(staged, final, force=opts.force, inputs=inputs)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)  # a failed route (or a refused commit) leaves nothing
    print(f"output: {final} ({os.path.getsize(final)} bytes)")
    return 0


def _route(
    inputs: list[str], in_fmts: list[str], staged: str, out_fmt: str, opts: _Options
) -> int:
    """One conversion into the staging path; every branch returns a process exit code."""
    single = len(inputs) == 1
    if out_fmt == "png":
        return _render_png(inputs[0], in_fmts[0], staged, opts)
    if out_fmt == "stl":
        if in_fmts == ["scad"]:
            return _openscad_stl(inputs[0], staged, opts)
        if in_fmts == ["3mf"] and _is_slicer_project(inputs[0]):
            return _project_stl(inputs[0], staged)
        if single and in_fmts == ["stl"]:
            shutil.copyfile(inputs[0], staged)  # a lossless re-encode is a copy
            return 0
    if single and in_fmts == [out_fmt] and out_fmt == "3mf":
        return _copy_project(inputs[0], staged)
    if single and in_fmts == [out_fmt] and out_fmt == "usdz":
        shutil.copyfile(inputs[0], staged)  # a lossless re-encode is a copy
        return 0
    return _mesh_tool(
        {"stl": "stl", "3mf": "project3mf", "usdz": "usdz"}[out_fmt],
        inputs,
        staged,
        opts,
        in_fmts,
    )


def _copy_project(inp: str, staged: str) -> int:
    """3mf → 3mf without retarget options: repackage the archive unchanged (a rename)."""
    from slicing.retarget_3mf import write_package
    from pathlib import Path

    write_package(Path(staged), _read_3mf(inp))
    return 0


def _retarget(src: str, final: str, opts: _Options, inputs: list[str]) -> int:
    """3mf → 3mf with --to/--set/…: delegate to the `3d retarget` machinery."""
    from commands import retarget as retarget_command

    staged = cp.staging_path(final)
    rc = retarget_command.run([src, "-o", staged, *opts.retarget_args])
    if rc != 0:
        if os.path.exists(staged):
            os.unlink(staged)
        return rc
    try:
        cp.commit(staged, final, force=opts.force, inputs=inputs)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    print(f"output: {final} ({os.path.getsize(final)} bytes)")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    opts = _parse(argv)
    if len(opts.positionals) < 2:
        raise UsageError(
            "expected IN... OUT — one or more inputs and, LAST, the result",
            command="convert",
            remediation=["3d convert model.scad out.3mf"],
        )
    inputs, out_arg = opts.positionals[:-1], opts.positionals[-1]
    for path in inputs:
        if not os.path.isfile(path):
            raise InputNotFound(path, command="convert")
    in_fmts = [cp.input_format(path) for path in inputs]
    out_fmt = cp.output_format(out_arg, in_fmts)
    if not cp.extension_of(out_arg):
        out_arg = f"{out_arg}.{out_fmt}"  # an extensionless OUT gains the filled format
    cp.check_route(in_fmts, out_fmt, retarget_opts=bool(opts.retarget_args))

    final, note = cp.resolve_output(inputs, out_arg, force=opts.force)
    if note:
        print(note)
    if opts.retarget_args:
        return _retarget(inputs[0], final, opts, inputs)
    return _convert(inputs, in_fmts, final, out_fmt, opts)


COMMAND = Command(
    name="convert",
    group="GEOMETRY & EXPORT",
    summary="convert models between formats (multicolor scad→3mf project, stl/usdz/png, retarget alias)",
    usage=USAGE,
    run=run,
)
