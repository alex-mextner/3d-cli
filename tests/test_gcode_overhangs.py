"""Overhang analysis of sliced G-code (lib/gcode_overhangs.py) on synthetic plates and, when
present, the real babushkin-dom K1 SE plates it was calibrated on."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from gcode_overhangs import format_lines, run_check
from overhang_fixtures import SQUARE, bridge, floating, plate, step_out, tip

K1 = Path.home() / "xp/garage-band/projects/babushkin-dom/print/k1se"


def _objects(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["name"]: o for o in report["objects"]}


def test_a_slab_on_four_bumps_prints_in_the_air_and_fails(tmp_path: Path) -> None:
    report = run_check(str(plate(tmp_path / "p.gcode", {"OVL": floating()})), png=None)
    ovl = _objects(report)["OVL"]
    assert report["status"] == "FAIL"
    # the 400 mm2 slab minus the bumps grown by the 45-degree reach, minus the edge strips
    # between two bumps (a < 15 mm span anchored at both ends counts as a bridge)
    assert ovl["status"] == "FAIL" and 150 < ovl["unsupported_mm2"] < 400
    assert {i["z"] for i in ovl["islands"] if i["kind"] == "island"} == {0.4}
    assert "OVL" in report["detail"] and "print on nothing" in report["detail"]


def test_an_edge_one_millimetre_wider_is_a_step_out_warning(tmp_path: Path) -> None:
    report = run_check(str(plate(tmp_path / "p.gcode", {"BOX": step_out()})), png=None)
    box = _objects(report)["BOX"]
    assert report["status"] == "WARN", report["detail"]
    assert box["unsupported_mm2"] == 0 and 5 < box["ledge_mm2"] < 20
    assert "edge step-outs" in box["detail"]


def test_a_slab_across_two_walls_is_a_bridge_and_passes(tmp_path: Path) -> None:
    report = run_check(str(plate(tmp_path / "p.gcode", {"SPAN": bridge(gap=10.0)})), png=None)
    assert report["status"] == "PASS", report["detail"]
    assert _objects(report)["SPAN"]["detail"].startswith("every line carried")


def test_a_span_longer_than_bridge_max_is_not_a_bridge(tmp_path: Path) -> None:
    gcode = str(plate(tmp_path / "p.gcode", {"SPAN": bridge(gap=10.0)}))
    report = run_check(gcode, png=None, bridge_max=5.0)
    span = _objects(report)["SPAN"]
    assert span["status"] == "FAIL" and span["unsupported_mm2"] > 100  # ~10 x 20 mm minus the reach


def test_a_tip_smaller_than_min_area_is_ignored(tmp_path: Path) -> None:
    gcode = str(plate(tmp_path / "p.gcode", {"TUFT": tip()}))
    report = run_check(gcode, png=None)
    assert report["status"] == "PASS", report["detail"]
    finer = _objects(run_check(gcode, png=None, min_area=0.5))["TUFT"]
    assert finer["status"] == "WARN" and finer["island_count"] == 1


def test_only_the_floating_object_of_a_plate_is_named(tmp_path: Path) -> None:
    shifted = [[(x0 + 40, y0, x1 + 40, y1) for x0, y0, x1, y1 in layer] for layer in floating()]
    report = run_check(str(plate(tmp_path / "p.gcode", {"GOOD": [[SQUARE]] * 6, "BAD": shifted})), png=None)
    objs = _objects(report)
    assert objs["GOOD"]["status"] == "PASS" and objs["BAD"]["status"] == "FAIL"
    assert "BAD" in report["detail"] and "GOOD" not in report["detail"]


def test_the_map_is_a_png_and_the_lines_point_to_it(tmp_path: Path) -> None:
    png = tmp_path / "map.png"
    report = run_check(str(plate(tmp_path / "p.gcode", {"OVL": floating()})), png=str(png))
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    lines = format_lines(report)
    assert lines[0].startswith("FAIL OVL:") and "islands: island z0.4" in lines[1]
    assert lines[-1] == f"map: {png}"


def test_a_gcode_without_extrusion_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "empty.gcode").write_text("G28\nG1 X10 Y10\n", encoding="utf-8")
    assert run_check(str(tmp_path / "empty.gcode"), png=None)["status"] == "SKIP"


@pytest.mark.parametrize(
    ("name", "status"),
    [("k1_ovl1_v3", "FAIL"), ("k1_base_3_v3", "WARN")],
)
def test_real_k1_plates_keep_their_calibrated_verdict(name: str, status: str) -> None:
    """The overlay whose underside printed in the air FAILs; BASE3's small ceilings WARN."""
    gcode = K1 / f"{name}.gcode"
    if not gcode.is_file():
        pytest.skip(f"{gcode} not on this machine")
    report = run_check(str(gcode), png=None)
    assert report["status"] == status, report["detail"]


def test_a_30_mb_plate_is_checked_in_seconds() -> None:
    gcode = K1 / "k1_base_4_v3.gcode"
    if not gcode.is_file():
        pytest.skip(f"{gcode} not on this machine")
    t = time.monotonic()
    report = run_check(str(gcode), png=None)
    assert report["status"] == "WARN"
    assert time.monotonic() - t <= 10.0
