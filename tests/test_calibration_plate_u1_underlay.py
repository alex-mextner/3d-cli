"""Tests for calibration_plates.u1_underlay: layout, read_by share and the G-code post-processing
(per-object hooks, hand-written lines, the per-layer tool scan) on synthetic Orca-style G-code."""

from __future__ import annotations

import pytest

from slicing.calibration_plates import u1_underlay as u1

@pytest.fixture(autouse=True)
def _two_layer_underlay():  # type: ignore[no-untyped-def]
    """These synthetic G-codes are the two-layer (U = 0.45) underlay; the default is one layer (#320)."""
    u1.configure(2, 4)
    yield
    u1.configure(1, 4)


CFG = {
    "filament_diameter": ["1.75"],
    "filament_flow_ratio": ["1"],
    "filament_retraction_length": ["1.2"],
}


def _layer(z: float, h: float = 0.2) -> list[str]:
    return [";LAYER_CHANGE", f";Z:{z:g}", f";HEIGHT:{h:g}"]


def _visit(name: str, lines: list[str]) -> list[str]:
    return [
        f"EXCLUDE_OBJECT_START NAME={name}_id_0_copy_0",
        *lines,
        f"EXCLUDE_OBJECT_END NAME={name}_id_0_copy_0",
    ]


def test_layout_is_inside_the_bed_and_does_not_overlap() -> None:
    tests, extra = u1.build_tests("C20261016P1", "2026-10-16")
    ul = extra["underlay"]
    assert ul[0] >= u1.EDGE and ul[2] <= u1.BED - u1.EDGE and ul[3] <= u1.BED - u1.EDGE
    rects = [t.rect for t in tests] + [extra["label_test"].rect]
    for i, a in enumerate(rects):
        assert ul[0] < a[0] and a[2] < ul[2] and ul[1] < a[1] and a[3] < ul[3]
        for b in rects[i + 1 :]:
            assert (
                a[2] <= b[0] + 1e-9
                or b[2] <= a[0] + 1e-9
                or a[3] <= b[1] + 1e-9
                or b[3] <= a[1] + 1e-9
            )


def test_temperature_bands_start_above_every_other_test() -> None:
    tests, _ = u1.build_tests("C20261016P1", "2026-10-16")
    temp = next(t for t in tests if t.name == "temp_tower")
    others = max(t.top for t in tests if t.name != "temp_tower")
    assert temp.bands[0].z0 > others
    assert (
        abs(
            (temp.bands[0].z0 - u1.U) / u1.LAYER_H
            - round((temp.bands[0].z0 - u1.U) / u1.LAYER_H)
        )
        < 1e-6
    )


def test_most_tests_are_read_by_the_top_camera() -> None:
    tests, extra = u1.build_tests("C20261016P1", "2026-10-16")
    share = u1.read_by_share(tests, extra["label_test"])
    assert share["camera_top_share"] >= 0.8
    assert set(share["by"]) <= {u1.TOP, u1.SIDE, u1.MANUAL}


def test_grouped_code_and_bridge_limit() -> None:
    assert u1.grouped("C20261016P1") == "C 2026 1016 P1"
    assert (
        max(u1.BRIDGES) <= 25
    )  # the U1's bridge_max_mm: print-preflight fails longer spans


def test_bands_switch_on_the_layer_of_the_step_and_restore_after() -> None:
    """#301: the band of a layer is the one its mid height lies in; after the visit the slicer's
    ACCEL comes back."""
    t = u1.Test("ring", "ringing", "", (0, 0, 1, 1), u1.SIDE)
    t.bands = [
        u1.Band(0.0, 1.45, ("A1",), lambda s: s),
        u1.Band(1.45, 9.0, ("A2",), lambda s: s),
    ]
    g = ["T0", "SET_VELOCITY_LIMIT ACCEL=5000"]
    g += (
        _layer(1.45)
        + _visit("ring", ["G1 X1 Y1 E0.1"])
        + _layer(1.65)
        + _visit("ring", ["G1 X2 Y1 E0.1"])
    )
    out, log = u1.apply_hooks("\n".join(g), [t], 1.2)
    lines = out.split("\n")
    assert lines[lines.index(";Z:1.45") + 3] == "A1"
    assert lines[lines.index(";Z:1.65") + 3] == "A2"
    assert (
        lines.count("SET_VELOCITY_LIMIT ACCEL=5000") == 3
    )  # the original + one restore per visit
    assert [e["z"] for e in log["ring"]] == [1.45, 1.65]


def test_flow_scales_extrusion_only_on_black_layers() -> None:
    t = u1.Test("flow_92", "flow", "", (0, 0, 1, 1), u1.TOP)
    t.flow = 0.92
    g = (
        ["T3"]
        + _layer(0.25)
        + _visit("flow_92", ["G1 X1 Y1 E1"])
        + ["T0"]
        + _layer(0.65)
        + _visit("flow_92", ["G1 X1 Y1 E1", "G1 E-0.8"])
    )
    out, _ = u1.apply_hooks("\n".join(g), [t], 1.2)
    assert out.count("G1 X1 Y1 E1") == 1  # the white base layer is untouched
    assert "G1 X1 Y1 E0.92000" in out and "G1 E-0.8" in out


def test_retraction_band_carries_its_extra_to_the_next_deretraction() -> None:
    t = u1.Test("retr", "retraction", "", (0, 0, 1, 1), u1.TOP)
    t.retract_bands = [(0.5, 9.0, 2.4)]
    g = (
        ["T0"]
        + _layer(1.05)
        + _visit("retr", ["G1 E1.2", "G1 X1 Y1 E0.1", "G1 E-1.2"])
        + ["G1 X50 Y50", "G1 E1.2"]
    )
    out, _ = u1.apply_hooks("\n".join(g), [t], 1.2)
    lines = out.split("\n")
    assert (
        "G1 E1.2" in lines
    )  # the first de-retraction undoes the previous object's retraction
    assert "G1 E-2.40000" in lines
    assert (
        lines[-1] == "G1 E2.40000"
    )  # the next object de-retracts what the band retracted


def test_hand_written_lines_follow_the_tool_change_in_their_own_objects() -> None:
    tests, _ = u1.build_tests("C20261016P1", "2026-10-16")
    g = [
        "EXCLUDE_OBJECT_DEFINE NAME=frame_50_id_1_copy_0",
        "EXCLUDE_OBJECT_DEFINE NAME=mvs_id_2_copy_0",
        "T3",
    ]
    g += _layer(0.25, 0.25) + ["G1 X10 Y10 E1"] + _layer(0.45) + ["G1 X10 Y10 E1", "T0"]
    for z in (
        0.65,
        0.85,
        1.05,
        1.25,
    ):  # Orca closes a visit after the next layer change
        g += _layer(z) + [
            "G1 E-0.8",
            "EXCLUDE_OBJECT_END NAME=x_id_9_copy_0",
            "EXCLUDE_OBJECT_START NAME=x_id_9_copy_0",
            f"G1 Z{z:.2f}",
            "G1 X20 Y30 E0.5 F3000",
        ]
    out, _, done = u1.splice_hand("\n".join(g), CFG, tests)
    assert done == [0.65, 0.85, 1.05]
    lines = out.split("\n")
    assert lines.count("EXCLUDE_OBJECT_START NAME=mvs_id_2_copy_0") == 3
    assert lines.count("EXCLUDE_OBJECT_START NAME=frame_50_id_1_copy_0") == 1
    first = lines.index("EXCLUDE_OBJECT_START NAME=frame_50_id_1_copy_0")
    assert (
        lines[first - 1] == "EXCLUDE_OBJECT_END NAME=x_id_9_copy_0"
    )  # after the open visit, not in it
    assert out.count("SET_PRESSURE_ADVANCE") == len(u1.PA_VALUES) + 1
    assert f"SET_PRESSURE_ADVANCE ADVANCE={u1.PRINTER_PA:g}" in out
    end = lines.index("; 3d calibrate plate: end of hand-written lines")
    assert "G1 X20.000 Y30.000 F18000" in lines[end - 5 : end]


def test_scan_counts_tool_changes_per_layer_and_the_fan_while_extruding() -> None:
    g = (
        ["T3", "M106 S0"]
        + _layer(0.25, 0.25)
        + ["G1 X1 Y1 E1", "M106 S255"]
        + _layer(0.45)
        + ["G1 X1 Y1 E1"]
        + _layer(0.65)
        + ["T0", "G1 X2 Y2 E1"]
    )
    sc = u1.scan("\n".join(g))
    assert sc["changes"] == [(0, "T-->T3"), (3, "T3->T0")]
    assert sc["layers"][1]["fan"] == [0.0] and sc["layers"][2]["fan"] == [255.0]
    assert sc["layers"][3]["extruding"] == ["T0"]


@pytest.mark.parametrize("cap", [2.0, 3.0])
def test_text_strokes(cap: float) -> None:
    src = u1.text("Ab8", 0, 0, u1.U, cap=cap)
    size = cap / u1.CAP_PER_SIZE
    stroke = u1.STROKE if cap >= 2.5 else u1.THIN_STROKE
    r = max(0.0, (stroke - u1.STEM_PER_SIZE * size) / 2)
    assert f"offset(r={r:.3f})" in src
