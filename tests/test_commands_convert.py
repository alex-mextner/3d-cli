"""Unit tests for commands.convert — routing, collisions and the retarget alias."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from orca_project_3mf import MeshPart, write_project_3mf

import commands.convert as cv
from commands.convert import run
from errors import InputNotFound, UsageError

CUBE_VERTS = [
    (0.0, 0.0, 0.0),
    (10.0, 0.0, 0.0),
    (0.0, 10.0, 0.0),
    (10.0, 10.0, 0.0),
    (0.0, 0.0, 10.0),
    (10.0, 0.0, 10.0),
    (0.0, 10.0, 10.0),
    (10.0, 10.0, 10.0),
]
CUBE_TRIS = [
    (0, 2, 1),
    (1, 2, 3),
    (4, 5, 6),
    (5, 7, 6),
    (0, 1, 5),
    (0, 5, 4),
    (2, 7, 3),
    (2, 6, 7),
    (0, 4, 2),
    (2, 4, 6),
    (1, 3, 7),
    (1, 7, 5),
]


def _project(path: Path) -> Path:
    write_project_3mf(
        str(path),
        [MeshPart("part", CUBE_VERTS, CUBE_TRIS, plate=0)],
        bed=270.0,
    )
    return path


def _fake_run_tool(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str, list[str]]]
) -> None:
    """run_tool that records the call and produces the output the route expects."""

    def fake(deps: str, script: str, args: list[str]) -> int:
        calls.append((deps, script, list(args)))
        if script == "convert_mesh.py":
            out = args[1]
            Path(out).write_bytes(b"mesh-tool-output")
        if script == "render.py":
            out = args[args.index("-o") + 1]
            Path(out).write_bytes(b"png-output")
        return 0

    monkeypatch.setattr(cv, "run_tool", fake)


# --------------------------------------------------------------------------- usage


def test_no_args_prints_usage_and_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run([]) == 1
    assert "3d convert" in capsys.readouterr().out


def test_help_prints_usage_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0
    assert "MULTICOLOR-aware" in capsys.readouterr().out


def test_missing_input_file_is_input_not_found() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad", "out.stl"])


def test_one_positional_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as err:
        run(["only.scad"])
    assert "IN... OUT" in str(err.value)


def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    with pytest.raises(UsageError) as err:
        run([str(src), "out.stl", "--wat"])
    assert "--wat" in str(err.value)


def test_unknown_output_extension_lists_accepted_formats(tmp_path: Path) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    with pytest.raises(UsageError) as err:
        run([str(src), "out.step"])
    remediation = " ".join(err.value.remediation)
    assert ".usdz" in remediation and "3d import" in remediation


# --------------------------------------------------------------------------- routes


def test_single_scad_to_stl_goes_through_openscad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    monkeypatch.setattr(cv, "require_openscad", lambda cmd: "/usr/bin/openscad")
    runs: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        runs.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_bytes(b"binary-stl")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    out = tmp_path / "out.stl"
    assert run([str(src), str(out), "-D", "w=2"]) == 0
    assert runs and runs[0][0] == "/usr/bin/openscad" and "-D" in runs[0]
    assert out.read_bytes() == b"binary-stl"


def test_single_stl_to_stl_is_a_lossless_copy(tmp_path: Path) -> None:
    src = tmp_path / "mesh.stl"
    src.write_bytes(b"STL BYTES")
    out = tmp_path / "copy.stl"
    assert run([str(src), str(out)]) == 0
    assert out.read_bytes() == b"STL BYTES"


def test_project_3mf_to_stl_uses_its_world_coordinates(tmp_path: Path) -> None:
    src = _project(tmp_path / "project.3mf")
    out = tmp_path / "mesh.stl"
    assert run([str(src), str(out)]) == 0
    import struct

    data = out.read_bytes()
    (count,) = struct.unpack("<I", data[80:84])
    assert count == len(CUBE_TRIS)
    assert len(data) == 84 + count * 50


def test_project_3mf_to_3mf_without_options_repackages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _project(tmp_path / "project.3mf")
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    out = tmp_path / "renamed.3mf"
    assert run([str(src), str(out)]) == 0
    assert not calls  # a rename never touches the mesh worker
    with ZipFile(out) as zf:
        assert zf.read("3D/3dmodel.model") == ZipFile(src).read("3D/3dmodel.model")


def test_scad_to_3mf_runs_the_colour_aware_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "m.scad"
    src.write_text('color("red") cube(1);')
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    out = tmp_path / "project.3mf"
    assert run([str(src), str(out), "--bed", "220"]) == 0
    (deps, script, args) = calls[0]
    assert script == "convert_mesh.py"
    assert deps == "numpy,trimesh"
    assert args[0] == "project3mf" and args[1].endswith(".3mf")
    assert "--bed" in args and "220" in args


def test_usdz_route_adds_usd_core_to_the_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    assert run([str(src), str(tmp_path / "o.usdz")]) == 0
    assert calls[0][0] == "numpy,trimesh,usd-core"
    usdz_src = tmp_path / "scene.usdz"
    usdz_src.write_bytes(b"zip")
    calls.clear()
    assert run([str(usdz_src), str(tmp_path / "back.3mf")]) == 0
    assert calls[0][0] == "numpy,trimesh,usd-core"  # reading usdz needs pxr too


def test_png_of_a_scad_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    monkeypatch.setattr(cv, "require_openscad", lambda cmd: "/usr/bin/openscad")
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    out = tmp_path / "shot.png"
    assert run([str(src), str(out)]) == 0
    assert calls[0][1] == "render.py" and calls[0][2][0] == "single"
    assert out.read_bytes() == b"png-output"


def test_png_of_a_3mf_stages_through_an_stl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _project(tmp_path / "project.3mf")
    monkeypatch.setattr(cv, "require_openscad", lambda cmd: "/usr/bin/openscad")
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    out = tmp_path / "shot.png"
    assert run([str(src), str(out)]) == 0
    assert [c[1] for c in calls] == ["convert_mesh.py", "render.py"]


def test_png_with_two_inputs_is_a_usage_error(tmp_path: Path) -> None:
    one, two = tmp_path / "a.stl", tmp_path / "b.stl"
    one.write_bytes(b"x")
    two.write_bytes(b"y")
    with pytest.raises(UsageError) as err:
        run([str(one), str(two), str(tmp_path / "shot.png")])
    assert "ONE model" in str(err.value)


# --------------------------------------------------------------------------- collisions


def test_existing_target_refuses_with_the_free_name_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    out = tmp_path / "out.stl"
    out.write_bytes(b"in the way")
    with pytest.raises(UsageError) as err:
        run([str(src), str(out)])
    remediation = " ".join(err.value.remediation)
    assert f"3d convert {src} {tmp_path}/out-2.stl" in remediation
    assert "--force" in remediation
    assert out.read_bytes() == b"in the way"


def test_force_overwrites_the_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "mesh.stl"
    src.write_bytes(b"new bytes")
    out = tmp_path / "copy.stl"
    out.write_bytes(b"old bytes")
    assert run([str(src), str(out), "--force"]) == 0
    assert out.read_bytes() == b"new bytes"


def test_output_overwriting_its_input_auto_suffixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "mesh.stl"
    src.write_bytes(b"original")
    assert run([str(src), str(src)]) == 0
    assert "instead" in capsys.readouterr().out
    assert (tmp_path / "mesh-2.stl").read_bytes() == b"original"
    assert src.read_bytes() == b"original"


def test_extensionless_output_from_scad_becomes_3mf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    calls: list[tuple[str, str, list[str]]] = []
    _fake_run_tool(monkeypatch, calls)
    assert run([str(src), str(tmp_path / "hub")]) == 0
    assert (tmp_path / "hub.3mf").exists()
    assert calls[0][2][0] == "project3mf"


# --------------------------------------------------------------------------- retarget


def test_3mf_to_3mf_with_to_delegates_to_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _project(tmp_path / "spoon.3mf")
    seen: list[list[str]] = []

    def fake_retarget(argv: list[str]) -> int:
        seen.append(list(argv))
        out = argv[argv.index("-o") + 1]
        Path(out).write_bytes(b"retargeted-project")
        return 0

    monkeypatch.setattr("commands.retarget.run", fake_retarget)
    out = tmp_path / "spoon-u1.3mf"
    assert run([str(src), str(out), "--to", "u1", "--keep", "1"]) == 0
    argv = seen[0]
    assert argv[0] == str(src) and argv[1] == "-o" and argv[2].endswith(".3mf")
    assert argv[3:] == ["--to", "u1", "--keep", "1"]
    assert out.read_bytes() == b"retargeted-project"


def test_3mf_extensionless_with_to_fills_the_3mf_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _project(tmp_path / "spoon.3mf")

    def fake_retarget(argv: list[str]) -> int:
        Path(argv[argv.index("-o") + 1]).write_bytes(b"retargeted")
        return 0

    monkeypatch.setattr("commands.retarget.run", fake_retarget)
    assert run([str(src), str(tmp_path / "spoon-u1"), "--to", "u1"]) == 0
    assert (tmp_path / "spoon-u1.3mf").read_bytes() == b"retargeted"


def test_retarget_opts_on_a_scad_input_is_a_usage_error(tmp_path: Path) -> None:
    src = tmp_path / "m.scad"
    src.write_text("cube(1);")
    with pytest.raises(UsageError) as err:
        run([str(src), str(tmp_path / "o.3mf"), "--to", "u1"])
    assert "--to" in str(err.value)


def test_failed_retarget_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _project(tmp_path / "spoon.3mf")
    monkeypatch.setattr("commands.retarget.run", lambda argv: 1)
    out = tmp_path / "spoon-u1.3mf"
    assert run([str(src), str(out), "--to", "u1"]) == 1
    assert not out.exists()
    assert not [p for p in os.listdir(tmp_path) if "stage" in p]
