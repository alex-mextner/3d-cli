"""E2E stories for `3d lint --printops` (fleet rules, issue #157) through `bin/3d`. The repo fleet
is only linted offline; `--live` stories probe an in-process fake, never a real printer."""

from __future__ import annotations

import json
from pathlib import Path

from fleet_fixtures import BAD_FLEET, REPO_FLEET, u1_entry, write_fleet
from printer_fakes import FakeMoonraker

from .workflow_helper import run_cli


def test_lint_printops_passes_on_the_repo_fleet_and_fails_on_a_broken_one(
    tmp_path: Path,
) -> None:
    """CI lints the fleet data: the repo file is clean; a broken file is reported rule by rule
    as JSON that a script can read back."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_FLEET, encoding="utf-8")
    clean = run_cli(
        tmp_path, "lint", "--printops", env_extra={"THREED_FLEET": str(REPO_FLEET)}
    )
    broken = run_cli(tmp_path, "lint", "--printops", "--fleet", str(bad), "--json")
    misuse = run_cli(tmp_path, "lint", "--live")

    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "lint --printops: PASS" in clean.stdout and "0 error(s)" in clean.stdout
    assert broken.returncode == 1
    report = json.loads(broken.stdout)
    assert {f["rule"] for f in report["findings"]} == {
        "fleet/schema",
        "fleet/model-known",
        "fleet/device",
    }
    assert (
        report["summary"]["errors"] == len(report["findings"])
        and report["summary"]["live"] is False
    )
    assert (
        misuse.returncode == 2 and "--live and --fleet need --printops" in misuse.stderr
    )


def test_lint_printops_live_compares_heads_with_the_printer(tmp_path: Path) -> None:
    """The user checks the fleet file against the real machines (here a fake U1 reporting only
    3 extruders): the head-count mismatch fails the lint; only GETs were sent."""
    moon = FakeMoonraker(
        settings={f"extruder{i or ''}": {"nozzle_diameter": 0.4} for i in range(3)}
    )
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        live = run_cli(tmp_path, "lint", "--printops", "--live", "--fleet", str(fleet))

    assert live.returncode == 1, live.stdout + live.stderr
    assert "u1: fleet lists 4 head(s), the printer reports 3 extruder(s)" in live.stdout
    assert "u1.T4: extruder 'extruder3' is not on the printer" in live.stdout
    assert {r.method for r in moon.requests} == {"GET"}
