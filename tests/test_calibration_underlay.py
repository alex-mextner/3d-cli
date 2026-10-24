"""Tests for slicing.calibration_underlay: the shared one-piece underlay's geometry, layer rule,
declarations, contrast pick and the calibrations.underlay_layers policy key (#320)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from printops import calibration_policy, policy
from slicing import calibration_underlay as ul
from slicing.calibration_gen import GenError


def _build(**kw: Any) -> ul.Underlay:
    args: dict[str, Any] = {
        "first_h": 0.2,
        "layer_h": 0.2,
        "layers": 1,
        "footprints": [(10, 10, 20, 20)],
    }
    args.update(kw)
    return ul.build_underlay(**args)


# ---- layers ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 3, -1, 1.5, "1", True, None])
def test_layers_outside_one_or_two_are_refused(bad: object) -> None:
    with pytest.raises(GenError, match="1 or 2"):
        ul.validate_layers(bad)


def test_single_head_prints_exactly_one_layer_whatever_the_policy_says() -> None:
    assert ul.layers_for(1, 1) == 1
    assert ul.layers_for(1, 2) == 1
    assert ul.layers_for(4, 1) == 1
    assert ul.layers_for(4, 2) == 2
    with pytest.raises(GenError, match="1 or 2"):
        ul.layers_for(4, 3)


def test_underlay_height_is_the_first_layer_plus_the_further_ones() -> None:
    assert ul.underlay_height(0.2, 0.2, 1) == 0.2
    assert ul.underlay_height(0.2, 0.2, 2) == 0.4
    assert ul.underlay_height(0.25, 0.2, 2) == 0.45


# ---- outline, cut-outs, one piece --------------------------------------------------------------


def test_outline_is_the_union_of_footprints_and_blocks_with_a_3_mm_margin() -> None:
    u = _build(footprints=[(10, 10, 20, 20), (30, 10, 40, 20)], blocks=[(10, 2, 40, 6)])
    assert u.bbox() == (7.0, -1.0, 43.0, 23.0)
    ring = {(x, y) for x in (8, 42) for y in (0, 5, 12, 22)} | {
        (25, 0),
        (25, 5),
    }  # inside the margin
    for x, y in ring:
        assert u.covers((x - 0.1, y - 0.1, x + 0.1, y + 0.1))
    assert not u.covers(
        (24.9, 21.9, 25.1, 22.1)
    )  # the gap between the tests, beyond their margins
    assert not u.covers((5.0, 12.0, 6.5, 13.0))  # beyond the 3 mm margin


def test_far_apart_groups_are_joined_into_one_piece() -> None:
    u = _build(
        footprints=[
            (10, 10, 20, 20),
            (60, 10, 70, 20),
            (10, 60, 20, 70),
            (60, 60, 70, 70),
        ]
    )
    assert u.pieces == 1
    diagonal = _build(footprints=[(10, 10, 20, 20), (50, 50, 60, 60)])
    assert diagonal.pieces == 1
    assert diagonal.covers((30, 15, 31, 16)) or diagonal.covers(
        (15, 30, 16, 31)
    )  # the L's strips


def test_the_one_connected_polygon_survives_the_cutouts() -> None:
    fp = [(10, 10, 20, 20), (24, 10, 34, 20), (38, 10, 48, 20), (10, 24, 48, 34)]
    u = _build(footprints=fp, cutouts=[fp[1]], bases=[fp[0], fp[2], fp[3]])
    assert u.pieces == 1
    # the cut-out is a hole of its footprint grown by 1 mm; nothing of the sheet is inside it
    assert not u.overlaps_sheet(fp[1])
    assert not u.overlaps_sheet((23, 9, 35, 21))
    assert u.overlaps_sheet(
        (9.9, 10, 10.1, 11)
    )  # the sheet is right at the neighbour's foot
    # bare cells between the tests are the first-layer map: the sheet, minus the feet
    for foot in (fp[0], fp[2], fp[3]):
        inner = (foot[0] + 0.5, foot[1] + 0.5, foot[2] - 0.5, foot[3] - 0.5)
        assert not any(_inside(inner, r) for r in u.sheet)
    assert u.area_mm2 > 0


def _inside(a: ul.Rect, b: ul.Rect) -> bool:
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def test_a_cutout_that_splits_the_underlay_is_refused() -> None:
    # a cut-out spanning the whole width of the outline cuts the plate in two
    with pytest.raises(GenError, match="falls into 2 pieces"):
        _build(
            footprints=[(10, 10, 60, 20), (10, 30, 60, 40)],
            cutouts=[(7, 19.0, 63, 31.0)],
        )


def test_a_cutout_never_removes_a_hand_written_block() -> None:
    u = _build(
        footprints=[(10, 10, 20, 20)],
        blocks=[(20.5, 10, 30, 20)],
        cutouts=[(10, 10, 20, 20)],
    )
    # the block keeps its sheet right up to the cut-out's own footprint, not the 1 mm margin
    assert u.overlaps_sheet((21, 10, 29, 20))
    with pytest.raises(GenError, match="print in the air"):
        _build(
            footprints=[(10, 10, 20, 20)],
            blocks=[(15, 15, 30, 30)],
            cutouts=[(10, 10, 20, 20)],
        )


def test_the_sheet_overlaps_each_foot_by_the_seam() -> None:
    u = _build(bases=[(10, 10, 20, 20)])
    ((x0, y0, z0, x1, y1, z1),) = u.foot_boxes()
    assert (x0, y0, x1, y1) == (10, 10, 20, 20) and (z0, z1) == (0.0, 0.2)
    # no sheet inside the foot except the SEAM-wide ring just inside its edge
    assert not any(_inside((11, 11, 19, 19), r) for r in u.sheet)
    ring = [
        r
        for r in u.sheet
        if r[2] > 10 and r[0] < 10.0 + ul.SEAM and r[1] < 20 and r[3] > 10
    ]
    assert ring or u.overlaps_sheet((10.01, 12, 10.04, 14))


def test_the_outline_must_stay_on_the_bed() -> None:
    with pytest.raises(GenError, match="leaves the usable bed"):
        _build(footprints=[(1, 10, 20, 20)], bed=(0, 0, 220, 220))
    assert _build(footprints=[(3, 10, 20, 20)], bed=(0, 0, 220, 220)).pieces == 1


def test_manifest_block_describes_the_underlay() -> None:
    u = _build(layers=2, cutouts=[(10, 10, 20, 20)])
    block = u.manifest(head="T1", light="raking")
    assert block["layers"] == 2 and block["top_mm"] == 0.4 and block["pieces"] == 1
    assert block["head"] == "T1" and block["read"]["light"] == "raking"
    assert block["cutouts"] and block["bare"] and block["area_mm2"] > 0
    assert "first_layer_map" in block["read"] and "uniformity" in block["read"]


# ---- declarations ----------------------------------------------------------------------------


def test_every_declaration_has_a_mode_and_a_reason() -> None:
    with pytest.raises(GenError, match="on or cutout"):
        ul.UnderlayDecl("sometimes", "why")  # type: ignore[arg-type]
    with pytest.raises(GenError, match="needs a reason"):
        ul.UnderlayDecl("on", "  ")
    with pytest.raises(GenError, match="must be a cutout"):
        ul.UnderlayDecl("on", "why", movable=True)
    assert ul.UnderlayDecl("cutout", "why", movable=True).movable


def test_declarations_must_match_the_tests() -> None:
    ok = {"a": ul.UnderlayDecl("on", "x"), "b": ul.UnderlayDecl("cutout", "y")}
    ul.check_declarations(["a", "b"], ok)
    with pytest.raises(GenError, match="missing c"):
        ul.check_declarations(["a", "b", "c"], ok)
    with pytest.raises(GenError, match="unknown b"):
        ul.check_declarations(["a"], ok)


# ---- lifting ---------------------------------------------------------------------------------


def test_shift_z_lifts_moves_and_layer_marks_only() -> None:
    lines = [
        ";Z:0.4",
        "G1 Z0.6 F600",
        "G1 X10 Y10 Z0.2 E.5 ; Z0.2 stays in the comment",
        "G1 X10 Y20 E.5",
        "G1 E-.8 F2400",
        "SET_VELOCITY_LIMIT ACCEL=5000",
        "M106 S255",
    ]
    out = ul.shift_z(lines, 0.2)
    assert out[0] == ";Z:0.600" and out[1] == "G1 Z0.800 F600"
    assert out[2] == "G1 X10 Y10 Z0.400 E.5 ; Z0.2 stays in the comment"
    assert out[3:] == lines[3:]


def test_shift_stl_and_merge_stl_keep_one_ascii_solid() -> None:
    from slicing.calibration_gen import boxes_stl

    a = boxes_stl([(0, 0, 0, 1, 1, 1)])
    lifted = ul.shift_stl(a, 0.2)
    assert (
        "vertex 0 0 0.2" in lifted
        and "vertex 1 1 1.2" in lifted
        and "vertex 0 0 0\n" not in lifted
    )
    merged = ul.merge_stl(lifted, boxes_stl([(0, 0, 0, 1, 1, 0.2)]))
    assert merged.startswith("solid merged") and merged.strip().endswith(
        "endsolid merged"
    )
    assert merged.count("solid") == 2 and merged.count("facet normal") == 24
    with pytest.raises(GenError, match="ASCII STL"):
        ul.merge_stl("\x00binary")


# ---- the head of a multi-head underlay -----------------------------------------------------------


COLOURS = {"T1": "#000000", "T2": "#FFFFFF", "T3": "#2D9E59", "T4": "#FFFFFF"}


def test_underlay_tool_is_the_most_contrasting_head() -> None:
    tool, de = ul.pick_underlay_tool(COLOURS, "T1")
    assert tool in ("T2", "T4") and de > 90
    # light under dark picks the white head even when a mid-grey is also loaded
    tool, _ = ul.pick_underlay_tool({**COLOURS, "T3": "#777777"}, "T1")
    assert tool in ("T2", "T4")


def test_underlay_tool_override_goes_through_the_contrast_check() -> None:
    assert ul.pick_underlay_tool(COLOURS, "T1", "T4")[0] == "T4"
    with pytest.raises(GenError, match="contrast too low"):
        ul.pick_underlay_tool({**COLOURS, "T3": "#050505"}, "T1", "T3")
    with pytest.raises(GenError, match="own head"):
        ul.pick_underlay_tool(COLOURS, "T1", "T1")
    with pytest.raises(GenError, match="no such head"):
        ul.pick_underlay_tool(COLOURS, "T1", "T9")
    with pytest.raises(GenError, match="not loaded"):
        ul.pick_underlay_tool(COLOURS, "T9")
    with pytest.raises(GenError, match="second loaded head"):
        ul.pick_underlay_tool({"T1": "#000000"}, "T1")


def test_a_pair_that_cannot_be_read_is_refused_even_as_the_best_choice() -> None:
    with pytest.raises(GenError, match="contrast too low"):
        ul.pick_underlay_tool({"T1": "#101010", "T2": "#202020"}, "T1")


# ---- calibrations.underlay_layers ----------------------------------------------------------------


def _levels() -> dict[str, str]:
    return dict.fromkeys(calibration_policy.REASONS, "warn")


def test_policy_underlay_layers_defaults_to_one_and_takes_one_or_two() -> None:
    assert calibration_policy.parse({"levels": _levels()}).underlay_layers == 1
    assert (
        calibration_policy.parse(
            {"levels": _levels(), "underlay_layers": 2}
        ).underlay_layers
        == 2
    )
    assert (
        calibration_policy.parse(
            {"levels": _levels(), "underlay_layers": 1}
        ).underlay_layers
        == 1
    )


@pytest.mark.parametrize("bad", [0, 3, -1, 1.5, "2", True, None, [1]])
def test_policy_underlay_layers_outside_one_or_two_is_an_error(bad: object) -> None:
    with pytest.raises(policy.PolicyError, match="underlay_layers must be 1 or 2"):
        calibration_policy.parse({"levels": _levels(), "underlay_layers": bad})


def test_committed_policy_sets_the_default_underlay_layers() -> None:
    assert policy.section("calibrations").underlay_layers == 1


def test_lint_reports_a_bad_underlay_layers(tmp_path: Path) -> None:
    text = Path(policy.policy_path()).read_text()
    assert "underlay_layers: 1" in text
    bad = tmp_path / "policy.yaml"
    bad.write_text(text.replace("underlay_layers: 1", "underlay_layers: 3"))
    problems = policy.check(bad)
    assert any("calibrations: underlay_layers must be 1 or 2" in p for p in problems), (
        problems
    )


# ---- the heads of `gen` on a multi-head printer ---------------------------------------------------


HEADS = [("T1", 0), ("T2", 1), ("T3", 2), ("T4", 3)]


def test_plan_heads_picks_the_contrasting_head_and_maps_it_to_its_tool() -> None:
    label, tool, de = ul.plan_heads(
        HEADS, "T1", {"T1": "#000000", "T3": "#777777", "T4": "#FFFFFF"}
    )
    assert (label, tool) == ("T4", "T3") and de > 90
    assert ul.plan_heads(HEADS, "T1", {"T1": "#000000", "T2": "#FFFFFF"}, "T2")[:2] == (
        "T2",
        "T1",
    )


def test_plan_heads_refuses_what_it_cannot_check() -> None:
    with pytest.raises(GenError, match="no such head"):
        ul.plan_heads(HEADS, "T9", {"T9": "#000000"})
    with pytest.raises(GenError, match="no such head"):
        ul.plan_heads(HEADS, "T1", {"T1": "#000000", "T7": "#FFFFFF"})
    with pytest.raises(GenError, match="no such head"):
        ul.plan_heads(HEADS, "T1", {"T1": "#000000"}, "T7")
    with pytest.raises(GenError, match="tests' head T1 has no colour"):
        ul.plan_heads(HEADS, "T1", {"T4": "#FFFFFF"})
    with pytest.raises(GenError, match="needs its colour"):
        ul.plan_heads(HEADS, "T1", {"T1": "#000000"}, "T4")
    with pytest.raises(GenError, match="colours loaded on the heads"):
        ul.plan_heads(HEADS, "T1", {"T1": "#000000"})
    with pytest.raises(GenError, match="contrast too low"):
        ul.plan_heads(HEADS, "T1", {"T1": "#000000", "T4": "#080808"}, "T4")
