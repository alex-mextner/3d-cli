"""Unit tests for lib/gcode_resume.py and the `resume:` block of printer-devices.yaml (issue #138).

The resume G-code is checked by gcode_resume_fixtures.check_resume, an independent re-reading
of the output (Z lifted before any X/Y move, nothing below the resume layer, objects balanced,
no tower left with --no-tower), on a synthetic plate and on the real U1 plate when present."""
from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest
import yaml

import gcode_resume as gr
from errors import GateFailure, ThreeDError, UsageError
from gcode_resume_fixtures import LAYER_H, LAYERS, REAL_GCODE, U1_RESUME_YAML, check_resume, synthetic_gcode
from printer_ops import devices
from printer_ops.devices import ResumeProfile


def _u1_profile() -> ResumeProfile:
    profile = devices._parse(Path("printer-devices.yaml"), yaml.safe_load(U1_RESUME_YAML))["u1"].resume
    assert profile is not None
    return profile


PROFILE = _u1_profile()


def _walk() -> gr.Walk:
    return gr.Walk(synthetic_gcode().encode().split(b"\n"))


def _text(res: gr.Resume) -> str:
    return b"\n".join(res.lines).decode()


def test_example_yaml_carries_the_u1_resume_profile() -> None:
    parsed = devices._parse(Path("x.yaml"), yaml.safe_load(devices.EXAMPLE))
    assert parsed["u1"].resume == PROFILE
    assert parsed["k1se"].resume is None


def test_resume_block_must_have_every_key() -> None:
    data = yaml.safe_load(U1_RESUME_YAML)
    del data["u1"]["resume"]["purge_mm"]
    with pytest.raises(ThreeDError, match="purge_mm"):
        devices._parse(Path("x.yaml"), data)


@pytest.mark.parametrize(("key", "value"), [("purge_mm", -1), ("purge_mm", True), ("purge_macro", "lower"),
                                            ("start_commands", "DEFECT_DETECTION_START")])
def test_resume_block_rejects_bad_values(key: str, value: object) -> None:
    data = yaml.safe_load(U1_RESUME_YAML)
    data["u1"]["resume"][key] = value
    with pytest.raises(ThreeDError, match=key):
        devices._parse(Path("x.yaml"), data)


def test_resume_passes_the_independent_checks() -> None:
    res = gr.build(_walk(), PROFILE, name="plate.gcode", layer=5)
    zr = 5 * LAYER_H
    check = check_resume(_text(res), zr)
    assert check.problems == []
    assert check.z_travel == pytest.approx(zr + 0.6 + PROFILE.clearance_mm)
    assert check.tower_extrusions > 0
    assert (res.layer, res.kept_layer, res.tools) == (5, 4, [0, 1])


def test_header_uses_the_profile_values() -> None:
    text = _text(gr.build(_walk(), PROFILE, name="plate.gcode", layer=5))
    header = text[text.index("; ===== RESUME of"):text.index(gr.HEADER_END)]
    assert "DEFECT_DETECTION_START" in header
    assert "SM_PRINT_PREEXTRUDE_FILAMENT INDEX=0 FORCE=1 TEMP=210 LENGTH=30 RETRACT_LENGTH=0.5" in header
    assert "SM_PRINT_PREEXTRUDE_FILAMENT INDEX=1 FORCE=1 TEMP=215 LENGTH=30 RETRACT_LENGTH=0.5" in header
    assert "M204 S10000" in header                    # the plate sets no M204: profile default_accel
    assert "G28" not in header
    other = ResumeProfile("MY_PURGE", 7, 0.25, 3, 9, 1, 1.75, 170, 4000, ())
    text2 = _text(gr.build(_walk(), other, name="plate.gcode", layer=5))
    assert "MY_PURGE INDEX=0 FORCE=1 TEMP=210 LENGTH=7 RETRACT_LENGTH=0.25" in text2
    assert "DEFECT_DETECTION_START" not in text2.split(gr.HEADER_END)[0]
    assert check_resume(text2, 1.0).z_travel == pytest.approx(1.0 + 0.6 + 9)


def test_body_starts_at_the_resume_layer_and_drops_the_preamble_object_end() -> None:
    res = gr.build(_walk(), PROFILE, name="plate.gcode", layer=5)
    body = _text(res).split(gr.HEADER_END)[1]
    assert body.lstrip("\n").startswith(";LAYER_CHANGE\n;Z:1.0")
    first_layer = body.split(";AFTER_LAYER_CHANGE")[0]
    assert "EXCLUDE_OBJECT_END" not in first_layer and "wipe" not in first_layer
    assert res.dropped == 4                            # object end, wipe, retract, lift


def test_only_objects_still_printed_are_defined() -> None:
    text = _text(gr.build(_walk(), PROFILE, name="plate.gcode", layer=5))
    assert "EXCLUDE_OBJECT_DEFINE NAME=box" in text
    assert "NAME=gone" not in text


def test_no_tower_purges_off_the_bed_after_every_tool_change() -> None:
    res = gr.build(_walk(), PROFILE, name="plate.gcode", layer=5, no_tower=True)
    text = _text(res)
    check = check_resume(text, 5 * LAYER_H, no_tower=True)
    assert check.problems == []
    body = text.split(gr.HEADER_END)[1]
    tool_changes = [ln for ln in body.split("\n") if ln in ("T0", "T1")]
    purges = [ln for ln in body.split("\n") if ln.startswith("SM_PRINT_PREEXTRUDE_FILAMENT")]
    assert len(tool_changes) == len(purges) == LAYERS - 5 + 1
    assert all("FORCE=1" in p and "RETRACT_LENGTH=0.5" in p for p in purges)


def test_top_z_resumes_at_the_first_layer_above_the_measured_top() -> None:
    res = gr.build(_walk(), PROFILE, name="plate.gcode", top_z=0.79)
    assert res.layer == 4
    assert res.gap == pytest.approx(0.01)


def test_paused_at_lifts_the_travel_and_refuses_layers_above_the_pause() -> None:
    w = _walk()
    pause = w.offsets[w.layers[5]["start"] + 20]       # inside layer 6
    res = gr.build(w, PROFILE, name="plate.gcode", layer=3, paused_at=pause)
    assert res.pause_layer == 6
    assert res.z_travel > 6 * LAYER_H + 0.4
    with pytest.raises(UsageError, match="above the paused layer"):
        gr.build(w, PROFILE, name="plate.gcode", layer=8, paused_at=pause)


@pytest.mark.parametrize("layer", [1, LAYERS + 1])
def test_resume_layer_out_of_range(layer: int) -> None:
    with pytest.raises(UsageError, match="out of range"):
        gr.build(_walk(), PROFILE, name="plate.gcode", layer=layer)


def _edited(old: str, new: str) -> gr.Walk:
    text = synthetic_gcode()
    at = text.index(";Z:1.4")                          # layer 7, above the resume layer 5
    return gr.Walk((text[:at] + text[at:].replace(old, new, 1)).encode().split(b"\n"))


def test_relative_descent_below_the_resume_layer_is_refused() -> None:
    w = _edited("G1 X38 Y20 E1.0", "G91\nG1 Z-5\nG90\nG1 X38 Y20 E1.0")
    with pytest.raises(GateFailure, match="below the resume layer"):
        gr.build(w, PROFILE, name="plate.gcode", layer=5)


def test_arcs_after_the_cut_are_refused() -> None:
    w = _edited("G1 X38 Y20 E1.0", "G2 X38 Y20 I1 J0 E1.0")
    with pytest.raises(UsageError, match="arcs"):
        gr.build(w, PROFILE, name="plate.gcode", layer=5)


def test_no_tower_keeps_every_layer_height() -> None:
    text = _text(gr.build(_walk(), PROFILE, name="plate.gcode", layer=5, no_tower=True))
    body = text.split(gr.HEADER_END)[1]
    for n in range(6, LAYERS + 1):
        layer = body.split(f";Z:{round(n * LAYER_H, 3)}")[1].split(";TYPE:Inner wall")[0]
        assert f"G1 Z{round(n * LAYER_H, 3)}" in layer


def test_source_name_cannot_inject_gcode_lines() -> None:
    text = _text(gr.build(_walk(), PROFILE, name="plate.gcode\nG28 Z", layer=5))
    assert "\nG28 Z" not in text
    assert "plate.gcode?G28 Z" in text


def test_statistics_count_the_purges_so_preflight_sees_every_head(tmp_path: Path) -> None:
    from printer_ops.preflight import read_gcode_meta

    res = gr.build(_walk(), PROFILE, name="plate.gcode", layer=LAYERS)
    out = tmp_path / "resume.gcode"
    out.write_bytes(b"\n".join(res.lines))
    stats = next(ln for ln in _text(res).split("\n") if ln.startswith("; filament used [mm] ="))
    assert all(float(v) >= PROFILE.purge_mm for v in stats.split("=")[1].split(","))
    assert set(read_gcode_meta(out).used_tools) >= {0, 1}


def _m73(text: str) -> list[tuple[int, int]]:
    """(P, R) of every M73 from the resume header on."""
    words = [ln.split() for ln in text[text.index("; ===== RESUME of"):].split("\n") if ln.startswith("M73 P")]
    return [(int(w[1][1:]), int(w[2][1:])) for w in words]


def _assert_progress_restarts(text: str) -> None:
    progress = _m73(text)
    assert progress[0][0] == 0
    assert progress[-1] == (100, 0)
    assert all(a[0] <= b[0] and a[1] >= b[1] for a, b in pairwise(progress))


def test_progress_is_rescaled_to_the_resume_file() -> None:
    text = _text(gr.build(_walk(), PROFILE, name="plate.gcode", layer=5))
    _assert_progress_restarts(text)


def test_not_an_orca_gcode() -> None:
    with pytest.raises(UsageError, match="not an Orca G-code"):
        gr.Walk([b"G1 X1", b"G1 X2"])


def test_where_and_table_report_layer_and_heads() -> None:
    w = _walk()
    pos = w.offsets[w.layers[2]["start"] + 12]
    report = gr.where(w, pos)
    assert f"layer 3 of {LAYERS}" in report and "active tool T0" in report
    rows = gr.table(w, "2:3").split("\n")
    assert len(rows) == 2
    assert "in T1" in rows[0] and "changes T0" in rows[0] and "box:T0/T1" in rows[0]
    with pytest.raises(UsageError):
        gr.table(w, "3:2")


real = pytest.mark.skipif(not REAL_GCODE.is_file(), reason=f"real U1 plate not present: {REAL_GCODE}")


@real
@pytest.mark.parametrize("no_tower", [False, True])
def test_real_u1_plate_resume_passes_the_checks(no_tower: bool) -> None:
    w = gr.Walk.read(REAL_GCODE)
    res = gr.build(w, PROFILE, name=REAL_GCODE.name, layer=603, no_tower=no_tower)
    check = check_resume(_text(res), res.z, no_tower=no_tower)
    assert check.problems == []
    assert res.tools == [2, 3] and res.active == 2
    assert check.body_min_z >= res.z
    if not no_tower:
        assert check.tower_extrusions > 0
    _assert_progress_restarts(_text(res))
