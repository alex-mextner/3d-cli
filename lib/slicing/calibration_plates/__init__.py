"""calibration_plates — the plate series of `3d calibrate gen plateN` (lib/slicing/calibration_plate.py).

Each module declares one plate: PLATE_ID, TESTS (test id -> description, for --only/--skip) and
`build(ctx: PlateContext) -> Plate`, which prints only the tests `ctx.wants()`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..calibration_plate import Plate, PlateContext
from . import filament


@dataclass(frozen=True)
class PlateDef:
    build: Callable[[PlateContext], Plate]
    tests: Mapping[str, str]


PLATES: dict[str, PlateDef] = {
    filament.PLATE_ID: PlateDef(filament.build, filament.TESTS),
}


def resolve_tests(plate_id: str, only: frozenset[str], skip: frozenset[str]) -> frozenset[str] | None:
    """--only/--skip test ids -> the set to print (None: all). Unknown ids are an error."""
    from ..calibration_gen import GenError

    known = PLATES[plate_id].tests
    unknown = sorted((only | skip) - set(known))
    if unknown:
        raise GenError(f"{plate_id} has no test {', '.join(unknown)} (tests: {', '.join(known)})")
    if not only and not skip:
        return None
    chosen = frozenset(only or known) - skip
    if not chosen:
        raise GenError(f"{plate_id}: --only/--skip leave no test to print")
    return chosen
