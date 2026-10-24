"""Tests for `3d profiles doctor` (lib/profiles/doctor.py): a good preset tree is clean, every injected defect is
reported under its own code with the fix, and ERRORs make the command exit 1."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from commands import profiles as profiles_cmd
from errors import UsageError
from profiles import doctor
from slicing.orca_cli import SystemPresets

U1 = "Snapmaker U1 (0.4 nozzle)"
SYS_PROCESS = "0.20mm Standard @U1"


def put(path: Path, obj: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=4) + "\n")


def info(path: Path, base_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"base_id = {base_id}\n")


@pytest.fixture
def res(tmp_path: Path) -> Path:
    """A tiny vendor install: a U1 machine (0.08..0.32, 4 heads), a 0.20 process preset, PLA."""
    root = tmp_path / "res" / "Snapmaker"
    put(
        root / "machine/fdm_u1.json",
        {
            "name": "fdm_u1",
            "min_layer_height": ["0.08"] * 4,
            "max_layer_height": ["0.32"] * 4,
            "nozzle_diameter": ["0.4"] * 4,
        },
    )
    put(
        root / f"machine/{U1}.json",
        {"name": U1, "inherits": "fdm_u1", "setting_id": "SM_U1"},
    )
    put(
        root / "process/fdm_process.json",
        {"name": "fdm_process", "layer_height": "0.2"},
    )
    put(
        root / f"process/{SYS_PROCESS}.json",
        {
            "name": SYS_PROCESS,
            "inherits": "fdm_process",
            "setting_id": "GP004",
            "compatible_printers": [U1],
        },
    )
    put(root / "filament/PLA.json", {"name": "PLA", "setting_id": "GFL99"})
    return tmp_path / "res"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A clean user-preset tree: one preset per kind, `.info` in sync."""
    root = tmp_path / "user"
    put(
        root / "process/My PLA.json",
        {
            "name": "My PLA",
            "inherits": SYS_PROCESS,
            "layer_height": "0.2",
            "wall_loops": "4",
        },
    )
    info(root / "process/My PLA.info", "GP004")
    put(root / f"machine/{U1} - Copy.json", {"name": f"{U1} - Copy", "inherits": U1})
    info(root / f"machine/{U1} - Copy.info", "SM_U1")
    put(root / "filament/Black.json", {"name": "Black", "inherits": "PLA"})
    info(root / "filament/Black.info", "GFL99")
    return root


def run_doctor(tree: Path, res: Path | None, **kw: object) -> doctor.Report:
    system = SystemPresets(res) if res is not None else None
    return doctor.diagnose([doctor.Tree.load("t", tree)], system, **kw)  # type: ignore[arg-type]


def codes(report: doctor.Report) -> list[str]:
    return [f.code for f in report.findings]


# ------------------------------------------------------------------ the good tree


def test_a_good_tree_is_clean(tree: Path, res: Path) -> None:
    report = run_doctor(tree, res)
    assert report.findings == []
    assert report.exit_code == 0


# ------------------------------------------------------------------ injected defects


def test_min_layer_height_above_layer_height(tree: Path, res: Path) -> None:
    put(
        tree / "process/My PLA.json",
        {
            "name": "My PLA",
            "inherits": SYS_PROCESS,
            "layer_height": "0.2",
            "min_layer_height": ["0.3"],
        },
    )
    report = run_doctor(tree, res)
    [f] = report.findings
    assert f.code == "layer-height-range" and f.severity == "ERROR"
    assert "layer_height 0.2 is below its min_layer_height 0.3" in f.message
    assert "min_layer_height" in f.fix and "My PLA.json" in f.fix
    assert report.exit_code == 1


def test_min_above_max_in_one_preset(tree: Path, res: Path) -> None:
    put(
        tree / "process/My PLA.json",
        {
            "name": "My PLA",
            "inherits": SYS_PROCESS,
            "min_layer_height": ["0.3"],
            "max_layer_height": ["0.1"],
        },
    )
    assert any(
        "is above max_layer_height" in f.message for f in run_doctor(tree, res).findings
    )


def test_missing_parent(tree: Path, res: Path) -> None:
    put(tree / "process/My PLA.json", {"name": "My PLA", "inherits": "No Such Parent"})
    report = run_doctor(tree, res)
    [f] = [f for f in report.findings if f.code == "parent-missing"]
    assert f.severity == "ERROR" and '"No Such Parent"' in f.message
    assert "No Such Parent" in f.fix
    assert report.exit_code == 1


def test_a_user_parent_in_the_tree_is_a_valid_parent(tree: Path, res: Path) -> None:
    put(tree / "process/Child.json", {"name": "Child", "inherits": "My PLA"})
    info(tree / "process/Child.info", "GP004")
    assert run_doctor(tree, res).findings == []  # negative control for parent-missing


def test_inherits_cycle(tree: Path, res: Path) -> None:
    put(tree / "process/A.json", {"name": "A", "inherits": "B"})
    put(tree / "process/B.json", {"name": "B", "inherits": "A"})
    report = run_doctor(tree, res)
    assert "inherits-cycle" in codes(report) and report.exit_code == 1


def test_layer_height_outside_the_machine_limits(tree: Path, res: Path) -> None:
    put(
        tree / "process/My PLA.json",
        {"name": "My PLA", "inherits": SYS_PROCESS, "layer_height": "0.06"},
    )
    report = run_doctor(tree, res)
    [f] = report.findings
    assert (
        f.code == "layer-height-range"
        and "0.06" in f.message
        and "0.08..0.32" in f.message
    )
    assert report.exit_code == 1


def test_declared_adaptive_band_wider_than_the_printer_limits(
    tree: Path, res: Path
) -> None:
    narrow = f"{U1} - Copy"
    put(
        tree / f"machine/{narrow}.json",
        {
            "name": narrow,
            "inherits": U1,
            "min_layer_height": ["0.12"] * 4,
            "max_layer_height": ["0.28"] * 4,
        },
    )
    name = "Chekhov adaptive 0.08-0.32"
    put(
        tree / f"process/{name}.json",
        {
            "name": name,
            "inherits": SYS_PROCESS,
            "compatible_printers": [narrow],
            "layer_height": "0.2",
        },
    )
    info(tree / f"process/{name}.info", "GP004")
    report = run_doctor(tree, res)
    clamped = [f for f in report.findings if f.code == "adaptive-clamped"]
    assert len(clamped) == 1 and clamped[0].severity == "ERROR"
    assert "0.08..0.32" in clamped[0].message and "0.12..0.28" in clamped[0].message
    assert "min_layer_height <= 0.08" in clamped[0].fix
    assert "machine-band" in codes(report)  # the machine itself is flagged too
    assert report.exit_code == 1


def test_the_default_band_on_a_narrow_u1_machine_is_a_warning(
    tree: Path, res: Path
) -> None:
    narrow = f"{U1} - Copy"
    put(
        tree / f"machine/{narrow}.json",
        {"name": narrow, "inherits": U1, "max_layer_height": ["0.28"] * 4},
    )
    report = run_doctor(tree, res)
    [f] = report.findings
    assert f.code == "machine-band" and f.severity == "WARN"
    assert report.exit_code == 0


def test_a_wide_enough_band_is_not_flagged(tree: Path, res: Path) -> None:
    name = "Fine adaptive 0.10-0.30"
    put(
        tree / f"process/{name}.json",
        {"name": name, "inherits": SYS_PROCESS, "layer_height": "0.2"},
    )
    info(tree / f"process/{name}.info", "GP004")
    assert run_doctor(tree, res).findings == []


def test_info_base_id_drift(tree: Path, res: Path) -> None:
    info(tree / "process/My PLA.info", "GP006")
    report = run_doctor(tree, res)
    [f] = report.findings
    assert f.code == "info-base-id" and f.severity == "ERROR"
    assert "GP006" in f.message and "GP004" in f.message
    assert "base_id = GP004" in f.fix
    assert report.exit_code == 1


def test_info_without_base_id_is_a_warning(tree: Path, res: Path) -> None:
    (tree / "process/My PLA.info").write_text(
        "sync_info = \nsetting_id = \nbase_id = \n"
    )
    [f] = run_doctor(tree, res).findings
    assert f.code == "info-base-id" and f.severity == "WARN"


def test_missing_and_orphan_info_files(tree: Path, res: Path) -> None:
    (tree / "filament/Black.info").unlink()
    info(tree / "filament/Ghost.info", "X")
    report = run_doctor(tree, res)
    assert sorted(codes(report)) == ["info-missing", "info-orphan"]
    assert report.exit_code == 0  # both are warnings
    missing = next(f for f in report.findings if f.code == "info-missing")
    assert "base_id = GFL99" in missing.fix


def test_info_setting_id_disagrees_with_the_json(tree: Path, res: Path) -> None:
    put(
        tree / "filament/Black.json",
        {"name": "Black", "inherits": "PLA", "setting_id": "A1"},
    )
    (tree / "filament/Black.info").write_text("setting_id = B2\nbase_id = GFL99\n")
    [f] = run_doctor(tree, res).findings
    assert f.code == "info-setting-id" and f.severity == "ERROR"


def test_unreadable_preset_json(tree: Path, res: Path) -> None:
    (tree / "process/Broken.json").write_text("{not json")
    report = run_doctor(tree, res)
    [f] = report.findings
    assert f.code == "preset-unreadable" and "Broken.json" in f.where
    assert report.exit_code == 1


def test_unreadable_info_is_a_finding_and_the_rest_is_still_checked(
    tree: Path, res: Path
) -> None:
    (tree / "process/My PLA.info").write_bytes(b"base_id = \xff\xfe\n")
    put(tree / "process/A.json", {"name": "A", "inherits": "B"})
    put(tree / "process/B.json", {"name": "B", "inherits": "A"})
    report = run_doctor(tree, res)
    [f] = [f for f in report.findings if f.code == "preset-unreadable"]
    assert "My PLA.info" in f.where and f.severity == "ERROR"
    assert "inherits-cycle" in codes(report)  # diagnosis went on to the other presets
    # the file exists: "create it" would clobber it
    assert not [f for f in report.findings if f.code == "info-missing" and "My PLA" in f.where]


def test_a_corrupt_vendor_parent_is_not_reported_as_an_inherits_loop(
    tree: Path, res: Path
) -> None:
    (res / f"Snapmaker/process/{SYS_PROCESS}.json").write_text("{truncated")
    report = run_doctor(tree, res)
    [f] = [f for f in report.findings if f.where.endswith("My PLA.json")]
    assert f.code == "parent-unreadable" and f.severity == "ERROR"
    assert SYS_PROCESS in f.message and "loop" not in f.message


def test_a_vendor_file_that_is_not_an_object_does_not_abort_the_run(
    tree: Path, res: Path
) -> None:
    (res / f"Snapmaker/process/{SYS_PROCESS}.json").write_text("[1, 2]")
    assert "parent-unreadable" in codes(run_doctor(tree, res))


def test_a_vendor_machine_with_a_missing_parent_does_not_abort_the_run(
    tree: Path, res: Path
) -> None:
    put(
        res / "Snapmaker/machine/Orphan M.json",
        {"name": "Orphan M", "inherits": "ghost"},
    )
    put(
        tree / "process/My PLA.json",
        {
            "name": "My PLA",
            "inherits": SYS_PROCESS,
            "layer_height": "0.2",
            "compatible_printers": ["Orphan M", U1],
        },
    )
    report = run_doctor(tree, res)  # must not raise
    assert not any(f.code == "layer-height-range" for f in report.findings)
    [f] = [f for f in report.findings if f.code == "machine-unverifiable"]
    assert f.severity == "WARN" and "Orphan M" in f.message and "ghost" in f.message


def test_u1_machine_preset_with_the_wrong_nozzle_for_its_name(
    tree: Path, res: Path
) -> None:
    put(
        tree / f"machine/{U1} - Copy.json",
        {"name": f"{U1} - Copy", "inherits": U1, "nozzle_diameter": ["0.6"] * 4},
    )
    [f] = run_doctor(tree, res).findings
    assert f.code == "machine-keys" and "0.6" in f.message


def test_u1_machine_preset_with_three_toolheads(tree: Path, res: Path) -> None:
    put(
        tree / f"machine/{U1} - Copy.json",
        {
            "name": f"{U1} - Copy",
            "inherits": U1,
            "nozzle_diameter": ["0.4"] * 3,
            "min_layer_height": ["0.08"] * 3,
            "max_layer_height": ["0.32"] * 3,
        },
    )
    [f] = run_doctor(tree, res).findings
    assert (
        f.code == "machine-keys" and f.severity == "WARN" and "4 toolheads" in f.message
    )


def test_a_u1_machine_without_any_limits_names_the_missing_keys(
    tmp_path: Path, res: Path
) -> None:
    root = tmp_path / "bare"
    put(root / "machine/Snapmaker U1 custom.json", {"name": "Snapmaker U1 custom"})
    info(root / "machine/Snapmaker U1 custom.info", "")
    report = run_doctor(root, res)
    missing = [f.message for f in report.findings if f.code == "machine-keys"]
    assert len(missing) == 3 and report.exit_code == 1


def test_without_vendor_presets_the_unverifiable_checks_warn_once_and_never_error(
    tree: Path,
) -> None:
    put(
        tree / "process/My PLA.json",
        {"name": "My PLA", "inherits": "Some Vendor Preset", "layer_height": "0.06"},
    )
    report = run_doctor(tree, None)
    assert codes(report).count("system-presets-missing") == 1
    assert report.exit_code == 0
    assert "ORCA_RES" in report.findings[0].fix


def test_a_parent_that_lives_only_in_the_other_tree_is_reported(
    tmp_path: Path, res: Path
) -> None:
    repo, slicer = tmp_path / "repo", tmp_path / "slicer"
    put(slicer / "process/Base.json", {"name": "Base", "inherits": SYS_PROCESS})
    info(slicer / "process/Base.info", "GP004")
    put(repo / "process/Child.json", {"name": "Child", "inherits": "Base"})
    info(repo / "process/Child.info", "GP004")
    report = doctor.diagnose(
        [doctor.Tree.load("repo", repo), doctor.Tree.load("slicer", slicer)],
        SystemPresets(res),
    )
    [f] = report.findings
    assert (
        f.code == "parent-elsewhere"
        and f.where.startswith("repo ")
        and "slicer" in f.message
    )


def test_findings_render_one_line_with_severity_and_fix(tree: Path, res: Path) -> None:
    put(tree / "process/My PLA.json", {"name": "My PLA", "inherits": "Nope"})
    [f] = run_doctor(tree, res).findings
    line = f.line()
    assert "\n" not in line and line.startswith("ERROR") and "-> fix:" in line


# ------------------------------------------------------------------ the command


@pytest.fixture
def env(
    tmp_path: Path, res: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Path], None]:
    """Point `3d profiles` at a fake home (no slicer data dir), a fake repo `profiles/` and the fake vendor install."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ORCA_RES", str(res))
    monkeypatch.setenv("ORCA", "")
    monkeypatch.setenv("SLICER", "")
    monkeypatch.setenv("PATH", "")

    def use(user_tree: Path) -> None:
        profiles = tmp_path / "profiles"
        link = profiles / "snapmaker-orca"
        profiles.mkdir(exist_ok=True)
        if not link.exists():
            link.symlink_to(user_tree)
        monkeypatch.setenv("THREED_PROFILES_DIR", str(profiles))

    return use


def test_command_exits_0_on_a_clean_tree(
    tree: Path, env: Callable[[Path], None], capsys: pytest.CaptureFixture[str]
) -> None:
    env(tree)
    assert profiles_cmd.run(["doctor", "--slicer", "snapmaker-orca"]) == 0
    assert "ok" in capsys.readouterr().out


def test_command_exits_1_and_prints_the_finding_on_an_error(
    tree: Path, env: Callable[[Path], None], capsys: pytest.CaptureFixture[str]
) -> None:
    put(tree / "process/My PLA.json", {"name": "My PLA", "inherits": "Nope"})
    env(tree)
    assert profiles_cmd.run(["doctor", "--slicer", "snapmaker-orca"]) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "parent-missing" in out and "fix:" in out


def test_command_exit_code_is_0_for_warnings_only(
    tree: Path, env: Callable[[Path], None], capsys: pytest.CaptureFixture[str]
) -> None:
    (tree / "filament/Black.info").unlink()
    env(tree)
    assert profiles_cmd.run(["doctor", "--slicer", "snapmaker-orca"]) == 0
    assert "WARN" in capsys.readouterr().out


def test_command_json_output(
    tree: Path, env: Callable[[Path], None], capsys: pytest.CaptureFixture[str]
) -> None:
    put(tree / "process/My PLA.json", {"name": "My PLA", "inherits": "Nope"})
    env(tree)
    assert profiles_cmd.run(["doctor", "--slicer", "snapmaker-orca", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    rep = data["slicers"]["snapmaker-orca"]
    assert rep["errors"] == 1 and rep["findings"][0]["code"] == "parent-missing"


def test_command_rejects_options_of_other_subcommands(
    tree: Path, env: Callable[[Path], None]
) -> None:
    env(tree)
    with pytest.raises(UsageError):
        profiles_cmd.run(["doctor", "--push"])
