"""`3d prep --printer k1se` as a user runs it through `bin/3d`: an STL in, K1 SE presets, G-code and gate verdicts out.

The slicer is a fake executable (`$ORCA`), so these run without Orca; one real slice is skipped when Orca is absent."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from k1se_fixtures import ELEGOO, fake_slicer, k1se_gcode, write_preset_tree

from .workflow_helper import run_cli

K1SE = (
    "prep",
    "--printer",
    "k1se",
    "--orient",
    "keep",
    "--no-adapt",
    "--skip",
    "fit,stability,proof",
    "--no-lock",
)


def _box(path: Path) -> Path:
    tm = pytest.importorskip("trimesh")
    tm.creation.box((40.0, 30.0, 0.6)).export(path)
    return path


def _env(tmp_path: Path, slicer: Path) -> dict[str, str]:
    return {
        "THREED_PROFILES_DIR": str(write_preset_tree(tmp_path / "profiles")),
        "ORCA": str(slicer),
        "FAKE_SLICER_LOG": str(tmp_path / "argv.jsonl"),
    }


def test_k1se_prep_slices_with_the_flat_presets_and_reports_the_real_speeds(
    tmp_path: Path,
) -> None:
    model = _box(tmp_path / "plate.stl")
    out = tmp_path / "out"
    res = run_cli(
        tmp_path, *K1SE, str(model), "--out", str(out), "--filament", ELEGOO, "--walls", "3",
        "--set", "outer_wall_speed=250", "--set", "inner_wall_speed=500", "--set", "travel_speed=600",
        "--max-volumetric", "30", "--json", timeout=300,
        env_extra=_env(tmp_path, fake_slicer(tmp_path, k1se_gcode())),
    )  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    steps = {s["name"]: s for s in json.loads(res.stdout)["steps"]}
    assert steps["project"]["verdict"] == "PASS" and steps["slice"]["verdict"] == "PASS"
    gates = {g["name"]: g for g in steps["gates"]["result"]["gates"]}
    assert (
        gates["feedrates"]["verdict"] == "PASS"
        and "Outer wall 250" in gates["feedrates"]["detail"]
    )
    assert (
        gates["overrides"]["verdict"] == "PASS"
        and gates["bed-bounds"]["verdict"] == "PASS"
    )
    assert (
        "32m 51s" in steps["gates"]["summary"]
        and "13.56 g" in steps["gates"]["summary"]
    )
    assert (out / "plate.gcode").is_file() and (
        out / "plate.profiles" / "filament.json"
    ).is_file()
    argv = json.loads((tmp_path / "argv.jsonl").read_text().splitlines()[-1])
    assert "--load-settings" in argv and "--load-filaments" in argv


def test_k1se_prep_exits_one_when_the_gcode_is_faster_than_requested(
    tmp_path: Path,
) -> None:
    res = run_cli(
        tmp_path, *K1SE, str(_box(tmp_path / "plate.stl")), "--out", str(tmp_path / "out"), "--walls", "3",
        "--set", "outer_wall_speed=200", "--quiet", timeout=300,
        env_extra=_env(tmp_path, fake_slicer(tmp_path, k1se_gcode())),
    )  # fmt: skip
    assert res.returncode == 1
    assert "gates" in res.stdout and "FAIL" in res.stdout


def test_k1se_prep_refuses_colours_and_u1_prep_refuses_supports(tmp_path: Path) -> None:
    model = _box(tmp_path / "plate.stl")
    colors = tmp_path / "colors.yaml"
    colors.write_text("filaments: []\n")
    res = run_cli(
        tmp_path,
        *K1SE,
        str(model),
        "--colors",
        str(colors),
        "--out",
        str(tmp_path / "o"),
    )
    assert res.returncode == 2 and "colors" in res.stderr
    res = run_cli(
        tmp_path,
        "prep",
        str(model),
        "--supports",
        "auto",
        "--out",
        str(tmp_path / "o2"),
        "--no-lock",
    )
    assert res.returncode == 2 and "supports" in res.stderr


def test_k1se_prep_with_an_overlay_file_renames_the_presets_for_a_changed_acceleration(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "o.json"
    overlay.write_text(json.dumps({"process": {"inner_wall_acceleration": 12000}}))
    res = run_cli(
        tmp_path, *K1SE, str(_box(tmp_path / "plate.stl")), "--out", str(tmp_path / "out"), "--walls", "3",
        "--overlay", str(overlay), "--no-slice", "--json", timeout=300,
        env_extra=_env(tmp_path, fake_slicer(tmp_path, k1se_gcode())),
    )  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    project = next(s for s in json.loads(res.stdout)["steps"] if s["name"] == "project")
    assert project["result"]["preset_names"]["process"].endswith(" - prep overrides")
    assert (
        next(s for s in json.loads(res.stdout)["steps"] if s["name"] == "slice")[
            "verdict"
        ]
        == "SKIP"
    )


def _real_orca_env() -> dict[str, str] | None:
    """The owner's real Orca and K1 SE system presets, or None (the test then skips)."""
    orca = shutil.which("snapmaker-orca") or str(
        Path.home() / "Applications/Snapmaker_Orca.AppImage"
    )
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if (
        not Path(orca).is_file()
        or not (config / "Snapmaker_Orca" / "system" / "Creality").is_dir()
    ):
        return None
    return {"ORCA": orca, "HOME": str(Path.home()), "XDG_CONFIG_HOME": str(config)}


@pytest.mark.skipif(
    _real_orca_env() is None,
    reason="snapmaker-orca and its Creality system presets are absent",
)
def test_k1se_prep_real_slice_of_a_small_block(tmp_path: Path) -> None:
    """Real Orca on a 30 x 20 x 4 mm block: the K1 SE presets resolve, the G-code carries their names, every gate runs."""
    env = _real_orca_env()
    assert env is not None
    tm = pytest.importorskip("trimesh")
    tm.creation.box((30.0, 20.0, 4.0)).export(tmp_path / "block.stl")
    res = run_cli(
        tmp_path, "prep", str(tmp_path / "block.stl"), "--printer", "k1se", "--orient", "keep", "--no-adapt",
        "--skip", "repair,fit,stability,proof", "--out", str(tmp_path / "out"), "--filament", ELEGOO,
        "--walls", "3", "--infill", "15", "--set", "outer_wall_speed=250", "--set", "inner_wall_speed=500",
        "--max-volumetric", "30", "--no-lock", "--json", timeout=900, env_extra=env,
    )  # fmt: skip
    steps = {s["name"]: s for s in json.loads(res.stdout)["steps"]}
    assert res.returncode == 0, f"{steps['slice']['verdict']}: {steps['slice']['summary']}"
    gates = {g["name"]: g for g in steps["gates"]["result"]["gates"]}
    assert gates["overrides"]["verdict"] == "PASS", gates["overrides"]
    assert gates["feedrates"]["verdict"] == "PASS", gates["feedrates"]
    assert gates["bed-bounds"]["verdict"] == "PASS"
    stats = steps["gates"]["result"]["stats"]
    assert (
        stats["time"]
        and stats["filament_g"]
        and stats["feed_max_mm_s"]["Outer wall"] <= 250.0
    )
