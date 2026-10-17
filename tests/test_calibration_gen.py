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
"""


def _fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, end: str = "END_PRINT") -> str:
    """An Orca stand-in: writes plate_1.gcode with one EXCLUDE_OBJECT block per STL argument."""
    root = tmp_path / "profiles"
    for kind, name, data in (
        ("machine", "Creality K1 SE 0.4 nozzle", {"bed_exclude_area": ["0x0"]}),
        ("process", "0.20mm Standard @Creality K1 SE", {"default_acceleration": "12000", "default_jerk": "9"}),
        ("filament", "Creality Generic PLA @K1-all", {"filament_flow_ratio": ["0.98"], "filament_colour": ["#FFFFFF"]}),
    ):
        d = root / "Creality" / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))
    monkeypatch.setenv("ORCA_PROFILES", str(root))
    body = FAKE_GCODE.replace("{end}", end)
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
