from __future__ import annotations

import json
from pathlib import Path

from visible_faces_fixtures import VISIBLE, gcode, ledge_slab, slab, slab_layers, window_opening, window_slab, write_stl

from .workflow_helper import require_cli_python_deps, run_cli


def test_visible_faces_gates_a_facade_mesh_and_its_sliced_plate(tmp_path: Path) -> None:
    """A user about to print a decorative wall checks that its front prints clean: the plain wall
    and the window with a 6 mm glazing bar pass, the steep ledge on the front fails with its
    coordinates, and the sliced plate whose outer wall sags 0.5 mm per layer fails; the text and
    JSON reports agree."""
    require_cli_python_deps(tmp_path, ["numpy", "scipy", "trimesh", "manifold3d"],
                            ["numpy", "scipy", "trimesh", "manifold3d"])
    write_stl(tmp_path / "wall.stl", slab())
    write_stl(tmp_path / "ledge.stl", ledge_slab(70))
    write_stl(tmp_path / "window.stl", window_slab(6))
    (tmp_path / "openings.json").write_text(json.dumps([window_opening(6)]))
    (tmp_path / "plate.gcode").write_text(gcode(slab_layers(3, step=0.5)))

    ok = run_cli(tmp_path, "visible-faces", "wall.stl", "window.stl", "--visible", VISIBLE,
                 "--openings", "openings.json")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "window   PASS" in ok.stdout and "bridges in openings: 2, longest 6.0 mm" in ok.stdout
    assert ">>> VISIBLE-FACES: PASS" in ok.stdout

    bad = run_cli(tmp_path, "visible-faces", "ledge.stl", "--visible", VISIBLE)
    assert bad.returncode == 1
    assert "ledge    FAIL" in bad.stdout and "z 10." in bad.stdout
    bad_json = run_cli(tmp_path, "visible-faces", "ledge.stl", "--visible", VISIBLE, "--json")
    assert bad_json.returncode == 1
    rep = json.loads(bad_json.stdout)
    assert rep["status"] == "FAIL"
    assert f"FAIL {len(rep['mesh'][0]['pieces'])} unsupported pieces" in bad.stdout

    sliced = run_cli(tmp_path, "visible-faces", "--gcode", "plate.gcode", "--visible", f"SLAB:{VISIBLE}")
    assert sliced.returncode == 1
    assert "SLAB_id_0_copy_0 FAIL 0 support moves and 2 unsupported islands" in sliced.stdout

    missing = run_cli(tmp_path, "visible-faces", "nope.stl", "--visible", VISIBLE)
    assert missing.returncode == 2 and "nope.stl" in missing.stderr
    no_side = run_cli(tmp_path, "visible-faces", "wall.stl")
    assert no_side.returncode == 2 and "--visible" in no_side.stderr
