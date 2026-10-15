"""Tests for slicing.calibration_gen (`3d calibrate gen`): the parts that run without an Orca build."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from printer_ops.move_bounds import _INERT, _INERT_TOWER, command_word
from slicing import calibration_gen as cg


def test_box_stl_has_twelve_facets_spanning_the_box() -> None:
    stl = cg.box_stl(1, 2, 0, 11, 22, 5)
    assert stl.count("facet normal") == 12
    xs = {
        float(line.split()[1]) for line in stl.splitlines() if line.startswith("vertex")
    }
    assert xs == {1.0, 11.0}


def _preset(root: Path, kind: str, name: str, data: dict[str, object]) -> None:
    d = root / "Acme" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))


def test_profile_index_resolves_inherits_child_wins(tmp_path: Path) -> None:
    _preset(tmp_path, "process", "base", {"wall_loops": "2", "layer_height": "0.2"})
    _preset(tmp_path, "process", "mid", {"inherits": "base", "wall_loops": "3"})
    _preset(tmp_path, "process", "leaf", {"inherits": "mid", "layer_height": "0.12"})
    flat = cg.ProfileIndex([tmp_path]).flat("Acme", "process", "leaf")
    assert flat["wall_loops"] == "3"
    assert flat["layer_height"] == "0.12"
    assert "inherits" not in flat


def test_profile_index_missing_preset_names_the_roots(tmp_path: Path) -> None:
    with pytest.raises(cg.GenError, match="not found"):
        cg.ProfileIndex([tmp_path]).flat("Acme", "machine", "nope")


def test_orca_values_keep_list_shape() -> None:
    out = cg._apply_overrides(
        {"filament_colour": ["#FFFFFF"], "wall_loops": "2"},
        {"filament_colour": "#000000", "wall_loops": 1},
    )
    assert out == {"filament_colour": ["#000000"], "wall_loops": "1"}


GCODE = """; header
START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55
EXCLUDE_OBJECT_START NAME=flow_m05.stl_id_1_copy_0
G1 X1 E1
EXCLUDE_OBJECT_END NAME=flow_m05.stl_id_1_copy_0
EXCLUDE_OBJECT_START NAME=flow_p10.stl_id_4_copy_0
G1 X2 E1
END_PRINT
"""


def test_insert_after_first_and_every() -> None:
    once = cg.insert_after(GCODE, r"^START_PRINT\b.*$", ["TUNING_TOWER X"])
    assert once.splitlines()[2] == "TUNING_TOWER X"

    def pct(m: object) -> list[str]:
        import re

        assert isinstance(m, re.Match)
        sign = -1 if m.group(1) == "m" else 1
        return [f"M221 S{100 + sign * int(m.group(2))}"]

    every = cg.insert_after(
        GCODE,
        r"^EXCLUDE_OBJECT_START NAME=flow_([mp])(\d+)\S*$",
        [],
        every=True,
        render=pct,
    )
    lines = every.splitlines()
    assert lines[3] == "M221 S95"
    assert lines[7] == "M221 S110"


def test_insert_after_without_a_match_fails() -> None:
    with pytest.raises(cg.GenError):
        cg.insert_after(GCODE, r"^NOPE$", ["x"])


def test_shaper_commands_for_k1se(tmp_path: Path) -> None:
    out = tmp_path / "shaper.txt"
    res = cg.generate("shaper", "Creality K1 SE", "", out)
    text = out.read_text()
    assert res.files == [out]
    for cmd in (
        "G28",
        "SHAPER_CALIBRATE AXIS=X",
        "SHAPER_CALIBRATE AXIS=Y",
        "SAVE_CONFIG",
    ):
        assert f"\n{cmd}\n" in f"\n{text}"


def test_unknown_model_and_kind() -> None:
    with pytest.raises(cg.GenError, match="no calibration presets"):
        cg.generate("pa", "Prusa MK4", "", Path("x.gcode"))
    with pytest.raises(cg.GenError, match="unknown kind"):
        cg.generate("ringing", "Creality K1 SE", "", Path("x.gcode"))


def test_generator_commands_are_inert_for_preflight() -> None:
    assert command_word("M221 S95") in _INERT
    assert _INERT_TOWER.match("TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0 FACTOR=0.005")
    # any other tower command may move the head: it stays a macro
    assert not _INERT_TOWER.match("TUNING_TOWER COMMAND=G1 PARAMETER=X START=0 FACTOR=1")
    assert "TUNING_TOWER" not in _INERT


def test_inherits_loop_is_an_error(tmp_path: Path) -> None:
    _preset(tmp_path, "process", "a", {"inherits": "b"})
    _preset(tmp_path, "process", "b", {"inherits": "a"})
    with pytest.raises(cg.GenError, match="loop"):
        cg.ProfileIndex([tmp_path]).flat("Acme", "process", "a")


FAKE_GCODE = """; thumbnails = 16x16/PNG
START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55
{objects}{end}
; use_relative_e_distances = {relative_e}
"""


def _fake_orca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, end: str = "END_PRINT", relative_e: str = "1"
) -> str:
    """An Orca stand-in: writes plate_1.gcode with one EXCLUDE_OBJECT block per STL argument."""
    root = tmp_path / "profiles"
    for kind, name, data in (
        ("machine", "Creality K1 SE 0.4 nozzle",
         {"bed_exclude_area": ["0x0"], "retraction_length": ["0.8"], "retraction_speed": ["40"]}),
        ("process", "0.20mm Standard @Creality K1 SE",
         {"default_acceleration": "12000", "default_jerk": "9", "initial_layer_print_height": "0.2"}),
        ("filament", "Creality Generic PLA @K1-all",
         {"filament_flow_ratio": ["0.98"], "filament_colour": ["#FFFFFF"], "filament_diameter": ["1.75"]}),
    ):
        d = root / "Creality" / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))
    monkeypatch.setenv("ORCA_PROFILES", str(root))
    body = FAKE_GCODE.replace("{end}", end).replace("{relative_e}", relative_e)
    script = tmp_path / "orca"
    script.write_text(f"""#!/usr/bin/env python3
import sys, pathlib
a = sys.argv[1:]
out = pathlib.Path(a[a.index("--outputdir") + 1])
objs = "".join(
    f"EXCLUDE_OBJECT_START NAME={{pathlib.Path(s).name}}_id_{{i}}_copy_0\\nG1 X100 Y100 Z0.2 E1\\n"
    f"EXCLUDE_OBJECT_END NAME={{pathlib.Path(s).name}}_id_{{i}}_copy_0\\n"
    for i, s in enumerate(x for x in a if x.endswith(".stl")))
(out / "plate_1.gcode").write_text({body!r}.replace("{{objects}}", objs))
""")
    script.chmod(0o755)
    return str(script)


def test_gen_pa_and_flow_end_to_end_with_a_fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from slicing.slice_warnings import evaluate

    orca = _fake_orca(tmp_path, monkeypatch)
    pa = tmp_path / "pa.gcode"
    cg.generate("pa", "Creality K1 SE", orca, pa, color="#E8732A")
    lines = pa.read_text().splitlines()
    start = lines.index("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55")
    assert lines[start + 3].startswith("TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE")
    assert lines[lines.index("END_PRINT") - 1] == "SET_PRESSURE_ADVANCE ADVANCE=0.04"
    assert "; thumbnail begin 16x16" in pa.read_text()
    assert evaluate(pa)[0] == "PASS"

    flow = tmp_path / "flow.gcode"
    res = cg.generate("flow", "Creality K1 SE", orca, flow)
    text = flow.read_text()
    for name, pct in (("flow_m10", 90), ("flow_m05", 95), ("flow_p00", 100), ("flow_p05", 105), ("flow_p10", 110)):
        assert f"EXCLUDE_OBJECT_START NAME={name}.stl" in text
        assert f"M221 S{pct}" in text
    assert text.splitlines()[text.splitlines().index("END_PRINT") - 1] == "M221 S100"
    assert "1: 0.882" in res.notes[1]
    assert evaluate(flow)[0] == "PASS"


def test_gen_without_end_print_fails_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orca = _fake_orca(tmp_path, monkeypatch, end="M84")
    out = tmp_path / "pa.gcode"
    with pytest.raises(cg.GenError, match="END_PRINT"):
        cg.generate("pa", "Creality K1 SE", orca, out)
    assert not out.exists()


def test_finish_accepts_the_empty_exclude_error_only_without_an_exclude_area(tmp_path: Path) -> None:
    from slicing.slice_warnings import load, warnings_of

    log = f"[2026-10-15 14:51:32.1] [0x1] [error] {cg.EMPTY_EXCLUDE_ERROR}\n"
    for area, accepted in ((["0x0"], True), (["0x0", "20x0", "20x20"], False)):
        gcode = tmp_path / str(len(area)) / "t.gcode"
        gcode.parent.mkdir()
        gcode.write_text("; thumbnails = 16x16/PNG\nG1 X1 Y1 E1\n")
        cg.finish(gcode, log, {"bed_exclude_area": area})
        [w] = warnings_of(load(gcode.with_name("t.slice-warnings.json")))
        assert (w.accepted is not None) is accepted


def test_digit_paths_chain_strokes() -> None:
    [one] = cg.digit_paths("1", 0, 0, 2, 3, 1)
    assert one == [(2, 3), (2, 1.5), (2, 0)]  # b then c, one stroke
    eight = cg.digit_paths("8", 10, 0, 2, 3, 1)
    assert sum(len(p) - 1 for p in eight) == 7
    assert all(10 <= x <= 12 and 0 <= y <= 3 for p in eight for x, y in p)
    two = cg.digit_paths("20", 0, 0, 2, 3, 1)
    assert max(x for p in two for x, _ in p) == 5  # second digit starts at 2 + 1


def test_gen_combo_end_to_end_with_a_fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from slicing.slice_warnings import evaluate

    orca = _fake_orca(tmp_path, monkeypatch)
    out = tmp_path / "combo.gcode"
    res = cg.generate("combo", "Creality K1 SE", orca, out, color="#000000")
    lines = out.read_text().splitlines()
    start = lines.index("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55")
    end_pa = lines.index("; 3d calibrate gen combo: end of PA lines")
    block = lines[start + 1 : end_pa]
    assert block[1] == "M221 S100"  # flow factor reset before any PA line
    pa_sets = [ln for ln in block if ln.startswith("SET_PRESSURE_ADVANCE ADVANCE=")]
    tested = [float(ln.split("=")[1]) for ln in pa_sets if ln != "SET_PRESSURE_ADVANCE ADVANCE=0.04"]
    assert tested == [v for v in cg.COMBO_PA_VALUES if v != 0.04]
    # every tested value is followed by its slow-fast-slow line: 20 mm, 50 mm, 20 mm at 0.5 x 0.2 mm
    i = block.index("SET_PRESSURE_ADVANCE ADVANCE=0.045")
    moves = [ln for ln in block[i + 1 : i + 9] if " E" in ln and " X" in ln]
    e_per_mm = 0.5 * 0.2 * 0.98 / (3.141592653589793 * 1.75**2 / 4)
    assert [round(float(m.split(" E")[1].split()[0]), 4) for m in moves] == [
        round(e_per_mm * d, 4) for d in cg.COMBO_SEGMENTS
    ]
    assert [m.split(" F")[1] for m in moves] == ["1200", "7200", "1200"]
    # tiles print at stock PA, 100 % and the process's own acceleration
    assert block[-3:] == ["SET_PRESSURE_ADVANCE ADVANCE=0.04", "M221 S100", "SET_VELOCITY_LIMIT ACCEL=12000 ACCEL_TO_DECEL=6000"]
    rest = lines[end_pa:]
    assert sum(ln.startswith("M221 S") for ln in rest) == 6  # one per tile + the reset before END_PRINT
    end = rest.index("END_PRINT")
    assert rest[end - 2 : end] == ["M221 S100", "SET_PRESSURE_ADVANCE ADVANCE=0.04"]
    assert "80=0.08" in res.notes[0]
    assert evaluate(out)[0] == "PASS"


def test_record_hints_name_catalog_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `--method M` the generators print is accepted by `3d calibrate record` for that kind."""
    import re

    from printops.calibration_catalog import load_catalog

    kinds = load_catalog().kinds
    orca = _fake_orca(tmp_path, monkeypatch)
    notes = [n for k in ("pa", "flow", "combo") for n in cg.generate(k, "Creality K1 SE", orca, tmp_path / f"{k}.gcode").notes]
    cg.generate("shaper", "Creality K1 SE", "", tmp_path / "s.txt")
    notes += (tmp_path / "s.txt").read_text().splitlines()
    hints = [(m.group(1), m.group(2)) for n in notes for m in re.finditer(r"record (\w+) .*?--method (\w+)", n)]
    assert {k for k, _ in hints} == {"pressure_advance", "flow_ratio", "input_shaper"}
    for kind, method in hints:
        assert method in kinds[kind].methods, (kind, method)


def test_gen_combo_refuses_absolute_extrusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orca = _fake_orca(tmp_path, monkeypatch, relative_e="0")
    out = tmp_path / "combo.gcode"
    with pytest.raises(cg.GenError, match="relative-E"):
        cg.generate("combo", "Creality K1 SE", orca, out)
    assert not out.exists()


def test_missing_preset_option_is_a_gen_error() -> None:
    with pytest.raises(cg.GenError, match="retraction_speed"):
        cg._first({}, "retraction_speed")
    with pytest.raises(cg.GenError, match="filament_diameter"):
        cg._first({"filament_diameter": ["x"]}, "filament_diameter")


def test_temp_steps_and_refusals() -> None:
    assert cg.temp_steps("PLA") == [230, 225, 220, 215, 210, 205, 200]
    assert cg.temp_steps("PETG")[0] == 250
    with pytest.raises(cg.GenError, match="no temperature range"):
        cg.temp_steps("TPU")
    with pytest.raises(cg.GenError, match="--nozzle-temp"):
        cg.generate("temp", "Creality K1 SE", "", Path("x.gcode"), temp=215)


def test_temp_tower_labels_stand_on_each_block_floor() -> None:
    steps = cg.temp_steps("PLA")
    boxes = cg.temp_tower_boxes(steps, 110, 110)
    bz = cg.TEMP_BASE[2]
    floors = [bz + i * cg.TEMP_BLOCK_H for i in range(len(steps))]
    labels = [b for b in boxes if b[5] - b[2] == pytest.approx(cg.TEMP_EMBOSS)]
    assert {b[2] for b in labels} == set(floors)  # one label per block, on its floor
    gap_x0 = 110 - (2 * cg.TEMP_PILLAR + cg.TEMP_GAP) / 2 + cg.TEMP_PILLAR
    assert all(gap_x0 <= b[0] and b[3] <= gap_x0 + cg.TEMP_GAP for b in labels)  # inside the gap
    assert max(b[5] for b in boxes) == pytest.approx(bz + len(steps) * cg.TEMP_BLOCK_H)


def test_temp_changes_on_first_layer_of_each_block() -> None:
    steps = [230, 225, 220]
    bz, h = cg.TEMP_BASE[2], cg.TEMP_BLOCK_H
    zs = [round(0.2 * i, 2) for i in range(1, int((bz + 3 * h) / 0.2) + 1)]
    text = "\n".join(f";LAYER_CHANGE\n;Z:{z}\nG1 X1 E1" for z in zs)
    lines = cg.temp_changes(text, steps).splitlines()
    changes = [(lines[i - 1], ln) for i, ln in enumerate(lines) if ln.startswith("M104")]
    assert [c[1].split()[1] for c in changes] == ["S225", "S220"]
    assert [float(c[0][3:]) for c in changes] == [pytest.approx(bz + h + 0.2), pytest.approx(bz + 2 * h + 0.2)]
    with pytest.raises(cg.GenError, match="temperature changes"):
        cg.temp_changes("G1 X1 E1", steps)
