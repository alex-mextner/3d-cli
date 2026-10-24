"""Tests for the U1 plate's underlay settings (#320): 1–2 layers from `calibrations.underlay_layers`,
the head chosen by the contrast check of #263 or `--underlay-tool`, no brim, and the G-code order
(underlay head first on its own layers, one tool change, tests from the layer above) on synthetic
Orca-style G-code."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from slicing.calibration_gen import GenError
from slicing.calibration_underlay import piece_count
from slicing.calibration_plates import u1_underlay as u1


@pytest.fixture(autouse=True)
def _restore_module_state() -> Iterator[None]:
    colours = list(u1.SLOT_COLOURS)
    yield
    u1.SLOT_COLOURS[:] = colours
    u1.configure(1, 4)


def _layer(z: float, h: float = 0.2) -> list[str]:
    return [";LAYER_CHANGE", f";Z:{z:g}", f";HEIGHT:{h:g}"]


def test_default_is_one_layer_in_t4_without_a_brim() -> None:
    assert u1.UNDERLAY_LAYERS == 1 and pytest.approx(0.25) == u1.U and u1.WHITE == 4
    assert u1.UNDERLAY_BRIM == 0.0
    assert (
        u1.PROCESS_OVERRIDES["brim_type"] == "no_brim"
        and "brim_width" not in u1.PROCESS_OVERRIDES
    )
    assert u1.FILAMENT_OVERRIDES["close_fan_the_first_x_layers"] == "1"


def test_two_layers_lift_the_tests_and_the_tab_and_keep_the_fan_off_on_both() -> None:
    u1.configure(2, 4)
    assert pytest.approx(0.45) == u1.U
    assert u1.TAB[2] == pytest.approx(0.45 + 7 * u1.LAYER_H)
    assert u1.FILAMENT_OVERRIDES["close_fan_the_first_x_layers"] == "2"
    tests, _ = u1.build_tests("C20261016P1", "2026-10-16")
    assert all(
        t.top >= u1.U for t in tests
    )  # nothing black starts under the underlay's top


@pytest.mark.parametrize("bad", [0, 3, -1])
def test_layers_outside_one_or_two_are_refused(bad: int) -> None:
    with pytest.raises(GenError, match="1 or 2"):
        u1.configure(bad, 4)


def test_the_underlay_head_cannot_be_the_tests_head() -> None:
    with pytest.raises(GenError, match="T1 prints the tests"):
        u1.configure(1, 1)
    with pytest.raises(GenError, match="T2, T3, T4"):
        u1.configure(1, 5)


def test_the_contrast_check_picks_the_most_contrasting_head_and_t4_on_a_tie() -> None:
    delta = u1.setup_underlay(None, None, None)
    assert u1.WHITE == 4 and delta > 90  # T2 and T4 are both white: the later head wins
    assert u1.setup_underlay(
        2, None, "#000000,#FFFFFF,#2D9E59,#101010"
    ) == pytest.approx(
        u1.pick_underlay_tool({"T1": "#000000", "T2": "#FFFFFF"}, "T1")[1], abs=1e-6
    )
    assert u1.WHITE == 2 and u1.UNDERLAY_LAYERS == 2


def test_underlay_tool_override_is_checked_too() -> None:
    assert u1.setup_underlay(1, "T2", None) > 90 and u1.WHITE == 2
    assert (
        u1.setup_underlay(1, "3", None) > 40 and u1.WHITE == 3
    )  # the green spool still contrasts
    with pytest.raises(GenError, match="own head"):
        u1.setup_underlay(1, "T1", None)
    with pytest.raises(GenError, match="contrast too low"):
        u1.setup_underlay(1, "T3", "#000000,#FFFFFF,#0A0A0A,#FFFFFF")
    with pytest.raises(GenError, match="wants T2, T3 or T4"):
        u1.setup_underlay(1, "head 4", None)
    with pytest.raises(GenError, match="four #RRGGBB"):
        u1.setup_underlay(1, None, "#000000,#FFFFFF")


def test_the_policy_gives_the_default_layers() -> None:
    assert u1.policy_layers() == 1


def test_cli_refuses_a_bad_layer_count_and_tool_with_exit_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "plate.gcode"
    assert u1.main(["-o", str(out), "--underlay-layers", "3", "--preview-only"]) == 2
    assert "1 or 2" in capsys.readouterr().err
    assert u1.main(["-o", str(out), "--underlay-tool", "T1", "--preview-only"]) == 2
    assert "own head" in capsys.readouterr().err
    assert not out.exists()


def _gcode(layers: int, tool: int = 3) -> str:
    """A sliced plate as the U1 prints it: the underlay head on its own layers, one tool change, the
    tests' head above."""
    g = [f"T{tool}", "M106 S0"]
    z = 0.25
    for i in range(layers):
        g += _layer(z, 0.25 if i == 0 else 0.2) + ["G1 X1 Y1 E1"]
        z += 0.2
    g += ["T0"]
    for _ in range(3):
        g += _layer(z) + ["G1 X2 Y2 E1"]
        z += 0.2
    return "\n".join(g)


@pytest.mark.parametrize("layers", [1, 2])
@pytest.mark.parametrize("tool", [1, 2, 3])
def test_gcode_order_underlay_head_first_one_tool_change_tests_from_the_layer_above(
    layers: int, tool: int
) -> None:
    sc = u1.scan(_gcode(layers, tool))
    assert sc["changes"] == [(0, f"T-->T{tool}"), (layers, f"T{tool}->T0")]
    under = list(sc["layers"][1 : layers + 1])
    assert all(
        L["extruding"] == [f"T{tool}"] for L in under
    )  # only the underlay head, no test yet
    assert all(L["extruding"] == ["T0"] for L in sc["layers"][layers + 1 :])
    assert [L["z"] for L in under][-1] == pytest.approx(0.25 + 0.2 * (layers - 1))


def test_manifest_block_for_the_u1_names_head_contrast_and_light(
    tmp_path: Path,
) -> None:
    u1.setup_underlay(2, "T4", None)
    tests, extra = u1.build_tests("C20261016P1", "2026-10-16")
    ul, label = extra["underlay"], extra["label_test"]
    bare = u1.rect_difference(ul, [t.rect for t in tests] + [label.rect])
    assert bare and all(r[2] > r[0] and r[3] > r[1] for r in bare)
    # the sheet, the test bases and the label strip are one rectangle: one piece, no gap
    assert piece_count([*bare, *(t.rect for t in tests), label.rect]) == 1
    assert u1.head_line(u1.WHITE) == "T4  белый  #1  Snapmaker PLA SnapSpeed"
    assert u1.head_line(u1.BLACK) == "T1  чёрный  #2  Snapmaker PLA SnapSpeed"
    u1.SLOT_COLOURS[2] = "#2D9E59"
    assert u1.head_line(3) == "T3  #2D9E59  Snapmaker PLA SnapSpeed"
    json.dumps({"bare": bare})  # serialisable into the manifest
