"""3d pack — deterministic bed layout planning from explicit part dimensions."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cli.registry import Command
from errors import UsageError

if TYPE_CHECKING:
    from packing import BedLayout, PartSpec

USAGE = """3d pack --bed WxD --part name=WxD[:qty] [options]
  Validate explicit 2D part footprints and emit a deterministic bed layout plan.
  This is a bounded shelf-layout skeleton, not an optimal nesting solver.

Options:
  --bed WxD              bed size in millimeters, for example 220x220
  --part name=WxD[:qty]  part footprint in millimeters; repeat for multiple part types
  --gap MM               clearance between bounding boxes (default: 0)
  --no-rotate            disable 90-degree rotation
  --json                 emit machine-readable JSON instead of a table

Examples:
  3d pack --bed 220x220 --part bracket=60x40 --part clip=30x20:4
  3d pack --bed 180x180 --gap 5 --part hinge=75x30:2 --json
  3d pack --bed 220x220 --no-rotate --part bracket=60x40"""


def _parse_pair(text: str, *, label: str) -> tuple[float, float]:
    bits = text.lower().split("x", 1)
    if len(bits) != 2:
        raise UsageError(
            f"{label} must be WxD, got {text!r}",
            command="pack",
            remediation=["Use dimensions in millimeters, for example --bed 220x220."],
        )
    try:
        width = float(bits[0])
        depth = float(bits[1])
    except ValueError:
        raise UsageError(
            f"{label} must contain numeric dimensions, got {text!r}",
            command="pack",
            remediation=["Use dimensions in millimeters, for example 60x40."],
        ) from None

    # Reuse the library validation for positive dimensions.
    if width <= 0 or depth <= 0:
        raise UsageError(
            f"{label} dimensions must be positive, got {text!r}",
            command="pack",
            remediation=["Dimensions are millimeters and must be greater than zero."],
        )
    return width, depth


def _parse_part(text: str) -> PartSpec:
    from packing import PartSpec

    if "=" not in text:
        raise UsageError(
            f"--part must be name=WxD[:qty], got {text!r}",
            command="pack",
            remediation=["Example: --part bracket=60x40:2"],
        )
    name, rest = text.split("=", 1)
    qty = 1
    dims = rest
    if ":" in rest:
        dims, qty_text = rest.rsplit(":", 1)
        try:
            qty = int(qty_text)
        except ValueError:
            raise UsageError(
                f"--part quantity must be an integer, got {qty_text!r}",
                command="pack",
                remediation=["Example: --part bracket=60x40:2"],
            ) from None
    width, depth = _parse_pair(dims, label="--part")
    return PartSpec(name, width, depth, quantity=qty)


def _fmt_mm(value: float) -> str:
    return f"{value:g}"


def _print_table(plan: BedLayout, *, allow_rotate: bool) -> None:
    print(
        f"PACK PLAN bed {_fmt_mm(plan.bed_width)} x {_fmt_mm(plan.bed_depth)} mm"
        f"   gap {_fmt_mm(plan.gap)} mm"
        f"   rotate {'yes' if allow_rotate else 'no'}"
    )
    print(f"{'PART':<16} {'SIZE':>13} {'ORIGIN':>15}  ROTATED")
    for placement in plan.placements:
        label = f"{placement.name}#{placement.index}"
        size = f"{_fmt_mm(placement.width)}x{_fmt_mm(placement.depth)}"
        origin = f"{_fmt_mm(placement.x)},{_fmt_mm(placement.y)}"
        print(f"{label:<16} {size:>13} {origin:>15}  {'yes' if placement.rotated else 'no'}")
    print(f"STATUS: PASS - {len(plan.placements)} part copies placed")


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    bed: tuple[float, float] | None = None
    parts: list[PartSpec] = []
    gap = 0.0
    allow_rotate = True
    json_out = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--bed":
            if i + 1 >= len(argv):
                raise UsageError(
                    "option --bed needs a value",
                    command="pack",
                    remediation=["Use --bed WxD, for example --bed 220x220."],
                )
            bed = _parse_pair(argv[i + 1], label="--bed")
            i += 2
        elif arg == "--part":
            if i + 1 >= len(argv):
                raise UsageError(
                    "option --part needs a value",
                    command="pack",
                    remediation=["Use --part name=WxD[:qty], for example --part bracket=60x40:2."],
                )
            parts.append(_parse_part(argv[i + 1]))
            i += 2
        elif arg == "--gap":
            if i + 1 >= len(argv):
                raise UsageError(
                    "option --gap needs a value",
                    command="pack",
                    remediation=["Use --gap MM, for example --gap 5."],
                )
            try:
                gap = float(argv[i + 1])
            except ValueError:
                raise UsageError(
                    f"--gap must be numeric, got {argv[i + 1]!r}",
                    command="pack",
                    remediation=["Use millimeters, for example --gap 5."],
                ) from None
            i += 2
        elif arg == "--no-rotate":
            allow_rotate = False
            i += 1
        elif arg == "--json":
            json_out = True
            i += 1
        else:
            raise UsageError(
                f"unknown option '{arg}'",
                command="pack",
                remediation=["See `3d pack --help` for accepted options."],
            )

    if bed is None:
        raise UsageError(
            "missing required --bed WxD",
            command="pack",
            remediation=["Example: 3d pack --bed 220x220 --part bracket=60x40"],
        )

    from packing import plan_bed_layout

    plan = plan_bed_layout(
        parts,
        bed_width=bed[0],
        bed_depth=bed[1],
        gap=gap,
        allow_rotate=allow_rotate,
    )
    if json_out:
        print(json.dumps(plan.to_dict(), allow_nan=False, indent=2, sort_keys=True))
    else:
        _print_table(plan, allow_rotate=allow_rotate)
    return 0


COMMAND = Command(
    name="pack",
    group="GEOMETRY & EXPORT",
    summary="plan deterministic bed layouts from explicit part dimensions",
    usage=USAGE,
    run=run,
)
