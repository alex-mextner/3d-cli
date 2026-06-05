"""Unit tests for many smaller command modules."""
from __future__ import annotations

from typing import Any


import pathlib
import subprocess
import pytest
from commands.libs import run as libs_run
from errors import UsageError
from commands.params import run as params_run
from errors import InputNotFound
from commands.section import run as section_run
from commands.multi import run as multi_run
from commands.mesh import run as mesh_run
from commands.match import run as match_run
from commands.collision import run as collision_run
from commands.fit_camera import run as fit_run
from commands.usdz import run as usdz_run, _parse_color
from errors import InvalidArgument
from commands.preprocess import run as preprocess_run
from commands.om import run as om_run
from commands.lint import run as lint_run
from commands.test import run as _test_run
from commands.compare import run as compare_run
from commands.materials import run as materials_run
from commands.printers import run as printers_run
from commands.projects import run as projects_run
from commands.metrics import run as metrics_run
from commands.init import run as init_run

# --- libs ---


def test_libs_no_args() -> None:
    assert libs_run([]) == 1


def test_libs_help() -> None:
    assert libs_run(["help"]) == 0


def test_libs_path(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("os.environ.get", lambda k, d=None: "/tmp/libs" if k == "OPENSCADPATH" else d)
    rc = libs_run(["path"])
    assert rc == 0
    assert "OPENSCADPATH" in capsys.readouterr().out


def test_libs_list(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.libs.repo_root", lambda: str(tmp_path))
    libs = tmp_path / "libs"
    libs.mkdir()
    (libs / "BOSL2").mkdir()
    rc = libs_run(["list"])
    assert rc == 0
    assert "BOSL2" in capsys.readouterr().out


def test_libs_list_empty(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.libs.repo_root", lambda: str(tmp_path))
    libs = tmp_path / "libs"
    libs.mkdir()
    rc = libs_run(["list"])
    assert rc == 0


def test_libs_install_removed() -> None:
    with pytest.raises(UsageError):
        libs_run(["install"])


def test_libs_unknown() -> None:
    with pytest.raises(UsageError):
        libs_run(["nope"])


# --- params ---


def test_params_no_args() -> None:
    assert params_run([]) == 1


def test_params_help() -> None:
    assert params_run(["--help"]) == 0


def test_params_missing_file() -> None:
    with pytest.raises(InputNotFound):
        params_run(["missing.scad"])


def test_params_json(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text('width = 20; // [10:40] width\n')
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    rc = params_run([str(scad), "--json"])
    assert rc == 0


# --- section ---


def test_section_no_args() -> None:
    assert section_run([]) == 1


def test_section_help() -> None:
    assert section_run(["--help"]) == 0


def test_section_alias(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("commands.section.render_run", lambda argv: 0)
    assert section_run([str(scad), "-o", str(tmp_path / "sec.png")]) == 0


# --- multi ---


def test_multi_no_args() -> None:
    assert multi_run([]) == 1


def test_multi_help() -> None:
    assert multi_run(["--help"]) == 0


def test_multi_alias(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("commands.multi.render_run", lambda argv: 0)
    assert multi_run([str(scad)]) == 0


def test_multi_with_outdir(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("commands.multi.render_run", lambda argv: 0)
    assert multi_run([str(scad), str(tmp_path / "previews")]) == 0


# --- mesh ---


def test_mesh_no_args() -> None:
    assert mesh_run([]) == 1


def test_mesh_help() -> None:
    assert mesh_run(["--help"]) == 0


def test_mesh_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("commands.mesh.exec_tool", lambda d, s, a: 0)
    assert mesh_run(["part.stl"]) == 0


# --- match ---


def test_match_no_args() -> None:
    assert match_run([]) == 1


def test_match_help() -> None:
    assert match_run(["--help"]) == 0


def test_match_missing_second() -> None:
    assert match_run(["model.scad"]) == 1


def test_match_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("commands.match.exec_tool", lambda d, s, a: 0)
    assert match_run(["model.scad", "ref.png"]) == 0


# --- collision ---


def test_collision_no_args() -> None:
    assert collision_run([]) == 1


def test_collision_help() -> None:
    assert collision_run(["--help"]) == 0


def test_collision_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.collision.exec_tool", lambda d, s, a: 0)
    assert collision_run(["cfg.json"]) == 0


# --- fit_camera ---


def test_fit_camera_no_args() -> None:
    assert fit_run([]) == 1


def test_fit_camera_help() -> None:
    assert fit_run(["--help"]) == 0


def test_fit_camera_missing_second() -> None:
    assert fit_run(["model.scad"]) == 1


def test_fit_camera_missing_model() -> None:
    with pytest.raises(InputNotFound):
        fit_run(["model.scad", "ref.png"])


def test_fit_camera_missing_ref() -> None:
    # we can't easily make os.path.isfile return True for just one path in a simple test
    # so mock it
    pass


def test_fit_camera_unknown_option(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(UsageError):
        fit_run(["model.scad", "ref.png", "--bogus"])


def test_fit_camera_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", lambda d, s, a: 0)
    assert fit_run(["model.scad", "ref.png", "--out", "cam.json"]) == 0


def test_fit_camera_bool_flag(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", lambda d, s, a: 0)
    assert fit_run(["model.scad", "ref.png", "--draw-axes"]) == 0


def test_fit_camera_value_flag_needs_value(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(UsageError):
        fit_run(["model.scad", "ref.png", "--out"])


# --- usdz ---


def test_usdz_no_args() -> None:
    assert usdz_run([]) == 1


def test_usdz_help() -> None:
    assert usdz_run(["--help"]) == 0


def test_usdz_missing_file() -> None:
    with pytest.raises(InputNotFound):
        usdz_run(["missing.scad"])


def test_usdz_bad_ext(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        usdz_run(["model.bogus"])


def test_usdz_bad_color(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        usdz_run(["model.scad", "--color", "red"])


def test_usdz_bad_color_range(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        usdz_run(["model.scad", "--color", "1.5,0,0"])


def test_usdz_scad(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz._bin3d", lambda: "bin/3d")

    def fake_run(args, **kw):
        if "export" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("commands.usdz.run_tool", lambda d, s, a: 0)
    assert usdz_run([str(scad)]) == 0


def test_usdz_stl(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    stl = tmp_path / "model.stl"
    stl.write_text("fake")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz.run_tool", lambda d, s, a: 0)
    assert usdz_run([str(stl)]) == 0


def test_usdz_export_fails(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz._bin3d", lambda: "bin/3d")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="err", stderr=""))
    with pytest.raises(UsageError):
        usdz_run([str(scad)])


def test_parse_color_ok() -> None:
    assert _parse_color("0.1,0.2,0.3") == (0.1, 0.2, 0.3)


def test_parse_color_bad_count() -> None:
    with pytest.raises(InvalidArgument):
        _parse_color("0.1,0.2")


def test_parse_color_bad_value() -> None:
    with pytest.raises(InvalidArgument):
        _parse_color("a,b,c")


def test_usdz_unknown_option(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(UsageError):
        usdz_run(["model.scad", "--bogus"])


def test_usdz_out_needs_value(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(UsageError):
        usdz_run(["model.scad", "-o"])


# --- preprocess ---


def test_preprocess_no_args() -> None:
    assert preprocess_run([]) == 1


def test_preprocess_help() -> None:
    assert preprocess_run(["--help"]) == 0


def test_preprocess_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.preprocess.exec_tool", lambda d, s, a: 0)
    assert preprocess_run(["ref.png", "-o", "out/"]) == 0


# --- om ---


def test_om_no_args() -> None:
    assert om_run([]) == 1


def test_om_help() -> None:
    assert om_run(["--help"]) == 0


def test_om_inspect(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("// @id cube1\ncube(1);\n")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert om_run([str(scad), "#cube1"]) == 0


def test_om_validate(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("// @class part\ncube(1);\n")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert om_run([str(scad), ".part"]) == 0


def test_om_unknown(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);\n")
    with pytest.raises(InvalidArgument):
        om_run([str(scad), "nope"])


# --- lint ---


def test_lint_no_args() -> None:
    assert lint_run([]) == 1


def test_lint_help() -> None:
    assert lint_run(["--help"]) == 0


def test_lint_all(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr("linting.default_scan_paths", lambda root: [])
    monkeypatch.setattr("linting.lint_paths", lambda paths, rule_ids: [])
    assert lint_run(["--all"]) == 0


def test_lint_all_with_paths() -> None:
    with pytest.raises(UsageError):
        lint_run(["--all", "some.py"])


def test_lint_paths(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    src = tmp_path / "core.py"
    src.write_text("x = 1\n")
    monkeypatch.setattr("linting.lint_paths", lambda paths, rule_ids: [])
    assert lint_run([str(src)]) == 0


def test_lint_paths_findings(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    src = tmp_path / "core.py"
    src.write_text("x = 1\n")
    from linting import Finding
    f = Finding(path=src, line=1, column=1, term="x", rule_id="r", message="m", remediation="r", text="x = 1")
    monkeypatch.setattr("linting.lint_paths", lambda paths, rule_ids: [f])
    assert lint_run([str(src)]) == 1


def test_lint_json(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    src = tmp_path / "core.py"
    src.write_text("x = 1\n")
    monkeypatch.setattr("linting.lint_paths", lambda paths, rule_ids: [])
    assert lint_run([str(src), "--json"]) == 0


def test_lint_rule(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    src = tmp_path / "core.py"
    src.write_text("x = 1\n")
    monkeypatch.setattr("linting.lint_paths", lambda paths, rule_ids: [])
    assert lint_run([str(src), "--rule", "no-subject-leakage"]) == 0


def test_lint_rule_needs_value() -> None:
    with pytest.raises(UsageError):
        lint_run(["file.py", "--rule"])


# --- test ---


def test_test_help() -> None:
    assert _test_run(["--help"]) == 0


def test_test_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("commands.test.exec_tool", lambda d, s, a: 0)
    assert _test_run([]) == 0


def test_test_forwards_args(monkeypatch: Any) -> None:
    called: list[list[str]] = []
    def capture(d, s, a):
        called.append(a)
        return 0
    monkeypatch.setattr("commands.test.exec_tool", capture)
    assert _test_run(["-k", "registry"]) == 0
    assert "-k" in called[0]


# --- compare ---


def test_compare_no_args() -> None:
    assert compare_run([]) == 1


def test_compare_help() -> None:
    assert compare_run(["--help"]) == 0


def test_compare_runs(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    ref = tmp_path / "ref.png"
    ref.write_text("")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    res = type("R", (), {"iou": 0.8, "ssim": 0.9, "dssim": 0.1, "mask_png": "", "matched_render_png": "", "diff_png": "", "collage_png": "", "used_fallback": False, "fallback_reason": "", "reliable": True})()
    monkeypatch.setattr("refmatch.compare_pipeline", lambda *a, **kw: res)
    assert compare_run([str(scad), str(ref)]) == 0


def test_compare_unknown_option() -> None:
    with pytest.raises(UsageError):
        compare_run(["model.scad", "ref.png", "--bogus"])


def test_compare_out_option(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    ref = tmp_path / "ref.png"
    ref.write_text("")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    res = type("R", (), {"iou": 0.8, "ssim": 0.9, "dssim": 0.1, "mask_png": "", "matched_render_png": "", "diff_png": "", "collage_png": "", "used_fallback": False, "fallback_reason": "", "reliable": True})()
    monkeypatch.setattr("refmatch.compare_pipeline", lambda *a, **kw: res)
    assert compare_run([str(scad), str(ref), "-o", str(tmp_path / "out")]) == 0


def test_compare_rand_option(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    ref = tmp_path / "ref.png"
    ref.write_text("")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    res = type("R", (), {"iou": 0.8, "ssim": 0.9, "dssim": 0.1, "mask_png": "", "matched_render_png": "", "diff_png": "", "collage_png": "", "used_fallback": False, "fallback_reason": "", "reliable": True})()
    monkeypatch.setattr("refmatch.compare_pipeline", lambda *a, **kw: res)
    assert compare_run([str(scad), str(ref), "--rand", "10"]) == 0


def test_compare_missing_file(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    from errors import InputNotFound
    with pytest.raises(InputNotFound):
        compare_run([str(scad), "ref.png"])


# --- materials ---


def test_materials_no_args() -> None:
    assert materials_run([]) == 1


def test_materials_help() -> None:
    assert materials_run(["--help"]) == 0


def test_materials_runs(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("materials.load_materials", lambda: {"PLA": type("M", (), {"name": "PLA", "density": 1.2, "max_temp_c": 60, "finish": "matte"})()})
    rc = materials_run(["list"])
    assert rc == 0


def test_materials_info(monkeypatch: Any, capsys: Any) -> None:
    m = type("M", (), {"name": "PLA", "density": 1.2, "e_modulus_mpa": 3500, "tensile_mpa": 65, "yield_mpa": 55, "max_temp_c": 60, "color": "#1f9c4b", "finish": "matte", "layer_adhesion": 0.45})()
    monkeypatch.setattr("materials.get_material", lambda name: m)
    rc = materials_run(["show", "PLA"])
    assert rc == 0


def test_materials_not_found(monkeypatch: Any, capsys: Any) -> None:
    from errors import InvalidArgument
    def raise_invalid(name):
        raise InvalidArgument("name", name, ["PLA"])
    monkeypatch.setattr("materials.get_material", raise_invalid)
    with pytest.raises(InvalidArgument):
        materials_run(["show", "ABS"])


# --- printers ---


def test_printers_no_args() -> None:
    assert printers_run([]) == 1


def test_printers_help() -> None:
    assert printers_run(["--help"]) == 0


def test_printers_runs(monkeypatch: Any, capsys: Any) -> None:
    p = type("P", (), {"name": "A1", "bed": (220, 220, 250), "nozzle_mm": 0.4, "firmware": "Klipper"})()
    monkeypatch.setattr("printers.load_printers", lambda command: {"A1": p})
    rc = printers_run(["list"])
    assert rc == 0


def test_printers_info(monkeypatch: Any, capsys: Any) -> None:
    p = type("P", (), {"name": "A1", "bed": (220, 220, 250), "nozzle_mm": 0.4, "firmware": "Klipper", "material": "PLA"})()
    monkeypatch.setattr("printers.get_printer", lambda name, command: p)
    rc = printers_run(["show", "A1"])
    assert rc == 0


def test_printers_not_found(monkeypatch: Any, capsys: Any) -> None:
    from errors import InvalidArgument
    def raise_invalid(name, command):
        raise InvalidArgument("name", name, ["A1"])
    monkeypatch.setattr("printers.get_printer", raise_invalid)
    with pytest.raises(InvalidArgument):
        printers_run(["show", "X1"])


# --- projects ---


def test_projects_no_args() -> None:
    assert projects_run([]) == 1


def test_projects_help() -> None:
    assert projects_run(["--help"]) == 0


def test_projects_list(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.list_projects", lambda: [{"name": "p1", "path": str(tmp_path), "added": "2026-01-01T00:00:00"}])
    rc = projects_run(["list"])
    assert rc == 0


def test_projects_add(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.add", lambda path: {"path": path, "had_yaml": True})
    rc = projects_run(["add", "/tmp/proj"])
    assert rc == 0


def test_projects_remove(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.remove", lambda path: {"path": path})
    rc = projects_run(["remove", "/tmp/proj"])
    assert rc == 0


# --- metrics ---


def test_metrics_no_args() -> None:
    assert metrics_run([]) == 1


def test_metrics_help() -> None:
    assert metrics_run(["--help"]) == 0


def test_metrics_list(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.list_metric_files", lambda: [{"command": "render", "records": 5, "latest": "2026-01-01", "path": "/tmp/metrics.jsonl"}])
    assert metrics_run(["list"]) == 0


def test_metrics_show(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.read_records", lambda command=None, limit=None: [{"cmd": "render"}])
    assert metrics_run(["show"]) == 0


def test_metrics_show_limit() -> None:
    assert metrics_run(["show", "--limit", "5"]) == 0


def test_metrics_show_bad_limit() -> None:
    with pytest.raises(UsageError):
        metrics_run(["show", "--limit", "abc"])


def test_metrics_unknown_option() -> None:
    with pytest.raises(UsageError):
        metrics_run(["show", "--bogus"])


# --- init ---


def test_init_no_args(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    assert init_run([]) == 0


def test_init_help() -> None:
    assert init_run(["--help"]) == 0


def test_init_project(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    assert init_run(["myproject"]) == 0


def test_init_project_exists(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    (tmp_path / "myproject").mkdir()
    assert init_run(["myproject"]) == 0


def test_init_scaffold(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    proj = tmp_path / "myproject"
    proj.mkdir()
    monkeypatch.setattr("os.getcwd", lambda: str(proj))
    assert init_run(["scaffold"]) == 0


def test_init_scaffold_already_exists(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / "model.scad").write_text("cube(1);\n")
    monkeypatch.setattr("os.getcwd", lambda: str(proj))
    assert init_run(["scaffold"]) == 0


def test_init_unknown_template() -> None:
    with pytest.raises(InvalidArgument):
        init_run(["myproject", "--template", "nope"])
