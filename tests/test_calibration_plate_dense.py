"""Tests for calibration_plates.dense (the dense plate) and the engine hooks it relies on, with the
fake Orca CLI of test_calibration_plate (Orca-shaped G-code: START_PRINT, EXCLUDE_OBJECT, ;Z:)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_calibration_plate import _ctx, _object_lines, orca  # noqa: F401  (fixture)

from slicing import calibration_plate as cp
from slicing.calibration_plates import PLATES, dense

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="needs OpenSCAD"
)


def test_motion_transform_sets_wall_speed_and_limits_only() -> None:
    tf = dense.motion(150, 4000, 5)
    assert tf("SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500") == (
        "SET_VELOCITY_LIMIT ACCEL=4000 ACCEL_TO_DECEL=2000 SQUARE_CORNER_VELOCITY=5"
    )
    assert tf("G1 X1 Y2 E.05 F12000 ; wall") == "G1 X1 Y2 E.05 F9000 ; wall"
    assert tf("G1 X1 Y2 E.05") == "G1 X1 Y2 E.05 F9000"
    for keep in ("G1 X5 Y5 F30000", "G1 E-.8 F2400", "G1 X1 Y1 E-.2", "G1 Z.6 F30000"):
        assert tf(keep) == keep


def test_hooks_resend_slicer_state_and_balance_extrusion() -> None:
    text = "\n".join(
        [
            "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500 SQUARE_CORNER_VELOCITY=7",
            "M106 S255",
            ";Z:1.2",
            "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0",
            "G1 E.8 F2400",
            "G1 X1 Y1 E.5",
            "G1 E-.8 F2400",
            "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0",
        ]
    )
    obj = cp.PlateObject(
        "a",
        "test",
        ((0.0, 0.0, 0.0, 5.0, 5.0, 2.0),),
        bands=(
            cp.Band(
                1.0,
                3.0,
                ("SET_VELOCITY_LIMIT ACCEL=2000",),
                {},
                cp.scale_retractions(2.0),
            ),
        ),
    )
    lines = cp.apply_hooks(text, (obj,), 2400.0).split("\n")
    end = lines.index("EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0")
    assert (
        lines[lines.index("G1 X1 Y1 E.5") - 1] == "G1 E.8 F2400"
    )  # the entry prime stays as sliced
    assert "G1 E-1.60000 F2400" in lines  # the band's own retraction is scaled …
    assert (
        lines[end - 3] == "G1 E0.80000 F2400"
    )  # … and balanced before the object ends
    assert (
        lines[end - 2]
        == "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500 SQUARE_CORNER_VELOCITY=7"
    )
    assert lines[end - 1] == "M106 S255"


def test_clearance_is_euclidean_and_skips_one_layer_objects() -> None:
    tower = [("t", (190.0, 200.0, 0.0, 214.0, 210.0, 17.6))]
    cp._check_clearance(
        [("far", (100.0, 150.0, 0.0, 140.0, 190.0, 13.0))], tower, 45.0, 45.0
    )
    cp._check_clearance(
        [("patch", (196.0, 170.0, 0.0, 214.0, 188.0, 0.2))], tower, 45.0, 45.0
    )
    with pytest.raises(cp.GenError, match="extruder_clearance_radius"):
        cp._check_clearance(
            [("near", (150.0, 160.0, 0.0, 170.0, 180.0, 3.0))], tower, 45.0, 45.0
        )
    with pytest.raises(cp.GenError, match="gantry rod"):
        cp._check_clearance(
            [("tall", (0.0, 0.0, 0.0, 5.0, 5.0, 50.0))], tower, 45.0, 45.0
        )


@needs_openscad
def test_dense_plate_builds_every_test_and_maps_it(orca: str, tmp_path: Path) -> None:  # noqa: F811
    assert PLATES[dense.PLATE_ID].tests is dense.TESTS
    ctx = _ctx(orca)
    plate = dense.build(ctx)
    kinds = {o.kind for o in plate.objects} | {c.kind for c in plate.custom}
    assert {
        "ringing",
        "vfa",
        "dimension",
        "fit",
        "hinge",
        "holes",
        "thin_wall",
        "first_layer",
    } <= kinds
    assert [o.name for o in plate.objects if o.phase == "sequential"] == ["temp_tower"]
    ring = next(o for o in plate.objects if o.name == "ringing_tower")
    assert [b.params["accel"] for b in ring.bands] == list(dense.RING_ACCELS)
    vfa = next(o for o in plate.objects if o.name == "vfa_tube")
    assert [b.params["speed_mm_s"] for b in vfa.bands] == list(dense.VFA_SPEEDS)

    out = tmp_path / "dense.gcode"
    cp.build_plate(ctx, plate, orca, out)
    text = out.read_text()
    side = json.loads(out.with_suffix(".json").read_text())
    assert {o["name"] for o in side["objects"]} >= {o.name for o in plate.objects}
    top = _object_lines(text, "ringing_tower")[-1]
    assert (
        "SET_VELOCITY_LIMIT ACCEL=12000 ACCEL_TO_DECEL=6000 SQUARE_CORNER_VELOCITY=5"
        in top
    )
    assert any(line.endswith("F9000") for line in top if " E" in line)


@needs_openscad
def test_dense_plate_skips_tests(orca: str) -> None:  # noqa: F811
    ctx = cp.make_context(
        "Creality K1 SE", orca, "PLA", "#E8732A", None, frozenset({"ringing", "vfa"})
    )
    assert {o.name for o in dense.build(ctx).objects} == {"ringing_tower", "vfa_tube"}
