"""Tests for the print workflow dry-run planner and `3d print` command skeleton."""
from __future__ import annotations

import importlib
import json

import pytest

from errors import InputNotFound, InvalidArgument, UsageError
from printing import JobFields, ProfileFields, build_dry_run_plan, plan_to_json


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.chdir(work)
    return work


def test_build_dry_run_plan_validates_fields_and_is_deterministic(isolated):
    model = isolated / "bracket.stl"
    model.write_text("solid bracket\nendsolid bracket\n", encoding="utf-8")
    machine = isolated / "machine.json"
    process = isolated / "process.ini"
    filament = isolated / "filament.json"
    for path in (machine, process, filament):
        path.write_text("{}", encoding="utf-8")

    profiles = ProfileFields(machine=machine, process=process, filament=filament)
    job = JobFields(name="bracket-left", material="PLA", copies=2, start=True)
    first = build_dry_run_plan(model, printer_name="Prusa MK4", profiles=profiles, job=job)
    second = build_dry_run_plan(model, printer_name="Prusa MK4", profiles=profiles, job=job)

    assert first == second
    assert first.plan_id == second.plan_id
    assert first.input_format == "stl"
    assert first.printer["name"] == "Prusa MK4"
    assert first.printer["bed_mm"] == [250.0, 210.0, 220.0]
    assert first.job == {
        "name": "bracket-left",
        "material": "PLA",
        "copies": 2,
        "start": True,
    }
    assert first.profiles == {
        "machine": str(machine.resolve()),
        "process": str(process.resolve()),
        "filament": str(filament.resolve()),
    }
    assert first.steps == [
        "validate input",
        "slice model",
        "upload job",
        "start print",
    ]


def test_plan_json_is_stable_and_sorted(isolated):
    model = isolated / "gear.gcode"
    model.write_text("; gcode\n", encoding="utf-8")

    plan = build_dry_run_plan(
        model,
        printer_name="Prusa MINI",
        profiles=ProfileFields(),
        job=JobFields(name="", material=None, copies=1, start=False),
    )
    rendered = plan_to_json(plan)
    parsed = json.loads(rendered)

    assert parsed["job"]["name"] == "gear"
    assert parsed["steps"] == ["validate input", "upload job"]
    assert rendered == plan_to_json(plan)
    assert "\n  \"input_format\"" in rendered


def test_unknown_printer_lists_registry_names(isolated):
    model = isolated / "part.stl"
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        build_dry_run_plan(
            model,
            printer_name="No Such Printer",
            profiles=ProfileFields(),
            job=JobFields(name="part", material=None, copies=1, start=False),
        )

    assert exc.value.flag == "printer"
    assert "Prusa MK4" in exc.value.accepted


def test_unknown_material_lists_registry_names(isolated):
    model = isolated / "part.stl"
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        build_dry_run_plan(
            model,
            printer_name="Prusa MK4",
            profiles=ProfileFields(),
            job=JobFields(name="part", material="MysterySpool", copies=1, start=False),
        )

    assert exc.value.flag == "material"
    assert "PLA" in exc.value.accepted


def test_project_printer_registry_resolves_from_input_path(isolated):
    project = isolated / "robot"
    project.mkdir()
    (project / "3d.yaml").write_text("project:\n  name: robot\n", encoding="utf-8")
    (project / "printers.yaml").write_text(
        """Shop Printer:
  bed: [180, 180, 180]
  nozzle_mm: 0.6
  firmware: klipper
""",
        encoding="utf-8",
    )
    model = project / "part.stl"
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")

    plan = build_dry_run_plan(
        model,
        printer_name="Shop Printer",
        profiles=ProfileFields(),
        job=JobFields(name="part", material=None, copies=1, start=False),
    )

    assert plan.printer["name"] == "Shop Printer"
    assert plan.printer["bed_mm"] == [180.0, 180.0, 180.0]
    assert plan.printer["firmware"] == "klipper"


def test_invalid_job_and_profile_fields_raise_structured_errors(isolated):
    model = isolated / "part.3mf"
    model.write_text("3mf", encoding="utf-8")
    bad_profile = isolated / "profile.txt"
    bad_profile.write_text("not a slicer profile", encoding="utf-8")

    with pytest.raises(UsageError):
        build_dry_run_plan(
            model,
            printer_name="Prusa MK4",
            profiles=ProfileFields(),
            job=JobFields(name="part", material=None, copies=0, start=False),
        )
    with pytest.raises(InvalidArgument):
        build_dry_run_plan(
            model,
            printer_name="Prusa MK4",
            profiles=ProfileFields(machine=bad_profile),
            job=JobFields(name="part", material=None, copies=1, start=False),
        )


def test_missing_input_or_profile_raises_input_not_found(isolated):
    model = isolated / "part.stl"
    missing_profile = isolated / "missing.json"

    with pytest.raises(InputNotFound):
        build_dry_run_plan(
            model,
            printer_name="Prusa MK4",
            profiles=ProfileFields(),
            job=JobFields(name="part", material=None, copies=1, start=False),
        )
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")
    with pytest.raises(InputNotFound):
        build_dry_run_plan(
            model,
            printer_name="Prusa MK4",
            profiles=ProfileFields(machine=missing_profile),
            job=JobFields(name="part", material=None, copies=1, start=False),
        )


def test_print_command_help_and_dry_run_output(isolated, capsys):
    print_cmd = importlib.import_module("commands.print")
    model = isolated / "part.stl"
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")

    assert print_cmd.run(["--help"]) == 0
    assert "dry-run" in capsys.readouterr().out

    rc = print_cmd.run([str(model), "--printer", "Generic 220", "--dry-run", "--copies", "3"])
    out = capsys.readouterr().out
    parsed = json.loads(out)

    assert rc == 0
    assert parsed["printer"]["name"] == "Generic 220"
    assert parsed["job"]["copies"] == 3
    assert parsed["job"]["name"] == "part"


def test_print_command_requires_dry_run_and_printer(isolated):
    print_cmd = importlib.import_module("commands.print")
    model = isolated / "part.stl"
    model.write_text("solid part\nendsolid part\n", encoding="utf-8")

    with pytest.raises(UsageError):
        print_cmd.run([str(model), "--printer", "Generic 220"])
    with pytest.raises(UsageError):
        print_cmd.run([str(model), "--dry-run"])
