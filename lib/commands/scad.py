"""3d scad — plain OpenSCAD tools: validate, preview, multi-preview, export-stl, params,
section, section-color, next-version.

WHAT: a command group that replaces the standalone `openscad/tools/*.sh` shell scripts
  (validate.sh, preview.sh, multi-preview.sh, export-stl.sh, extract-params.sh, section.sh,
  section-color.sh, version-scad.sh). Every subcommand takes one .scad file and drives the
  `openscad` binary directly, with the same flags and behaviour the scripts had.

WHY: one maintained, tested implementation instead of a copy per project. The CLI also
  resolves the openscad binary (PATH / $OPENSCAD / macOS app bundles) and exports
  OPENSCADPATH (BOSL2, NopSCADlib) itself, so `include <BOSL2/std.scad>` just works.

KEY BEHAVIOUR: OpenSCAD exits 0 when an `assert()` fails or a library include is missing
  (it only prints `ERROR: Assertion ...`). Every subcommand therefore scans OpenSCAD's
  output for `ERROR:` / `Assertion` and fails with a non-zero exit when it finds one.
  `export-stl` also fails when OpenSCAD reports non-manifold or self-intersecting geometry.

Examples:
  3d scad export-stl box.scad out/box.stl -D 'width=80'
  3d scad multi-preview box.scad previews/ --render
  3d scad section assembly.scad cut.png --module='shaft()' --plane=YZ

INVARIANTS: stdlib-only at import time (registry contract). The only heavy work is the openscad
subprocess.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cli.env import openscad_display_prefix, require_openscad
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

COLORSCHEME = "Tomorrow Night"
DEFAULT_CAMERA = "0,0,0,55,0,25,0"
PLANES = ("YZ", "XZ", "XY")
KEEPS = ("neg", "pos")

USAGE = """3d scad <subcommand> [args]   (plain OpenSCAD tools; OPENSCADPATH libs auto-resolved)
  validate <in.scad>                      syntax/ERROR check, no render
  preview <in.scad> <out.png> [--camera=CAM] [--size=WxH] [--render] [-D 'var=val' ...]
                                          one PNG, default isometric
  multi-preview <in.scad> <out_dir/> [--render] [-D 'var=val' ...]
                                          six PNGs: iso front back left right top
  export-stl <in.scad> <out.stl> [--ascii] [-D 'var=val' ...]
                                          STL + manifold / self-intersection check
  params <in.scad> [--json]               list customizer parameters of the file
  section <in.scad> <out.png> --module='name()' [--plane=YZ|XZ|XY] [--keep=neg|pos]
                                          [--size=WxH] [-D 'var=val' ...]
                                          mono cross-section of one module
  section-color <assembly.scad> <out.png> [--plane=YZ|XZ|XY] [--keep=neg|pos]
                                          [--center=X,Y,Z] [--dist=N] [--size=WxH]
                                          [-D 'var=val' ...]
                                          per-part COLOURED cross-section of an assembly
  next-version <model-name>               next free <name>_NNN.scad in the current dir

Common options:
  -D 'var=val'    pass-through OpenSCAD define (repeatable; `-Dvar=val` also accepted)
  --render        force CGAL/manifold render (no throwntogether cut-face artifacts)
  --size=WxH      image size (preview default 800x600, section 1200x900)
  --camera=CAM    7 values 'tx,ty,tz,rx,ry,rz,dist' (gimbal) or 6 values
                  'ex,ey,ez,cx,cy,cz' (VECTOR eye+center). Default 0,0,0,55,0,25,0
                  (with --autocenter --viewall, so dist 0 still frames the model).

Failures: OpenSCAD exits 0 on a failed assert(); any `ERROR:` / `Assertion` line in its
output makes the subcommand exit 1. export-stl prints `STATUS: PASSED` or `STATUS: FAILED`.

Examples:
  3d scad export-stl box.scad box.stl -D 'width=80' -D 'height=60'
  3d scad preview box.scad box.png --camera=0,0,0,90,0,0,0 --render
  3d scad multi-preview box.scad previews/
  3d scad section part.scad cut.png --module='shaft()' --plane=XZ --keep=pos
  3d scad section-color assembly.scad cut.png --plane=YZ --center=0,0,118 --dist=210
  3d scad params box.scad --json | jq '.[].name'
  3d scad next-version bracket        # -> bracket_003.scad if _001/_002 exist

Docs: docs/commands/scad.md, docs/commands/scad-section-color.md"""

SECTION_USAGE = (
    "3d scad section <in.scad> <out.png> --module='name()' "
    "[--plane=YZ|XZ|XY] [--keep=neg|pos] [--size=WxH] [-D 'var=val']"
)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
@dataclass
class Parsed:
    positionals: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    defines: list[str] = field(default_factory=list)


def _parse(
    argv: Sequence[str],
    sub: str,
    *,
    valued: Sequence[str] = (),
    bools: Sequence[str] = (),
    defines: bool = True,
) -> Parsed:
    """Parse `--opt=v`, `--opt v`, bool `--flag` and repeatable `-D v` / `-Dv`."""
    out = Parsed()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-D" and defines:
            i += 1
            if i >= len(argv):
                raise UsageError(
                    "-D needs a 'var=value' argument", command=f"scad {sub}"
                )
            out.defines.append(argv[i])
        elif arg.startswith("-D") and len(arg) > 2 and defines:
            out.defines.append(arg[2:])
        elif arg in bools:
            out.flags.add(arg)
        elif arg.startswith("--"):
            name, eq, val = arg.partition("=")
            if name not in valued:
                raise UsageError(
                    f"unknown option: {arg}",
                    command=f"scad {sub}",
                    remediation=[
                        f"Run `3d scad {sub} --help` for the accepted options."
                    ],
                )
            if not eq:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"{name} needs a value", command=f"scad {sub}")
                val = argv[i]
            out.values[name] = val
        else:
            out.positionals.append(arg)
        i += 1
    return out


def _need_positionals(p: Parsed, sub: str, n: int, usage: str) -> None:
    if len(p.positionals) != n:
        raise UsageError(
            f"expected {n} positional argument(s), got {len(p.positionals)}",
            command=f"scad {sub}",
            remediation=[f"Usage: {usage}"],
        )


def _define_args(defines: Sequence[str]) -> list[str]:
    return [a for d in defines for a in ("-D", d)]


def _size(raw: str, sub: str) -> str:
    """`WxH` or `W,H` -> the `W,H` that --imgsize wants."""
    s = raw.replace("x", ",", 1)
    if not re.fullmatch(r"\d+,\d+", s):
        raise InvalidArgument(
            "--size", raw, ["WxH (e.g. 800x600)"], command=f"scad {sub}"
        )
    return s


def _camera(raw: str, sub: str) -> str:
    parts = raw.split(",")
    if len(parts) not in (6, 7) or not all(
        re.fullmatch(r"-?\d+(\.\d+)?", x.strip()) for x in parts
    ):
        raise InvalidArgument(
            "--camera",
            raw,
            [
                "6 numbers ex,ey,ez,cx,cy,cz (vector)",
                "7 numbers tx,ty,tz,rx,ry,rz,dist (gimbal)",
            ],
            command=f"scad {sub}",
        )
    return ",".join(x.strip() for x in parts)


def _choice(
    p: Parsed, key: str, accepted: Sequence[str], default: str, sub: str
) -> str:
    val = p.values.get(key, default)
    if val not in accepted:
        raise InvalidArgument(key, val, accepted, command=f"scad {sub}")
    return val


def _require_file(path: str, sub: str) -> Path:
    f = Path(path)
    if not f.is_file():
        raise InputNotFound(path, command=f"scad {sub}")
    return f


# ---------------------------------------------------------------------------
# running OpenSCAD
# ---------------------------------------------------------------------------
# OpenSCAD exits 0 on a failed assert() and on a missing include; both only show up in the
# output as `ERROR: ...` / `Assertion ...` lines.
_ERROR_RE = re.compile(r"^\s*(ERROR:.*|.*\bAssertion\b.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Result:
    code: int
    output: str

    @property
    def errors(self) -> list[str]:
        return [m.group(1).strip() for m in _ERROR_RE.finditer(self.output)]

    @property
    def failed(self) -> bool:
        return self.code != 0 or bool(self.errors)


def _openscad(args: Sequence[str], *, image: bool = False) -> Result:
    exe = require_openscad("scad")
    prefix = openscad_display_prefix() if image else []
    proc = subprocess.run(
        [*prefix, exe, *args], capture_output=True, text=True, check=False
    )
    return Result(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _report_failure(res: Result, what: str) -> int:
    print(f"ERROR: {what} failed (openscad exit {res.code}).", file=sys.stderr)
    print(res.errors and "\n".join(res.errors) or res.output.rstrip(), file=sys.stderr)
    return 1


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _render_png(
    inp: Path,
    out: Path,
    *,
    camera: str,
    size: str,
    render: bool,
    defines: Sequence[str],
    extra: Sequence[str] = (),
    autocenter: bool = True,
    label: str,
) -> int:
    """One OpenSCAD PNG render. Returns 0, or 1 after reporting the failure."""
    args = [
        *(["--render"] if render else []),
        f"--camera={camera}",
        f"--imgsize={size}",
        f"--colorscheme={COLORSCHEME}",
        *(["--autocenter", "--viewall"] if autocenter else []),
        *extra,
        *_define_args(defines),
        "-o",
        str(out),
        str(inp),
    ]
    res = _openscad(args, image=True)
    if res.failed:
        return _report_failure(res, label)
    if not out.is_file():
        print(
            f"ERROR: {label} produced no output file {out}.\n{res.output.rstrip()}",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_validate(argv: list[str]) -> int:
    p = _parse(argv, "validate", defines=False)
    _need_positionals(p, "validate", 1, "3d scad validate <in.scad>")
    inp = _require_file(p.positionals[0], "validate")
    print(f"Validating: {inp}")
    out = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "3d-cli"
    out.mkdir(parents=True, exist_ok=True)
    echo = out / f"validate-{os.getpid()}.echo"
    try:
        res = _openscad(["-o", str(echo), "--export-format=echo", str(inp)])
        echoed = (
            echo.read_text(encoding="utf-8", errors="replace") if echo.is_file() else ""
        )
    finally:
        echo.unlink(missing_ok=True)
    # The echo export writes OpenSCAD's log lines (incl. assert failures) into the file.
    res = Result(res.code, res.output + echoed)
    if res.failed:
        print("✗ Validation failed")
        print(res.output.rstrip())
        return 1
    print("✓ Syntax OK")
    if echoed.strip():
        print("\nEcho output:")
        print(echoed.rstrip())
    return 0


def cmd_preview(argv: list[str]) -> int:
    p = _parse(argv, "preview", valued=("--camera", "--size"), bools=("--render",))
    _need_positionals(
        p,
        "preview",
        2,
        "3d scad preview <in.scad> <out.png> [--camera=...] [--size=WxH]",
    )
    inp = _require_file(p.positionals[0], "preview")
    out = Path(p.positionals[1])
    camera = _camera(p.values.get("--camera", DEFAULT_CAMERA), "preview")
    size = _size(p.values.get("--size", "800x600"), "preview")
    _ensure_dir(out)
    render = "--render" in p.flags
    print(f"Rendering: {inp} -> {out}{' --render' if render else ''}")
    if _render_png(
        inp,
        out,
        camera=camera,
        size=size,
        render=render,
        defines=p.defines,
        label="preview",
    ):
        return 1
    print(f"Preview saved: {out}")
    return 0


# angle -> camera, in the order the old script rendered them.
MULTI_ANGLES: tuple[tuple[str, str], ...] = (
    ("iso", "0,0,0,55,0,25,0"),
    ("front", "0,0,0,90,0,0,0"),
    ("back", "0,0,0,90,0,180,0"),
    ("left", "0,0,0,90,0,90,0"),
    ("right", "0,0,0,90,0,-90,0"),
    ("top", "0,0,0,0,0,0,0"),
)


def cmd_multi_preview(argv: list[str]) -> int:
    p = _parse(argv, "multi-preview", bools=("--render",))
    _need_positionals(
        p, "multi-preview", 2, "3d scad multi-preview <in.scad> <out_dir/> [--render]"
    )
    inp = _require_file(p.positionals[0], "multi-preview")
    out_dir = Path(p.positionals[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    render = "--render" in p.flags
    print(f"Generating multi-angle previews: {inp}\nOutput: {out_dir}\n")
    made: list[Path] = []
    for angle, camera in MULTI_ANGLES:
        out = out_dir / f"{inp.stem}_{angle}.png"
        print(f"  [{angle}] rendering...{' --render' if render else ''}")
        if _render_png(
            inp,
            out,
            camera=camera,
            size="800,600",
            render=render,
            defines=p.defines,
            label=f"multi-preview [{angle}]",
        ):
            return 1
        made.append(out)
    print("\nGenerated:")
    for out in made:
        print(f"  {out} ({out.stat().st_size} bytes)")
    return 0


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _geometry_issues(output: str) -> tuple[list[str], list[str]]:
    """(blocking issues, warnings) found in OpenSCAD's export output."""
    blocking: list[str] = []
    if re.search(r"not.*manifold|non-manifold", output, re.IGNORECASE):
        blocking.append("Non-manifold geometry (holes in mesh)")
    if re.search(r"self-intersect", output, re.IGNORECASE):
        blocking.append("Self-intersecting geometry")
    warnings: list[str] = []
    if re.search(r"degenerate", output, re.IGNORECASE):
        warnings.append("Degenerate faces (zero-area triangles)")
    return blocking, warnings


def cmd_export_stl(argv: list[str]) -> int:
    p = _parse(argv, "export-stl", bools=("--ascii",))
    _need_positionals(
        p,
        "export-stl",
        2,
        "3d scad export-stl <in.scad> <out.stl> [-D 'var=value'] [--ascii]",
    )
    inp = _require_file(p.positionals[0], "export-stl")
    out = Path(p.positionals[1])
    fmt = "asciistl" if "--ascii" in p.flags else "binstl"
    _ensure_dir(out)
    print("=" * 40)
    print(f"Export STL: {inp.name}")
    if p.defines:
        print(f"Parameters: {' '.join(_define_args(p.defines))}")
    print("=" * 40)

    out.unlink(missing_ok=True)  # a stale file must not pass for this run's result
    res = _openscad(
        ["--export-format", fmt, *_define_args(p.defines), "-o", str(out), str(inp)]
    )
    if res.failed or not out.is_file():
        print("--- Export Failed ---")
        print(res.output.rstrip())
        causes = _geometry_issues(res.output)[0] or [
            "OpenSCAD reported errors (a failed assert() exits 0 but prints ERROR)"
        ]
        print("STATUS: FAILED — " + "; ".join(causes))
        return 1

    blocking, warnings = _geometry_issues(res.output)
    print(f"\nOutput: {out} ({_human_size(out.stat().st_size)})")
    if fmt == "binstl":
        with out.open("rb") as fh:
            header = fh.read(84)
        if len(header) == 84:
            print(f"Triangles: {int.from_bytes(header[80:84], 'little')}")
    print("\n--- Geometry Validation ---")
    for w in warnings:
        print(f"WARNING: {w}")
    if blocking:
        print("STATUS: FAILED")
        for b in blocking:
            print(f"- {b}")
        print(
            "\nFixes: Non-manifold → closed solids; Self-intersect → use union(); "
            "Degenerate → no zero-thickness features"
        )
        print("=" * 40)
        return 1
    print("STATUS: PASSED — manifold, no self-intersections, ready for slicer")
    print("=" * 40)
    return 0


_PARAM_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^;]+);\s*(?://\s*(.*))?")


@dataclass(frozen=True)
class Param:
    name: str
    value: str
    type: str
    range: str
    options: str
    description: str


def _classify(value: str) -> tuple[str, str]:
    if value in ("true", "false"):
        return "boolean", value
    if re.fullmatch(r"-?\d+", value):
        return "integer", value
    if re.fullmatch(r"-?\d*\.?\d+", value):
        return "number", value
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return "string", value[1:-1]
    if value.startswith("["):
        return "array", value
    return "expression", value


def extract_params(text: str) -> list[Param]:
    """Top-level `name = value; // [range|options] description` assignments (brace depth 0)."""
    params: list[Param] = []
    depth = 0
    for line in text.splitlines():
        depth += line.count("{") - line.count("}")
        if depth > 0:
            continue
        m = _PARAM_RE.match(line)
        if not m:
            continue
        ptype, value = _classify(m.group(2).strip())
        comment = m.group(3) or ""
        range_v = options_v = ""
        desc = comment
        bm = re.match(r"\[([^\]]+)\]\s*(.*)", comment)
        if bm:
            bracket, desc = bm.group(1), bm.group(2)
            if ":" in bracket and "," not in bracket:
                range_v = bracket
            else:
                options_v = bracket
        params.append(Param(m.group(1), value, ptype, range_v, options_v, desc))
    return params


def cmd_params(argv: list[str]) -> int:
    p = _parse(argv, "params", bools=("--json",), defines=False)
    _need_positionals(p, "params", 1, "3d scad params <in.scad> [--json]")
    inp = _require_file(p.positionals[0], "params")
    params = extract_params(inp.read_text(encoding="utf-8", errors="replace"))
    if "--json" in p.flags:
        rows = []
        for q in params:
            row = {"name": q.name, "value": q.value, "type": q.type}
            if q.range:
                row["range"] = q.range
            if q.options:
                row["options"] = q.options
            if q.description:
                row["description"] = q.description
            rows.append(row)
        print(json.dumps(rows, indent=2))
        return 0
    print(f"Parameters in: {inp}")
    print("=" * 47)
    print(f"{'NAME':<20} {'VALUE':<15} {'TYPE':<10} CONSTRAINT/DESC")
    print("-" * 47)
    for q in params:
        constraint = (
            f"[{q.range}]" if q.range else f"[{q.options}]" if q.options else ""
        )
        if q.description:
            constraint = f"{constraint} {q.description}".strip()
        print(f"{q.name:<20} {q.value:<15} {q.type:<10} {constraint}")
    return 0


# plane -> keep -> (cut box, eye) for the mono section; mirrors the verified section.sh.
_CUT_BOXES: dict[str, dict[str, tuple[str, str]]] = {
    "YZ": {
        "neg": ("translate([0,-300,-300]) cube([600,600,600]);", "240,70,120"),
        "pos": ("translate([-600,-300,-300]) cube([600,600,600]);", "-240,70,120"),
    },
    "XZ": {
        "neg": ("translate([-300,0,-300]) cube([600,600,600]);", "70,240,120"),
        "pos": ("translate([-300,-600,-300]) cube([600,600,600]);", "70,-240,120"),
    },
    "XY": {
        "neg": ("translate([-300,-300,0]) cube([600,600,600]);", "120,70,260"),
        "pos": ("translate([-300,-300,-600]) cube([600,600,600]);", "120,70,-260"),
    },
}


def cmd_section(argv: list[str]) -> int:
    p = _parse(argv, "section", valued=("--module", "--plane", "--keep", "--size"))
    _need_positionals(p, "section", 2, SECTION_USAGE)
    inp = _require_file(p.positionals[0], "section")
    out = Path(p.positionals[1])
    module = p.values.get("--module", "")
    if not module:
        raise UsageError(
            "--module='name()' is required (the geometry call to section)",
            command="scad section",
            remediation=[f"Usage: {SECTION_USAGE}"],
        )
    plane = _choice(p, "--plane", PLANES, "YZ", "section")
    keep = _choice(p, "--keep", KEEPS, "neg", "section")
    size = _size(p.values.get("--size", "1200x900"), "section")
    cut, eye = _CUT_BOXES[plane][keep]
    _ensure_dir(out)

    # The temp file lives next to the INPUT: use/include resolve against the file's own
    # directory, not the cwd.
    tmp = inp.resolve().parent / f".section_tmp_{os.getpid()}.scad"
    tmp.write_text(
        f"// AUTO-GENERATED cross-section (3d scad section) — safe to delete\n"
        f"use <{inp.name}>\n"
        f"difference() {{\n    {module}\n    {cut}\n}}\n",
        encoding="utf-8",
    )
    print(f"Section: {inp}  plane={plane} keep={keep} -> {out}")
    try:
        rc = _render_png(
            tmp,
            out,
            camera=f"{eye},0,0,0",
            size=size,
            render=True,
            defines=p.defines,
            label="section",
        )
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    if rc:
        return 1
    print(f"Section saved: {out}")
    return 0


def section_eye(
    plane: str, keep: str, center: tuple[float, float, float], dist: float
) -> tuple[float, float, float]:
    """Eye position on the REMOVED side of the cut, with angled offsets (0.29 / 0.50 of
    dist) on the two secondary axes so the cut face reads in 3D."""
    cx, cy, cz = center
    main = dist if keep == "neg" else -dist
    o1, o2 = 0.29 * dist, 0.50 * dist
    if plane == "YZ":
        return cx + main, cy + o1, cz + o2
    if plane == "XZ":
        return cx + o1, cy + main, cz + o2
    return cx + o2, cy + o1, cz + main


def _parse_center(raw: str) -> tuple[float, float, float]:
    parts = (raw.split(",") + ["0", "0", "0"])[:3]
    try:
        x, y, z = (float(v) if v.strip() else 0.0 for v in parts)
    except ValueError:
        raise InvalidArgument(
            "--center", raw, ["X,Y,Z numbers"], command="scad section-color"
        ) from None
    return x, y, z


def cmd_section_color(argv: list[str]) -> int:
    p = _parse(
        argv,
        "section-color",
        valued=("--plane", "--keep", "--center", "--dist", "--size"),
    )
    _need_positionals(
        p,
        "section-color",
        2,
        "3d scad section-color <assembly.scad> <out.png> [--plane=YZ|XZ|XY] "
        "[--keep=neg|pos] [--center=X,Y,Z] [--dist=N] [--size=WxH] [-D 'var=val']",
    )
    inp = _require_file(p.positionals[0], "section-color")
    out = Path(p.positionals[1])
    plane = _choice(p, "--plane", PLANES, "YZ", "section-color")
    keep = _choice(p, "--keep", KEEPS, "neg", "section-color")
    center_raw = p.values.get("--center", "0,0,0")
    center = _parse_center(center_raw)
    dist_raw = p.values.get("--dist", "250")
    try:
        dist = float(dist_raw)
    except ValueError:
        raise InvalidArgument(
            "--dist", dist_raw, ["a number"], command="scad section-color"
        ) from None
    size = _size(p.values.get("--size", "1200x900"), "section-color")
    ex, ey, ez = section_eye(plane, keep, center, dist)
    camera = f"{ex:.4f},{ey:.4f},{ez:.4f},{center[0]},{center[1]},{center[2]}"
    _ensure_dir(out)

    print(
        f"Coloured section: {inp}  plane={plane} keep={keep} center={center_raw} dist={dist_raw}"
    )
    print(f"Camera (eye+center): {camera}")
    # The assembly contract auto-defines come first so a user -D can still override them.
    auto = ["cut=true", f'section_plane="{plane}"', f'section_keep="{keep}"']
    if _render_png(
        inp,
        out,
        camera=camera,
        size=size,
        render=True,
        defines=[*auto, *p.defines],
        autocenter=False,
        label="section-color",
    ):
        return 1
    nbytes = out.stat().st_size
    print(f"Section saved: {out} ({nbytes} bytes)")
    if nbytes < 20000:
        print("WARNING: output < 20KB — frame may be empty (check camera/center/dist).")
    return 0


def cmd_next_version(argv: list[str]) -> int:
    p = _parse(argv, "next-version", defines=False)
    _need_positionals(p, "next-version", 1, "3d scad next-version <model-name>")
    name = p.positionals[0]
    pattern = re.compile(rf"{re.escape(name)}_(\d{{3}})\.scad")
    found = sorted(
        (int(m.group(1)), f.name)
        for f in Path.cwd().iterdir()
        if f.is_file() and (m := pattern.fullmatch(f.name))
    )
    if not found:
        print(f"No existing versions for '{name}'")
        print(f"Create: {name}_001.scad")
        return 0
    print("Existing: " + " ".join(n for _, n in found))
    print(f"Create: {name}_{found[-1][0] + 1:03d}.scad")
    return 0


SUBCOMMANDS = {
    "validate": cmd_validate,
    "preview": cmd_preview,
    "multi-preview": cmd_multi_preview,
    "export-stl": cmd_export_stl,
    "params": cmd_params,
    "section": cmd_section,
    "section-color": cmd_section_color,
    "next-version": cmd_next_version,
}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    handler = SUBCOMMANDS.get(sub)
    if handler is None:
        raise InvalidArgument("subcommand", sub, list(SUBCOMMANDS), command="scad")
    if rest and rest[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return handler(rest)


COMMAND = Command(
    name="scad",
    group="GEOMETRY & EXPORT",
    summary="plain OpenSCAD tools: validate, preview, multi-preview, export-stl, params, section, section-color, next-version",
    usage="scad <validate|preview|multi-preview|export-stl|params|section|section-color|next-version> ...",
    run=run,
)
