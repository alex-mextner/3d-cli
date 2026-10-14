"""E2E stories for the fleet registry fleet/printers.yaml (issue #157) through `bin/3d`:
`3d printer list` over the whole fleet, offline/read-only refusals. No story contacts a real
printer: the repo fleet is only listed, every contactable printer in a fixture fleet is an
in-process fake. Lint stories: test_printops_lint_workflows.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fleet_fixtures import REPO_FLEET, u1_entry, write_fleet
from printer_fakes import FakeMoonraker

from .workflow_helper import THREED, isolated_env, run_cli

REPO = {"THREED_FLEET": str(REPO_FLEET)}


def test_printer_list_shows_the_whole_fleet_with_the_broken_u1_head(
    tmp_path: Path,
) -> None:
    """The user plans where to print: every printer they can use, whose it is, whether 3d may
    drive it, and which heads work — the U1's T2 is broken."""
    listed = run_cli(tmp_path, "printer", "list", env_extra=REPO)
    as_json = run_cli(tmp_path, "printer", "list", "--json", env_extra=REPO)

    assert listed.returncode == 0, listed.stderr
    assert "T1 | T2 UNUSABLE | T3 | T4" in listed.stdout
    assert "T2: head T2 does not work" in listed.stdout
    assert "neighbour" in listed.stdout and "- (never contacted)" in listed.stdout
    rows = json.loads(as_json.stdout)
    assert len(rows) >= 5
    by_name = {r["name"]: r for r in rows}
    assert {r["access"] for r in rows} == {"full", "offline"}
    assert by_name["p1s"]["owner"] == "neighbour" and by_name["p1s"]["address"] is None
    assert [h["label"] for h in by_name["u1"]["heads"] if not h["usable"]] == ["T2"]
    assert by_name["u1"]["heads"][0]["nozzle"] == {
        "diameter_mm": 0.4,
        "material": "stainless",
        "type": "std",
    }


def test_unusable_heads_pipe_through_jq_style_shell_chain(tmp_path: Path) -> None:
    """A shell script lists the broken heads of the fleet: `3d printer list --json | python`."""
    env = isolated_env(tmp_path) | REPO
    script = (
        f"{sys.executable} {THREED} printer list --json | {sys.executable} -c "
        "\"import json,sys; print(' '.join(r['name']+'/'+h['label'] for r in json.load(sys.stdin) "
        "for h in r['heads'] if not h['usable']))\" > broken.txt"
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "broken.txt").read_text(encoding="utf-8").strip() == "u1/T2"


def test_contacting_an_offline_printer_is_refused(tmp_path: Path) -> None:
    """The user mistypes the neighbour's P1S into a status/start command: 3d explains it never
    contacts other people's printers instead of trying."""
    (tmp_path / "part.gcode").write_text("G28\n", encoding="utf-8")
    status = run_cli(tmp_path, "printer", "status", "p1s", env_extra=REPO)
    start = run_cli(
        tmp_path,
        "printer",
        "start",
        "a1mini",
        "part.gcode",
        "--yes",
        env_extra=REPO,
    )

    assert status.returncode == 1
    assert (
        "p1s (Bambu Lab P1S, owner neighbour) is an offline fleet printer: 3d never contacts it"
        in status.stderr
    )
    assert start.returncode == 1 and "offline fleet printer" in start.stderr


def test_preflight_on_an_offline_bambu_uses_its_static_profile(tmp_path: Path) -> None:
    """The user checks a G-code for Boris's A1 mini: 3d never contacts it, but its fleet axis
    limits allow the waste-chute moves and its Bambu firmware commands are trusted."""
    gcode = tmp_path / "a1.gcode"
    gcode.write_text(
        "G28\nM620 S0A\nG1 X-13.5 Y20 F3000\nM621 S0A\nG1 X187 Y178\nM1002 gcode_claim_action : 2\n",
        encoding="utf-8",
    )
    done = run_cli(
        tmp_path,
        "print-preflight",
        str(gcode),
        "--printer",
        "a1mini",
        "--json",
        env_extra=REPO,
    )

    report = json.loads(done.stdout)
    checks = {c["name"]: c for c in report["checks"]}
    assert (
        checks["axis travel"]["status"] == "PASS"
        and "a1mini in" in checks["axis travel"]["detail"]
    )
    assert checks["macros"]["status"] == "PASS"
    assert "printer" not in checks  # state never read


def test_read_only_printer_answers_status_but_refuses_upload(tmp_path: Path) -> None:
    """A printer marked read-only can be watched, never written: upload is refused before any
    request reaches it."""
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text("G28\nG1 X10\n", encoding="utf-8")
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon, access="read-only"))
        env = {"THREED_FLEET": str(fleet)}
        status = run_cli(tmp_path, "printer", "status", "u1", env_extra=env)
        upload = run_cli(
            tmp_path, "printer", "upload", "u1", "part.gcode", env_extra=env
        )
        cancel = run_cli(tmp_path, "printer", "cancel", "u1", "--yes", env_extra=env)

    assert status.returncode == 0, status.stderr
    assert "state     idle" in status.stdout
    assert (
        upload.returncode == 1
        and "u1 is read-only" in upload.stderr
        and "refused" in upload.stderr
    )
    assert cancel.returncode == 1 and "u1 is read-only" in cancel.stderr
    assert moon.writes() == []
