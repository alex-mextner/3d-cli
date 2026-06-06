from __future__ import annotations

import importlib
import shlex

import pytest

from errors import InvalidArgument, UsageError
from import_formats import plan_import, wrapper_source, write_wrapper


def test_stl_import_plan_writes_openscad_wrapper(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")
    out = tmp_path / "part.import.scad"

    plan = plan_import(str(src), out_path=str(out))
    written = write_wrapper(plan)

    assert written == str(out)
    assert plan.action == "wrapper"
    text = out.read_text(encoding="utf-8")
    assert 'import("part.stl", convexity = 10);' in text
    assert "3d render" in text


def test_obj_import_plan_describes_conversion_without_writing(tmp_path) -> None:
    src = tmp_path / "asset.obj"
    src.write_text("# obj\n", encoding="utf-8")

    plan = plan_import(str(src))

    assert plan.action == "plan"
    assert plan.output_path is None
    assert any("convert" in step.lower() for step in plan.steps)
    assert any(".stl" in step.lower() for step in plan.steps)


def test_direct_format_plan_describes_wrapper_without_writing(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    plan = plan_import(str(src), mode="plan")

    assert plan.action == "plan"
    assert plan.output_path == str(tmp_path / "part.import.scad")
    assert any("Write OpenSCAD wrapper" in step for step in plan.steps)
    assert not any("Convert STL mesh" in step for step in plan.steps)


def test_conversion_plan_honors_requested_wrapper_output(tmp_path) -> None:
    src = tmp_path / "asset.obj"
    src.write_text("# obj\n", encoding="utf-8")
    out = tmp_path / "custom-wrapper.scad"

    plan = plan_import(str(src), out_path=str(out))

    assert plan.action == "plan"
    assert plan.output_path == str(out)
    assert any(str(out) in step for step in plan.steps)


def test_conversion_plan_preserves_wrapper_options(tmp_path) -> None:
    src = tmp_path / "asset.obj"
    src.write_text("# obj\n", encoding="utf-8")

    plan = plan_import(str(src), scale=25.4, convexity=12)

    follow_up = "\n".join(plan.steps)
    assert "--scale 25.4" in follow_up
    assert "--convexity 12" in follow_up


def test_conversion_plan_quotes_follow_up_paths_with_spaces(tmp_path) -> None:
    project = tmp_path / "My Models"
    project.mkdir()
    src = project / "asset scan.obj"
    src.write_text("# obj\n", encoding="utf-8")

    plan = plan_import(str(src))

    follow_up = "\n".join(plan.steps)
    assert shlex.quote(str(project / "asset scan.stl")) in follow_up
    assert shlex.quote(str(project / "asset scan.import.scad")) in follow_up


def test_wrapper_output_cannot_overwrite_input_mesh(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src), out_path=str(src))

    assert "output path" in exc.value.render(color=False)


def test_wrapper_output_must_be_scad_source_path(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src), out_path=str(tmp_path / "previous-export.stl"))

    rendered = exc.value.render(color=False)
    assert "output path" in rendered
    assert ".scad" in rendered


def test_wrapper_output_write_failure_is_structured_error(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")
    out_dir = tmp_path / "wrapper.scad"
    out_dir.mkdir()
    plan = plan_import(str(src), out_path=str(out_dir))

    with pytest.raises(UsageError) as exc:
        write_wrapper(plan)

    assert "could not write import wrapper" in exc.value.render(color=False)


def test_direct_wrapper_requires_openscad_readable_extension(tmp_path) -> None:
    src = tmp_path / "part.bin"
    src.write_bytes(b"solid part\nendsolid part\n")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src), format_override="stl")

    rendered = exc.value.render(color=False)
    assert "input extension" in rendered
    assert ".stl" in rendered


def test_wrapper_source_uses_absolute_path_when_relative_path_fails(tmp_path, monkeypatch) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")
    out = tmp_path / "part.import.scad"
    plan = plan_import(str(src), out_path=str(out))

    def fail_relpath(path: str, start: str | None = None) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr("import_formats.os.path.relpath", fail_relpath)

    text = wrapper_source(plan)

    assert str(src) in text
    assert 'import("' in text


def test_wrapper_mode_rejects_conversion_only_format(tmp_path) -> None:
    src = tmp_path / "mesh.glb"
    src.write_bytes(b"glTF")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src), mode="wrapper")

    assert "accepted" in exc.value.render(color=False)
    assert ".stl" in exc.value.render(color=False)


def test_unknown_import_format_is_structured_error(tmp_path) -> None:
    src = tmp_path / "shape.abc"
    src.write_text("x", encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src))

    rendered = exc.value.render(color=False)
    assert "import format" in rendered
    assert ".obj" in rendered


def test_nonfinite_scale_is_structured_error(tmp_path) -> None:
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    with pytest.raises(InvalidArgument) as exc:
        plan_import(str(src), scale=float("nan"))

    assert "--scale" in exc.value.render(color=False)


def test_import_command_help_and_no_args() -> None:
    command = importlib.import_module("commands.import")

    assert command.run(["--help"]) == 0
    assert command.run([]) == 1


def test_import_command_wrapper_output_mentions_output_once(tmp_path, capsys) -> None:
    command = importlib.import_module("commands.import")
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    assert command.run([str(src)]) == 0

    assert capsys.readouterr().out.count("output:") == 1


def test_import_command_rejects_value_flag_followed_by_another_flag(tmp_path) -> None:
    command = importlib.import_module("commands.import")
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid part\nendsolid part\n")

    with pytest.raises(UsageError):
        command.run([str(src), "--format", "--mode", "plan"])
