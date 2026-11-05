"""Gate: no module of lib/ may run a slicer that writes a G-code without handing that G-code to a thumbnail layer.

The Orca CLI writes no thumbnails (docs/commands/gcode-thumbnails.md). A new slicing entry point that forgets the
layer would ship G-code that `3d print-preflight` refuses; this test fails the commit that adds it. A module
that runs a slicer for something other than a kept G-code must say so in EXEMPT, with the reason."""

from __future__ import annotations

import re
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"

# a slicer invocation that writes files: the Orca-family `--slice` / `--outputdir` run, or PrusaSlicer's `-g --output`
RUNS_SLICER = re.compile(r'"--slice"|"--outputdir"|"-g",\s*"--output"')
# the layer: slicing.thumbnails finalize_* (or gcode_thumbnails.ensure) applied to what the slicer wrote
HAS_LAYER = re.compile(r"\b(?:finalize_(?:gcode|slice|workdir)|ensure)\(")

EXEMPT = {
    "commands/slice_check.py": "a verification gate: the G-code it slices is counted and deleted, never kept",
    "slicing/mix_tester.py": "slices a 10 mm probe only to read the project settings back; no G-code is kept or printed",
    "slicing/calibration_plates/u1_underlay.py": (
        "KNOWN GAP, issue #724: the module is over the 2400-line lint gate and cannot be committed until it is split; "
        "its plate G-code still gets thumbnails from `3d printer upload|start` and `print-preflight --fix`"
    ),
}


def _slicer_modules() -> list[str]:
    return [
        path.relative_to(LIB).as_posix()
        for path in sorted(LIB.rglob("*.py"))
        if RUNS_SLICER.search(path.read_text(encoding="utf-8"))
    ]


def test_every_module_that_slices_to_gcode_applies_a_thumbnail_layer() -> None:
    uncovered = [
        rel
        for rel in _slicer_modules()
        if rel not in EXEMPT
        and not HAS_LAYER.search((LIB / rel).read_text(encoding="utf-8"))
    ]
    assert not uncovered, (
        f"{uncovered} run a slicer but never call slicing.thumbnails.finalize_gcode/finalize_slice/finalize_workdir "
        "(or gcode_thumbnails.ensure) on its output: the Orca CLI writes no thumbnails, so the G-code would fail "
        "`3d print-preflight`. Call the layer before the G-code is hashed or moved, or add the module to EXEMPT "
        "with the reason."
    )


def test_exemptions_are_still_slicer_runners() -> None:
    """A stale exemption hides a module that was rewritten into a real slicing path."""
    runners = set(_slicer_modules())
    assert set(EXEMPT) <= runners, (
        f"EXEMPT lists modules that no longer run a slicer: {set(EXEMPT) - runners}"
    )


def test_the_guard_sees_the_known_entry_points() -> None:
    """If the patterns stop matching, the gate above would pass for the wrong reason."""
    assert {
        "commands/slice.py",
        "commands/retarget.py",
        "slicing/orca_cli.py",
        "slicing/calibration_gen.py",
        "slicing/calibration_plates/u1_underlay.py",
    } <= set(_slicer_modules())


def test_gcode_resume_gives_its_derived_gcode_the_thumbnails_too() -> None:
    """`3d gcode-resume` writes a new G-code from an old one's head: when that had no thumbnails the resume must get them."""
    source = (LIB / "commands" / "gcode_resume.py").read_text(encoding="utf-8")
    assert "finalize_gcode(dest)" in source
