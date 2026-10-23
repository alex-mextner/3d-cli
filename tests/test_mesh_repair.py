"""lib/mesh_repair.py + `3d mesh --repair`: the adapter over the public meshfix library (ticket #432).

The tests run against the REAL library (no fake backend): they need meshfix importable, or a checkout
that can be built (git + cmake + a C++17 compiler); otherwise they skip. Each repair test also runs with the
repair disabled (flags=0) and shows the defect is still there, so none of them can pass on an untouched mesh.
"""
from __future__ import annotations

import os
import sys

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

import mesh_check  # noqa: E402
import mesh_repair  # noqa: E402
from errors import MissingDependency  # noqa: E402

_CUBE_V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
# outward triangles; rows 2 and 3 (the top quad) are the ones the tests remove
_CUBE_F = np.array([[0, 3, 2], [0, 2, 1], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                    [3, 7, 6], [3, 6, 2], [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5]])
_OPEN_F = np.delete(_CUBE_F, [2, 3], axis=0)


@pytest.fixture(scope="module")
def meshfix_module():
    try:
        return mesh_repair.load_meshfix()
    except (MissingDependency, ImportError) as exc:
        pytest.skip(f"meshfix is not available here: {exc}")


@pytest.fixture()
def open_cube_stl(tmp_path):
    path = tmp_path / "open_cube.stl"
    trimesh.Trimesh(_CUBE_V, _OPEN_F, process=False).export(path)
    return path


def test_repair_mesh_closes_a_hole_and_keeps_the_report_keys(meshfix_module) -> None:
    v, f, report = mesh_repair.repair_mesh(_CUBE_V, _OPEN_F)
    for key in mesh_repair.STABLE_REPORT_KEYS:
        assert key in report, key
    assert report["watertight_before"] is False and report["watertight_after"] is True
    assert report["volume_after"] == pytest.approx(1000.0)
    assert report["faces_before"] == 10 and report["faces_after"] == len(f) == 12
    assert report["holes_filled"] == 1
    assert report["engine"].startswith("meshfix ")
    assert trimesh.Trimesh(v, f, process=True).is_watertight


def test_repair_mesh_with_the_repair_off_leaves_the_hole(meshfix_module) -> None:
    v, f, report = mesh_repair.repair_mesh(_CUBE_V, _OPEN_F, flags=0)
    assert report["watertight_after"] is False and report["holes_filled"] == 0
    assert len(f) == 10


def test_repair_file_stl_round_trip(meshfix_module, open_cube_stl, tmp_path) -> None:
    dst = tmp_path / "fixed.stl"
    report = mesh_repair.repair_file(str(open_cube_stl), str(dst))
    assert report["watertight_after"] is True
    fixed = trimesh.load(dst, process=True)
    assert fixed.is_watertight and fixed.is_winding_consistent
    assert fixed.volume == pytest.approx(1000.0, rel=1e-6)


def test_repair_file_3mf_goes_through_trimesh(meshfix_module, tmp_path) -> None:
    src = tmp_path / "open_cube.3mf"
    try:
        trimesh.Trimesh(_CUBE_V, _OPEN_F, process=False).export(src)
    except Exception as exc:  # the 3MF exporter needs optional trimesh extras
        pytest.skip(f"trimesh cannot write 3MF here: {exc}")
    dst = tmp_path / "fixed.stl"
    report = mesh_repair.repair_file(str(src), str(dst))
    assert report["watertight_before"] is False and report["watertight_after"] is True
    assert trimesh.load(dst, process=True).is_watertight


def test_format_report_summarises_the_proof(meshfix_module) -> None:
    _, _, report = mesh_repair.repair_mesh(_CUBE_V, _OPEN_F)
    text = mesh_repair.format_report(report)
    assert "watertight no -> yes" in text and "faces 10 -> 12" in text and "holes filled 1" in text


def test_mesh_repair_flag_repairs_then_gates_the_result(meshfix_module, open_cube_stl, tmp_path, capsys) -> None:
    out = tmp_path / "gate_fixed.stl"
    with pytest.raises(SystemExit) as plain:
        mesh_check.main(["mesh_check.py", str(open_cube_stl)])
    assert plain.value.code == 1  # the open cube fails the gate as it is

    with pytest.raises(SystemExit) as repaired:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair", "-o", str(out)])
    assert repaired.value.code == 0  # the repaired copy passes
    shown = capsys.readouterr().out
    assert "watertight no -> yes" in shown and f"repaired mesh written to {out}" in shown
    assert trimesh.load(out, process=True).is_watertight


def test_mesh_repair_flag_default_output_sits_next_to_the_input(meshfix_module, open_cube_stl) -> None:
    with pytest.raises(SystemExit) as repaired:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair"])
    assert repaired.value.code == 0
    assert (open_cube_stl.parent / "open_cube.fixed.stl").is_file()
    assert open_cube_stl.exists()  # the original is never overwritten


def test_output_flag_without_repair_is_a_usage_error(open_cube_stl, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "-o", "x.stl"])
    assert exc.value.code == 2
    assert "--repair" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("inp", "expected"),
    [("a/part.stl", "a/part.fixed.stl"), ("part.obj", "part.fixed.obj"), ("part.3mf", "part.fixed.stl"),
     ("model.scad", "model.fixed.stl")],
)
def test_default_repair_path(inp: str, expected: str) -> None:
    assert mesh_check.default_repair_path(inp) == expected


def test_missing_library_is_a_missing_dependency_with_the_install_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mesh_repair, "_module", None)
    monkeypatch.setattr(sys, "path", list(sys.path))  # a checkout added to the path stays out of later tests
    monkeypatch.setitem(sys.modules, "meshfix", None)  # `import meshfix` raises ImportError
    monkeypatch.setattr(mesh_repair, "_build_dir", lambda: str(tmp_path / "no-build"))
    monkeypatch.setattr(mesh_repair, "_repo_root", lambda: str(tmp_path / "no-repo"))
    monkeypatch.setattr(mesh_repair.shutil, "which", lambda _name: None)
    with pytest.raises(MissingDependency) as exc:
        mesh_repair.load_meshfix()
    assert exc.value.exit_code == 127
    assert "pip install" in exc.value.render(color=False) and mesh_repair.MESHFIX_TAG in exc.value.render(color=False)


def test_pinned_tag_matches_the_pip_requirement() -> None:
    assert mesh_repair.MESHFIX_PIP.endswith("@" + mesh_repair.MESHFIX_TAG)
    assert os.path.basename(mesh_repair.MESHFIX_URL) == "meshfix.git"


def test_repair_refuses_to_overwrite_its_input(open_cube_stl, capsys) -> None:
    before = open_cube_stl.read_bytes()
    with pytest.raises(SystemExit) as exc:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair", "-o", str(open_cube_stl)])
    assert exc.value.code == 2
    assert "overwrite its input" in capsys.readouterr().err
    assert open_cube_stl.read_bytes() == before


def test_repair_refuses_to_write_through_a_link_to_its_input(open_cube_stl, tmp_path, capsys) -> None:
    link, hard = tmp_path / "alias.stl", tmp_path / "hard.stl"
    link.symlink_to(open_cube_stl)
    os.link(open_cube_stl, hard)
    before = open_cube_stl.read_bytes()
    for alias in (link, hard):
        with pytest.raises(SystemExit) as exc:
            mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair", "-o", str(alias)])
        assert exc.value.code == 2
        assert "overwrite its input" in capsys.readouterr().err
    assert open_cube_stl.read_bytes() == before


def test_unsupported_output_extension_is_a_usage_error(meshfix_module, open_cube_stl, tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair", "-o", str(tmp_path / "fixed.gcode")])
    assert exc.value.code == 2
    assert ".gcode" in capsys.readouterr().err
    assert not (tmp_path / "fixed.gcode").exists()


def test_a_failed_build_leaves_no_half_configured_cache(monkeypatch, tmp_path) -> None:
    repo, build = tmp_path / "repo", tmp_path / "cache" / "meshfix-build"
    monkeypatch.setattr(mesh_repair, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(mesh_repair, "_build_dir", lambda: str(build))
    monkeypatch.setattr(mesh_repair.shutil, "which", lambda name: f"/usr/bin/{name}")
    (repo / "libs" / "meshfix" / ".git").mkdir(parents=True)

    def failing_run(cmd: list[str], what: str) -> None:
        build.mkdir(parents=True, exist_ok=True)
        (build / "CMakeCache.txt").write_text("stale")
        raise mesh_repair._missing(f"{what} failed")

    monkeypatch.setattr(mesh_repair, "_run", failing_run)
    with pytest.raises(MissingDependency):
        mesh_repair._ensure_checkout_and_build()
    assert not build.exists()


def test_repair_without_trimesh_is_an_error_not_a_silent_pass(open_cube_stl, monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "trimesh", None)  # `import trimesh` raises ImportError
    with pytest.raises(SystemExit) as exc:
        mesh_check.main(["mesh_check.py", str(open_cube_stl), "--repair"])
    assert exc.value.code == 127
    assert "needs trimesh" in capsys.readouterr().err


def test_concurrent_first_uses_clone_and_build_once(monkeypatch, tmp_path) -> None:
    """Two `3d` processes starting at once must not clone into the same directory or build in parallel."""
    import threading
    import time

    repo, build = tmp_path / "repo", tmp_path / "cache" / "meshfix-build"
    monkeypatch.setattr(mesh_repair, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(mesh_repair, "_build_dir", lambda: str(build))
    monkeypatch.setattr(mesh_repair.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[str] = []

    def fake_run(cmd: list[str], what: str) -> None:
        calls.append(cmd[0])
        time.sleep(0.2)  # long enough for the other threads to arrive
        if cmd[0] == "git":
            target = cmd[-1]
            os.makedirs(os.path.join(target, ".git"))
            os.makedirs(os.path.join(target, "python", "meshfix"))
        elif "--build" in cmd:
            os.makedirs(build, exist_ok=True)
            (build / "libmeshfix.so").write_text("")

    monkeypatch.setattr(mesh_repair, "_run", fake_run)
    results: list[tuple[str, str]] = []
    threads = [threading.Thread(target=lambda: results.append(mesh_repair._ensure_checkout_and_build())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls.count("git") == 1 and calls.count("cmake") == 2  # one clone, one configure + one build
    assert len(results) == 4 and len(set(results)) == 1
