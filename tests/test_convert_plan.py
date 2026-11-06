"""Unit tests for convert_plan — the route/collision policy of `3d convert`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import convert_plan as cp
from errors import UsageError


# --------------------------------------------------------------------------- formats


def test_input_format_accepts_every_read_format(tmp_path: Path) -> None:
    for fmt in cp.READ_FORMATS:
        assert cp.input_format(f"model.{fmt}") == fmt


def test_input_format_rejects_unknown_extension() -> None:
    with pytest.raises(UsageError) as err:
        cp.input_format("model.step")
    remediation = " ".join(err.value.remediation)
    assert ".scad" in remediation and ".usdz" in remediation
    assert "3d import" in remediation


def test_input_format_rejects_extensionless_path() -> None:
    with pytest.raises(UsageError) as err:
        cp.input_format("model")
    assert "no extension" in str(err.value)


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        ("out.stl", "stl"),
        ("out.STL", "stl"),
        ("out.3mf", "3mf"),
        ("out.usdz", "usdz"),
        ("out.png", "png"),
        ("some.dir/out", "3mf"),  # .dir is a directory component, not an extension
    ],
)
def test_output_format_from_extension(out: str, expected: str) -> None:
    assert cp.output_format(out, ["scad"]) == expected


def test_output_format_extensionless_keeps_single_stl_input_format() -> None:
    assert cp.output_format("copy", ["stl"]) == "stl"


def test_output_format_extensionless_keeps_single_3mf_input_format() -> None:
    assert cp.output_format("project-u1", ["3mf"]) == "3mf"


def test_output_format_extensionless_becomes_the_3mf_hub() -> None:
    assert cp.output_format("out", ["scad"]) == "3mf"
    assert cp.output_format("out", ["usdz"]) == "3mf"
    assert cp.output_format("out", ["stl", "3mf"]) == "3mf"


def test_output_format_rejects_unwritable_extension() -> None:
    with pytest.raises(UsageError) as err:
        cp.output_format("out.scad", ["stl"])
    remediation = " ".join(err.value.remediation)
    assert ".png" in remediation  # the accepted list names every writable format
    assert "3d import" in remediation


# --------------------------------------------------------------------------- routes


def test_png_needs_exactly_one_input() -> None:
    cp.check_route(["scad"], "png", retarget_opts=False)
    with pytest.raises(UsageError) as err:
        cp.check_route(["stl", "stl"], "png", retarget_opts=False)
    assert "ONE model" in str(err.value)


def test_retarget_opts_need_single_3mf_in_and_3mf_out() -> None:
    cp.check_route(["3mf"], "3mf", retarget_opts=True)
    for bad in ((["scad"], "3mf"), (["3mf"], "stl"), (["3mf", "3mf"], "3mf")):
        with pytest.raises(UsageError) as err:
            cp.check_route(*bad, retarget_opts=True)
        assert "--to" in str(err.value)


# --------------------------------------------------------------------------- collisions


def test_free_output_appends_increasing_suffixes(tmp_path: Path) -> None:
    target = tmp_path / "out.3mf"
    target.write_text("x")
    assert cp.free_output(str(target)) == str(tmp_path / "out-2.3mf")
    (tmp_path / "out-2.3mf").write_text("x")
    (tmp_path / "out-3.3mf").write_text("x")
    assert cp.free_output(str(target)) == str(tmp_path / "out-4.3mf")


def test_command_line_is_ready_to_run_and_quoted(tmp_path: Path) -> None:
    line = cp.command_line(["my model.scad"], str(tmp_path / "out 2.3mf"))
    assert line == f"3d convert 'my model.scad' '{tmp_path}/out 2.3mf'"


def test_existing_target_without_force_refuses_with_a_free_name_command(
    tmp_path: Path,
) -> None:
    inputs = [str(tmp_path / "model.scad")]
    target = tmp_path / "out.3mf"
    target.write_text("existing")
    with pytest.raises(UsageError) as err:
        cp.resolve_output(inputs, str(target), force=False)
    assert err.value.exit_code == 2
    remediation = " ".join(err.value.remediation)
    assert f"3d convert {inputs[0]} {tmp_path}/out-2.3mf" in remediation
    assert "--force" in remediation


def test_existing_target_with_force_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "out.3mf"
    target.write_text("existing")
    final, note = cp.resolve_output(["in.scad"], str(target), force=True)
    assert final == str(target)
    assert note == ""


def test_output_overwriting_its_input_auto_suffixes(tmp_path: Path) -> None:
    src = tmp_path / "a.stl"
    src.write_text("input")
    final, note = cp.resolve_output([str(src)], str(src), force=False)
    assert final == str(tmp_path / "a-2.stl")
    assert "overwrites the input" in note or "overwrite the input" in note
    assert src.read_text() == "input"


def test_output_equal_to_input_by_relative_path_auto_suffixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.stl"
    src.write_text("input")
    monkeypatch.chdir(tmp_path)
    final, _note = cp.resolve_output([str(src)], "a.stl", force=False)
    assert final == str(tmp_path / "a-2.stl")


def test_commit_moves_the_staged_file(tmp_path: Path) -> None:
    staged = cp.staging_path(str(tmp_path / "out.3mf"))
    with open(staged, "w") as handle:
        handle.write("result")
    cp.commit(staged, str(tmp_path / "out.3mf"), force=False, inputs=["in.scad"])
    assert (tmp_path / "out.3mf").read_text() == "result"
    assert not os.path.exists(staged)


def test_commit_refuses_a_target_that_appeared_late(tmp_path: Path) -> None:
    """The write-time re-check: a file that appears after planning still refuses."""
    staged = cp.staging_path(str(tmp_path / "out.3mf"))
    with open(staged, "w") as handle:
        handle.write("result")
    (tmp_path / "out.3mf").write_text("appeared meanwhile")
    with pytest.raises(UsageError) as err:
        cp.commit(staged, str(tmp_path / "out.3mf"), force=False, inputs=["in.scad"])
    assert f"{tmp_path}/out-2.3mf" in " ".join(err.value.remediation)
    assert not os.path.exists(staged)  # nothing left behind
    assert (tmp_path / "out.3mf").read_text() == "appeared meanwhile"


def test_commit_with_force_replaces_the_target(tmp_path: Path) -> None:
    staged = cp.staging_path(str(tmp_path / "out.3mf"))
    with open(staged, "w") as handle:
        handle.write("new")
    (tmp_path / "out.3mf").write_text("old")
    cp.commit(staged, str(tmp_path / "out.3mf"), force=True, inputs=["in.scad"])
    assert (tmp_path / "out.3mf").read_text() == "new"


def test_commit_without_a_staged_result_is_a_gate_failure(tmp_path: Path) -> None:
    from errors import GateFailure

    with pytest.raises(GateFailure):
        cp.commit(
            str(tmp_path / ".missing.stage.3mf"),
            str(tmp_path / "out.3mf"),
            force=False,
            inputs=["in.scad"],
        )


def test_staging_path_keeps_the_extension_and_hides_beside_the_target(
    tmp_path: Path,
) -> None:
    staged = cp.staging_path(str(tmp_path / "sub" / "out.usdz"))
    name = os.path.basename(staged)
    assert name.startswith(".") and name.endswith(".usdz")
    assert os.path.dirname(staged) == str(tmp_path / "sub")
