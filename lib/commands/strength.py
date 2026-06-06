"""3d strength — structural-check skeleton with validated dry-run reports."""
from __future__ import annotations

import json
import math
import pathlib
from typing import TYPE_CHECKING

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

if TYPE_CHECKING:
    from materials import Material
    from strength import StrengthReport, StrengthRequest

USAGE = """3d strength <file.scad|.stl|.3mf> --material NAME --load NEWTONS [options]
  Structural-check skeleton: validate the load case and print the report that the
  future solver will consume. This command is intentionally dry-run/report-only for now.

Required:
  --material NAME          material registry name (see `3d materials list`)
  --load NEWTONS           positive force in newtons

Options:
  --axis X|Y|Z             load axis (default: Z)
  --fixture TYPE           cantilever | simple | compression (default: cantilever)
  --safety-factor N        target safety factor metadata (default: 2)
  --dry-run                explicit dry-run mode (currently the only mode)
  --json                   emit the structured report as JSON

Exit 0 = report produced, 2 = invalid invocation/input.

Examples:
  3d strength bracket.scad --material PLA --load 25 --axis Z
  3d structural-check part.stl --material PETG --load 12.5 --fixture simple --json"""


def _parse_positive(raw: str, *, flag: str) -> float:
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


def _validate_part(path_raw: str) -> pathlib.Path:
    from strength import SUPPORTED_EXTENSIONS

    path = pathlib.Path(path_raw)
    if not path.is_file():
        raise InputNotFound(path_raw, command="strength")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise InvalidArgument(
            "file",
            suffix or "(none)",
            list(SUPPORTED_EXTENSIONS),
            command="strength",
            extra="Use a supported CAD/mesh file for the structural-check skeleton.",
        )
    return path


def _resolve_material(name: str, *, start: pathlib.Path) -> Material:
    from materials import load_materials

    materials = load_materials(start=start)
    material = materials.get(name)
    if material is None:
        raise InvalidArgument(
            "--material",
            name,
            sorted(materials),
            command="strength",
            extra="Run `3d materials list` to see available material names.",
        )
    return material


def _parse(argv: list[str]) -> tuple[StrengthRequest, bool] | None:
    from strength import AXES, FIXTURES, StrengthRequest

    if not argv:
        print(USAGE)
        return None

    file_arg = ""
    material = ""
    load_n: float | None = None
    axis = "Z"
    fixture = "cantilever"
    safety_factor = 2.0
    as_json = False
    dry_run = True

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--material":
            if i + 1 >= len(argv):
                raise UsageError("--material needs a value", command="strength")
            material = argv[i + 1]
            i += 2
        elif arg == "--load":
            if i + 1 >= len(argv):
                raise UsageError("--load needs a value", command="strength")
            load_n = _parse_positive(argv[i + 1], flag="--load")
            i += 2
        elif arg == "--axis":
            if i + 1 >= len(argv):
                raise UsageError("--axis needs a value", command="strength")
            axis = argv[i + 1].upper()
            if axis not in AXES:
                raise InvalidArgument("--axis", argv[i + 1], AXES, command="strength")
            i += 2
        elif arg == "--fixture":
            if i + 1 >= len(argv):
                raise UsageError("--fixture needs a value", command="strength")
            fixture = argv[i + 1]
            if fixture not in FIXTURES:
                raise InvalidArgument("--fixture", fixture, FIXTURES, command="strength")
            i += 2
        elif arg == "--safety-factor":
            if i + 1 >= len(argv):
                raise UsageError("--safety-factor needs a value", command="strength")
            safety_factor = _parse_positive(argv[i + 1], flag="--safety-factor")
            i += 2
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
                remediation=["Pass one part file; repeat the command for independent load cases."],
            )

    if not file_arg:
        raise UsageError("no input file given", command="strength")
    if not material:
        raise UsageError("--material is required", command="strength")
    if load_n is None:
        raise UsageError("--load is required", command="strength")

    part = _validate_part(file_arg)
    request = StrengthRequest(
        path=part,
        material=material,
        load_n=load_n,
        axis=axis,
        fixture=fixture,
        safety_factor=safety_factor,
        dry_run=dry_run,
    )
    return request, as_json


def _fmt_number(value: float) -> str:
    return f"{value:g}"


def _print_text_report(report: StrengthReport) -> None:
    print("=== strength (structural check) ===")
    print(f"  status: {report.status}")
    print(f"  file: {report.request.path}")
    print(f"  material: {report.material_name}")
    print(f"  load: {_fmt_number(report.request.load_n)} N")
    print(f"  load axis: {report.request.axis}")
    print(f"  fixture: {report.request.fixture}")
    print(f"  safety factor target: {_fmt_number(report.request.safety_factor)}")
    print(f"  controlling strength: {_fmt_number(report.controlling_strength_mpa)} MPa")
    print()
    print("planned checks:")
    for check in report.checks:
        print(f"  - [{check.status}] {check.name}")
    print()
    print(">>> STRENGTH: DRY-RUN  (structural solver not implemented)")


def run(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv):
        print(USAGE)
        return 0
    parsed = _parse(argv)
    if parsed is None:
        return 1
    request, as_json = parsed

    from strength import build_report

    material = _resolve_material(request.material, start=request.path.parent)
    report = build_report(
        request,
        material_name=material.name,
        yield_mpa=material.yield_mpa,
        layer_adhesion=material.layer_adhesion,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        _print_text_report(report)
    return 0


COMMAND = Command(
    name="strength",
    group="QA & GATES",
    summary="structural-check skeleton: validate load cases and emit dry-run reports",
    usage=USAGE,
    run=run,
    aliases=("structural-check",),
)
