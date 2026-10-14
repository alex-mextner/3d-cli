"""Tests for `3d visible-faces` — the visible side of a part prints with no support and no sag.

Synthetic standing slabs (tests/visible_faces_fixtures.py) with ledges, windows and bars
stand in for decorative walls; hand-written G-code plates stand in for the sliced plate.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import visible_faces as vf
from commands.visible_faces import _input_paths
from commands.visible_faces import run as run_command
from errors import UsageError
from visible_faces_fixtures import (
    ROT90_AT_100,
    VISIBLE,
    box,
    gcode,
    ledge_slab,
    slab,
    slab_layers,
    window_opening,
    window_slab,
    write_3mf,
    write_stl,
)


def _spec(visible: str = VISIBLE, openings: list[list[float]] | None = None,
          hidden: list[list[list[float]]] | None = None) -> vf.FaceSpec:
    return vf.FaceSpec(vf.parse_visible(visible),
                       [np.asarray(h, float) for h in hidden or []],
                       [(o[0], o[1], o[2], o[3]) for o in openings or []])


# ---------------------------------------------------------------- mesh pass
def test_flat_visible_face_passes() -> None:
    rep = vf.mesh_report("slab", slab(), _spec())
    assert rep["status"] == "PASS"
    assert rep["pieces"] == [] and rep["layers"] == 250


def test_steep_ledge_on_the_visible_side_fails_with_area_and_z() -> None:
    rep = vf.mesh_report("ledge", ledge_slab(70), _spec())
    assert rep["status"] == "FAIL"
    first = rep["pieces"][0]
    assert first["area_mm2"] > 0.5
    assert 10.0 < first["z"] < 11.0                       # the ledge underside starts at z 10
    assert first["bbox"][1] < 0 and first["bbox"][0] == pytest.approx(0) and first["bbox"][2] == pytest.approx(40)


def test_horizontal_ledge_fails_as_one_big_piece() -> None:
    rep = vf.mesh_report("shelf", ledge_slab(90), _spec())
    assert rep["status"] == "FAIL"
    assert rep["pieces"][0]["area_mm2"] == pytest.approx(40 * (2 - 0.22), rel=0.02)


@pytest.mark.parametrize("angle", [45, 60])
def test_ledge_within_the_self_supporting_reach_passes(angle: int) -> None:
    # layer 0.12: reach 0.22 mm + the 0.1 mm sliver filter => steps up to ~0.32 mm/layer (~69°) carry
    assert vf.mesh_report("ledge", ledge_slab(angle), _spec())["status"] == "PASS"


def test_the_same_steep_ledge_on_the_hidden_side_passes() -> None:
    assert vf.mesh_report("back", ledge_slab(70, side="hidden"), _spec())["status"] == "PASS"
    # every face visible: the back ledge counts again
    assert vf.mesh_report("back", ledge_slab(70, side="hidden"), _spec("all"))["status"] == "FAIL"


def test_thicker_layers_fail_a_60_degree_ledge_only_past_the_reach() -> None:
    # 0.3 mm layers: 60° steps 0.52 mm per layer, past reach 0.4 + sliver 0.1
    assert vf.mesh_report("ledge", ledge_slab(60), _spec(), layer=0.3)["status"] == "FAIL"
    assert vf.mesh_report("ledge", ledge_slab(45), _spec(), layer=0.3)["status"] == "PASS"


def test_hidden_polygon_masks_the_ledge() -> None:
    covered = [[[-1.0, 9.0], [41.0, 9.0], [41.0, 20.0], [-1.0, 20.0]]]
    assert vf.mesh_report("ledge", ledge_slab(70), _spec(hidden=covered))["status"] == "PASS"
    half = [[[-1.0, 9.0], [20.0, 9.0], [20.0, 20.0], [-1.0, 20.0]]]
    rep = vf.mesh_report("ledge", ledge_slab(70), _spec(hidden=half))
    assert rep["status"] == "FAIL" and all(p["bbox"][0] >= 19.9 for p in rep["pieces"])


def test_short_bridge_in_an_opening_passes_and_a_long_one_fails() -> None:
    short = vf.mesh_report("win6", window_slab(6), _spec(openings=[window_opening(6)]))
    assert short["status"] == "PASS"
    assert {round(b["z"], 2) for b in short["bridges"]} == {14.1, 20.1}   # the bar and the lintel
    assert all(b["span_mm"] == pytest.approx(6.0, abs=0.05) for b in short["bridges"])

    long = vf.mesh_report("win12", window_slab(12), _spec(openings=[window_opening(12)]))
    assert long["status"] == "FAIL" and not long["bridges"]
    assert long["pieces"][0]["bbox"][2] - long["pieces"][0]["bbox"][0] == pytest.approx(12 - 0.44, abs=0.05)


def test_a_bridge_outside_any_declared_opening_fails() -> None:
    assert vf.mesh_report("win6", window_slab(6), _spec())["status"] == "FAIL"


def test_colour_volumes_are_unioned_so_their_interface_is_no_surface(tmp_path: Path) -> None:
    lower, upper = box(0, 0, 0, 40, 5, 15), box(0, 0, 15, 40, 5, 30)
    a, b = write_stl(tmp_path / "w_a.stl", lower), write_stl(tmp_path / "w_b.stl", upper)
    res = vf.check([f"{a},{b}"], None, vf.build_specs([VISIBLE], [], []), vf.Options())
    assert res["status"] == "PASS" and res["mesh"][0]["name"] == "w_a"


def test_3mf_build_items_are_checked_per_object_in_bed_coordinates(tmp_path: Path) -> None:
    path = write_3mf(tmp_path / "plate.3mf", [("WALL", [box(0, 0, 0, 40, 5, 15), box(0, 0, 15, 40, 5, 30)]),
                                              ("LEDGE", [ledge_slab(70)])])
    specs = vf.build_specs(["WALL:y<102.5", "LEDGE:y<102.5"], [], [])
    res = vf.check([str(path)], None, specs, vf.Options())
    by = {r["name"]: r for r in res["mesh"]}
    assert by["WALL"]["status"] == "PASS" and by["LEDGE"]["status"] == "FAIL"
    assert by["LEDGE"]["pieces"][0]["bbox"][1] < 100.0
    only_wall = vf.check([str(path)], None, vf.build_specs(["WALL:y<102.5"], [], []), vf.Options())
    assert [r["name"] for r in only_wall["mesh"]] == ["WALL"] and only_wall["skipped"] == ["LEDGE"]


# ---------------------------------------------------------------- G-code pass
def _gcode_check(tmp_path: Path, text: str, visible: str = VISIBLE) -> dict:
    p = tmp_path / "plate.gcode"
    p.write_text(text)
    return vf.check([], str(p), vf.build_specs([visible], [], []), vf.Options())


def test_gcode_clean_object_over_a_raft_passes(tmp_path: Path) -> None:
    res = _gcode_check(tmp_path, gcode(slab_layers(3)))
    assert res["status"] == "PASS", res
    assert res["gcode"][0]["name"] == "SLAB_id_0_copy_0" and res["gcode"][0]["layers"] == 3


def test_gcode_support_above_the_raft_on_the_visible_side_fails(tmp_path: Path) -> None:
    res = _gcode_check(tmp_path, gcode(slab_layers(3), support={1: (5, 0.5, 15, 0.5)}))
    assert res["status"] == "FAIL"
    assert res["gcode"][0]["support"] == [{"type": "Support", "x": 5.0, "y": 0.5, "z": 0.44}]
    # the same support line on the hidden half passes
    assert _gcode_check(tmp_path, gcode(slab_layers(3), support={1: (5, 4.5, 15, 4.5)}))["status"] == "PASS"


def test_gcode_outer_line_stepping_out_half_a_mm_fails(tmp_path: Path) -> None:
    res = _gcode_check(tmp_path, gcode(slab_layers(3, step=0.5)))
    assert res["status"] == "FAIL"
    isl = res["gcode"][0]["islands"]
    assert [i["z"] for i in isl] == [0.44, 0.56]
    assert all(i["y"] < 0 and i["area_mm2"] > 5 for i in isl)


def test_gcode_chamfer_step_passes_and_a_hidden_side_step_passes(tmp_path: Path) -> None:
    assert _gcode_check(tmp_path, gcode(slab_layers(3, step=0.14)))["status"] == "PASS"
    assert _gcode_check(tmp_path, gcode(slab_layers(3, step=0.5, side="hidden")))["status"] == "PASS"


def test_gcode_object_pattern_that_matches_nothing_fails(tmp_path: Path) -> None:
    res = _gcode_check(tmp_path, gcode(slab_layers(3)), visible="ROOF:all")
    assert res["status"] == "FAIL"
    assert res["gcode"] == [{"name": "ROOF", "kind": "gcode", "status": "FAIL", "visible": "", "layers": 0, "support": [],
                                 "islands": [], "bridges": [], "error": "no object in the G-code matches this name"}]
    assert res["skipped"] == ["SLAB_id_0_copy_0"]


def test_gcode_object_pattern_matches_without_the_orca_suffix(tmp_path: Path) -> None:
    res = _gcode_check(tmp_path, gcode(slab_layers(3, step=0.5)), visible="SLAB:y<2.5")
    assert res["status"] == "FAIL" and res["gcode"][0]["name"] == "SLAB_id_0_copy_0"


def test_gcode_support_outside_object_blocks_is_charged_to_the_object_it_touches(tmp_path: Path) -> None:
    tail = ";Z:0.440\n;TYPE:Support\nG1 X5 Y0.5 F9000\nG1 X15 Y0.5 E0.3\n"
    res = _gcode_check(tmp_path, gcode(slab_layers(3)) + tail)
    assert res["status"] == "FAIL" and res["gcode"][0]["support"][0]["z"] == 0.44
    far = ";Z:0.440\n;TYPE:Support\nG1 X150 Y0.5 F9000\nG1 X160 Y0.5 E0.3\n"
    assert _gcode_check(tmp_path, gcode(slab_layers(3)) + far)["status"] == "PASS"


def test_gcode_object_literally_named_plate_is_checked(tmp_path: Path) -> None:
    text = gcode(slab_layers(3, step=0.5)).replace("SLAB_id_0_copy_0", "plate")
    res = _gcode_check(tmp_path, text)
    assert res["status"] == "FAIL" and [r["name"] for r in res["gcode"]] == ["plate"]


def test_gcode_hidden_polygon_is_applied_in_model_z_over_a_raft(tmp_path: Path) -> None:
    (tmp_path / "h.json").write_text(json.dumps([[[0, 0], [40, 0], [40, 0.3], [0, 0.3]]]))
    specs = vf.build_specs([VISIBLE], [str(tmp_path / "h.json")], [])
    text = gcode(slab_layers(3), support={1: (5, 0.5, 15, 0.5)})
    p = tmp_path / "plate.gcode"
    # over the raft the support at G-code z 0.44 is model z 0.24: inside the hidden band
    p.write_text(text)
    assert vf.check([], str(p), specs, vf.Options())["status"] == "PASS"
    # no raft (a thick 0.32 mm first layer): G-code z is model z, 0.44 is above the band
    p.write_text("\n".join(ln for ln in text.splitlines() if not ln.startswith("G1 X41") and "X-1 Y" not in ln))
    assert vf.check([], str(p), specs, vf.Options())["status"] == "FAIL"


def test_gcode_arcs_and_absolute_extrusion_are_followed(tmp_path: Path) -> None:
    p = tmp_path / "arc.gcode"
    p.write_text("M82\nG92 E0\n;Z:0.2\nG1 X10 Y0 F600\n;TYPE:Outer wall\nG2 X10 Y0 I-10 J0 E5\n"
                 "G1 X20 Y0\nG1 X30 Y0 E5\n")   # no E advance: a travel in absolute mode
    objs, seen = vf.parse_gcode(str(p), vf.build_specs(["all"], [], []))
    assert seen == ["plate"]
    pts = objs["plate"].layers[0.2].points()
    assert np.allclose(np.hypot(pts[:, 0], pts[:, 1]), 10.0, atol=1e-6)    # the full circle, nothing else
    assert pts[:, 1].min() < -9.9 and pts[:, 1].max() > 9.9


# ---------------------------------------------------------------- mesh inputs
def test_floating_part_fails_its_first_layer() -> None:
    rep = vf.mesh_report("float", box(0, 0, 10, 40, 5, 30), _spec())
    assert rep["status"] == "FAIL" and rep["pieces"][0]["z"] == pytest.approx(10.02, abs=0.07)


def test_3mf_rotation_and_units_follow_the_spec(tmp_path: Path) -> None:
    # rotated 90°: the ledge (model y < 0) lands at bed x 100..102, the slab at x 95..100
    rot = write_3mf(tmp_path / "rot.3mf", [("LEDGE", [ledge_slab(70)])], [ROT90_AT_100])
    assert vf.check([str(rot)], None, vf.build_specs(["x>97.5"], [], []), vf.Options())["status"] == "FAIL"
    assert vf.check([str(rot)], None, vf.build_specs(["x<97.5"], [], []), vf.Options())["status"] == "PASS"
    cm = write_3mf(tmp_path / "cm.3mf", [("LEDGE", [ledge_slab(70)])], unit="centimeter", per_mm=0.1)
    res = vf.check([str(cm)], None, vf.build_specs(["y<102.5"], [], []), vf.Options())
    assert res["status"] == "FAIL" and 97.9 < res["mesh"][0]["pieces"][0]["bbox"][1] < 100.0


def test_3mf_child_model_with_its_own_unit_is_converted(tmp_path: Path) -> None:
    import zipfile
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    prod = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    tet = ('<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
           '<vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices><triangles>'
           '<triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>'
           '<triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/></triangles></mesh>')
    path = tmp_path / "child_inch.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model",
                   f'<model unit="millimeter" xmlns="{core}" xmlns:p="{prod}"><resources>'
                   '<object id="2" name="T"><components><component p:path="/3D/Objects/o.model" objectid="1" '
                   'transform="1 0 0 0 1 0 0 0 1 10 0 0"/></components></object></resources>'
                   '<build><item objectid="2"/></build></model>')
        z.writestr("3D/Objects/o.model",
                   f'<model unit="inch" xmlns="{core}"><resources><object id="1">{tet}</object></resources></model>')
    [(name, [(V, _F)])] = vf.load_3mf(str(path))
    assert name == "T"
    assert V.min(0) == pytest.approx([10, 0, 0]) and V.max(0) == pytest.approx([35.4, 25.4, 25.4])


def test_mesh_pattern_matching_no_part_fails(tmp_path: Path) -> None:
    stl = write_stl(tmp_path / "wall.stl", slab())
    res = vf.check([str(stl)], None, vf.build_specs(["WAL_TYPO:y<2.5", "wall:y<2.5"], [], []), vf.Options())
    assert res["status"] == "FAIL"
    assert [(r["name"], r["status"], r.get("error")) for r in res["mesh"]] == [
        ("wall", "PASS", None), ("WAL_TYPO", "FAIL", "no part matches this name")]


def test_command_wrapper_finds_input_paths_and_accepts_equals_forms() -> None:
    argv = ["a.stl,b.stl", "--visible", "W:y<1", "--hidden", "h.json", "--gcode=p.gcode", "c.3mf",
            "--layer", "0.2", "--json", "--visible=all"]
    assert _input_paths(argv) == ["a.stl", "b.stl", "p.gcode", "c.3mf"]
    assert _input_paths(["--gcode", "p.gcode", "--visible", "all"]) == ["p.gcode"]
    with pytest.raises(UsageError, match="--visible"):
        run_command(["a.stl"])


# ---------------------------------------------------------------- inputs and main
@pytest.mark.parametrize("spec,expect", [("y<4.8", vf.Visible("y", True, 4.8)), (" X > -2 ", vf.Visible("x", False, -2.0)),
                                         ("all", vf.Visible(None))])
def test_parse_visible(spec: str, expect: vf.Visible) -> None:
    assert vf.parse_visible(spec) == expect


@pytest.mark.parametrize("spec", ["z<3", "y<", "y=4", "front"])
def test_parse_visible_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(vf.InputError, match="expected x<V"):
        vf.parse_visible(spec)


def test_hidden_scanline_and_object_bound_options(tmp_path: Path) -> None:
    (tmp_path / "h.json").write_text(json.dumps([[[0, 0], [10, 0], [10, 10], [0, 10]], [[20, 0], [30, 5], [20, 10]]]))
    specs = vf.build_specs(["y<1", "ROOF:all"], [f"WALL*:{tmp_path / 'h.json'}"], [])
    wall = specs.resolve(vf.object_names("WALL2_id_3_copy_0"))
    assert wall is not None and wall.visible.text() == "y<1"
    assert wall.hidden_at(5.0) == [(0.0, 10.0), (20.0, 30.0)]
    assert wall.is_hidden(25.0, 5.0) and not wall.is_hidden(25.0, 9.5)
    roof = specs.resolve(["ROOF"])
    assert roof is not None and roof.visible.axis is None and roof.hidden == []


def test_bad_json_options_are_input_errors(tmp_path: Path) -> None:
    (tmp_path / "o.json").write_text("[[1, 2, 3]]")
    with pytest.raises(vf.InputError, match="entry 0 must be"):
        vf.load_openings(str(tmp_path / "o.json"))
    (tmp_path / "h.json").write_text("{")
    with pytest.raises(vf.InputError, match="not valid JSON"):
        vf.load_hidden(str(tmp_path / "h.json"))


def test_main_exit_codes_and_json_agree_with_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stl = write_stl(tmp_path / "ledge.stl", ledge_slab(70))
    assert vf.main([str(stl), "--visible", VISIBLE]) == 1
    text = capsys.readouterr().out
    assert vf.main([str(stl), "--visible", VISIBLE, "--json"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "FAIL" and f"FAIL {len(rep['mesh'][0]['pieces'])} unsupported pieces" in text
    assert vf.main([str(stl), "--visible", "y<-5"]) == 0
    assert vf.main([str(stl)]) == 2
    assert vf.main([str(stl), "--visible", VISIBLE, "--layer", "0"]) == 2
    assert "invalid option: --layer 0.0" in capsys.readouterr().err
