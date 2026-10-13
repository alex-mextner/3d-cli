"""Unit tests for lib/standing_props.py (`3d props`): rib stations, the snap-off gap, the
straight inner edge behind a ribbed back, tabs, overhang-free props, the stability gate and
the 3MF round trip with the props as an extra part of the object."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import standing_props as sp
from manifold3d import Error, Manifold, OpType
from props_fixtures import box, ribbed_panel, write_orca_3mf, write_stl


def _without_tabs(r: sp.PropsResult) -> Manifold:
    return r.mesh - Manifold.batch_boolean([box(*t) for t in r.tabs], OpType.Add)


def test_slab_gets_three_ribs_clear_of_the_part_except_the_tabs() -> None:
    slab = box(0, 0, 0, 120, 4, 60)
    r = sp.props(slab, 1)

    assert r.info["stations"] == 3 and r.info["station_x"] == [10.0, 60.0, 110.0]
    assert r.info["height"] == 36.0
    assert r.mesh.status() == Error.NoError and r.mesh.volume() > 0
    # snap-off gap: nothing but the tabs comes closer than the gap
    assert _without_tabs(r).min_gap(slab, 1.0) == pytest.approx(0.25, abs=1e-3)
    # the tabs do reach the part, and every prop sits behind its back
    assert r.tabs and all((slab ^ box(*t)).volume() > 0 for t in r.tabs)
    assert r.mesh.bounding_box()[1] >= 4.0 - 0.3 - 1e-6
    # ratio = H / (depth at the bed + reach): reach = gap + 1 + (36 - 0.1)·cot 55°
    reach = 0.25 + 1.0 + 35.9 / np.tan(np.radians(55))
    assert r.info["reach"] == round(reach, 1)
    assert r.info["ratio"] == round(60 / (4 + reach), 2) and r.info["ok"] is True


def test_back_minus_y_mirrors_the_props() -> None:
    slab = box(0, 0, 0, 120, 4, 60)
    r = sp.props(slab, -1)

    assert _without_tabs(r).bounding_box()[4] == pytest.approx(-0.25, abs=1e-6)   # max y: gap behind -y back
    assert r.info["ratio"] == sp.props(slab, 1).info["ratio"]


def test_ribbed_back_gets_a_straight_inner_edge_behind_the_hindmost_point() -> None:
    panel = ribbed_panel()
    r = sp.props(panel, 1)
    ribs = _without_tabs(r)

    # in the cells (z 5, 20, 25) the back is at y = 2, yet the rib edge stays at 6 + gap
    for z in (5.0, 20.0, 25.0, 33.0):
        sl = (ribs ^ box(9, -10, 0, 11, 60, 60)).slice(z)
        assert sl.bounds()[1] == pytest.approx(6.25, abs=1e-6), z
    # tabs only where the back reaches the rib (the two horizontal ribs)
    zs = sorted({round((t[2] + t[5]) / 2, 1) for t in r.tabs})
    assert all(10 <= z <= 12 or 30 <= z <= 32 for z in zs) and zs
    assert ribs.min_gap(panel, 1.0) == pytest.approx(0.25, abs=1e-3)


def test_part_and_props_print_without_overhang() -> None:
    import trimesh
    from printability_mesh import OVERHANG_AREA_FRAC, overhang

    slab = box(0, 0, 0, 120, 4, 60)
    r = sp.props(slab, 1)
    tm = trimesh.Trimesh(*sp.mesh_arrays(slab + r.mesh), process=False)
    above_bed = tm.triangles_center[:, 2] > 1e-6          # the bed carries the first layer
    above = tm.submesh([np.flatnonzero(above_bed)], append=True)
    assert isinstance(above, trimesh.Trimesh)
    frac = overhang(above)[1]
    assert frac < OVERHANG_AREA_FRAC / 10
    # the only downward faces above the bed are the 0.8 mm tab undersides bridging the gap;
    # the sloped rib edges and the rib tops face up
    props = trimesh.Trimesh(*sp.mesh_arrays(r.mesh), process=False)
    down = (props.face_normals[:, 2] < -1e-6) & (props.triangles_center[:, 2] > 1e-6)
    for cx, cy, cz in props.triangles_center[down]:
        assert any(abs(cz - t[2]) < 1e-4 and t[0] - 1e-4 <= cx <= t[3] + 1e-4 and t[1] - 1e-4 <= cy <= t[4] + 1e-4
                   for t in r.tabs), (cx, cy, cz)


def test_short_ribs_on_a_tall_thin_slab_fail_the_gate() -> None:
    tall = box(0, 0, 0, 20, 2, 80)
    default = sp.props(tall, 1)
    short = sp.props(tall, 1, height=0.2)

    assert default.info["stations"] == 1                   # 20 mm long: one rib in the middle
    assert default.info["ok"] is True and default.info["ratio"] < 1 / (0.6 / np.tan(np.radians(55)))
    assert short.info["ok"] is False and short.info["ratio"] == round(80 / (2 + short.info["reach"]), 2)
    assert short.info["ratio"] > 3


def test_stations_spacing_and_short_parts() -> None:
    assert list(sp.stations(0, 120, 50)) == [10, 60, 110]
    assert list(sp.stations(0, 40, 50)) == [10, 30]
    assert list(sp.stations(5, 12, 50)) == [8.5]


def test_cli_writes_3mf_with_props_as_extra_part_of_the_object(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wall = box(0, 0, 0, 120, 4, 60)
    sign = box(30, 4, 20, 90, 5, 30)                        # a second colour on the back, below rib top
    other = box(0, 0, 0, 20, 20, 10)
    src = tmp_path / "wall.3mf"
    write_orca_3mf(src, [("wall", [("panel", wall, 2), ("sign", sign, 3)]), ("cube", [("cube", other, 1)])],
                   offsets=[(0, 0), (200, 0)], unprintable=["cube"])

    assert sp.main([str(src), "--back", "+y"]) == 2
    assert "pick the standing one with --object" in capsys.readouterr().err

    out = tmp_path / "out.3mf"
    assert sp.main([str(src), "--object", "wall", "--back", "+y", "-o", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS" and report["stations"] == 3 and report["output"] == str(out)

    objs = sp.load_3mf(str(out))
    assert [o.name for o in objs] == ["wall", "cube"]
    assert [o.printable for o in objs] == [True, False]     # a disabled object stays disabled
    wall_obj = objs[0]
    assert [b.name for b in wall_obj.bodies] == ["panel", "sign", "wall props"]
    assert wall_obj.bodies[2].extruder == 2                 # the largest part's filament
    assert objs[1].bodies[0].vertices[:, 0].min() == pytest.approx(200)
    near_middle = wall_obj.bodies[2].vertices
    near_middle = near_middle[np.abs(near_middle[:, 0] - 60) < 1.0]
    assert near_middle[:, 1].min() >= 5.0 - 0.3 - 1e-4      # the middle rib stands behind the sign
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert 'value="wall props"' in cfg and 'subtype="normal_part"' in cfg


def test_tabs_reach_a_back_that_stands_short_of_its_hindmost_point() -> None:
    """the back is 0.4 mm short of its hindmost bump at every tab height: each tab must still
    reach into it (anchored to the bump, the tabs would float 0.1 mm behind the back)"""
    panel = box(0, 0, 0, 60, 2, 60) + box(0, 2, 15, 60, 2.4, 17)
    r = sp.props(panel, 1)

    assert r.tabs and all((panel ^ box(*t)).volume() > 0 for t in r.tabs)
    assert min(t[2] for t in r.tabs) >= 0.0                 # no tab under the bed


def test_depth_is_what_touches_the_bed() -> None:
    """a 2 mm deep first layer under a 100 mm deep body stands on 2 mm, not on 100"""
    part = box(0, 0, 0, 10, 2, 0.05) + box(0, -49, 0.05, 10, 51, 100)
    assert sp.props(part, 1).info["depth"] == 2.0


def test_3mf_object_files_in_inches_load_in_millimetres(tmp_path: Path) -> None:
    src = tmp_path / "inch.3mf"
    write_orca_3mf(src, [("wall", [("panel", box(0, 0, 0, 120, 4, 60), 1)])], offsets=[(10, 0)], sub_unit="inch")
    V = sp.load_3mf(str(src))[0].bodies[0].vertices
    assert V.min(axis=0) == pytest.approx([10, 0, 0], abs=1e-4)          # manifold meshes are float32
    assert V.max(axis=0) == pytest.approx([130, 4, 60], abs=1e-4)


def test_malformed_3mf_is_a_load_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "bad.3mf"
    write_orca_3mf(src, [("wall", [("panel", box(0, 0, 0, 120, 4, 60), 1)])])
    with zipfile.ZipFile(src) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files["3D/3dmodel.model"] = files["3D/3dmodel.model"].replace(b'transform="1 0', b'transform="nope 0')
    with zipfile.ZipFile(src, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)
    assert sp.main([str(src), "--back", "+y"]) == 2
    assert "malformed 3MF" in capsys.readouterr().err


def test_cli_stl_minus_y_and_failure_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_stl(tmp_path / "wall.stl", box(0, 0, 0, 120, 4, 60))
    out = tmp_path / "props.stl"
    assert sp.main([str(tmp_path / "wall.stl"), "--back", "-y", "-o", str(out)]) == 0
    assert ">>> PROPS: PASS; wrote" in capsys.readouterr().out
    V = sp.load_stl(str(out))[0].bodies[0].vertices
    assert V[:, 1].max() <= 0.3 + 1e-4

    write_stl(tmp_path / "tall.stl", box(0, 0, 0, 20, 2, 80))
    bad = tmp_path / "bad.stl"
    assert sp.main([str(tmp_path / "tall.stl"), "--back", "+y", "--height", "0.2", "-o", str(bad)]) == 1
    text = capsys.readouterr().out
    assert ">>> PROPS: FAIL height/base ratio 5.56 > 3" in text and "nothing written" in text
    assert not bad.exists()


def test_cli_rejects_open_meshes_and_bad_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import trimesh

    open_box = trimesh.creation.box((10, 10, 10))
    open_box.update_faces(np.arange(len(open_box.faces)) > 1)
    open_box.export(tmp_path / "open.stl")
    assert sp.main([str(tmp_path / "open.stl"), "--back", "+y"]) == 2
    assert "not a closed manifold" in capsys.readouterr().err

    with pytest.raises(SystemExit) as e:
        sp.main([str(tmp_path / "open.stl"), "--back", "+y", "--height", "1.5"])
    assert e.value.code == 2
    assert "--height must be > 0.0 and <= 1.0" in capsys.readouterr().err
