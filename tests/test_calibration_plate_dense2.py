"""Tests for calibration_plates.dense2 and the engine pieces it brought: hand-written brims and
arc-fitted visits swapped in (fake Orca of test_calibration_plate)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from test_calibration_plate import STOCK, _ctx, _object_lines, orca  # noqa: F401  (fixture)

from slicing import calibration_plate as cp
from slicing.calibration_plates import PLATES, dense2

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="needs OpenSCAD"
)
START = "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0"
END = "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0"


def test_brim_rule_and_block(orca: str) -> None:  # noqa: F811
    ctx = _ctx(orca)
    narrow = cp.PlateObject("narrow", "t", ((0.0, 0.0, 0.0, 8.0, 8.0, 5.0),))
    tall = cp.PlateObject("tall", "t", ((20.0, 0.0, 0.0, 32.0, 12.0, 15.0),))
    flat = cp.PlateObject("flat", "t", ((40.0, 0.0, 0.0, 60.0, 20.0, 0.2),))
    off = cp.PlateObject("off", "t", ((70.0, 0.0, 0.0, 72.0, 5.0, 0.4),), brim=0.0)
    assert [cp.brim_width(o, 3.0) for o in (narrow, tall, flat, off)] == [
        3.0,
        3.0,
        0.0,
        0.0,
    ]
    assert (
        cp.brim_width(narrow, 0.0) == 0.0
    )  # plates without a brim keep printing without one
    plate = cp.Plate("p", "p", (narrow, flat, off), brim=3.0)
    block = cp.brim_block(ctx, plate)
    assert block is not None and dict(block.params) == {"narrow": "3 mm"}
    bb = block.bbox()
    loops = max(1, round(3.0 / ctx.ex.line_w))
    edge = cp.BRIM_GAP + (loops - 0.5) * ctx.ex.line_w  # outer line's centre
    assert bb[0] == pytest.approx(-edge, abs=1e-3) and bb[3] == pytest.approx(
        8.0 + edge, abs=1e-3
    )
    assert bb[5] == pytest.approx(ctx.ex.layer_h)
    assert cp.brim_rects(narrow, 3.0, ctx.ex.line_w)[0][0] == pytest.approx(
        -(edge + ctx.ex.line_w / 2)
    )


def _visit(x: float, body: list[str], entry: str = "") -> list[str]:
    """One visit like Orca's: z-hop, travel, down, prime, `body`, retract."""
    return [
        START,
        "G1 Z.6 F30000",
        entry or f"G1 X{x} Y5 F30000",
        "G1 Z.2",
        "G1 E.8 F2400",
        *body,
        "G1 E-.8 F2400",
        END,
    ]


LIFT = ["G1 Z.6 F30000", "G1 X50 Y50"]  # Orca's z-hop and travel to the next object


def test_swap_visits_takes_the_arc_fitted_visit_and_restores_state() -> None:
    plain = "\n".join(
        [
            "SET_VELOCITY_LIMIT ACCEL=5000",
            *_visit(1, ["G1 X2 Y5 E.1", "G1 X3 Y6 E.1"]),
            *LIFT,
        ]
    )
    # arc fitting reordered the paths: the visit starts and ends elsewhere, and its slice
    # believed another acceleration was set when it entered
    arc = "\n".join(
        ["SET_VELOCITY_LIMIT ACCEL=3000", *_visit(4, ["G2 X7 Y6 I1 J0 E.2"]), *LIFT]
    )
    out = cp.swap_visits(plain, arc, {"a"}).split("\n")
    assert out == [
        "SET_VELOCITY_LIMIT ACCEL=5000",
        START,
        "SET_VELOCITY_LIMIT ACCEL=3000",
        "G1 Z.6 F30000",
        "G1 X4 Y5 F30000",
        "G1 Z.2",
        "G1 E.8 F2400",
        "G2 X7 Y6 I1 J0 E.2",
        "G1 E-.8 F2400",
        "SET_VELOCITY_LIMIT ACCEL=5000",
        END,
        *LIFT,
    ]


def test_swap_visits_refuses_unsafe_visits() -> None:
    plain = "\n".join([*_visit(1, ["G1 X3 Y6 E.1"]), *LIFT])
    retracts_more = "\n".join([*_visit(1, ["G1 X3 Y6 E.1", "G1 E-.2"]), *LIFT])
    with pytest.raises(cp.GenError, match="retract"):
        cp.swap_visits(plain, retracts_more, {"a"})
    # a wipe as the entry move drags the nozzle across the plate at the previous Z
    wipes_in = "\n".join(
        [*_visit(1, ["G1 X3 Y6 E.1"], entry="G1 X99 Y99 E-.8"), "G1 E.8", *LIFT]
    )
    with pytest.raises(cp.GenError, match="z-hop"):
        cp.swap_visits(plain, wipes_in, {"a"})
    # no retract lift in the process: the travel after the visit runs at the print Z
    no_lift = "\n".join([*_visit(1, ["G1 X3 Y6 E.1"]), "G1 X50 Y50"])
    with pytest.raises(cp.GenError, match="retract lift"):
        cp.swap_visits(no_lift, no_lift, {"a"})
    # balanced E totals (+.8 -.8 -.8 +.8 = 0, like the plain visit's +.8 -.8), but the visit
    # ends primed: the lift and travel after it would ooze
    ends_primed = "\n".join(
        [
            *_visit(1, ["G1 X3 Y6 E.1", "G1 E-.8 F2400", "G1 E-.8 F2400"])[:-2],
            "G1 E.8 F2400",
            END,
            *LIFT,
        ]
    )
    with pytest.raises(cp.GenError, match="end retracted"):
        cp.swap_visits(plain, ends_primed, {"a"})
    with pytest.raises(cp.GenError, match="visits"):
        cp.swap_visits(plain, plain + "\n" + plain, {"a"})


def test_temp_alone_is_refused(orca: str) -> None:  # noqa: F811
    ctx = cp.make_context(
        "Creality K1 SE",
        orca,
        "PLA",
        "#E8732A",
        None,
        frozenset({"temp"}),
        presets=STOCK,
    )
    with pytest.raises(cp.GenError, match="besides temp"):
        dense2.build(ctx)


def test_grid_cells_do_not_overlap_and_patches_fill_the_rest() -> None:
    taken = [(s.col + k, s.row) for s in dense2.CELLS.values() for k in range(s.span)]
    assert len(taken) == len(set(taken))
    assert len(taken) + len(dense2.patch_cells()) == dense2.GRID**2
    assert dense2.union_area(
        [(0, 0, 2, 2), (1, 1, 3, 3), (10, 10, 11, 11)]
    ) == pytest.approx(8.0)


def test_every_spot_belongs_to_one_test() -> None:
    owners = [s for spots in dense2.SPOTS.values() for s in spots]
    assert sorted(owners) == sorted(dense2.CELLS)  # no orphan cell, no spot twice
    assert set(dense2.SPOTS) == set(dense2.TESTS) - {"first_layer"}


@needs_openscad
def test_dense2_builds_maps_and_splices(orca: str, tmp_path: Path) -> None:  # noqa: F811
    assert PLATES[dense2.PLATE_ID].tests is dense2.TESTS
    ctx = _ctx(orca)
    plate = dense2.build(ctx)
    by = {o.name: o for o in plate.objects}
    assert [o.name for o in plate.objects if o.arc_fitting] == ["hq_af"]
    assert [o.name for o in plate.objects if o.phase == "sequential"] == ["temp_tower"]
    assert cp.brim_width(by["bridge_14_brim"], plate.brim) == dense2.BRIM
    assert cp.brim_width(by["overhang_60_brim"], plate.brim) == dense2.BRIM
    assert cp.brim_width(by["hq_pl"], plate.brim) == 0.0
    assert plate.layer_filament == {}  # MVS and inner-wall ACCEL come from the presets
    assert "inner_wall_acceleration" not in plate.layer_process
    patches = [o for o in plate.objects if o.kind == "first_layer"]
    assert len(patches) == len(dense2.patch_cells())
    assert all(
        abs(o.base_rects()[0][2] - o.base_rects()[0][0] - dense2.PATCH) < 1e-9
        for o in patches
    )
    # the tower prints last: its brim is Orca's, in the sequential slice, not a cold ring
    assert cp.seq_brim(plate) == {
        "brim_type": "outer_only",
        "brim_width": dense2.BRIM,
        "brim_object_gap": cp.BRIM_GAP,
    }
    assert cp.seq_brim(replace(plate, brim=0.0)) == {}

    out = tmp_path / "dense2.gcode"
    cp.build_plate(ctx, plate, orca, out)
    text = out.read_text()
    side = json.loads(out.with_suffix(".json").read_text())
    objs = {o["name"]: o for o in side["objects"]}
    assert set(objs) >= set(by) | {"brims", "mvs_strip"}
    assert (
        objs["hq_af"]["arc_fitting"] is True
        and objs["bridge_14_brim"]["brim_mm"] == dense2.BRIM
    )
    assert "temp_tower" not in objs["brims"]["params"]
    assert "bridge_14_brim" in objs["brims"]["params"]
    assert side["slicer_overrides"] == {
        "process": dict(dense2.PROCESS),
        "filament": {},
    }
    assert any(n.startswith("Footprint:") for n in side["notes"])
    assert _object_lines(text, "hq_af") and text.index("brims") < text.index(
        "EXCLUDE_OBJECT_START"
    )
