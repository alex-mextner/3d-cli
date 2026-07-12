"""3d fit-niche — generate a printable parametric insert that mates into a described cavity.

Pure parametric OpenSCAD geometry (no AI backend). Given a cavity description — a
rectangular pocket or a round bore, plus optional retention/entry features — this command
resolves FDM mating clearances and writes a parametric `.scad` for the insert (plug). The
heavy lifting (clearance math + scad emission) lives in `lib/niche_fit.py`; this module
only parses flags, loads an optional `--spec` JSON, writes the file, and optionally chains
`3d render` / `3d check` on the result.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from cli.registry import Command
from errors import InputNotFound, UsageError

if TYPE_CHECKING:
    from niche_fit import NicheSpec

USAGE = """3d fit-niche [cavity options] [feature flags] [output/proof]
  Generate a parametric OpenSCAD insert/plug that seats into a described cavity with
  correct FDM clearances. Clearance convention: rect = per mating face (each wall);
  round = radial (uniform gap all around). Height is a full seat (no Z clearance).

Cavity shape + size (or use --spec):
  --shape rect|round    cavity shape. Default: rect.
                        Example: 3d fit-niche --shape round --diameter 20 --height 12
  --width MM            rect cavity opening in X. Required for --shape rect.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12
  --depth MM            rect cavity opening in Y. Required for --shape rect.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12
  --diameter MM         round cavity bore. Required for --shape round.
                        Example: 3d fit-niche --shape round --diameter 20 --height 12
  --height MM           cavity depth in Z (= insert height). Always required.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12

Fit / clearance:
  --fit snug|normal|loose   clearance preset (snug 0.10, normal 0.20, loose 0.35 mm).
                        Default: normal.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --fit snug
  --clearance MM        explicit clearance per mating face (radial for round); overrides
                        --fit. Example: 3d fit-niche --width 20 --depth 16 --height 12 --clearance 0.15

Features:
  --lead-in             add a 45-degree entry chamfer at the top rim so the insert
                        self-guides into the cavity.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --lead-in
  --groove              add a retention channel around the insert.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --groove
  --tab, --snap         add a retention ridge on the +X mating face.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --snap

Input / output / proof:
  --spec FILE           read the cavity spec from a JSON file (same field names as the
                        flags: shape/width/depth/diameter/height/clearance/fit/lead_in/
                        groove/snap_tab). Flags override matching JSON fields.
                        Example: 3d fit-niche --spec cavity.json -o insert.scad
  -o, --out PATH        output .scad path. Default: insert.scad
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 -o plug.scad
  --render              also render a preview PNG next to the .scad (needs OpenSCAD).
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --render
  --check               also run `3d check` on the emitted insert (needs OpenSCAD).
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --check
  --json                print the resolved spec (dims, clearance, insert size) as JSON.
                        Example: 3d fit-niche --width 20 --depth 16 --height 12 --json

Examples:
  3d fit-niche --width 20 --depth 16 --height 12 -o insert.scad
  3d fit-niche --shape round --diameter 20 --height 14 --lead-in --render
  3d fit-niche --width 24 --depth 18 --height 12 --fit loose --groove --snap
  3d fit-niche --spec cavity.json -o plug.scad --check
  # Seated-in-cavity section proof (shows the clearance gap):
  3d render insert.scad --section --plane YZ -D show_cavity=true -o proof.png"""

_FLOAT_FLAGS = {
    "--width": "width",
    "--depth": "depth",
    "--diameter": "diameter",
    "--height": "height",
    "--clearance": "clearance",
}


def _parse_args(argv: list[str]) -> dict[str, object]:
    """Parse argv into a plain options dict (floats as float, flags as bool)."""
    opts: dict[str, object] = {
        "shape": None, "fit": None, "spec": None, "out": "insert.scad",
        "lead_in": False, "groove": False, "snap_tab": False,
        "render": False, "check": False, "json": False,
    }
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a in _FLOAT_FLAGS:
            opts[_FLOAT_FLAGS[a]] = _need_float(argv, i, a)
            i += 2
        elif a == "--shape":
            opts["shape"] = _need_value(argv, i, a)
            i += 2
        elif a == "--fit":
            opts["fit"] = _need_value(argv, i, a)
            i += 2
        elif a == "--spec":
            opts["spec"] = _need_value(argv, i, a)
            i += 2
        elif a in ("-o", "--out"):
            opts["out"] = _need_value(argv, i, a)
            i += 2
        elif a == "--lead-in":
            opts["lead_in"] = True
            i += 1
        elif a == "--groove":
            opts["groove"] = True
            i += 1
        elif a in ("--tab", "--snap"):
            opts["snap_tab"] = True
            i += 1
        elif a in ("--render", "--check", "--json"):
            opts[a[2:]] = True
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="fit-niche")
    return opts


def _need_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv):
        raise UsageError(f"option {flag} needs a value", command="fit-niche")
    return argv[i + 1]


def _need_float(argv: list[str], i: int, flag: str) -> float:
    raw = _need_value(argv, i, flag)
    try:
        return float(raw)
    except ValueError:
        raise UsageError(
            f"option {flag} needs a numeric value, got {raw!r}", command="fit-niche"
        ) from None


def _build_spec(opts: dict[str, object]) -> NicheSpec:
    """Resolve the NicheSpec from a --spec file and/or flags (flags win over JSON)."""
    from niche_fit import make_spec, spec_from_json

    spec_path = opts["spec"]
    if spec_path is not None:
        assert isinstance(spec_path, str)
        if not os.path.isfile(spec_path):
            raise InputNotFound(spec_path, command="fit-niche")
        try:
            data = json.loads(open(spec_path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"could not read spec JSON: {exc}", command="fit-niche",
                remediation=['Example: {"shape": "rect", "width": 20, "depth": 16, "height": 12}'],
            ) from None
        if not isinstance(data, dict):
            raise UsageError("spec file must contain a JSON object", command="fit-niche")
        _overlay_flags(data, opts)
        return spec_from_json(data)

    _require_dims(opts)
    return make_spec(
        shape=str(opts["shape"] or "rect"),
        height=_opt_float(opts, "height"),
        clearance=_opt_float_or_none(opts, "clearance"),
        fit=(str(opts["fit"]) if opts["fit"] is not None else None),
        width=_opt_float(opts, "width"),
        depth=_opt_float(opts, "depth"),
        diameter=_opt_float(opts, "diameter"),
        lead_in=bool(opts["lead_in"]),
        groove=bool(opts["groove"]),
        snap_tab=bool(opts["snap_tab"]),
    )


def _require_dims(opts: dict[str, object]) -> None:
    """Report a missing REQUIRED cavity dimension as 'missing --x', not 'got 0.0'."""
    shape = str(opts.get("shape") or "rect")
    required = ["height"] + (["width", "depth"] if shape == "rect" else ["diameter"])
    for key in required:
        if opts.get(key) is None:
            raise UsageError(
                f"missing required --{key}",
                command="fit-niche",
                remediation=[f"Example: 3d fit-niche --width 20 --depth 16 --height 12 "
                             f"(--{key} is a millimetre dimension)"],
            )


def _overlay_flags(data: dict[str, object], opts: dict[str, object]) -> None:
    """Let explicitly-passed flags override matching fields from the spec file."""
    for key in ("shape", "fit", "width", "depth", "diameter", "height", "clearance"):
        val = opts.get(key)
        if val is not None:
            data[key] = val
    for flag in ("lead_in", "groove", "snap_tab"):
        if opts.get(flag):
            data[flag] = True


def _opt_float(opts: dict[str, object], key: str) -> float:
    val = opts.get(key)
    return float(val) if isinstance(val, (int, float)) else 0.0


def _opt_float_or_none(opts: dict[str, object], key: str) -> float | None:
    val = opts.get(key)
    return float(val) if isinstance(val, (int, float)) else None


def _render_preview(scad_path: str) -> int:
    from commands import render as render_cmd

    png = os.path.splitext(scad_path)[0] + ".png"
    rc = render_cmd.run([scad_path, "--view", "3-4", "-o", png])
    if rc == 0:
        print(f"preview: {png}")
    return rc


def _run_check(scad_path: str) -> int:
    from commands import check as check_cmd

    return check_cmd.run([scad_path])


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    from niche_fit import emit_scad

    opts = _parse_args(argv)
    spec = _build_spec(opts)
    out = str(opts["out"])

    scad = emit_scad(spec)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(scad)

    summary = spec.summary()
    if opts["json"]:
        print(json.dumps({"output": out, **summary}, indent=2, sort_keys=True))
    else:
        _print_summary(out, summary)

    rc = 0
    if opts["render"]:
        rc = _render_preview(out) or rc
    if opts["check"]:
        rc = _run_check(out) or rc
    return rc


def _print_summary(out: str, summary: dict[str, object]) -> None:
    cavity = summary["cavity"]
    insert = summary["insert"]
    assert isinstance(cavity, dict) and isinstance(insert, dict)
    print(f"fit-niche: wrote {out}")
    print(f"  shape: {summary['shape']}   fit: {summary['fit']}   "
          f"clearance: {summary['clearance']} mm ({summary['clearance_convention']})")
    print(f"  cavity: {_dims(cavity)}")
    print(f"  insert: {_dims(insert)}")
    feats = summary["features"]
    assert isinstance(feats, dict)
    on = [name for name, enabled in feats.items() if enabled]
    print(f"  features: {', '.join(on) if on else 'none'}")
    print("  seated-section proof: 3d render "
          f"{out} --section --plane YZ -D show_cavity=true -o proof.png")


def _dims(d: dict[str, object]) -> str:
    return "  ".join(f"{k}={v}mm" for k, v in d.items())


COMMAND = Command(
    name="fit-niche",
    group="GEOMETRY & EXPORT",
    summary="generate a parametric insert/plug that mates into a described cavity with FDM clearances",
    usage=USAGE,
    run=run,
)
