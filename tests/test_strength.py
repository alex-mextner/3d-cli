"""Tests for the `3d strength` structural-check skeleton."""
from __future__ import annotations

import json
import pathlib

import pytest

from errors import InputNotFound, InvalidArgument, UsageError


def _part(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "bracket.scad"
    path.write_text("cube([10, 20, 5]);\n", encoding="utf-8")
    return path


def test_strength_report_serializes_dry_run_plan(tmp_path) -> None:
    import strength

    request = strength.StrengthRequest(
        path=_part(tmp_path),
        material="PLA",
        load_n=25.0,
        axis="Z",
        fixture="cantilever",
        safety_factor=2.5,
        dry_run=True,
    )
    report = strength.build_report(
        request,
        material_name="PLA",
        yield_mpa=55.0,
        layer_adhesion=0.45,
    )

    assert report.status == "DRY-RUN"
    assert report.verdict == "NOT_EVALUATED"
    assert report.controlling_strength_mpa == pytest.approx(24.75)
    assert report.steps[0] == "validate input part path and units"
    data = report.to_dict()
    assert data["request"]["file"].endswith("bracket.scad")
    assert data["steps"] == list(report.steps)
    assert data["checks"][0]["status"] == "planned"


def test_command_help_and_no_args(capsys) -> None:
    from commands import strength as cmd

    assert cmd.run(["--help"]) == 0
    assert "3d strength" in capsys.readouterr().out

    assert cmd.run([]) == 1
    assert "3d strength" in capsys.readouterr().out


def test_command_help_after_positional_returns_success(tmp_path, capsys) -> None:
    from commands import strength as cmd

    assert cmd.run([str(_part(tmp_path)), "--help"]) == 0
    assert "3d strength" in capsys.readouterr().out


def test_command_text_dry_run(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(tmp_path)
    from commands import strength as cmd

    rc = cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", "25", "--axis", "z"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "=== strength (structural check) ===" in out
    assert "status: DRY-RUN" in out
    assert "material: PLA" in out
    assert "load: 25 N" in out
    assert "load axis: Z" in out
    assert "controlling strength: 20.25 MPa" in out
    assert ">>> STRENGTH: DRY-RUN" in out


def test_command_accepts_explicit_dry_run_flag(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(tmp_path)
    from commands import strength as cmd

    rc = cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", "25", "--dry-run", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["request"]["dry_run"] is True


def test_command_json_dry_run(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(tmp_path)
    from commands import strength as cmd

    rc = cmd.run(
        [
            str(_part(tmp_path)),
            "--material",
            "PETG",
            "--load",
            "12.5",
            "--fixture",
            "simple",
            "--json",
        ]
    )

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "DRY-RUN"
    assert data["request"]["material"] == "PETG"
    assert data["request"]["load_n"] == 12.5
    assert data["request"]["fixture"] == "simple"
    assert data["verdict"] == "NOT_EVALUATED"


def test_command_requires_load(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(UsageError) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "PLA"])
    assert "--load" in str(exc.value)


def test_command_rejects_bad_load(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", "0"])
    assert exc.value.flag == "--load"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_command_rejects_non_finite_load(tmp_path, bad: str) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", bad])
    assert exc.value.flag == "--load"


def test_command_rejects_non_finite_safety_factor(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run(
            [
                str(_part(tmp_path)),
                "--material",
                "PLA",
                "--load",
                "5",
                "--safety-factor",
                "nan",
            ]
        )
    assert exc.value.flag == "--safety-factor"


def test_command_rejects_negative_safety_factor(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run(
            [
                str(_part(tmp_path)),
                "--material",
                "PLA",
                "--load",
                "5",
                "--safety-factor",
                "-1",
            ]
        )
    assert exc.value.flag == "--safety-factor"


def test_command_rejects_bad_axis(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", "5", "--axis", "diagonal"])
    assert exc.value.flag == "--axis"


def test_command_rejects_bad_fixture(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run(
            [
                str(_part(tmp_path)),
                "--material",
                "PLA",
                "--load",
                "5",
                "--fixture",
                "floating",
            ]
        )
    assert exc.value.flag == "--fixture"


def test_command_rejects_unknown_option(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(UsageError) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "PLA", "--load", "5", "--solver"])
    assert "unknown option" in str(exc.value)


def test_command_rejects_extra_positional(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(UsageError) as exc:
        cmd.run([str(_part(tmp_path)), "other.scad", "--material", "PLA", "--load", "5"])
    assert "unexpected extra input" in str(exc.value)


def test_command_rejects_unknown_material(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run([str(_part(tmp_path)), "--material", "NOPE", "--load", "5"])
    assert exc.value.flag == "--material"
    assert "PLA" in exc.value.render(color=False)


def test_command_rejects_bad_extension(tmp_path) -> None:
    bad = tmp_path / "part.txt"
    bad.write_text("not geometry\n", encoding="utf-8")
    from commands import strength as cmd

    with pytest.raises(InvalidArgument) as exc:
        cmd.run([str(bad), "--material", "PLA", "--load", "5"])
    assert exc.value.flag == "file"


def test_command_missing_file_raises_input_not_found(tmp_path) -> None:
    from commands import strength as cmd

    with pytest.raises(InputNotFound):
        cmd.run([str(tmp_path / "missing.scad"), "--material", "PLA", "--load", "5"])


def test_structural_check_alias_is_discovered() -> None:
    from cli.registry import discover

    reg = discover()
    assert reg.resolve("structural-check") is reg.resolve("strength")
