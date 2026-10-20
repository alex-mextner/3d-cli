"""Unit tests for the head-sequence check (slicing.head_sequence) — synthetic 3MF + G-code."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from errors import GateFailure, UsageError
from head_sequence_fixtures import gcode, project_3mf
from slicing.head_sequence import (
    check,
    heads_by_layer,
    preflight_check,
    read_object_extruders,
    read_recipes,
)


def _by_name(report):
    return {o.name: o for o in report.objects}


def test_pattern_followed_at_phase_one_ok_and_wrong_head_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5, "B": 5})
    # A starts at phase 1 of "14" (head 4 first); B prints head 1 on every layer.
    layers = [{"A": 4 if i % 2 == 0 else 1, "B": 1} for i in range(6)]
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", layers)))
    objs = _by_name(report)
    assert objs["A"].status == "ok"
    assert objs["B"].status == "FAIL"
    assert objs["B"].patterns == ["14"]
    assert report.failures == 1
    assert report.to_dict()["status"] == "FAIL"


def test_single_wrong_layer_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    layers = [{"A": 1 if i % 2 == 0 else 4} for i in range(6)]
    layers[3] = {"A": 2}
    assert check(str(proj), str(gcode(tmp_path / "p.gcode", layers))).failures == 1


def test_raft_layers_in_support_filament_are_skipped(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5}, raft_layers=4)
    layers = [{"A": 3}] * 4 + [{"A": 1 if i % 2 == 0 else 4} for i in range(4)]
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", layers)))
    assert _by_name(report)["A"].status == "ok"
    assert _by_name(report)["A"].layers == 4


def test_ratio_row_reported_local_z_not_failed(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"C": 6})
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"C": 2}, {"C": 2}])))
    assert _by_name(report)["C"].status == "local-z"
    assert report.failures == 0


def test_bambu_pseudo_tool_changes_are_not_heads() -> None:
    lines = [";LAYER_CHANGE", "T2", "EXCLUDE_OBJECT_START NAME=A", "T255", "T1000",
             "G1 X1 Y1 E0.2", "EXCLUDE_OBJECT_END NAME=A"]
    assert heads_by_layer(lines)["A"][0] == {3}


def test_moves_and_retracts_do_not_count() -> None:
    lines = [";LAYER_CHANGE", "EXCLUDE_OBJECT_START NAME=A", "T1", "G1 X1 Y1 F3000",
             "G1 E-0.5", "G1 X2 Y2 ; E1 in a comment", "EXCLUDE_OBJECT_END NAME=A"]
    assert "A" not in heads_by_layer(lines)


def test_recipes_virtual_ids_follow_physical(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 1}, raft_layers=2)
    with zipfile.ZipFile(proj) as zf:
        assert read_recipes(zf) == ({5: "14", 6: ""}, 2)


def test_unknown_object_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 1})
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"Zed": 1}])))
    assert [o.status for o in report.objects if o.name.startswith("Zed")] == ["FAIL"]


def test_single_plate_object_missing_from_gcode_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5, "B": 5})
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"A": 1}, {"A": 4}])))
    assert _by_name(report)["B"].status == "FAIL"
    assert "not printed" in _by_name(report)["B"].detail


def test_gcode_without_object_labels_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    g = tmp_path / "p.gcode"
    g.write_text(";LAYER_CHANGE\nT0\nG1 X1 Y1 E1\n")
    assert check(str(proj), str(g)).failures == 1


def test_prefix_overlapping_names_match_longest(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 1, "AB": 5})
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"A": 1, "AB": 1}, {"A": 1, "AB": 1}])))
    assert _by_name(report)["A"].status == "ok"
    assert _by_name(report)["AB"].status == "FAIL"


def test_phase_explosion_is_bounded(tmp_path: Path) -> None:
    row = "1,4,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1," + "1234" * 5
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5}, mixed=";".join([row] * 6))
    # Object A uses all six virtual rows (20^6 phase combinations).
    with zipfile.ZipFile(proj) as zf:
        ms = zf.read("Metadata/model_settings.config").decode()
        cfg = zf.read("Metadata/project_settings.config")
    parts = "".join(f'<part id="{i}"><metadata key="extruder" value="{5 + i}"/></part>' for i in range(6))
    ms = ms.replace('<part id="1"><metadata key="extruder" value="5"/></part>', parts)
    with zipfile.ZipFile(proj, "w") as zf:
        zf.writestr("Metadata/project_settings.config", cfg)
        zf.writestr("Metadata/model_settings.config", ms)
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"A": 1}])))
    assert _by_name(report)["A"].status == "FAIL"
    assert "combinations" in _by_name(report)["A"].detail


@pytest.mark.parametrize("kind", ["stl", "bare3mf", "corrupt"])
def test_cli_rejects_non_project_input(tmp_path: Path, kind: str) -> None:
    from commands.slice_check import run

    inp = tmp_path / ("m.stl" if kind == "stl" else "m.3mf")
    if kind == "bare3mf":
        with zipfile.ZipFile(inp, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
    elif kind == "corrupt":
        with zipfile.ZipFile(inp, "w") as zf:
            zf.writestr("Metadata/project_settings.config", "{not json")
            zf.writestr("Metadata/model_settings.config", "<config/>")
    else:
        inp.write_text("solid x\nendsolid x\n")
    g = gcode(tmp_path / "p.gcode", [{"A": 1}])
    with pytest.raises(UsageError):
        run([str(inp), "--gcode", str(g), "--check", "head-sequence"])


def test_preflight_skip_when_no_mixed_and_single_head(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 2}, mixed="")
    result = preflight_check(str(gcode(tmp_path / "p.gcode", [{"A": 2}])), str(proj))
    assert result["check"] == "head_sequence"
    assert result["status"] == "SKIP"


def test_preflight_pass(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    g = gcode(tmp_path / "p.gcode", [{"A": 1}, {"A": 4}])
    assert preflight_check(str(g), str(proj))["status"] == "PASS"


def test_cli_requires_gcode_with_check(tmp_path: Path) -> None:
    from commands.slice_check import run

    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    with pytest.raises(UsageError):
        run([str(proj), "--check", "head-sequence"])


def test_cli_fail_raises_gate_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from commands.slice_check import run

    proj = project_3mf(tmp_path / "p.3mf", {"B": 5})
    g = gcode(tmp_path / "p.gcode", [{"B": 1}, {"B": 1}])
    with pytest.raises(GateFailure):
        run([str(proj), "--gcode", str(g), "--check", "head-sequence"])
    assert "FAIL B" in capsys.readouterr().out


def test_label_matches_its_own_object_not_a_longer_digit_sibling() -> None:
    """now_grey_plus: `grph_14_—_тёмно-серый_14_id_5_copy_0` was attributed to `grph_141`
    (alphanumerics `grph1414…` start with `grph141`), so grph_14 read as not printed."""
    from slicing.head_sequence import _match_object

    objs = {f"grph_{n} — тёмно-серый {n}": [1] for n in ("1", "14", "141", "1141", "11411")}
    for n in ("1", "14", "141", "1141", "11411"):
        assert _match_object(f"grph_{n}_—_тёмно-серый_{n}_id_0_copy_0", objs) == f"grph_{n} — тёмно-серый {n}"
    assert _match_object("A_id_0_copy_0", {"A": [1], "AB": [2]}) == "A"
    assert _match_object("AB_id_0_copy_0", {"A": [1], "AB": [2]}) == "AB"
    assert _match_object("A1_id_0_copy_0", {"A": [1]}) is None


def _cli_project(path: Path, obj_extruder: str, part_extruder: str | None) -> Path:
    """model_settings as the Orca CLI writes it for an STL: the extruder on the object, parts bare."""
    part = (f'<metadata key="extruder" value="{part_extruder}"/>' if part_extruder is not None else "")
    ms = (f'<config><object id="2"><metadata key="name" value="A.stl"/>'
          f'<metadata key="extruder" value="{obj_extruder}"/><part id="1">{part}</part></object></config>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", '{"filament_colour": ["#000000"]}')
        zf.writestr("Metadata/model_settings.config", ms)
    return path


def test_extruder_zero_is_the_first_head(tmp_path: Path) -> None:
    """Orca CLI projects from STL carry object extruder 0 (= default, filament 1); the G-code prints head 1."""
    g = gcode(tmp_path / "p.gcode", [{"A.stl": 1}, {"A.stl": 1}])
    for obj, part in (("0", None), ("0", "0")):
        proj = _cli_project(tmp_path / "p.3mf", obj, part)
        with zipfile.ZipFile(proj) as zf:
            assert read_object_extruders(zf) == {"A.stl": [1]}
        assert check(str(proj), str(g)).failures == 0


def test_part_extruder_zero_inherits_the_object(tmp_path: Path) -> None:
    proj = _cli_project(tmp_path / "p.3mf", "2", "0")
    with zipfile.ZipFile(proj) as zf:
        assert read_object_extruders(zf) == {"A.stl": [2]}
    report = check(str(proj), str(gcode(tmp_path / "p.gcode", [{"A.stl": 1}])))
    assert report.failures == 1


def test_mixed_parts_inherit_per_part(tmp_path: Path) -> None:
    """A bare or 0 printed part inherits the object next to a part set explicitly; a bare modifier adds nothing."""
    parts = ('<part id="1"><metadata key="extruder" value="3"/></part>'
             '<part id="2"><metadata key="extruder" value="0"/></part>'
             '<part id="3" subtype="normal_part"></part>'
             '<part id="4" subtype="modifier_part"></part>')
    ms = (f'<config><object id="2"><metadata key="name" value="A"/>'
          f'<metadata key="extruder" value="2"/>{parts}</object>'
          f'<object id="4"><metadata key="name" value="B"/><metadata key="extruder" value="x"/>'
          f'<part id="1"><metadata key="extruder" value="4"/></part></object>'
          f'<object id="6"><metadata key="name" value="C"/><part id="1"></part></object></config>')
    proj = tmp_path / "p.3mf"
    with zipfile.ZipFile(proj, "w") as zf:
        zf.writestr("Metadata/model_settings.config", ms)
    with zipfile.ZipFile(proj) as zf:
        assert read_object_extruders(zf) == {"A": [2, 3], "B": [4], "C": [1]}


A23_ROW = "2,3,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,12"


def test_pattern_tokens_one_and_two_are_the_rows_components(tmp_path: Path) -> None:
    """Row a=2, b=3, tokens `12` print heads 2,3,2,3 — the pattern digits are not the heads."""
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5}, mixed=A23_ROW)
    with zipfile.ZipFile(proj) as zf:
        assert read_recipes(zf)[0] == {5: "23"}
    good = gcode(tmp_path / "good.gcode", [{"A": 2 + i % 2} for i in range(6)])
    assert check(str(proj), str(good)).failures == 0


def test_pattern_tokens_read_as_heads_fail_teeth(tmp_path: Path) -> None:
    """The old reading (digits = heads: 1,2) is what the printer does NOT do: it must FAIL."""
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5}, mixed=A23_ROW)
    wrong = gcode(tmp_path / "wrong.gcode", [{"A": 1 + i % 2} for i in range(6)])
    assert check(str(proj), str(wrong)).failures == 1


def test_pattern_tokens_three_and_up_are_direct_heads() -> None:
    from slicing.mix_pattern import decode_pattern

    assert decode_pattern("431", 2, 3) == "432"
    assert decode_pattern("1212", 1, 2) == "1212"
