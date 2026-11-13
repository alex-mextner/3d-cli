#!/usr/bin/env python3
"""assembly_pair_cli.py — the worker behind `3d assembly pair A B` (run through `cli.pyrun` with trimesh / manifold3d /
numpy / scipy / shapely / PyYAML).

WHAT: argument parsing and printing for the pair check; the geometry lives in `lib/assembly/pair.py` (+ `helical.py`,
  `printed.py`, `lib/gcode_walls.py`). `commands/assembly.py` routes `pair` here and everything else to `assembly_cli.py`.

CONVENTIONS: as `assembly_cli.py` - `--json` prints exactly one JSON document (schema `3d.assembly/1`, `kind: pair`); a failed
  verdict exits 1 (`GateFailure`), bad input 2, a missing dependency 127. Errors are rendered here because this runs in a
  child process of the dispatcher. A flag value that starts with one `-` (a negative window bound, `--axis -z`) is joined to
  its flag with `=` first, which argparse needs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from assembly_cli import CMD, _apply_mem_limit, _exit_fail, _fail, _peak_mb
from errors import (
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)

Box = tuple[float, float, float, float, float, float]


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="3d assembly pair", add_help=False)
    ap.add_argument("sub", choices=("pair",))
    ap.add_argument("target")
    ap.add_argument("other", nargs="?")
    for part in ("a", "b"):
        ap.add_argument(f"--scale-{part}", type=float, default=1.0)
        ap.add_argument(f"--pose-{part}")
        ap.add_argument(f"--print-{part}")
        ap.add_argument(f"--gcode-{part}")
        ap.add_argument(f"--object-{part}")
        ap.add_argument(f"--{part}-side", choices=("inner", "outer"))
        ap.add_argument(f"--{part}-z")
        ap.add_argument(f"--{part}-r")
    ap.add_argument("--axis", default="z")
    ap.add_argument("--axis-point")
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--hand", choices=("auto", "right", "left"), default="auto")
    ap.add_argument("--feature-name", default="feature")
    ap.add_argument("--features")
    ap.add_argument("--min-radial-gap", type=float)
    ap.add_argument("--min-clearance", type=float)
    ap.add_argument("--max-overlap-mm3", type=float, default=0.01)
    ap.add_argument("--seat-tol", type=float, default=0.02)
    ap.add_argument("--zone")
    ap.add_argument("--screw-to-best", action="store_true")
    ap.add_argument("--skip-below", type=float, default=0.5)
    ap.add_argument("--max-faces", type=int, default=400_000)
    ap.add_argument("--fit")
    ap.add_argument("--shape", default="round")
    ap.add_argument("--printer")
    ap.add_argument("--material")
    ap.add_argument("--nozzle", type=float)
    ap.add_argument("--layer", type=float)
    ap.add_argument("--mem-limit-mb", type=int)
    ap.add_argument("--json", action="store_true")
    return ap


def _join_dash_values(argv: list[str]) -> list[str]:
    """`--a-z -36:-28` / `--axis -z` -> `--a-z=-36:-28`: argparse takes a value that starts with one `-` for a flag."""
    out: list[str] = []
    for i, tok in enumerate(argv):
        prev = argv[i - 1] if i else ""
        joins = (
            prev.startswith("--")
            and "=" not in prev
            and tok.startswith("-")
            and not tok.startswith("--")
            and len(tok) > 1
        )
        if joins:
            out[-1] = f"{prev}={tok}"
        else:
            out.append(tok)
    return out


def _zone_box(text: str | None) -> Box | None:
    if not text:
        return None
    try:
        v = [float(x) for x in text.split(",")]
    except ValueError:
        v = []
    if len(v) != 6 or v[0] > v[3] or v[1] > v[4] or v[2] > v[5]:
        raise InvalidArgument(
            "--zone", text, ["XMIN,YMIN,ZMIN,XMAX,YMAX,ZMAX (min <= max)"], command=CMD
        )
    return (v[0], v[1], v[2], v[3], v[4], v[5])


def _features(args: argparse.Namespace) -> list[Any]:
    """The feature(s) the flags or `--features FILE` describe (none: interpenetration and clearance only)."""
    from assembly import pair

    flagged = args.a_side or args.b_side
    if args.features:
        if flagged or args.pitch:
            raise UsageError(
                "--features cannot be combined with --a-side/--b-side/--pitch",
                command=CMD,
                remediation=["Put the feature in the file, or use the flags alone."],
            )
        return pair.features_from_file(Path(args.features))
    if not flagged and not args.pitch:
        return []
    if not (args.a_side and args.b_side):
        raise UsageError(
            "a feature needs both --a-side and --b-side (inner = bore wall, outer = boss)",
            command=CMD,
            remediation=[
                "Example: --a-side inner --b-side outer --pitch 3.007 --a-z 1:9.5 --b-z 11:16.8"
            ],
        )

    def side(label: str, kind: str) -> Any:
        z, r = getattr(args, f"{label}_z"), getattr(args, f"{label}_r")
        return pair.FeatureSide(
            kind,
            pair.parse_range(z, f"--{label}-z") if z else None,
            pair.parse_range(r, f"--{label}-r") if r else None,
        )

    return [
        pair.Feature(
            args.feature_name,
            side("a", args.a_side),
            side("b", args.b_side),
            args.pitch,
            args.hand,
        )
    ]


def _part(args: argparse.Namespace, label: str, path: str) -> Any:
    from assembly import pair

    gcode = getattr(args, f"gcode_{label}")
    mesh_path, object_id = pair.split_part_path(path)
    return pair.PartInput(
        mesh_path,
        getattr(args, f"scale_{label}"),
        pair.pose_from_text(getattr(args, f"pose_{label}"), f"--pose-{label}"),
        pair.pose_from_text(getattr(args, f"print_{label}"), f"--print-{label}"),
        Path(gcode) if gcode else None,
        getattr(args, f"object_{label}"),
        object_id,
    )


def _options(args: argparse.Namespace) -> Any:
    from assembly import pair
    from assembly.vec import parse_direction, parse_point

    fit = (
        pair.fit_requirement(
            args.fit, args.shape, args.printer, args.material, args.nozzle, args.layer
        )
        if args.fit
        else None
    )
    return pair.PairOptions(
        _part(args, "a", args.target),
        _part(args, "b", args.other),
        _features(args),
        parse_point(args.axis_point, "--axis-point") if args.axis_point else None,
        parse_direction(args.axis, "--axis"),
        args.min_radial_gap,
        args.min_clearance,
        args.max_overlap_mm3,
        args.seat_tol,
        _zone_box(args.zone),
        args.screw_to_best,
        args.skip_below,
        args.max_faces,
        fit,
    )


def cmd_pair(args: argparse.Namespace) -> int:
    from assembly import pair, pair_report

    if not args.other:
        raise UsageError(
            "`3d assembly pair` needs two parts: A and B",
            command=CMD,
            remediation=[
                "Example: 3d assembly pair rim.stl plug.stl --pose-b '{translate: [0, 0, -10]}'"
            ],
        )
    doc = pair.run_pair(_options(args))
    doc["resources"] = {"peak_rss_mb": _peak_mb()}
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(pair_report.pair_lines(doc)))
    if doc["ok"]:
        return 0
    return _exit_fail("assembly pair failed: " + ", ".join(doc["summary"]["fail"]))


def main(argv: list[str]) -> int:
    try:
        try:
            args = _parser().parse_args(_join_dash_values(argv))
        except SystemExit as exc:
            raise UsageError(
                "bad arguments", command=CMD, remediation=["Run `3d assembly --help`."]
            ) from exc
        _apply_mem_limit(args.mem_limit_mb)
        try:
            return cmd_pair(args)
        except MemoryError as exc:
            raise _fail(
                "out of memory while checking the pair",
                ["Raise --mem-limit-mb or lower --max-faces."],
            ) from exc
    except ImportError as exc:
        err: ThreeDError = MissingDependency(
            str(exc.name or exc),
            install="uv sync --extra dev",
            degrades="`3d assembly pair` cannot run",
            command=CMD,
        )
        sys.stderr.write(err.render() + "\n")
        return err.exit_code
    except ThreeDError as exc:
        text = exc.render()
        if text:
            sys.stderr.write(text + "\n")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
