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

SKIP_HOST_DEPENDENT = "watch-units-dry-run,camera-snapshot,camera-config,alert-subscribers,alert-fallback-agent,printer-supervision"  # systemd / cameras of the host running the test


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
    """The owner runs `3d printers doctor --all --json` on the fleet: Klipper on one printer sits in `shutdown`, another's Moonraker
    books filament to the wrong spool, two printers are offline. The report names each finding, never contacts the offline ones,
    exits 1 because findings remain, and nothing is written to a printer or to 3d-api."""
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
    """The owner repairs with `--fix`: the safe active-spool fix is applied, verified and journalled (offline buffer + 3d-api import)
    while the Klipper restart waits for `--yes`; `--fix --yes` then restarts it, the exit code turns 0 and a third run does nothing."""
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
    """The owner runs `--fix --yes` while the printer is printing: the restart is refused with the reason and nothing reaches the printer."""
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
    """The owner reads the text report through the `3d printer doctor` alias, then makes the usual mistakes: no printer named and an
    unknown printer are usage errors (exit 2) that list the valid names; `--help` documents `--list-checks`."""
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
    """On a machine with no fleet file `3d printers doctor --list-checks --json` still lists every check, sorted by id."""
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


def test_a_read_only_printer_is_diagnosed_but_never_restarted(tmp_path: Path) -> None:
    """A printer registered `access: read-only` sits in Klipper `shutdown`. `--fix --yes` reports the finding and the manual
    steps, refuses the restart because 3d only writes to access: full printers, sends nothing and exits 1."""
    u1, k1, api = lab_servers()
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        lab.env["THREED_FLEET"] = str(
            write_fleet(tmp_path / "ro-fleet.yaml", u1_entry(u1, access="read-only"))
        )
        run = lab.doctor("u1", "--check", "klippy-state", "--fix", "--yes", "--json")
    assert run.returncode == 1, run.stderr
    fix = results(json.loads(run.stdout), "u1")["klippy-state"]["fix"]
    assert fix["applied"] is False and fix["error"].startswith(
        "refused: access read-only"
    )
    assert fix["manual_steps"] and u1.writes() == []
    assert lab.journal("u1") == []


def _tg_ctl(tmp_path: Path, sessions: list[dict[str, Any]]) -> dict[str, str]:
    """A fake `tg-ctl` first on PATH that lists `sessions` as the live omp sessions."""
    import os
    import sys

    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "tg-ctl"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if sys.argv[1:3] == ['omp-live', 'status']:\n"
        f"    print({json.dumps(sessions)!r})\n"
        "else:\n    print('{\"ok\":true}')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}


def test_alert_subscribers_prunes_dead_subscriptions_then_passes_once_a_live_session_subscribes(
    tmp_path: Path,
) -> None:
    """Three subscriptions of a session that died days ago swallow every U1 alert. `3d printers doctor u1 --check
    alert-subscribers` fails (exit 1) and names each dead entry; `--fix` removes the dead entries (journalled), says the
    finding remains and prints the subscribe command; a rerun is a no-op; after a live session subscribes the check passes."""
    u1, k1, api = lab_servers()
    live = {
        "pid": 433553,
        "sessionId": "01a12cd6-7b39-7356",
        "cwd": "/tmp",
        "title": "main",
    }
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        lab.env.update(_tg_ctl(tmp_path, [live]))
        ids = []
        for session in ("dead0001", "dead0002"):
            made = run_cli(
                tmp_path,
                "printer",
                "subscribe",
                "--session",
                session,
                "--printer",
                "u1",
                "--json",
                env_extra=lab.env,
            )
            assert made.returncode == 0, made.stderr
            ids.append(json.loads(made.stdout)["id"])
        state = (
            Path(isolated_env(tmp_path)["XDG_STATE_HOME"])
            / "3d-cli"
            / "printer-subscriptions.json"
        )
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"subscriptions": {
            ids[0]: {"failed": 89, "consecutive_failed": 89, "last_error": "no live omp session matches 'dead0001'"},
            ids[1]: {"failed": 41, "consecutive_failed": 41, "last_error": "no live omp session matches 'dead0002'"},
        }, "agents": {}}))  # fmt: skip

        read_only = lab.doctor("u1", "--check", "alert-subscribers", "--json")
        fixed = lab.doctor("u1", "--check", "alert-subscribers", "--fix", "--json")
        again = lab.doctor("u1", "--check", "alert-subscribers", "--fix", "--json")
        subscribed = run_cli(
            tmp_path,
            "printer",
            "subscribe",
            "--session",
            "01a12cd6",
            "--printer",
            "u1",
            env_extra=lab.env,
        )
        healthy = lab.doctor("u1", "--check", "alert-subscribers", "--json")

    assert read_only.returncode == 1, read_only.stderr
    first = results(json.loads(read_only.stdout), "u1")["alert-subscribers"]
    assert first["status"] == "fail" and first["severity"] == "error"
    assert sorted(first["evidence"]["prunable"]) == sorted(ids)
    assert {row["consecutive_failed"] for row in first["evidence"]["entries"]} == {
        89,
        41,
    }
    assert first["fix"]["risk"] == "safe" and first["fix"]["applied"] is False

    assert fixed.returncode == 1  # the finding remains: nobody is subscribed
    fix = results(json.loads(fixed.stdout), "u1")["alert-subscribers"]
    assert (
        fix["fix"]["applied"] is True
        and fix["fix"]["verified"] is True
        and fix["fix"]["journal"] == "imported"
    )
    assert fix["status"] == "fail" and "no subscription covers" in fix["summary"]
    assert (
        "then: 3d printer subscribe --session SESSION_ID --printer u1"
        in fix["fix"]["manual_steps"]
    )
    (line,) = lab.journal("u1")
    assert (line["kind"], line["key"], line["new"]) == (
        "doctor_fix",
        "alert-subscribers",
        [],
    )
    assert len(line["old"]) == 2

    noop = results(json.loads(again.stdout), "u1")["alert-subscribers"]["fix"]
    assert noop["applied"] is False and noop["error"].startswith(
        "refused: nothing to prune"
    )
    assert len(lab.journal("u1")) == 1

    assert subscribed.returncode == 0, subscribed.stderr
    assert healthy.returncode == 0, healthy.stdout
    assert (
        results(json.loads(healthy.stdout), "u1")["alert-subscribers"]["status"]
        == "pass"
    )
    assert u1.writes() == [] and k1.writes() == []  # the printers were never written to


def _fake_agent_tools(tmp_path: Path) -> dict[str, str]:
    """Fake `tmux` and `omp` first on PATH (the check only looks that they are installed)."""
    import sys

    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    for tool in ("tmux", "omp"):
        script = bindir / tool
        script.write_text(
            f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8"
        )
        script.chmod(0o755)
    return {}


def test_alert_fallback_agent_names_a_dry_run_watcher_as_the_reason_alerts_went_nowhere(
    tmp_path: Path,
) -> None:
    """The U1's watcher unit ran with `--dry-run` and its PAUSED alert never reached the temporary agent: with no live subscriber
    `3d printers doctor u1 --check alert-fallback-agent` fails (exit 1), says the dry-run watcher swallowed alerts after the
    agent's last real one, and prints the tmux and unit commands; once a live session subscribes the check passes."""
    u1, k1, api = lab_servers()
    live = {
        "pid": 433553,
        "sessionId": "01a12cd6-7b39-7356",
        "cwd": "/tmp",
        "title": "main",
    }
    with u1.serve(), k1.serve(), api.serve():
        lab = Lab(tmp_path, u1, k1, api)
        lab.env.update(_tg_ctl(tmp_path, [live]))
        _fake_agent_tools(tmp_path)
        lab.env["PATH"] = f"{tmp_path / 'fakebin'}:{lab.env['PATH']}"
        state_home = Path(isolated_env(tmp_path)["XDG_STATE_HOME"]) / "3d-cli"
        (state_home / "vision").mkdir(parents=True, exist_ok=True)
        (state_home / "printer-subscriptions.json").write_text(json.dumps({"subscriptions": {}, "agents": {"u1": {
            "last_ts": "2026-10-21T16:08:00+00:00", "last_kinds": ["printer_paused"], "stopped": "2026-10-21T17:00:00+00:00",
            "dry_run_ts": "2026-10-22T20:22:59+00:00", "dry_run_events": 2}}}))  # fmt: skip
        (state_home / "vision" / "u1.json").write_text(json.dumps({"schema": 1, "printer": "u1", "ts": 1,
            "last_event": {"kind": "printer_paused", "severity": "warning", "ts": 1_792_700_579.0}}))  # fmt: skip

        broken = lab.doctor("u1", "--check", "alert-fallback-agent", "--json")
        text = lab.doctor("u1", "--check", "alert-fallback-agent")
        run_cli(
            tmp_path,
            "printer",
            "subscribe",
            "--session",
            "01a12cd6",
            "--printer",
            "u1",
            env_extra=lab.env,
        )
        healthy = lab.doctor("u1", "--check", "alert-fallback-agent", "--json")

    assert broken.returncode == 1, broken.stderr
    res = results(json.loads(broken.stdout), "u1")["alert-fallback-agent"]
    assert (
        res["status"] == "fail"
        and res["severity"] == "error"
        and res["fix"]["risk"] == "manual-only"
    )
    assert "--dry-run swallowed 2 alert(s)" in res["summary"]
    assert (
        res["evidence"]["agent"]["dry_run_events"] == 2
        and res["evidence"]["watcher_newest_alert"]["kind"] == "printer_paused"
    )
    assert res["fix"]["applied"] is False
    assert "$ 3d printer vision doctor u1" in res["fix"]["manual_steps"]
    assert (
        "        $ systemctl --user cat 3d-printer-watch@u1.service 3d-printer-vision@u1.service"
        in text.stdout
    )
    assert healthy.returncode == 0, healthy.stdout
    ok = results(json.loads(healthy.stdout), "u1")["alert-fallback-agent"]
    assert ok["status"] == "pass" and "not needed" in ok["summary"]
    assert u1.writes() == [] and k1.writes() == []
