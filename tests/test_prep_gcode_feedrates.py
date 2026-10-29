"""The `feedrates` gate: the real F of every move per feature, travel, limits that can FAIL, print facts."""

from __future__ import annotations

from pathlib import Path

import pytest
from k1se_fixtures import FEATURE_SPEEDS, TRAVEL_MM_S, k1se_gcode, with_feedrates

from prep import gcode_gates
from prep.gcode_gates import GateSpec, check_gcode, parse

SPEC = GateSpec(walls=None, infill=None)


def gcode(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "part.gcode"
    path.write_text("M83\n" + text, encoding="utf-8")  # relative extrusion, so every E > 0 move extrudes
    return path


def test_the_max_feedrate_per_feature_is_read_from_the_moves(tmp_path: Path) -> None:
    parsed = parse(gcode(tmp_path, k1se_gcode()))
    assert parsed.feed_max["Outer wall"] == pytest.approx(250.0)
    assert parsed.feed_max["Inner wall"] == pytest.approx(450.0)
    assert parsed.feed_max["Sparse infill"] == pytest.approx(376.0)
    assert parsed.feed_max["travel"] == pytest.approx(TRAVEL_MM_S)


def test_the_fastest_move_of_a_feature_wins_not_the_last_or_the_first(
    tmp_path: Path,
) -> None:
    text = "\n".join(
        [
            ";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2", ";TYPE:Outer wall",
            "G1 F6000", "G1 X10 Y10 E1", "G1 F15000", "G1 X20 Y10 E1", "G1 F3000", "G1 X30 Y10 E1",
        ]
    )  # fmt: skip
    assert parse(gcode(tmp_path, text + "\n")).feed_max == {"Outer wall": 250.0}


def test_start_gcode_z_hops_and_unretracts_are_not_feature_speeds(
    tmp_path: Path,
) -> None:
    text = "\n".join(
        [
            "G1 X5 Y5 E5 F30000",  # purge line before the first layer
            ";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2", "G1 Z.6 F36000", "G1 E.8 F2400", ";TYPE:Outer wall",
            "G1 F6000", "G1 X10 Y10 E1", "G1 X20 Y10 E-.4 F18000",
        ]
    )  # fmt: skip
    feed = parse(gcode(tmp_path, text + "\n")).feed_max
    assert feed == {"Outer wall": 100.0, "wipe": 300.0}


def test_end_gcode_parking_moves_are_not_print_travel(tmp_path: Path) -> None:
    text = "\n".join(
        [
            ";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2", ";TYPE:Outer wall",
            "G1 F6000", "G1 X10 Y10 E1", "G1 X20 Y20 F12000",
            ";TYPE:Custom", "G1 X0 Y200 F30000",
        ]
    )  # fmt: skip
    feed = parse(gcode(tmp_path, text + "\n")).feed_max
    assert feed == {"Outer wall": 100.0, "travel": 200.0}


def test_the_feature_comment_of_other_slicers_is_understood(tmp_path: Path) -> None:
    text = "\n".join(
        [
            ";LAYER_CHANGE",
            ";Z:0.2",
            ";HEIGHT:0.2",
            "; FEATURE: Inner wall",
            "G1 F12000",
            "G1 X10 Y10 E1",
        ]
    )
    parsed = parse(gcode(tmp_path, text + "\n"))
    assert parsed.feed_max == {"Inner wall": 200.0} and parsed.types == {
        "Inner wall": 1
    }


def test_without_limits_the_gate_reports_the_speeds(tmp_path: Path) -> None:
    rep = check_gcode(gcode(tmp_path, k1se_gcode()), SPEC)
    g = rep.gate("feedrates")
    assert g.verdict == "PASS"
    assert (
        "Outer wall 250" in g.detail
        and "Inner wall 450" in g.detail
        and "travel 600" in g.detail
    )
    assert rep.stats["feed_max_mm_s"]["Sparse infill"] == 376.0


def test_a_feature_faster_than_requested_fails_and_names_it(tmp_path: Path) -> None:
    spec = GateSpec(
        walls=None, infill=None, speed_limits={"Outer wall": 200.0, "Inner wall": 500.0}
    )
    g = check_gcode(gcode(tmp_path, k1se_gcode()), spec).gate("feedrates")
    assert g.verdict == "FAIL"
    assert (
        "Outer wall 250 > 200" in g.detail
        and "Inner wall" not in g.detail.split("max per feature")[0]
    )


def test_speeds_within_the_limits_pass_and_a_capped_feature_is_listed(
    tmp_path: Path,
) -> None:
    limits = {
        "Outer wall": 250.0,
        "Inner wall": 500.0,
        "Sparse infill": 550.0,
        "travel": 600.0,
    }
    g = check_gcode(
        gcode(tmp_path, k1se_gcode()),
        GateSpec(walls=None, infill=None, speed_limits=limits),
    ).gate("feedrates")
    assert g.verdict == "PASS"
    # 376 of 550 is below 90 %: the volumetric limit capped it; 450 of 500 is exactly 90 %: not listed
    assert (
        "volumetric cap, cornering, layer time): Sparse infill 376 of 550" in g.detail
        and "Inner wall 450 of" not in g.detail
    )


def test_the_rounding_of_f_to_whole_mm_per_min_is_tolerated(tmp_path: Path) -> None:
    text = with_feedrates(k1se_gcode(), {**FEATURE_SPEEDS, "Outer wall": 250.1})
    spec = GateSpec(walls=None, infill=None, speed_limits={"Outer wall": 250.0})
    assert check_gcode(gcode(tmp_path, text), spec).gate("feedrates").verdict == "PASS"


def test_a_gcode_without_any_f_skips_the_gate_naming_why(tmp_path: Path) -> None:
    text = "\n".join(
        [
            ";LAYER_CHANGE",
            ";Z:0.2",
            ";HEIGHT:0.2",
            ";TYPE:Outer wall",
            "G1 X10 Y10 E1",
            "G1 X20 Y10 E1",
        ]
    )
    g = check_gcode(gcode(tmp_path, text + "\n"), SPEC).gate("feedrates")
    assert g.verdict == "SKIP" and "feedrate" in g.detail


def test_the_summary_carries_time_grams_and_the_extrusion_extent(
    tmp_path: Path,
) -> None:
    rep = check_gcode(gcode(tmp_path, k1se_gcode()), SPEC)
    s = rep.summary()
    assert "32m 51s" in s and "13.56 g" in s and "x 90" in s and " y 90" in s
    assert rep.stats["time_s"] == 32 * 60 + 51 and rep.stats["filament_g"] == [13.56]


def test_the_spec_carries_the_limits_into_the_report(tmp_path: Path) -> None:
    spec = GateSpec(walls=None, infill=None, speed_limits={"travel": 600.0})
    assert check_gcode(gcode(tmp_path, k1se_gcode()), spec).to_dict()["spec"][
        "speed_limits"
    ] == {"travel": 600.0}
    assert gcode_gates.GateSpec().to_dict()["speed_limits"] is None


def test_the_overrides_gate_confirms_settings_the_config_block_echoes(tmp_path: Path) -> None:
    text = k1se_gcode() + '; wall_loops = 3\n; filament_settings_id = "Elegoo RAPID PLA+ @K1-all"\n; pressure_advance = 0.04\n'
    want = {"wall_loops": "3", "filament_settings_id": "Elegoo RAPID PLA+ @K1-all", "pressure_advance": "0.040"}
    g = check_gcode(gcode(tmp_path, text), GateSpec(walls=None, infill=None, expect_settings=want)).gate("overrides")
    assert g.verdict == "PASS" and "3 requested" in g.detail


def test_the_overrides_gate_fails_on_a_missing_key_and_on_another_value(tmp_path: Path) -> None:
    text = k1se_gcode() + "; wall_loops = 2\n; outer_wall_speed = 250\n"
    want = {"wall_loops": "3", "outer_wal_speed": "250", "outer_wall_speed": "250"}
    g = check_gcode(gcode(tmp_path, text), GateSpec(walls=None, infill=None, expect_settings=want)).gate("overrides")
    assert g.verdict == "FAIL"
    assert "outer_wal_speed" in g.detail and "wall_loops = 2 (asked 3)" in g.detail and "outer_wall_speed =" not in g.detail


def test_the_overrides_gate_skips_when_nothing_was_asked_or_there_is_no_config_block(tmp_path: Path) -> None:
    assert check_gcode(gcode(tmp_path, k1se_gcode()), SPEC).gate("overrides").verdict == "SKIP"
    bare = ";LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\nG1 F6000\nG1 X10 Y10 E1\n"
    spec = GateSpec(walls=None, infill=None, expect_settings={"wall_loops": "3"})
    g = check_gcode(gcode(tmp_path, bare), spec).gate("overrides")
    assert g.verdict == "SKIP" and "config block" in g.detail
