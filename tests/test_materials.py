"""Tests for the materials registry + loader (lib/materials.py, ROADMAP §2a) and the
`3d materials` command (lib/commands/materials.py).

Exercises the real loader (three-layer merge, field-level override, unknown-name error)
and the command's run() directly — no subprocess, no ./bin/3d."""
from __future__ import annotations

import pytest

import materials
from registries import materials as registry_materials
from errors import InvalidArgument


# ---- builtin layer ----------------------------------------------------------

def test_builtin_has_common_filaments(tmp_path):
    # Isolate from any real user/project files: point config at an empty dir, start in tmp.
    mats = materials.load_materials(start=tmp_path)
    for name in ("PLA", "PETG", "ABS", "ASA", "TPU"):
        assert name in mats, f"{name} missing from builtin registry"


def test_root_materials_module_re_exports_registry_core() -> None:
    assert materials.FINISHES is registry_materials.FINISHES
    assert materials.OVERRIDE_FILENAME is registry_materials.OVERRIDE_FILENAME
    assert materials.Material is registry_materials.Material
    assert materials.MaterialError is registry_materials.MaterialError
    assert materials.load_materials is registry_materials.load_materials
    assert materials.get_material is registry_materials.get_material


def test_builtin_pla_fields(tmp_path):
    pla = materials.get_material("PLA", start=tmp_path)
    assert pla.name == "PLA"
    assert pla.density == pytest.approx(1.24)
    assert pla.finish == "matte"
    # Anisotropy: PLA is the weakest across layers (~0.45 per task).
    assert 0.0 < pla.layer_adhesion <= 1.0
    assert pla.layer_adhesion == pytest.approx(0.45)


def test_all_builtin_materials_construct(tmp_path):
    # Every builtin entry must be a complete, valid Material (no missing required field).
    mats = materials.load_materials(start=tmp_path)
    for name, m in mats.items():
        assert m.name == name
        assert m.finish in ("matte", "gloss", "metal")
        assert m.density > 0


# ---- unknown name -----------------------------------------------------------

def test_unknown_name_raises_invalid_argument(tmp_path):
    with pytest.raises(InvalidArgument) as exc:
        materials.get_material("NYLON-X", start=tmp_path)
    # The error lists the accepted names so the fix is obvious.
    assert "PLA" in str(exc.value)
    assert exc.value.command == "materials"


# ---- user layer (XDG_CONFIG_HOME) ------------------------------------------

def test_user_layer_overrides_field(tmp_path, monkeypatch):
    cfg = tmp_path / "xdg"
    (cfg / "3d-cli").mkdir(parents=True)
    (cfg / "3d-cli" / "materials.yaml").write_text('PLA:\n  color: "#ff0000"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))

    pla = materials.get_material("PLA", start=tmp_path)
    assert pla.color == "#ff0000"          # overridden
    assert pla.density == pytest.approx(1.24)  # survives from builtin (field-level merge)


def test_user_layer_adds_new_material(tmp_path, monkeypatch):
    cfg = tmp_path / "xdg"
    (cfg / "3d-cli").mkdir(parents=True)
    (cfg / "3d-cli" / "materials.yaml").write_text(
        "NYLON:\n  density: 1.14\n  e_modulus_mpa: 1800\n  tensile_mpa: 60\n"
        "  yield_mpa: 50\n  max_temp_c: 120\n  color: \"#cccccc\"\n  finish: matte\n"
        "  layer_adhesion: 0.65\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))

    nylon = materials.get_material("NYLON", start=tmp_path)
    assert nylon.density == pytest.approx(1.14)
    assert nylon.max_temp_c == 120


def test_user_new_material_missing_field_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "xdg"
    (cfg / "3d-cli").mkdir(parents=True)
    # A wholly new material defined only by the user must be COMPLETE.
    (cfg / "3d-cli" / "materials.yaml").write_text("WONKY:\n  density: 1.0\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))

    with pytest.raises(materials.MaterialError):
        materials.load_materials(start=tmp_path)


def test_user_bad_finish_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "xdg"
    (cfg / "3d-cli").mkdir(parents=True)
    # Override a builtin's finish with an invalid value — this is the field-failure path.
    (cfg / "3d-cli" / "materials.yaml").write_text("PLA:\n  finish: sparkly\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))

    with pytest.raises(materials.MaterialError) as exc:
        materials.load_materials(start=tmp_path)
    assert "finish" in str(exc.value)


# ---- project layer (./materials.yaml next to 3d.yaml) ----------------------

def test_project_layer_overrides_user_and_builtin(tmp_path, monkeypatch):
    # Empty config so only builtin + project apply.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    (tmp_path / "3d.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    (tmp_path / "materials.yaml").write_text("PETG:\n  density: 1.30\n", encoding="utf-8")

    petg = materials.get_material("PETG", start=tmp_path)
    assert petg.density == pytest.approx(1.30)     # project override
    assert petg.finish == "gloss"                  # survives from builtin


def test_project_layer_found_from_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    (tmp_path / "3d.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    (tmp_path / "materials.yaml").write_text("ABS:\n  color: \"#000000\"\n", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    abs_m = materials.get_material("ABS", start=sub)
    assert abs_m.color == "#000000"


# ---- command ----------------------------------------------------------------

def test_command_help(capsys):
    from commands import materials as cmd
    rc = cmd.run(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "materials" in out
    # Rule 5: why + example present.
    assert "Example" in out or "example" in out


def test_command_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    from commands import materials as cmd
    rc = cmd.run(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PLA" in out and "PETG" in out


def test_command_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    from commands import materials as cmd
    rc = cmd.run(["show", "PLA"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PLA" in out
    assert "density" in out.lower()
    assert "layer" in out.lower()  # anisotropy field shown


def test_command_show_unknown_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    from commands import materials as cmd
    with pytest.raises(InvalidArgument):
        cmd.run(["show", " NOPE"])


def test_command_no_args_returns_usage(capsys):
    from commands import materials as cmd
    rc = cmd.run([])
    assert rc == 1
    assert "materials" in capsys.readouterr().out


def test_command_unknown_subcommand_raises():
    from errors import UsageError
    from commands import materials as cmd
    with pytest.raises(UsageError):
        cmd.run(["frobnicate"])
