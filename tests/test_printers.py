"""Tests for the printer registry (lib/printers.py) + the `3d printers` command (ROADMAP §2a).

Exercises the real loader/command code: built-in data shape, the layered
built-in<user<project override, the structured unknown-name error, and the CLI frontend's
list/show/usage paths. The loader reads ~/.config/3d-cli and the nearest 3d.yaml, so every
test isolates those via XDG_CONFIG_HOME + an empty cwd to keep results deterministic."""
from __future__ import annotations


import pytest

import commands.printers as printers_cmd
import printers as printers_mod
from registries import printers as registry_printers
from errors import InvalidArgument, UsageError
from printers import PrinterError, get_printer, load_printers


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the user-config layer at an empty tmp dir and cd into an empty dir (no 3d.yaml),
    so only the built-in layer is active unless a test writes its own override files."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.chdir(work)
    return tmp_path


# ---- built-in data -------------------------------------------------------------

def test_builtin_has_expected_machines(isolated):
    p = load_printers()
    for name in ("Bambu Lab A1", "Bambu Lab A1 mini", "Prusa MK4", "Prusa MINI", "Generic 220"):
        assert name in p, f"built-in registry missing {name}"


def test_root_printers_module_re_exports_registry_core() -> None:
    assert printers_mod.ACCEPTED_FIRMWARE is registry_printers.ACCEPTED_FIRMWARE
    assert printers_mod.DEFAULT_NOZZLE_MM is registry_printers.DEFAULT_NOZZLE_MM
    assert printers_mod.Printer is registry_printers.Printer
    assert printers_mod.PrinterError is registry_printers.PrinterError
    assert printers_mod.load_printers is registry_printers.load_printers
    assert printers_mod.get_printer is registry_printers.get_printer


def test_published_bed_sizes(isolated):
    p = load_printers()
    assert p["Bambu Lab A1"].bed == [256.0, 256.0, 256.0]
    assert p["Bambu Lab A1 mini"].bed == [180.0, 180.0, 180.0]
    assert p["Prusa MK4"].bed == [250.0, 210.0, 220.0]
    assert p["Prusa MINI"].bed == [180.0, 180.0, 180.0]


def test_defaults_and_fields(isolated):
    mk4 = load_printers()["Prusa MK4"]
    assert mk4.nozzle_mm == 0.4
    assert mk4.firmware == "prusa"
    assert mk4.material is not None  # a sensible default material is set


def test_all_firmwares_accepted(isolated):
    for pr in load_printers().values():
        assert pr.firmware in printers_mod.ACCEPTED_FIRMWARE


# ---- get_printer ---------------------------------------------------------------

def test_get_printer_known(isolated):
    assert get_printer("Prusa MK4").name == "Prusa MK4"


def test_get_printer_unknown_lists_accepted(isolated):
    with pytest.raises(InvalidArgument) as exc:
        get_printer("Nonexistent 9000")
    err = exc.value
    assert err.flag == "printer"
    assert "Prusa MK4" in err.accepted  # the message lists the real names
    assert err.exit_code == 2


# ---- layering: user + project override -----------------------------------------

def test_user_layer_overrides_builtin(isolated, monkeypatch):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text(
        '"Prusa MK4":\n  bed: [999, 999, 999]\n  firmware: klipper\n', encoding="utf-8"
    )
    mk4 = load_printers()["Prusa MK4"]
    assert mk4.bed == [999.0, 999.0, 999.0]
    assert mk4.firmware == "klipper"


def test_user_layer_adds_new_printer(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text(
        '"My Custom":\n  bed: [300, 300, 400]\n', encoding="utf-8"
    )
    p = load_printers()
    assert "My Custom" in p
    assert p["My Custom"].nozzle_mm == 0.4  # default applied


def test_project_layer_beats_user_layer(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text(
        '"Prusa MK4":\n  bed: [111, 111, 111]\n', encoding="utf-8"
    )
    work = isolated / "work"
    (work / "3d.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    (work / "printers.yaml").write_text(
        '"Prusa MK4":\n  bed: [222, 222, 222]\n', encoding="utf-8"
    )
    assert load_printers()["Prusa MK4"].bed == [222.0, 222.0, 222.0]


# ---- malformed input -----------------------------------------------------------

def test_user_layer_bad_bed_raises(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text('"Broken":\n  bed: [1, 2]\n', encoding="utf-8")
    with pytest.raises(PrinterError):
        load_printers()


def test_user_layer_bad_firmware_raises(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text(
        '"Broken":\n  bed: [1, 2, 3]\n  firmware: nonsense\n', encoding="utf-8"
    )
    with pytest.raises(InvalidArgument):
        load_printers()


def test_user_layer_not_mapping_raises(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(PrinterError):
        load_printers()


def test_empty_user_layer_is_ignored(isolated):
    cfg = isolated / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printers.yaml").write_text("", encoding="utf-8")
    assert "Prusa MK4" in load_printers()  # built-in still present


# ---- command frontend ----------------------------------------------------------

def test_cmd_help_returns_zero(capsys):
    assert printers_cmd.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "list" in out and "show" in out
    assert "Example" in out  # §11: every command carries an example


def test_cmd_no_args_prints_usage_nonzero(capsys):
    assert printers_cmd.run([]) == 1


def test_cmd_list(isolated, capsys):
    assert printers_cmd.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "Prusa MK4" in out
    assert "256x256x256" in out  # A1 bed printed compactly


def test_cmd_show_known(isolated, capsys):
    assert printers_cmd.run(["show", "Prusa MK4"]) == 0
    out = capsys.readouterr().out
    assert "Prusa MK4" in out
    assert "250 x 210 x 220" in out


def test_cmd_show_unknown_raises(isolated):
    with pytest.raises(InvalidArgument):
        printers_cmd.run(["show", "Nope"])


def test_cmd_show_without_name_raises():
    with pytest.raises(UsageError):
        printers_cmd.run(["show"])


def test_cmd_unknown_subcommand_raises():
    with pytest.raises(UsageError):
        printers_cmd.run(["frobnicate"])


def test_command_metadata():
    assert printers_cmd.COMMAND.name == "printers"
    assert printers_cmd.COMMAND.group == "LIBRARIES"
