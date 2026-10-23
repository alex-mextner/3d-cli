"""E2E stories for `3d printers doctor` through `bin/3d` (and the `3d printer doctor` alias): a fleet
of loopback fakes — a Klipper that sits in `shutdown`, a single-head printer whose Moonraker books
filament to the wrong spool, two offline printers — and a loopback 3d-api. Only loopback is ever
contacted (AGENTS.md: never a real printer)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_fixtures import k1se_moonraker_entry, u1_entry, write_fleet
from printer_doctor_fakes import JPEG, FakeApi, printer_view
from printer_fakes import FakeMoonraker, free_port

from .workflow_helper import isolated_env, run_cli

SKIP_HOST_DEPENDENT = "watch-units-dry-run,camera-snapshot,camera-config"  # systemd / cameras of the host running the test


class Lab:
    """u1 (4 heads, Klipper in shutdown), k1se (1 head, Moonraker active spool 2 while PrintOps
    binds 5) and a 3d-api; the env for `run_cli`."""

    def __init__(
        self, tmp_path: Path, u1: FakeMoonraker, k1: FakeMoonraker, api: FakeApi
    ) -> None:
        self.tmp, self.u1, self.k1, self.api = tmp_path, u1, k1, api
        fleet = write_fleet(
            tmp_path / "fleet.yaml", u1_entry(u1), k1se_moonraker_entry(k1)
        )
        self.env = {
            "THREED_FLEET": str(fleet),
            "THREED_WEBUI_PORTS": str(free_port()),
            "PRINTOPS_API": api.url,
            "PRINTOPS_TOKEN": "po_test",
        }

    def doctor(self, *argv: str, printer_alias: bool = False) -> Any:
        head = ("printer", "doctor") if printer_alias else ("printers", "doctor")
        return run_cli(self.tmp, *head, *argv, env_extra=self.env, timeout=120)

    def journal(self, printer: str) -> list[dict[str, Any]]:
        path = (
            Path(isolated_env(self.tmp)["XDG_DATA_HOME"])
            / "3d-cli"
            / "printer-history"
            / f"{printer}.jsonl"
        )
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )


def lab_servers() -> tuple[FakeMoonraker, FakeMoonraker, FakeApi]:
    u1 = FakeMoonraker(
        server_info={
            "klippy_connected": True,
            "klippy_state": "shutdown",
            "moonraker_version": "v0",
        },
        components=["file_manager"],
        camera=JPEG,
    )
    k1 = FakeMoonraker(
        components=["file_manager", "spoolman"], spoolman=True, active_spool=2
    )
    api = FakeApi(routes={
        "/v1/printers/u1": printer_view("u1", heads=4),
        "/v1/printers/k1se": printer_view("k1se", heads=1, bound={0: [5]}),
        "/v1/anomalies": {"anomalies": []},
        "/v1/printers/u1/faults": {"faults": [{"id": 1, "head": "T2"}]},  # T2 is unusable in u1_entry
        "/v1/printers/k1se/faults": {"faults": []},
        "/v1/printers/p1s/faults": {"faults": []},
        "/v1/printers/boris/faults": {"faults": []},
    })  # fmt: skip
    return u1, k1, api


def results(doc: dict[str, Any], printer: str) -> dict[str, dict[str, Any]]:
    (p,) = [p for p in doc["printers"] if p["name"] == printer]
    return {r["check"]: r for r in p["results"]}


def test_read_only_diagnosis_of_the_whole_fleet_reports_findings_and_writes_nothing(
    tmp_path: Path,
) -> None:
    u1, k1, api = lab_servers()
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        run = lab.doctor("--all", "--json", "--skip", SKIP_HOST_DEPENDENT)
    assert run.returncode == 1, (
        run.stderr
    )  # findings remain: exit 1, and nothing is printed twice
    assert run.stderr == ""
    doc = json.loads(run.stdout)
    assert doc["schema"] == "printers.doctor/1"
    assert {p["name"] for p in doc["printers"]} == {"u1", "k1se", "p1s", "boris"}
    u1_res, k1_res = results(doc, "u1"), results(doc, "k1se")
    assert (
        u1_res["klippy-state"]["status"] == "fail"
        and u1_res["klippy-state"]["fix"]["risk"] == "needs-yes"
    )
    assert (
        u1_res["active-spool-binding"]["status"] == "skip"
        and "4 heads" in u1_res["active-spool-binding"]["summary"]
    )
    assert (
        k1_res["active-spool-binding"]["status"] == "fail"
        and k1_res["active-spool-binding"]["fix"]["risk"] == "safe"
    )
    assert k1_res["active-spool-binding"]["evidence"]["active_spool_id"] == 2
    for off in ("p1s", "boris"):
        r = results(doc, off)
        assert r["reachable"]["summary"] == "never contacted (access: offline)"
    assert doc["summary"]["remaining"] == 2 and doc["summary"]["fixed"] == 0
    assert (
        u1.writes() == []
        and k1.writes() == []
        and not any(m == "POST" for m, _ in api.requests)
    )
    assert lab.journal("k1se") == []


def test_fix_applies_the_safe_fix_only_then_yes_adds_the_restart_and_a_rerun_is_a_noop(
    tmp_path: Path,
) -> None:
    u1, k1, api = lab_servers()
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        first = lab.doctor("--all", "--fix", "--json", "--skip", SKIP_HOST_DEPENDENT)
        assert first.returncode == 1, (
            first.stderr
        )  # u1's needs-yes restart is still open
        doc = json.loads(first.stdout)
        assert u1.writes() == []  # --fix without --yes never restarts anything
        assert [w.path for w in k1.writes()] == [
            "/server/spoolman/spool_id"
        ] and k1.active_spool == 5
        fixed = results(doc, "k1se")["active-spool-binding"]
        assert (
            fixed["status"] == "pass"
            and fixed["fix"]["applied"]
            and fixed["fix"]["verified"] is True
        )
        assert fixed["fix"]["journal"] == "imported"
        klippy = results(doc, "u1")["klippy-state"]
        assert klippy["fix"]["applied"] is False and "--yes" in klippy["fix"]["error"]
        (line,) = lab.journal("k1se")  # the offline buffer holds the journal line ...
        assert (line["old"], line["new"], line["kind"], line["key"]) == (
            2,
            5,
            "doctor_fix",
            "active-spool-binding",
        )
        assert (
            len(api.imported) == 1 and json.loads(api.imported[0])["uid"] == line["uid"]
        )  # ... and 3d-api got it

        second = lab.doctor(
            "--all", "--fix", "--yes", "--json", "--skip", SKIP_HOST_DEPENDENT
        )
        assert second.returncode == 0, second.stdout + second.stderr
        doc2 = json.loads(second.stdout)
        assert [w.path for w in u1.writes()] == ["/printer/firmware_restart"]
        assert results(doc2, "u1")["klippy-state"]["status"] == "pass"
        assert doc2["summary"]["remaining"] == 0 and doc2["summary"]["fixed"] == 1
        assert (
            len(k1.writes()) == 1 and len(lab.journal("k1se")) == 1
        )  # idempotent: nothing re-applied or re-journalled

        third = lab.doctor("--all", "--fix", "--yes", "--skip", SKIP_HOST_DEPENDENT)
        assert third.returncode == 0 and len(u1.writes()) == 1 and len(k1.writes()) == 1


def test_a_busy_printer_refuses_the_restart_and_nothing_is_sent(tmp_path: Path) -> None:
    u1, k1, api = lab_servers()
    u1.status["print_stats"].update(state="printing")
    with u1.serve(), k1.serve(), api.serve():
        run = Lab(tmp_path, u1, k1, api).doctor(
            "u1", "--fix", "--yes", "--check", "klippy-state", "--json"
        )
    assert run.returncode == 1
    fix = results(json.loads(run.stdout), "u1")["klippy-state"]["fix"]
    assert (
        fix["applied"] is False
        and fix["error"].startswith("refused:")
        and "printing" in fix["error"]
    )
    assert u1.writes() == []


def test_text_report_and_the_printer_alias_and_usage_errors(tmp_path: Path) -> None:
    u1, k1, api = lab_servers()
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        text = lab.doctor("u1", "--check", "klippy-state", printer_alias=True)
        none = lab.doctor()
        bad = lab.doctor("nope")
        hlp = lab.doctor("--help")
        camera = lab.doctor("u1", "--check", "camera-snapshot,camera-config", "--json")
    assert text.returncode == 1 and "u1 (moonraker, access full)" in text.stdout
    assert (
        "FAIL klippy-state [error]" in text.stdout
        and "run with --fix --yes to apply it" in text.stdout
    )
    assert (
        none.returncode == 2
        and "no printer given" in none.stderr
        and "boris, k1se, p1s, u1" in none.stderr
    )
    assert bad.returncode == 2 and "accepted: boris, k1se, p1s, u1" in bad.stderr
    assert hlp.returncode == 0 and "--list-checks" in hlp.stdout
    cam = results(
        json.loads(camera.stdout), "u1"
    )  # the model's default camera answers from the fake
    assert (
        cam["camera-snapshot"]["status"] == "pass"
        and cam["camera-config"]["status"] == "pass"
    )
    assert u1.writes() == []


def test_list_checks_works_without_any_fleet(tmp_path: Path) -> None:
    run = run_cli(
        tmp_path,
        "printers",
        "doctor",
        "--list-checks",
        "--json",
        env_extra={"THREED_FLEET": ""},
    )
    assert run.returncode == 0, run.stderr
    ids = [c["id"] for c in json.loads(run.stdout)["checks"]]
    assert "klippy-state" in ids and "spoolman-cors" in ids and ids == sorted(ids)
