"""Unit tests for misc smaller command modules."""
from __future__ import annotations

import pathlib
from typing import Any

import pytest
from commands.params import run as params_run
from commands.section import run as section_run
from commands.multi import run as multi_run
from commands.mesh import run as mesh_run
from commands.match import run as match_run
from commands.collision import run as collision_run
from commands.fit_camera import run as fit_run
from commands.preprocess import run as preprocess_run
from commands.lint import run as lint_run
from commands.compare import run as compare_run
from errors import InputNotFound, UsageError

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
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
        with pytest.raises(InputNotFound):
            fit_run(["model.scad", "ref.png"])


def test_fit_camera_missing_ref() -> None:
    model = "model.scad"
    ref = "ref.png"

    def exists(path: str) -> bool:
        return path == model

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.path.isfile", exists)
        mp.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
        with pytest.raises(InputNotFound):
            fit_run([model, ref])


def test_fit_camera_unknown_option(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(UsageError):
        fit_run(["model.scad", "ref.png", "--bogus"])


def test_fit_camera_runs(monkeypatch: Any) -> None:
    seen: list[str] = []
    seen_deps = ""

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        nonlocal seen_deps
        seen_deps = deps
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)
    assert fit_run(["model.scad", "ref.png", "--out", "cam.json", "--el-range", "-20,75"]) == 0
    assert seen_deps == "numpy,pillow"
    assert seen == [
        "--model",
        "model.scad",
        "--ref",
        "ref.png",
        "--out",
        "cam.json",
        "--el-range",
        "-20,75",
    ]


def test_fit_camera_bool_flag(monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)
    assert fit_run(["model.scad", "ref.png", "--draw-axes"]) == 0
    assert "--draw-axes" in seen


def test_fit_camera_seed_forwarded(monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)
    assert fit_run(["model.scad", "ref.png", "--seed", "11"]) == 0
    assert "--seed" in seen
    assert seen[seen.index("--seed") + 1] == "11"


def test_fit_camera_value_flag_equals_form(monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)
    assert fit_run(["model.scad", "ref.png", "--el-range=-15,70"]) == 0
    assert "--el-range=-15,70" in seen


def test_fit_camera_spatial_flags_forwarded(monkeypatch: Any) -> None:
    seen: list[str] = []
    seen_deps = ""

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        nonlocal seen_deps
        seen_deps = deps
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)
    assert (
        fit_run(
            [
                "model.scad",
                "ref.png",
                "--spatial-report",
                "report",
                "--trace",
                "trace.jsonl",
                "--objective",
                "contour",
                "--mask-polarity",
                "light",
                "--backplate",
                "photo.jpg",
            ]
        )
        == 0
    )
    assert seen_deps == "numpy,pillow,scipy"
    assert seen[seen.index("--spatial-report") + 1] == "report"
    assert seen[seen.index("--trace") + 1] == "trace.jsonl"
    assert seen[seen.index("--objective") + 1] == "contour"
    assert seen[seen.index("--mask-polarity") + 1] == "light"
    assert seen[seen.index("--backplate") + 1] == "photo.jpg"


def test_fit_camera_proof_aliases_forward_to_current_surface(monkeypatch: Any) -> None:
    seen: list[str] = []
    seen_deps = ""

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        nonlocal seen_deps
        seen_deps = deps
        seen[:] = args
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)

    assert (
        fit_run(
            [
                "model.scad",
                "mask.png",
                "--ref-polarity",
                "bright",
                "--proof-reference",
                "photo.jpg",
                "--search-mode",
                "proof",
            ]
        )
        == 0
    )

    assert seen_deps == "numpy,pillow,scipy"
    assert "--ref-polarity" not in seen
    assert "--proof-reference" not in seen
    assert seen[seen.index("--mask-polarity") + 1] == "light"
    assert seen[seen.index("--backplate") + 1] == "photo.jpg"
    assert seen[seen.index("--search-mode") + 1] == "proof"


def test_fit_camera_explicit_normal_search_keeps_light_deps(monkeypatch: Any) -> None:
    seen_deps = ""

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        nonlocal seen_deps
        seen_deps = deps
        return 0

    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.fit_camera.exec_tool", fake_exec_tool)

    assert fit_run(["model.scad", "ref.png", "--search-mode", "normal"]) == 0

    assert seen_deps == "numpy,pillow"


def test_fit_camera_missing_backplate() -> None:
    model = "model.scad"
    ref = "ref.png"

    def exists(path: str) -> bool:
        return path in {model, ref}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.path.isfile", exists)
        mp.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
        with pytest.raises(InputNotFound):
            fit_run([model, ref, "--backplate", "missing.jpg"])


def test_fit_camera_missing_proof_reference() -> None:
    model = "model.scad"
    ref = "ref.png"

    def exists(path: str) -> bool:
        return path in {model, ref}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.path.isfile", exists)
        mp.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
        with pytest.raises(InputNotFound):
            fit_run([model, ref, "--proof-reference", "missing.jpg"])


def test_fit_camera_value_flag_needs_value(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(UsageError):
        fit_run(["model.scad", "ref.png", "--out"])


# --- preprocess ---


def test_preprocess_no_args() -> None:
    assert preprocess_run([]) == 1


def test_preprocess_help() -> None:
    assert preprocess_run(["--help"]) == 0


def test_preprocess_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.preprocess.exec_tool", lambda d, s, a: 0)
    assert preprocess_run(["ref.png", "-o", "out/"]) == 0


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
    with pytest.raises(InputNotFound):
        compare_run([str(scad), "ref.png"])
