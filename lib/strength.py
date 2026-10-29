"""`3d strength` request, argument parsing and report formatting (stdlib only).

The command module (lib/commands/strength.py) validates argv here and hands the same argv to
the solver tool (lib/strength_run.py) through `cli.pyrun`, so `3d help` never imports numpy.
The heavy work lives in lib/structural/.

Result schema `3d.strength/2` (see docs/commands/strength.md):

  schema, verdict (PASS | WEAK | FAIL), method (beam | fea), label (ANALYTICAL_SCREEN | FEA_LINEAR),
  safety_factor, safety_factor_required, max_von_mises_mpa, max_displacement_mm, governing,
  input{}, materials[], cases[], contacts[], mesh{}, assumptions[], unverified[], warnings[],
  artifacts{}, timing_s{}

WEAK = the part holds (SF >= 1) but misses the required safety factor; FAIL = SF < 1. Both exit 1.
"""

from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass
from typing import Any

from errors import InputNotFound, InvalidArgument, UsageError

SCHEMA = "3d.strength/2"
SUPPORTED_EXTENSIONS = (".scad", ".stl", ".3mf")
FIXTURES = ("cantilever", "simple", "compression")
METHODS = ("auto", "beam", "fea")
CONTACTS = ("tied", "frictionless", "none")
AXES = ("X", "Y", "Z")
LABELS = {"beam": "ANALYTICAL_SCREEN", "fea": "FEA_LINEAR"}
UNVERIFIED = (
    "creep and stress relaxation under sustained load",
    "fatigue / repeated loading (use --repeated-use for the snap-fit strain derating only)",
    "nonlinear material behaviour above yield, buckling and large deformation",
    "print quality: under-extrusion, voids, seam and layer-line notches, brand-to-brand scatter",
)
HOOK_KEYS = ("t", "L", "w", "y", "taper", "lead", "retention", "mu")
ARM_AXES = ("X", "Y", "Z")


@dataclass(slots=True, frozen=True)
class StrengthRequest:
    """Validated `3d strength` inputs."""

    path: pathlib.Path | None
    material: str | None
    load_n: float | None
    loads_path: pathlib.Path | None
    axis: str
    fixture: str
    span_axis: str | None
    method: str
    orientation: str | None
    infill: str | None
    walls: int
    nozzle_mm: float
    safety_factor: float
    repeated_use: bool
    order: int
    mesh_size_mm: float | None
    max_elements: int | None
    contact: str
    crop: tuple[float, ...] | None
    defines: tuple[str, ...]
    hook: dict[str, float] | None
    arm_axis: str
    viz: pathlib.Path | None
    as_json: bool
    dry_run: bool


def parse_positive(raw: str, *, flag: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise InvalidArgument(
            flag,
            raw,
            ["positive number"],
            command="strength",
            extra=f"Pass {flag} as a number, for example `{flag} 25`.",
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise InvalidArgument(
            flag,
            raw,
            ["finite positive number"],
            command="strength",
            extra=f"Pass {flag} as a value greater than zero.",
        )
    return value


def _parse_int(raw: str, *, flag: str, low: int, high: int | None = None) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise InvalidArgument(
            flag, raw, [f"an integer >= {low}"], command="strength"
        ) from None
    if value < low or (high is not None and value > high):
        accepted = (
            f"an integer in {low}..{high}"
            if high is not None
            else f"an integer >= {low}"
        )
        raise InvalidArgument(flag, raw, [accepted], command="strength")
    return value


def _validate_input(path_raw: str) -> pathlib.Path:
    path = pathlib.Path(path_raw)
    if path.is_dir():
        if not (path / "3d.yaml").is_file():
            raise InputNotFound(str(path / "3d.yaml"), command="strength")
        return path
    if not path.is_file():
        raise InputNotFound(path_raw, command="strength")
    suffix = path.suffix.lower()
    if path.name.lower() in ("3d.yaml", "3d.yml") or suffix in (".yaml", ".yml"):
        return path
    if suffix not in SUPPORTED_EXTENSIONS:
        raise InvalidArgument(
            "file",
            suffix or "(none)",
            [*SUPPORTED_EXTENSIONS, "3d.yaml", "a project directory"],
            command="strength",
            extra="Pass a CAD/mesh file, a 3d.yaml, or a project directory.",
        )
    return path


def parse_hook(raw: str) -> dict[str, float]:
    """`t=1,L=7,w=6,y=0.2[,taper=0.5,lead=30,retention=45,mu=0.4]` -> floats."""
    out: dict[str, float] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or key not in HOOK_KEYS:
            raise InvalidArgument(
                "--hook",
                raw,
                [
                    f"comma-separated {k}=NUMBER with keys {', '.join(HOOK_KEYS)}"
                    for k in ("",)
                ][:1],
                command="strength",
            )
        out[key] = parse_positive(value.strip(), flag=f"--hook {key}")
    missing = [k for k in ("t", "L", "w", "y") if k not in out]
    if missing:
        raise UsageError(
            f"--hook needs {', '.join(missing)}",
            command="strength",
            remediation=[
                "Example: --hook t=1.0,L=7,w=6,y=0.2  (arm thickness, free length, width, tip deflection; all mm)"
            ],
        )
    if not 0.0 < out.get("taper", 1.0) <= 1.0:
        raise InvalidArgument(
            "--hook taper",
            str(out["taper"]),
            ["a tip/root thickness ratio in (0, 1]"],
            command="strength",
        )
    return out


def parse_args(argv: list[str]) -> StrengthRequest:
    """Validate argv. Raises UsageError / InvalidArgument / InputNotFound (exit 2)."""
    file_arg = ""
    values: dict[str, Any] = {
        "material": None,
        "load_n": None,
        "loads_path": None,
        "axis": "Z",
        "fixture": "cantilever",
        "span_axis": None,
        "method": "auto",
        "orientation": None,
        "infill": None,
        "walls": 3,
        "nozzle_mm": 0.4,
        "safety_factor": 2.0,
        "repeated_use": False,
        "order": 2,
        "mesh_size_mm": None,
        "max_elements": None,
        "contact": "tied",
        "crop": None,
        "hook": None,
        "arm_axis": "Z",
        "viz": None,
    }
    defines: list[str] = []
    as_json = False
    dry_run = False
    valued = {
        "--material",
        "--load",
        "--loads",
        "--axis",
        "--fixture",
        "--span-axis",
        "--method",
        "--orientation",
        "--infill",
        "--walls",
        "--nozzle",
        "--safety-factor",
        "--order",
        "--mesh-size",
        "--max-elements",
        "--contact",
        "--crop",
        "--hook",
        "--arm-axis",
        "-D",
        "--define",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in valued:
            if i + 1 >= len(argv):
                raise UsageError(f"{arg} needs a value", command="strength")
            val = argv[i + 1]
            i += 2
            if arg == "--material":
                values["material"] = val
            elif arg == "--load":
                values["load_n"] = parse_positive(val, flag="--load")
            elif arg == "--loads":
                values["loads_path"] = pathlib.Path(val)
            elif arg == "--axis":
                text = val.strip().upper().lstrip("+-")
                if text not in AXES:
                    raise InvalidArgument(
                        "--axis", val, [*AXES, "-X", "-Y", "-Z"], command="strength"
                    )
                values["axis"] = val.strip().upper()
            elif arg == "--fixture":
                if val not in FIXTURES:
                    raise InvalidArgument(
                        "--fixture", val, FIXTURES, command="strength"
                    )
                values["fixture"] = val
            elif arg == "--span-axis":
                if val.upper() not in AXES:
                    raise InvalidArgument("--span-axis", val, AXES, command="strength")
                values["span_axis"] = val.upper()
            elif arg == "--method":
                if val not in METHODS:
                    raise InvalidArgument("--method", val, METHODS, command="strength")
                values["method"] = val
            elif arg == "--orientation":
                values["orientation"] = val
            elif arg == "--infill":
                values["infill"] = val
            elif arg == "--walls":
                values["walls"] = _parse_int(val, flag="--walls", low=1, high=20)
            elif arg == "--nozzle":
                values["nozzle_mm"] = parse_positive(val, flag="--nozzle")
            elif arg == "--safety-factor":
                values["safety_factor"] = parse_positive(val, flag="--safety-factor")
            elif arg == "--order":
                values["order"] = _parse_int(val, flag="--order", low=1, high=2)
            elif arg == "--mesh-size":
                values["mesh_size_mm"] = parse_positive(val, flag="--mesh-size")
            elif arg == "--max-elements":
                values["max_elements"] = _parse_int(
                    val, flag="--max-elements", low=500, high=400_000
                )
            elif arg == "--contact":
                if val not in CONTACTS:
                    raise InvalidArgument(
                        "--contact", val, CONTACTS, command="strength"
                    )
                values["contact"] = val
            elif arg == "--crop":
                parts = val.split(",")
                try:
                    box = tuple(float(p) for p in parts)
                except ValueError:
                    box = ()
                if len(box) != 6 or not all(math.isfinite(b) for b in box):
                    raise InvalidArgument(
                        "--crop",
                        val,
                        ["x0,y0,z0,x1,y1,z1 (six finite numbers, mm)"],
                        command="strength",
                    )
                values["crop"] = box
            elif arg == "--hook":
                values["hook"] = parse_hook(val)
            elif arg == "--arm-axis":
                if val.upper() not in ARM_AXES:
                    raise InvalidArgument(
                        "--arm-axis", val, ARM_AXES, command="strength"
                    )
                values["arm_axis"] = val.upper()
            else:  # -D / --define
                if "=" not in val:
                    raise InvalidArgument(
                        arg,
                        val,
                        ["NAME=VALUE, e.g. -D 'view=\"bottom\"'"],
                        command="strength",
                    )
                defines.append(val)
        elif arg == "--viz":
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt.lower().endswith(".png"):
                values["viz"] = pathlib.Path(nxt)
                i += 2
            else:
                values["viz"] = pathlib.Path("")  # default name chosen by the solver
                i += 1
        elif arg == "--repeated-use":
            values["repeated_use"] = True
            i += 1
        elif arg == "--json":
            as_json = True
            i += 1
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg.startswith("-"):
            raise UsageError(f"unknown option '{arg}'", command="strength")
        elif not file_arg:
            file_arg = arg
            i += 1
        else:
            raise UsageError(
                f"unexpected extra input '{arg}'",
                command="strength",
                remediation=[
                    "Pass one part, 3d.yaml or project directory; assemblies are described by 3d.yaml."
                ],
            )

    hook = values["hook"]
    if hook is None and not file_arg:
        raise UsageError(
            "no input file given",
            command="strength",
            remediation=[
                "Pass a part/3d.yaml, or use --hook t=...,L=...,w=...,y=... for a snap-hook check."
            ],
        )
    if hook is not None and file_arg:
        raise UsageError(
            "--hook is a stand-alone closed-form check and takes no input file",
            command="strength",
        )
    if (
        hook is None
        and values["loads_path"] is None
        and values["load_n"] is None
        and not _is_project_arg(file_arg)
    ):
        raise UsageError(
            "--load is required (or --loads 3d.yaml / a 3d.yaml input)",
            command="strength",
            remediation=["Example: 3d strength bracket.scad --material PLA --load 25"],
        )
    path = _validate_input(file_arg) if file_arg else None
    loads_path = values["loads_path"]
    if loads_path is not None:
        loads_path = _validate_input(str(loads_path))
    if hook is not None and values["method"] == "fea":
        raise UsageError(
            "--hook is closed-form; it cannot run with --method fea",
            command="strength",
        )
    return StrengthRequest(
        path=path,
        loads_path=loads_path,
        defines=tuple(defines),
        hook=hook,
        as_json=as_json,
        dry_run=dry_run,
        **{k: v for k, v in values.items() if k not in ("loads_path", "hook")},
    )


def _is_project_arg(arg: str) -> bool:
    if not arg:
        return False
    p = pathlib.Path(arg)
    return p.is_dir() or p.suffix.lower() in (".yaml", ".yml")


def verdict_for(safety_factor: float, required: float) -> str:
    """PASS (SF >= required), WEAK (1 <= SF < required), FAIL (SF < 1)."""
    if not math.isfinite(safety_factor):
        return "PASS"
    if safety_factor >= required:
        return "PASS"
    return "WEAK" if safety_factor >= 1.0 else "FAIL"


def exit_code_for(verdict: str) -> int:
    return 0 if verdict == "PASS" else 1


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "-"
    if not math.isfinite(value):
        return "inf"
    return f"{value:.{digits}g}"


def format_text(result: dict[str, Any]) -> str:
    """Human report of a `3d.strength/2` result."""
    out: list[str] = ["=== strength (structural check) ==="]
    out.append(f"  method: {result['method']}  [{result['label']}]")
    inp = result.get("input", {})
    if inp.get("file"):
        out.append(f"  input: {inp['file']}")
    for body in inp.get("bodies", []):
        orient = body.get("orientation", {})
        out.append(
            f"  body {body['name']}: material {body['material']}, build axis {orient.get('build_axis')} "
            f"({orient.get('source', '?')})"
        )
    for mat in result.get("materials", []):
        allow = mat["allowables"]
        out.append(
            f"  material {mat['name']}: E {_num(mat['e_in_plane_mpa'], 5)}/{_num(mat['e_across_layers_mpa'], 5)} MPa "
            f"(in-plane/across), allowable {_num(allow['in_plane_mpa'])} MPa in-plane, "
            f"{_num(allow['across_layers_mpa'])} MPa across layers"
        )
    mesh = result.get("mesh")
    if mesh:
        per_body = ", ".join(
            "{}: {} tets @ {} mm".format(b["name"], b["tets"], _num(b["edge_length_mm"])) for b in mesh["bodies"]
        )
        out.append(f"  mesh: P{mesh['order']}, {mesh['total_tets']} tets, {mesh['total_dofs']} dofs ({per_body})")
    out.extend(
        f"  contact {c['a']} <-> {c['b']}: {c['kind']} ({c['origin']}), {c.get('nodes', 0)} nodes"
        for c in result.get("contacts", [])
    )
    out.append("")
    out.append("cases:")
    for case in result["cases"]:
        out.append(
            f"  - {case['name']}: {case['verdict']}  SF {_num(case['safety_factor'])}"
            f" (need {_num(result['safety_factor_required'])}), governing: {case.get('governing', '-')}"
        )
        if "max_von_mises_mpa" in case:
            out.append(
                f"      max von Mises {_num(case['max_von_mises_mpa'])} MPa, "
                f"max displacement {_num(case.get('max_displacement_mm'))} mm"
            )
        sfs = case.get("safety_factors", {})
        if sfs:
            out.append(
                "      SF per direction: "
                + ", ".join(f"{k} {_num(v)}" for k, v in sfs.items())
            )
        out.extend(f"      {line}" for line in case.get("detail", []))
        for i, spot in enumerate(case.get("hotspots", []), 1):
            near = ""
            if spot.get("near_constraint"):
                near = " (at a load/support patch: stress there is a singularity)"
            elif spot.get("near_contact"):
                near = " (on a contact face: node-to-surface stress is mesh-dependent)"
            out.append(
                f"      hotspot {i}: {spot['body']} at ({spot['xyz'][0]:.2f}, {spot['xyz'][1]:.2f}, {spot['xyz'][2]:.2f}) mm, "
                f"von Mises {_num(spot['von_mises_mpa'])} MPa, SF {_num(spot['safety_factor'])} [{spot['mode']}]{near}"
            )
    out.append("")
    out.append("assumptions:")
    out.extend(f"  - {a}" for a in result["assumptions"])
    out.append("not verified:")
    out.extend(f"  - {u}" for u in result["unverified"])
    if result.get("warnings"):
        out.append("warnings:")
        out.extend(f"  ! {w}" for w in result["warnings"])
    png = result.get("artifacts", {}).get("stress_png")
    if png:
        out.append(f"stress picture: {png}")
    out.append("")
    out.append(
        f">>> STRENGTH: {result['verdict']}  ({result['label']}, safety factor {_num(result['safety_factor'])}"
        f" vs required {_num(result['safety_factor_required'])})"
    )
    return "\n".join(out)
