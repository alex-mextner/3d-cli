"""usdz.py — `3d usdz` command: export a .scad/.stl to a COLORED USDZ for AR Quick Look.

Accessed via: the `3d usdz` subcommand (discovered by lib/cli/registry.py).
Invariants: stdlib-only at module top level (discovery imports every command on every
  `3d` call); the heavy converter (trimesh + pxr) lives in lib/usdz.py and is imported
  LAZILY inside run(). A .scad input is first exported to a temp STL by shelling out to
  `bin/3d export` (so geometry validation runs), then converted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from cli.pyrun import run_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

# Stone / travertine — a neutral default so an untextured part still reads as a real
# object in Quick Look rather than flat gray. (RGB in 0..1.)
DEFAULT_COLOR = (0.78, 0.74, 0.66)

USAGE = """3d usdz <file.scad|file.stl> [options]
  Export a model to a COLORED USDZ for Apple AR Quick Look.

Why:
  USDZ is the format an iPhone/Mac rotates natively — AirDrop or Message the file, then
  tap and drag to spin the result in 3D (and place it in AR) without any app. The export
  fixes what a naive one gets wrong: Z-up -> Y-up so it stands upright, units in mm, and
  a UsdPreviewSurface material so it renders shaded instead of untextured.

Options:
  -o, --out PATH        output .usdz (default: <file>.usdz)
  --color r,g,b         diffuse colour, each 0..1 (default: stone/travertine)

Examples:
  3d usdz part.scad -o part.usdz
  3d usdz part.stl --color 0.30,0.55,0.85"""


def _parse_color(raw: str) -> tuple[float, float, float]:
    parts = raw.split(",")
    if len(parts) != 3:
        raise InvalidArgument(
            "--color", raw, ["r,g,b with three comma-separated floats"],
            command="usdz",
            extra="Each component is 0..1, e.g. --color 0.30,0.55,0.85",
        )
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError:
        raise InvalidArgument(
            "--color", raw, ["three floats in 0..1"],
            command="usdz",
            extra="Each component is 0..1, e.g. --color 0.30,0.55,0.85",
        ) from None
    for v in vals:
        if not (0.0 <= v <= 1.0):
            raise InvalidArgument(
                "--color", raw, ["each component in 0..1"],
                command="usdz",
                extra="e.g. --color 0.30,0.55,0.85",
            )
    return (vals[0], vals[1], vals[2])


def _bin3d() -> str:
    """Locate this repo's `bin/3d` (for the .scad -> STL export step)."""
    repo = os.environ.get("REPO_ROOT")
    if not repo:
        # lib/commands/usdz.py -> lib/commands -> lib -> repo root
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo, "bin", "3d")


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    inp = argv[0]
    rest = argv[1:]
    out = ""
    color = DEFAULT_COLOR
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="usdz")
            out = rest[i + 1]
            i += 2
        elif a == "--color":
            if i + 1 >= n:
                raise UsageError("option --color needs a value", command="usdz")
            color = _parse_color(rest[i + 1])
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="usdz")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="usdz")

    ext = inp.rsplit(".", 1)[-1].lower() if "." in inp else ""
    if ext not in ("scad", "stl"):
        raise InvalidArgument(
            "input extension", "." + ext if ext else "(none)", [".scad", ".stl"],
            command="usdz",
        )

    if not out:
        out = (inp[: -(len(ext) + 1)] if ext else inp) + ".usdz"

    stem = os.path.splitext(os.path.basename(inp))[0]
    color_args = [str(color[0]), str(color[1]), str(color[2])]

    if ext == "scad":
        # Export to a temp STL first (via `3d export`, so geometry validation runs).
        with tempfile.TemporaryDirectory() as tmp:
            stl = os.path.join(tmp, stem + ".stl")
            r = subprocess.run(
                [_bin3d(), "export", inp, "-o", stl],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not os.path.isfile(stl):
                sys.stderr.write(r.stdout or "")
                sys.stderr.write(r.stderr or "")
                raise UsageError(
                    f"`3d export` failed for {inp} (see output above)", command="usdz"
                )
            # run_tool dispatches via .venv/uv so trimesh+pxr are always available.
            return run_tool("trimesh,usd-core", "usdz.py", [stl, out] + color_args + [stem])
    else:
        return run_tool("trimesh,usd-core", "usdz.py", [inp, out] + color_args + [stem])


COMMAND = Command(
    name="usdz",
    group="GEOMETRY & EXPORT",
    summary="export .scad/.stl to a colored USDZ for AR Quick Look (Y-up, mm)",
    usage=USAGE,
    run=run,
)
