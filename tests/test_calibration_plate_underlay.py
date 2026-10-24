# ruff: noqa: F811
"""Tests for the plate engine on the shared underlay (#320): the underlay is the first thing printed,
tests stand on it or in cut-outs of it, hand-written blocks and towers are lifted by its height,
brims and per-test bases are gone, the gantry check includes its height and the manifest
`calib-plate/1` carries the `underlay` block (fake Orca of test_calibration_plate)."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_calibration_plate import STOCK, _object_lines, _section_at, orca  # noqa: F401  (fixture)

from slicing import calibration_gen as cg
from slicing import calibration_plate as cp
from slicing import calibration_plate_underlay as pu
from slicing import calibration_underlay as cu
from slicing.calibration_plates import PLATES, dense, dense2, filament

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="needs OpenSCAD"
)
U = 0.2  # the K1 SE preset's first layer: a one-layer underlay


def _ctx(
    orca: str, layers: int | None = 1, tests: frozenset[str] | None = None
) -> cp.PlateContext:
    return cp.make_context(
        "Creality K1 SE",
        orca,
        "PLA",
        "#E8732A",
        None,
        tests,
        presets=STOCK,
        underlay_layers=layers,
    )


def _z_marks(lines: list[str]) -> list[float]:
    return [float(m.group(1)) for ln in lines if (m := re.match(r";Z:([\d.]+)", ln))]


def _build(
    orca: str,
    tmp_path: Path,
    layers: int | None = 1,
    tests: frozenset[str] | None = None,
):  # type: ignore[no-untyped-def]
    ctx = _ctx(orca, layers, tests)
    plate = filament.build(ctx)
    out = tmp_path / "plate1.gcode"
    cp.build_plate(ctx, plate, orca, out)
    return ctx, out.read_text(), json.loads(out.with_suffix(".json").read_text())


# ---- plate 1 on a K1 SE: the G-code -------------------------------------------------------------


def test_k1se_plate1_gcode_prints_the_underlay_first_and_everything_on_top(
    orca: str,
    tmp_path: Path,  # noqa: F811
) -> None:
    _, text, data = _build(orca, tmp_path)
    lines = text.splitlines()
    starts = [ln for ln in lines if ln.startswith("EXCLUDE_OBJECT_START")]
    assert starts[0].startswith(
        "EXCLUDE_OBJECT_START NAME=underlay.stl"
    )  # first thing printed
    first_layer = lines.index(";Z:0.20")
    second_layer = lines.index(";Z:0.40")
    # only the underlay and the tests' own feet (underlay material) print in the first layer
    sheet_layer = lines[first_layer:second_layer]
    visited = [
        m.group(1)
        for ln in sheet_layer
        if (m := re.match(r"EXCLUDE_OBJECT_START NAME=(\w+)\.stl", ln))
    ]
    assert visited[0] == "underlay"
    assert "pa_lines" not in visited
    # the hand-written blocks print on the finished underlay: after its layer, lifted by its height
    labels = lines.index("; 3d calibrate plate: object labels")
    assert (
        first_layer
        < labels
        < lines.index("; 3d calibrate plate: end of object labels")
        < second_layer
    )
    block_z = [
        float(m.group(1))
        for ln in lines[labels:second_layer]
        if (m := re.search(r" Z([\d.]+)", ln))
    ]
    assert (
        block_z and min(block_z) >= U + 0.2 - 1e-6
    )  # the first-layer height above the underlay
    pa = lines.index(
        "; 3d calibrate plate1: PA lines, label = PA x 1000; slow-fast-slow per line"
    )
    assert first_layer < pa < second_layer
    # the tests start at the underlay's top: nothing of them below U except their foot
    for name in ("flow_m10", "bridge_8", "overhang_45", "retraction_tower"):
        obj = next(o for o in data["objects"] if o["name"] == name)
        assert obj["underlay"] == "on" and obj["brim_mm"] == 0.0
        assert (
            obj["bbox"][2] == 0.0
        )  # the foot (the underlay's thickness) under its base
    assert data["print_order"][0] == "layer: underlay"


def test_k1se_plate1_sequential_tower_is_lifted_onto_the_finished_underlay(
    orca: str,
    tmp_path: Path,  # noqa: F811
) -> None:
    _, text, data = _build(orca, tmp_path)
    lines = text.splitlines()
    mark = lines.index(cp.SPLICE_MARK)
    assert all(
        "temp_tower" not in ln
        for ln in lines[:mark]
        if ln.startswith("EXCLUDE_OBJECT_START")
    )
    assert not any(
        "NAME=underlay" in ln for ln in lines[mark:]
    )  # the underlay is done before
    tower = lines[mark:]
    zs = _z_marks(tower)
    first = (
        U + filament.SEQ_LAYER_H
    )  # the tower's layer height above the underlay's top
    assert zs[0] == pytest.approx(first)  # its first layer stands on the underlay's top
    assert min(zs) >= first - 1e-6
    z_moves = [
        float(m.group(1)) for ln in tower if (m := re.match(r"G1 Z([\d.]+)", ln))
    ]
    assert all(
        z >= U + 0.2 - 1e-6 for z in z_moves[1:]
    )  # no move down onto the bare bed
    # its temperature bands moved up with it: one M104 per block, in order
    steps = cg.temp_steps("PLA")
    tt = [ln for ln in tower if re.match(r"^M10[49] S", ln)]
    assert list(dict.fromkeys(tt[1:-1])) == [f"M104 S{t}" for t in steps]
    temp = next(o for o in data["objects"] if o["name"] == "temp_tower")
    assert temp["underlay"] == "on" and temp["brim_mm"] == 0.0
    assert temp["bands"][0]["z0"] >= U  # the first band starts above the underlay


def test_k1se_plate1_layer_bands_move_up_with_the_tests(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    _, text, _ = _build(orca, tmp_path)
    shape = filament.RETRACT_SHAPE
    for i, length in enumerate(filament.RETRACT_LENGTHS):
        z = round(shape.block_floor(i) + 0.2 + U, 2)
        sec = _section_at(text, "retraction_tower", z)
        assert f"G1 E-{0.56 * length / 0.8:.5f} F2400" in sec
    for name, pct in (("flow_m10", 90), ("flow_p10", 110)):
        for sec in _object_lines(text, name):
            assert sec[1] == f"M221 S{pct}" and sec[-2] == "M221 S100"


def test_two_underlay_layers_lift_everything_by_both(orca: str, tmp_path: Path) -> None:  # noqa: F811
    _, text, data = _build(orca, tmp_path, layers=2)
    lines = text.splitlines()
    assert data["underlay"]["layers"] == 2 and data["underlay"]["top_mm"] == 0.4
    third = lines.index(";Z:0.60")
    assert (
        lines.index(";Z:0.40")
        < lines.index("; 3d calibrate plate: object labels")
        < third
    )
    block_z = [
        float(m.group(1))
        for ln in lines[lines.index("; 3d calibrate plate: object labels") : third]
        if (m := re.search(r" Z([\d.]+)", ln))
    ]
    assert min(block_z) >= 0.6 - 1e-6
    mark = lines.index(cp.SPLICE_MARK)
    assert _z_marks(lines[mark:])[0] == pytest.approx(0.4 + filament.SEQ_LAYER_H)


def test_bare_bed_path_is_unchanged(orca: str, tmp_path: Path) -> None:  # noqa: F811
    _, text, data = _build(orca, tmp_path, layers=None)
    assert "NAME=underlay" not in text and data["underlay"] is None
    assert data["print_order"][0].startswith("custom: ")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("START_PRINT"))
    assert lines[start + 1] == "; 3d calibrate plate: object labels"


# ---- the manifest ------------------------------------------------------------------------------


def test_manifest_carries_the_underlay_block(orca: str, tmp_path: Path) -> None:  # noqa: F811
    _, _, data = _build(orca, tmp_path)
    assert data["schema"] == "calib-plate/1"
    ul = data["underlay"]
    assert ul["layers"] == 1 and ul["top_mm"] == U and ul["pieces"] == 1
    assert (
        ul["head"] == "T1" and ul["read"]["light"] == "raking"
    )  # single head: raking light
    assert ul["brim"].startswith("none")
    x0, y0, x1, y1 = ul["bbox"]
    assert x0 >= 0 and y0 >= 0 and x1 <= 220 and y1 <= 220
    assert ul["bare"] and all(len(r) == 4 for r in ul["bare"])
    sheet = next(o for o in data["objects"] if o["name"] == "underlay")
    assert sheet["underlay"] == "sheet" and sheet["kind"] == "underlay"
    assert all(o.get("brim_mm", 0) == 0 for o in data["objects"])


# ---- brims and bases ---------------------------------------------------------------------------


def _plate(*objs: cp.PlateObject, brim: float = 3.0) -> cp.Plate:
    return cp.Plate("p", "p", objs, brim=brim)


NARROW = ((0.0, 0.0, 0.0, 8.0, 8.0, 5.0),)  # a tall 8 mm base: the brim rule's case


def test_no_brim_block_and_no_brim_width_on_an_underlay_plate(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    a = cp.PlateObject("narrow", "t", ((20.0, 20.0, 0.0, 28.0, 28.0, 5.0),))
    b = cp.PlateObject("tall", "t", ((60.0, 20.0, 0.0, 72.0, 32.0, 15.0),))
    plate = _plate(a, b)
    assert (
        cp.brim_width(a, plate.brim) == 3.0
    )  # the bare-bed rule is still the rule ...
    ctx = _ctx(orca, 1)
    out = tmp_path / "p.gcode"
    cp.build_plate(ctx, plate, orca, out)
    data = json.loads(out.with_suffix(".json").read_text())
    assert [o for o in data["objects"] if o["phase"] == "custom"] == []  # no brim block
    assert {o["name"]: o["brim_mm"] for o in data["objects"]} == {
        "underlay": 0.0,
        "narrow": 0.0,
        "tall": 0.0,
    }
    assert "; 3d calibrate plate: brims" not in out.read_text()


def test_brim_rule_runs_with_no_underlay(orca: str, tmp_path: Path) -> None:  # noqa: F811
    a = cp.PlateObject("narrow", "t", ((20.0, 20.0, 0.0, 28.0, 28.0, 5.0),))
    plate = _plate(a)
    ctx = _ctx(orca, None)
    out = tmp_path / "p.gcode"
    cp.build_plate(ctx, plate, orca, out)
    data = json.loads(out.with_suffix(".json").read_text())
    assert [o["name"] for o in data["objects"] if o["phase"] == "custom"] == ["brims"]
    assert next(o for o in data["objects"] if o["name"] == "narrow")["brim_mm"] == 3.0
    assert "; 3d calibrate plate: brims" in out.read_text()


def test_brim_rule_runs_for_an_object_outside_the_underlay(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    on = cp.PlateObject("on_sheet", "t", ((20.0, 20.0, 0.0, 28.0, 28.0, 5.0),))
    outside = cp.PlateObject(
        "outside", "t", ((150.0, 150.0, 0.0, 158.0, 158.0, 5.0),), underlay="off"
    )
    plate = _plate(on, outside)
    ctx = _ctx(orca, 1)
    new, ul = pu.apply_underlay(ctx, plate, 1)
    assert ul.covers((20.0, 20.0, 28.0, 28.0)) and not ul.covers(
        (150.0, 150.0, 158.0, 158.0)
    )
    by_name = {o.name: o for o in new.objects}
    assert cp.brim_width(by_name["on_sheet"], new.brim) == 0.0
    assert cp.brim_width(by_name["outside"], new.brim) == 3.0
    out = tmp_path / "p.gcode"
    cp.build_plate(ctx, plate, orca, out)
    data = json.loads(out.with_suffix(".json").read_text())
    assert next(o for o in data["objects"] if o["name"] == "brims")["params"] == {
        "outside": "3 mm"
    }


# ---- sequential printing and the gantry ---------------------------------------------------------


def _seq_plate(tower_h: float, layer_h: float = 10.0) -> cp.Plate:
    low = cp.PlateObject("low", "t", ((10.0, 10.0, 0.0, 30.0, 30.0, layer_h),))
    t1 = cp.PlateObject(
        "tower_a", "t", ((100.0, 10.0, 0.0, 110.0, 20.0, tower_h),), phase="sequential"
    )
    t2 = cp.PlateObject(
        "tower_b",
        "t",
        ((100.0, 70.0, 0.0, 110.0, 80.0, tower_h + 4.0),),
        phase="sequential",
    )
    return cp.Plate("p", "p", (low, t1, t2))


def test_sequential_order_underlay_first_then_towers_starting_at_its_top(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    out = tmp_path / "p.gcode"
    cp.build_plate(ctx, _seq_plate(20.0), orca, out)
    text = out.read_text()
    lines = text.splitlines()
    visits = [
        m.group(1)
        for ln in lines
        if (m := re.match(r"EXCLUDE_OBJECT_START NAME=(\w+)\.stl", ln))
    ]
    order = list(dict.fromkeys(visits))
    assert order == [
        "underlay",
        "low",
        "tower_a",
        "tower_b",
    ]  # the underlay before any object
    mark = lines.index(cp.SPLICE_MARK)
    for tower in ("tower_a", "tower_b"):
        secs = _object_lines(text, tower)
        first = secs[0]
        assert any(
            f"G1 Z{U + 0.2:.3f}" in ln or f"G1 Z{U + 0.2:.2f}" in ln for ln in first
        )
        z_before = _z_marks(
            lines[: lines.index(first[0])]
        )  # layer marks before the tower's visit
        assert z_before and max(z_before) >= 10.0 + U  # the layer phase finished first
    towers = _z_marks(lines[mark:])
    assert min(towers) == pytest.approx(
        U + 0.2
    )  # each tower's first layer is on the underlay top
    assert lines.index(first[0]) > mark


def test_gantry_clearance_includes_the_underlay_height(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    # rod clearance 45 mm (the K1 SE preset): a 44.9 mm layer object passes on the bare bed ...
    bare = _ctx(orca, None)
    cp.build_plate(bare, _seq_plate(20.0, layer_h=44.9), orca, tmp_path / "bare.gcode")
    # ... and fails on the underlay: 44.9 + 0.2 mm is taller than the rod
    ctx = _ctx(orca, 1)
    with pytest.raises(cg.GenError, match="taller than the gantry rod clearance 45 mm"):
        cp.build_plate(ctx, _seq_plate(20.0, layer_h=44.9), orca, tmp_path / "ul.gcode")
    # a tower (the second one is 4 mm taller) under the rod on the bare bed is over it on the underlay
    cp.build_plate(bare, _seq_plate(40.9), orca, tmp_path / "bare2.gcode")
    with pytest.raises(cg.GenError, match="taller than the gantry rod clearance"):
        cp.build_plate(ctx, _seq_plate(40.9), orca, tmp_path / "ul2.gcode")


def test_one_layer_objects_stay_below_the_nozzle_on_the_underlay() -> None:
    low = (
        "flat",
        (0.0, 0.0, 0.0, 10.0, 10.0, 0.4 + 0.4),
    )  # a 0.4 mm strip on a 0.4 mm underlay
    tower = ("tower", (20.0, 0.0, 0.0, 30.0, 10.0, 10.0))
    cp._check_clearance([low], [tower], 45.0, 45.0, underlay_h=0.4)
    with pytest.raises(cg.GenError, match="keep 45 mm"):
        cp._check_clearance([low], [tower], 45.0, 45.0, underlay_h=0.0)


# ---- cut-outs and declarations on the real plates ------------------------------------------------


def _prepared(orca: str, plate_id: str):  # type: ignore[no-untyped-def]
    ctx = _ctx(orca, 1)
    plate = PLATES[plate_id].build(ctx)
    labels = cp.bed_labels(ctx, plate.objects)
    plate = replace(
        plate, custom=(*((labels,) if labels is not None else ()), *plate.custom)
    )
    return ctx, plate


def _plates() -> list[Any]:
    return [
        "plate1",
        pytest.param("dense", marks=needs_openscad),
        pytest.param("dense2", marks=needs_openscad),
    ]


@pytest.mark.parametrize("plate_id", _plates())
def test_every_catalog_test_declares_on_or_cutout_with_a_reason(plate_id: str) -> None:
    pdef = PLATES[plate_id]
    assert set(pdef.underlay) == set(pdef.tests)
    for test, decl in pdef.underlay.items():
        assert decl.mode in ("on", "cutout"), test
        assert len(decl.reason) > 15, test  # a reason, not a placeholder
        assert not (decl.movable and decl.mode == "on"), test


def test_the_declared_cutouts_are_the_loose_in_place_hole_and_foot_tests() -> None:
    d, d2 = dense.UNDERLAY, dense2.UNDERLAY
    for test in (
        "fit",
        "hinge",
        "screw",
        "holes",
    ):  # print in place, loose insert, through holes
        assert d[test].mode == "cutout"
    for test in (
        "fit",
        "hinge",
        "elephant_foot",
        "screw",
        "slots",
        "hole_quality",
        "ovality",
    ):
        assert d2[test].mode == "cutout"
    assert (
        d["fit"].movable
        and d["hinge"].movable
        and d2["fit"].movable
        and d2["hinge"].movable
    )
    assert all(v.mode == "on" for v in filament.UNDERLAY.values())


@pytest.mark.parametrize("plate_id", _plates())
def test_one_connected_underlay_and_no_movable_part_touches_it(
    orca: str, plate_id: str
) -> None:  # noqa: F811
    ctx, plate = _prepared(orca, plate_id)
    new, ul = pu.apply_underlay(ctx, plate, 1)
    assert ul.pieces == 1  # one connected polygon after the cut-outs
    decls = PLATES[plate_id].underlay
    movable = [o for o in new.objects if o.movable]
    if plate_id != "plate1":
        assert movable  # the print-in-place and loose parts are really there
    for o in new.objects:
        if o.underlay == "sheet":
            continue
        decl = decls[o.test]
        assert o.underlay == (
            "cutout" if decl.mode == "cutout" or o.movable else "on"
        ), o.name
    for o in movable:
        assert o.underlay == "cutout", o.name
        for r in o.base_rects():
            assert not ul.overlaps_sheet(r), f"{o.name} {r} touches the underlay"
    # every test footprint lies inside the outline
    for o in new.objects:
        if o.underlay in ("on", "cutout"):
            assert ul.covers(cp.box_rect(o.bbox())), o.name
    sheet = next(o for o in new.objects if o.underlay == "sheet")
    assert new.objects[0] is sheet  # the underlay is the first object of the plate
    assert new.brim == plate.brim and all(
        cp.brim_width(o, new.brim) == 0.0 for o in new.objects
    )  # no brims under an underlay


@needs_openscad
def test_dense_plates_have_no_first_layer_patches_brims_or_tile_bases_on_the_underlay(
    orca: str,
) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    bare = _ctx(orca, None)
    for mod in (dense, dense2):
        on = mod.build(ctx)
        assert not [o for o in on.objects if o.kind == "first_layer"]
        assert [
            o for o in mod.build(bare).objects if o.kind == "first_layer"
        ]  # the bare plate keeps them
    on2 = dense2.build(ctx)
    assert on2.brim == 0.0 and dense2.build(bare).brim == dense2.BRIM
    thin = next(o for o in on2.objects if o.name == "thin_y")
    assert thin.bbox()[5] == pytest.approx(
        dense2.THIN_H
    )  # no 0.6 mm tile under the walls
    assert next(o for o in dense2.build(bare).objects if o.name == "thin_y").bbox()[
        5
    ] == pytest.approx(dense2.BASE_H + dense2.THIN_H)
    only = _ctx(orca, 1, frozenset({"first_layer"}))
    for mod in (dense, dense2):
        with pytest.raises(cg.GenError, match="bare cells"):
            mod.build(only)


def test_lifting_an_object_adds_a_foot_and_moves_its_bands(orca: str) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    band = cp.Band(1.0, 3.0, ("B",), {"k": 1})
    obj = cp.PlateObject(
        "tower",
        "t",
        ((10.0, 10.0, 0.0, 20.0, 20.0, 4.0),),
        bands=(band,),
        items=(cp.Item("i", (10.0, 10.0, 1.0, 20.0, 20.0, 3.0)),),
    )
    new, ul = pu.apply_underlay(ctx, cp.Plate("p", "p", (obj,)), 1)
    lifted = next(o for o in new.objects if o.name == "tower")
    assert lifted.bbox() == (10.0, 10.0, 0.0, 20.0, 20.0, 4.0 + ul.height)
    assert lifted.boxes[0] == (
        10.0,
        10.0,
        0.0,
        20.0,
        20.0,
        ul.height,
    )  # the foot, then the part
    assert lifted.boxes[1] == (10.0, 10.0, ul.height, 20.0, 20.0, 4.0 + ul.height)
    assert (lifted.bands[0].z0, lifted.bands[0].z1) == (
        1.0 + ul.height,
        3.0 + ul.height,
    )
    assert lifted.items[0].bbox[2] == 1.0 + ul.height
    assert lifted.base_rects() == ((10.0, 10.0, 20.0, 20.0),)


def test_thin_bases_get_a_wide_enough_foot_and_overlapping_feet_are_refused(
    orca: str,
) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    wall = cp.PlateObject("wall", "t", ((10.0, 10.0, 0.0, 10.3, 18.0, 3.0),))
    new, _ = pu.apply_underlay(ctx, cp.Plate("p", "p", (wall,)), 1)
    foot = new.objects[1].boxes[0]
    assert foot[3] - foot[0] == pytest.approx(
        pu.MIN_FOOT
    )  # 0.3 mm widened to a printable foot
    near = cp.PlateObject("wall2", "t", ((11.0, 10.0, 0.0, 11.3, 18.0, 3.0),))
    with pytest.raises(cg.GenError, match="feet on the underlay overlap"):
        pu.apply_underlay(ctx, cp.Plate("p", "p", (wall, near)), 1)


def test_cutout_objects_stay_on_the_bare_bed(orca: str) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    loose = cp.PlateObject(
        "loose",
        "t",
        ((10.0, 10.0, 0.0, 20.0, 20.0, 3.0),),
        underlay="cutout",
        movable=True,
    )
    on = cp.PlateObject("on", "t", ((40.0, 10.0, 0.0, 50.0, 20.0, 3.0),))
    new, ul = pu.apply_underlay(ctx, cp.Plate("p", "p", (loose, on)), 1)
    got = {o.name: o for o in new.objects}
    assert (
        got["loose"].boxes == loose.boxes and got["loose"].bbox() == loose.bbox()
    )  # not lifted
    assert got["on"].bbox()[5] == 3.0 + ul.height
    assert not ul.overlaps_sheet((10.0, 10.0, 20.0, 20.0)) and not ul.overlaps_sheet(
        (9.0, 9.0, 21.0, 21.0)
    )
    assert ul.overlaps_sheet(
        (40.0, 10.0, 50.0, 20.0)
    )  # the foot of the other test is part of the piece


def test_a_sequential_object_cannot_be_a_cutout(orca: str) -> None:  # noqa: F811
    ctx = _ctx(orca, 1)
    low = cp.PlateObject("low", "t", ((10.0, 10.0, 0.0, 30.0, 30.0, 5.0),))
    tower = cp.PlateObject(
        "tower",
        "t",
        ((100.0, 10.0, 0.0, 110.0, 20.0, 5.0),),
        phase="sequential",
        underlay="cutout",
    )
    with pytest.raises(cg.GenError, match="cannot be a cutout"):
        pu.apply_underlay(ctx, cp.Plate("p", "p", (low, tower)), 1)


def test_declare_forces_cutouts_and_movable_parts(orca: str) -> None:  # noqa: F811
    decls = {
        "a": cu.UnderlayDecl("on", "stands on the sheet"),
        "b": cu.UnderlayDecl("cutout", "a through hole", movable=False),
        "c": cu.UnderlayDecl("cutout", "print in place", movable=True),
    }
    o = cp.PlateObject("o", "t", NARROW)
    loose = cp.PlateObject("loose", "t", NARROW, movable=True)
    assert pu.declare((o,), "a", decls)[0].underlay == "on"
    assert (
        pu.declare((loose,), "a", decls)[0].underlay == "cutout"
    )  # a loose part is never on the sheet
    got = pu.declare((o,), "b", decls)[0]
    assert got.underlay == "cutout" and got.test == "b" and not got.movable
    assert pu.declare((o,), "c", decls)[0].movable
    with pytest.raises(cg.GenError, match="no underlay declaration"):
        pu.declare((o,), "zz", decls)


def test_plate_definitions_reject_a_test_without_a_declaration() -> None:
    from slicing.calibration_plates import PlateDef

    with pytest.raises(cg.GenError, match="missing b"):
        PlateDef(
            filament.build,
            {"a": "x", "b": "y"},
            {"a": cu.UnderlayDecl("on", "because")},
        )


# ---- multi-head: the underlay in its own head --------------------------------------------------


def _multi_ctx(orca: str, layers: int = 1) -> cp.PlateContext:
    heads = cp.UnderlayHeads("T4", "T3", "T1", "T0", 97.0)
    return cp.make_context(
        "Creality K1 SE",
        orca,
        "PLA",
        "#000000",
        None,
        presets=STOCK,
        underlay_layers=layers,
        heads=heads,
    )


@pytest.mark.parametrize("layers", [1, 2])
def test_multi_head_plate_prints_the_underlay_in_its_head_then_one_change_to_the_tests(
    orca: str,
    tmp_path: Path,
    layers: int,  # noqa: F811
) -> None:
    ctx = _multi_ctx(orca, layers)
    out = tmp_path / "u1.gcode"
    cp.build_plate(ctx, filament.build(ctx), orca, out)
    lines = out.read_text().splitlines()
    tools = [(i, ln) for i, ln in enumerate(lines) if re.fullmatch(r"T\d", ln)]
    assert [t for _, t in tools] == [
        "T3",
        "T0",
    ]  # exactly one change, underlay head → tests' head
    start = next(i for i, ln in enumerate(lines) if ln.startswith("START_PRINT"))
    first_layer = lines.index(";Z:0.20")
    first_visit = next(
        i for i, ln in enumerate(lines) if ln.startswith("EXCLUDE_OBJECT_START")
    )
    assert (
        start < tools[0][0] < first_visit
    )  # the underlay head is selected before its first object
    assert first_layer < first_visit
    top = round(0.2 * layers, 2)
    last_underlay = lines.index(f";Z:{top:.2f}")
    next_layer = lines.index(f";Z:{top + 0.2:.2f}")
    assert (
        last_underlay < tools[1][0] < next_layer
    )  # the tests' head, after the underlay's last layer
    under = lines[tools[0][0] : tools[1][0]]
    # the underlay head prints the sheet and the tests' feet (underlay material) and nothing above
    assert max(_z_marks(lines[: tools[1][0]])) == pytest.approx(top)
    assert not any(
        ln.startswith("; 3d calibrate plate") for ln in under
    )  # no hand-written block yet
    data = json.loads(out.with_suffix(".json").read_text())
    ul = data["underlay"]
    assert (
        ul["head"] == "T4"
        and ul["read"]["light"] == "contrast"
        and "ΔE 97" in ul["read"]["note"]
    )
    assert ul["layers"] == layers


def test_single_head_plate_has_no_tool_change(orca: str, tmp_path: Path) -> None:  # noqa: F811
    _, text, _ = _build(orca, tmp_path)
    assert not [ln for ln in text.splitlines() if re.fullmatch(r"T\d", ln)]


def test_single_tests_declare_why_they_carry_no_underlay() -> None:
    assert set(cg.KIND_UNDERLAY) == set(cg.KINDS)
    assert all(len(why) > 30 for why in cg.KIND_UNDERLAY.values())


# ---- review findings: bare map, heads of the bare-bed objects, fan state, `off` towers --------------


def _rects_overlap(a: list[float], b: tuple[float, float, float, float]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def test_bare_map_leaves_out_blocks_towers_and_spans(orca: str) -> None:  # noqa: F811
    ctx, plate = _prepared(orca, "plate1")
    new, ul = pu.apply_underlay(ctx, plate, 1)
    assert ul.bare
    blocks = [r for c in plate.custom for r in pu.block_rects(c)]
    assert len(blocks) >= 3  # PA lines, the MVS strip, the labels
    spans = [cp.box_rect(o.bbox()) for o in new.objects if o.underlay == "on"]
    tower = next(o for o in new.objects if o.name == "temp_tower")
    assert cp.box_rect(tower.bbox()) in spans
    for rect in ul.bare:
        for covered in (*blocks, *spans):
            inner = (
                covered[0] + 1e-3,
                covered[1] + 1e-3,
                covered[2] - 1e-3,
                covered[3] - 1e-3,
            )  # grid keys are 0.1 µm
            assert not _rects_overlap(list(rect), inner), (
                f"bare {rect} lies under {covered}"
            )
    # the bare map is a real part of the sheet: less than the sheet, more than nothing
    assert 0 < sum((r[2] - r[0]) * (r[3] - r[1]) for r in ul.bare) < ul.area_mm2
    assert ul.manifest(head="T1", light="raking")["bare"] == [
        [round(v, 3) for v in r] for r in ul.bare
    ]


def _cutout_plate() -> cp.Plate:
    on = cp.PlateObject("on_a", "t", ((10.0, 10.0, 0.0, 30.0, 20.0, 4.0),), test="a")
    hole = cp.PlateObject(
        "bare_b",
        "t",
        ((50.0, 10.0, 0.0, 70.0, 20.0, 4.0),),
        underlay="cutout",
        test="b",
    )
    return cp.Plate("p", "p", (on, hole))


def test_bare_bed_objects_print_their_first_layers_with_the_tests_head(
    orca: str,
    tmp_path: Path,  # noqa: F811
) -> None:
    ctx = _multi_ctx(orca, 2)
    out = tmp_path / "c.gcode"
    cp.build_plate(ctx, _cutout_plate(), orca, out)
    lines = out.read_text().splitlines()
    seq: list[str] = []
    for ln in lines:
        if re.fullmatch(r"T\d", ln):
            seq.append(ln)
        elif m := re.match(r"EXCLUDE_OBJECT_START NAME=(\w+)\.stl", ln):
            seq.append(m.group(1))
    # per layer: the sheet and the foot-carrying test in T3 (the underlay), then the bare-bed
    # test's own layers in T0 (its first layers are the test); the same again in layer 2
    assert seq[:10] == ["T3", "underlay", "on_a", "T0", "bare_b"] * 2
    bare_visit = next(
        i
        for i, ln in enumerate(lines)
        if ln.startswith("EXCLUDE_OBJECT_START NAME=bare_b")
    )
    assert [ln for ln in lines[:bare_visit] if re.fullmatch(r"T\d", ln)] == ["T3", "T0"]
    # the heated nozzle follows every head selection
    for i, ln in enumerate(lines):
        if re.fullmatch(r"T\d", ln):
            assert lines[i + 1].startswith("M109 S")


def test_single_head_cutout_keeps_its_low_clearance(orca: str, tmp_path: Path) -> None:  # noqa: F811
    # a one-layer test on the bare bed is below the nozzle tip next to a tower (0.4 mm, not 0.6 mm)
    cp._check_clearance(
        [("flat", (0.0, 0.0, 0.0, 10.0, 10.0, 0.5))],
        [("tower", (20.0, 0.0, 0.0, 30.0, 10.0, 10.0))],
        45.0,
        45.0,
        0.4,
        frozenset({"flat"}),
    )
    with pytest.raises(cg.GenError, match="keep 45 mm"):
        cp._check_clearance(
            [("flat", (0.0, 0.0, 0.0, 10.0, 10.0, 0.8))],
            [("tower", (20.0, 0.0, 0.0, 30.0, 10.0, 10.0))],
            45.0,
            45.0,
            0.4,
            frozenset({"flat"}),
        )


def test_the_slicers_fan_is_put_back_after_the_blocks_that_moved_into_the_print() -> (
    None
):
    ctx = cp.PlateContext.__new__(
        cp.PlateContext
    )  # splice reads only heads and ex.layer_h here
    object.__setattr__(ctx, "heads", None)
    object.__setattr__(ctx, "ex", type("E", (), {"layer_h": 0.2})())
    a = "\n".join(
        [
            "START_PRINT",
            ";LAYER_CHANGE",
            ";Z:0.2",
            "M106 S255",
            "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500",
            "G1 X1 Y1 E1",
            ";LAYER_CHANGE",
            ";Z:0.4",
            "G1 X2 Y2 E1",
            "END_PRINT",
        ]
    )
    block = cp.CustomBlock("b", "b", ("; block", "M106 S0", "G1 X5 Y5 E1"))
    lines = cp.splice(ctx, a, None, (block,), 0.2).split("\n")
    i = lines.index("; block")
    assert lines.index("M106 S255", i) < lines.index(";Z:0.4")


def test_sequential_objects_are_all_on_the_underlay_or_all_outside_it(
    orca: str, tmp_path: Path
) -> None:  # noqa: F811
    low = cp.PlateObject("low", "t", ((10.0, 10.0, 0.0, 30.0, 30.0, 5.0),))
    a = cp.PlateObject(
        "tower_a", "t", ((100.0, 10.0, 0.0, 110.0, 20.0, 9.0),), phase="sequential"
    )
    b = cp.PlateObject(
        "tower_b",
        "t",
        ((100.0, 80.0, 0.0, 110.0, 90.0, 9.0),),
        phase="sequential",
        underlay="off",
    )
    with pytest.raises(cg.GenError, match="all stand on the underlay or all outside"):
        cp.build_plate(
            _ctx(orca, 1), cp.Plate("p", "p", (low, a, b)), orca, tmp_path / "m.gcode"
        )
    # all outside: the tower is not lifted
    c = cp.PlateObject(
        "tower_c",
        "t",
        ((100.0, 10.0, 0.0, 110.0, 20.0, 9.0),),
        phase="sequential",
        underlay="off",
    )
    out = tmp_path / "o.gcode"
    cp.build_plate(_ctx(orca, 1), cp.Plate("p", "p", (low, c)), orca, out)
    lines = out.read_text().splitlines()
    assert min(_z_marks(lines[lines.index(cp.SPLICE_MARK) :])) == pytest.approx(0.2)


def test_underlay_is_first_even_when_the_slicer_orders_the_objects_its_own_way(
    orca: str,
    tmp_path: Path,  # noqa: F811
) -> None:
    """Orca orders a layer's objects by its own rules (the real slice of `gen dense` had the sheet
    12th): the engine puts the sheet's visit first in every underlay layer."""
    body = [";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2"]
    for name in ("b", "a", "underlay", "c"):
        body += [
            f"EXCLUDE_OBJECT_START NAME={name}.stl_id_0_copy_0",
            "G1 Z.6 F30000",
            "G1 X1 Y1 F30000",
            "G1 Z.2",
            "G1 X2 Y2 E.5",
            "G1 E-.8 F2400",
            f"EXCLUDE_OBJECT_END NAME={name}.stl_id_0_copy_0",
        ]
    body += [";LAYER_CHANGE", ";Z:0.4"]
    ordered = cp._reorder_visits(
        body, len(body) - 2, lambda n: 0 if n == "underlay" else 1
    )
    starts = [
        ln.split("NAME=")[1].split(".")[0]
        for ln in ordered
        if ln.startswith("EXCLUDE_OBJECT_START")
    ]
    assert starts == ["underlay", "b", "a", "c"]  # stable for the rest
    assert len(ordered) == len(body) and sorted(ordered) == sorted(body)
    # an unsafe visit (enters without a z-hop) leaves the layer as the slicer ordered it
    bad = list(body)
    bad[bad.index("G1 E-.8 F2400", 5)] = "G1 X9 Y9 E.1"  # ends primed
    assert (
        cp._reorder_visits(bad, len(bad) - 2, lambda n: 0 if n == "underlay" else 1)
        == bad
    )


def test_the_map_names_the_first_object(orca: str, tmp_path: Path) -> None:  # noqa: F811
    _, text, data = _build(orca, tmp_path)
    assert data["underlay"]["first_object"] == "underlay" == cp.first_object(text)


def test_a_visit_orca_closes_after_the_next_layer_header_is_closed_before_it() -> None:
    """Orca ends the last visit of a layer after the next layer's header: header, then the visit's
    retraction and wipe, then EXCLUDE_OBJECT_END. The tail moves before the header, so the layer
    boundary (a head change, a hand-written block) never cuts a visit in two."""
    body = [
        ";LAYER_CHANGE",
        ";Z:0.2",
        ";HEIGHT:0.2",
        "G92 E0",
        "",
        "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0",
        "G1 Z.6 F30000",
        "G1 X1 Y1 F30000",
        "G1 Z.2",
        "G1 X2 Y2 E.5",
        "G1 E-.8 F2400",
        "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0",
        "EXCLUDE_OBJECT_START NAME=b.stl_id_1_copy_0",
        "G1 Z.6 F30000",
        "G1 X3 Y3 F30000",
        "G1 Z.2",
        "G1 X4 Y4 E.5",
        ";LAYER_CHANGE",
        ";Z:0.4",
        ";HEIGHT:0.2",
        ";BEFORE_LAYER_CHANGE",
        ";0.4",
        "G92 E0",
        "",
        "G1 E-.56 F2400",
        ";WIPE_START",
        "G1 X5 Y5 E-.1",
        ";WIPE_END",
        "EXCLUDE_OBJECT_END NAME=b.stl_id_1_copy_0",
        "EXCLUDE_OBJECT_START NAME=c.stl_id_2_copy_0",
        "G1 X6 Y6 E.5",
    ]
    h = body.index(";Z:0.4") - 1
    out = cp._close_visits(body, h)
    assert len(out) == len(body) and sorted(out) == sorted(body)
    h2 = out.index(";Z:0.4") - 1
    assert (
        out[h2 - 1] == "EXCLUDE_OBJECT_END NAME=b.stl_id_1_copy_0"
    )  # closed right before the header
    assert out[h2 - 5 : h2 - 1] == [
        "G1 E-.56 F2400",
        ";WIPE_START",
        "G1 X5 Y5 E-.1",
        ";WIPE_END",
    ]
    assert out[h2 : h2 + 7] == [
        ";LAYER_CHANGE",
        ";Z:0.4",
        ";HEIGHT:0.2",
        ";BEFORE_LAYER_CHANGE",
        ";0.4",
        "G92 E0",
        "",
    ]
    assert out[h2 + 7].startswith(
        "EXCLUDE_OBJECT_START NAME=c"
    )  # the next layer's visit follows
    assert cp._close_visits(out, h2) == out  # idempotent
    # and the visit can now be reordered like any other
    first = cp._reorder_visits(out, h2, lambda n: 0 if n == "b" else 1)
    assert [
        ln.split("NAME=")[1][:1]
        for ln in first
        if ln.startswith("EXCLUDE_OBJECT_START")
    ] == ["b", "a", "c"]


def test_moved_visits_keep_the_acceleration_their_predecessor_left_inherited() -> None:
    """Orca writes SET_VELOCITY_LIMIT only on change: A sets 500, the sheet 1000, B inherits 1000.
    After the sheet moves first, B would run at A's 500 — every visit of a reordered layer gets
    the state it had before it."""

    def visit(name: str, limit: str | None) -> list[str]:
        return [
            f"EXCLUDE_OBJECT_START NAME={name}.stl_id_0_copy_0",
            "G1 Z.6 F30000",
            "G1 X1 Y1 F30000",
            "G1 Z.2",
            *([f"SET_VELOCITY_LIMIT ACCEL={limit}"] if limit else []),
            "G1 X2 Y2 E.5",
            "G1 E-.8 F2400",
            f"EXCLUDE_OBJECT_END NAME={name}.stl_id_0_copy_0",
        ]

    body = [";LAYER_CHANGE", ";Z:0.2", "SET_VELOCITY_LIMIT ACCEL=2000"]
    body += visit("a", "500") + visit("underlay", "1000") + visit("b", None)
    cut = len(body)
    body += [";LAYER_CHANGE", ";Z:0.4"]
    out = cp._reorder_visits(body, cut, lambda n: 0 if n == "underlay" else 1)
    seq = []
    for ln in out:
        if m := re.match(r"EXCLUDE_OBJECT_START NAME=(\w+)\.stl", ln):
            seq.append(m.group(1))
        elif ln.startswith("SET_VELOCITY_LIMIT"):
            seq.append(ln.split("=")[1].split()[0])
    # every visit enters with the state it had in Orca's order: the sheet after a (500), a after
    # the layer's 2000, b after the sheet's 1000
    assert seq == ["2000", "500", "underlay", "1000", "2000", "a", "500", "1000", "b"]
    i = out.index("EXCLUDE_OBJECT_START NAME=b.stl_id_0_copy_0")
    assert out[i - 1] == "SET_VELOCITY_LIMIT ACCEL=1000"


def test_an_underlay_only_layer_phase_does_not_index_past_the_body() -> None:
    body = [";LAYER_CHANGE", ";Z:0.2", "G1 X1 Y1 E1"]
    assert cp._close_visits(body, len(body)) == body
    assert cp._reorder_visits(body, len(body), lambda n: 0) == body


def test_the_blocks_get_the_retraction_the_slicer_left_and_give_it_back() -> None:
    ctx = cp.PlateContext.__new__(cp.PlateContext)
    object.__setattr__(ctx, "heads", None)
    object.__setattr__(
        ctx, "ex", type("E", (), {"layer_h": 0.2, "retract_f": 2400.0})()
    )
    a = "\n".join(
        [
            "START_PRINT",
            ";LAYER_CHANGE",
            ";Z:0.2",
            "EXCLUDE_OBJECT_START NAME=underlay.stl_id_0_copy_0",
            "G1 X1 Y1 E1",
            "G1 E-.8 F2400",
            "EXCLUDE_OBJECT_END NAME=underlay.stl_id_0_copy_0",
            ";LAYER_CHANGE",
            ";Z:0.4",
            "G1 X2 Y2 E1",
            "END_PRINT",
        ]
    )
    block = cp.CustomBlock(
        "b", "b", ("; block", "G1 E-.800 F2400", "G1 X5 Y5 E1", "G1 E.800 F2400")
    )
    lines = cp.splice(ctx, a, None, (block,), 0.2).split("\n")
    i = lines.index("; block")
    assert (
        lines[i - 1] == "G1 E0.80000 F2400"
    )  # the owed retraction is undone before the block
    j = lines.index("G1 E.800 F2400", i)
    assert (
        lines[j + 1] == "G1 E-0.80000 F2400"
    )  # and made again after it: the next visit de-retracts


def test_u1_plate_args_pass_the_head_choice_and_every_loaded_colour() -> None:
    from types import SimpleNamespace

    from commands import calibrate

    heads = [SimpleNamespace(label=f"T{i}", index=i - 1) for i in (1, 2, 3, 4)]
    entry = SimpleNamespace(name="u1", heads=heads)
    args = calibrate.u1_plate_args(
        "o.gcode",
        {
            "--underlay-tool": "T2",
            "--head-colours": "T4=#ffffff,T1=#000000,T2=#EEEEEE,T3=#2D9E59",
        },
        set(),
        entry,  # type: ignore[arg-type]
        date="2026-10-24",
    )
    assert args == [
        "-o", "o.gcode", "--date", "2026-10-24", "--underlay-tool", "T2",
        "--slot-colours", "#000000,#EEEEEE,#2D9E59,#FFFFFF",
    ]  # fmt: skip
    assert calibrate.u1_plate_args("o.gcode", {}, set(), entry) == ["-o", "o.gcode"]  # type: ignore[arg-type]
