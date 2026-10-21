"""E2E stories for `3d printers show NAME` through `bin/3d`: the one-screen dossier of a printer —
model spec, machine, web UIs, live status, queue, history. Only loopback fakes are contacted
(AGENTS.md: never a real printer)."""

from __future__ import annotations

import json
from pathlib import Path

from fleet_fixtures import k1_entry, u1_entry, write_fleet
from printer_fakes import (
    FLUIDD_HTML,
    HISTORY_JOBS,
    HISTORY_TOTALS,
    JOB_QUEUE,
    MAINSAIL_HTML,
    FakeCreality,
    FakeMoonraker,
    FakeWebServer,
    free_port,
)

from .workflow_helper import REPO_ROOT, run_cli


def _env(tmp_path: Path, fleet: Path, *web_ports: int) -> dict[str, str]:
    return {
        "THREED_FLEET": str(fleet),
        "THREED_WEBUI_PORTS": ",".join(map(str, web_ports or (free_port(),))),
    }


def _show(tmp_path: Path, env: dict[str, str], *argv: str):  # noqa: ANN202 — CompletedProcess[str]
    return run_cli(tmp_path, "printers", "show", *argv, env_extra=env, timeout=60)


def test_show_a_fleet_printer_prints_the_whole_dossier(tmp_path: Path) -> None:
    """The owner asks about `u1` and gets spec, address, heads, the web UIs that answer, what it is
    printing, what is queued and what it printed — on one screen, with nothing written to the printer."""
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS, queue=JOB_QUEUE)
    moon.status["print_stats"].update(state="printing", filename="base_1.gcode")
    moon.status["virtual_sdcard"]["progress"] = 0.42
    fluidd, mainsail = FakeWebServer(FLUIDD_HTML), FakeWebServer(MAINSAIL_HTML)
    with moon.serve(), fluidd.serve(), mainsail.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(tmp_path, _env(tmp_path, fleet, fluidd.port, mainsail.port), "u1")

    assert shown.returncode == 0, shown.stderr
    out = shown.stdout
    sections = [
        out.index(s)
        for s in (
            "Model spec: Snapmaker U1",
            "Machine: u1",
            "Web UI",
            "Live status",
            "Queue",
            "History",
        )
    ]
    assert sections == sorted(sections)
    assert "build volume   270 x 270 x 270 mm" in out
    assert f"moonraker  127.0.0.1:{moon.port}" in out
    assert "UNUSABLE: T2 is broken" in out and "stainless" in out
    assert (
        f"http://127.0.0.1:{fluidd.port}/" in out
        and f"http://127.0.0.1:{mainsail.port}/" in out
    )
    assert "Fluidd" in out and "Mainsail" in out and "Moonraker API" in out
    assert (
        "printing  [printing]" in out
        and "base_1.gcode  42.0%" in out
        and "T4 #FFFFFF PLA" in out
    )
    assert "next_part.gcode" in out
    assert (
        "127 jobs" in out
        and "benchy.gcode" in out
        and "cancelled" in out
        and "cube.gcode" in out
    )
    assert moon.writes() == []


def test_history_flag_limits_the_jobs_listed(tmp_path: Path) -> None:
    """`--history 1` lists one job, `--history 0` only the lifetime totals."""
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        env = _env(tmp_path, fleet)
        by_n = {
            n: json.loads(_show(tmp_path, env, "u1", "--history", n, "--json").stdout)
            for n in ("0", "1", "10")
        }

    jobs = {
        n: [j["filename"] for j in doc["printers"][0]["history"]["jobs"]]
        for n, doc in by_n.items()
    }
    assert jobs == {
        "0": [],
        "1": ["benchy.gcode"],
        "10": ["benchy.gcode", "cube.gcode", "old.gcode"],
    }
    assert all(
        doc["printers"][0]["history"]["totals"]["jobs"] == 127 for doc in by_n.values()
    )


def test_the_default_history_is_the_last_five_jobs(tmp_path: Path) -> None:
    """The printer has run eight jobs; without --history the user sees the five most recent."""
    many = [
        {
            "job_id": str(i),
            "filename": f"job{i}.gcode",
            "status": "completed",
            "start_time": 1_700_000_000.0 - i,
            "print_duration": 60.0,
            "filament_used": 1000.0,
        }
        for i in range(8)
    ]
    moon = FakeMoonraker(history=many, totals=HISTORY_TOTALS)
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        doc = json.loads(_show(tmp_path, _env(tmp_path, fleet), "u1", "--json").stdout)

    assert [j["filename"] for j in doc["printers"][0]["history"]["jobs"]] == [
        f"job{i}.gcode" for i in range(5)
    ]


def test_a_model_name_shows_the_spec_and_every_machine_of_that_model(
    tmp_path: Path,
) -> None:
    """`3d printers show "Snapmaker U1"` keeps working and now also shows the fleet's U1."""
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(tmp_path, _env(tmp_path, fleet), "Snapmaker U1")
        prusa = _show(tmp_path, _env(tmp_path, fleet), "Prusa MK4")

    assert shown.returncode == 0, shown.stderr
    assert "Model spec: Snapmaker U1" in shown.stdout and "Machine: u1" in shown.stdout
    assert "benchy.gcode" in shown.stdout
    assert (
        prusa.returncode == 0
        and "Model spec: Prusa MK4" in prusa.stdout
        and "Machine:" not in prusa.stdout
    )


def test_json_is_one_machine_readable_dossier(tmp_path: Path) -> None:
    """A script asks for `--json` and gets one object with the model, the machine, the web UIs, the live state, the queue and the history."""
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS, queue=JOB_QUEUE)
    fluidd = FakeWebServer()
    with moon.serve(), fluidd.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(
            tmp_path,
            _env(tmp_path, fleet, fluidd.port),
            "u1",
            "--json",
            "--history",
            "2",
        )

    assert shown.returncode == 0, shown.stderr
    doc = json.loads(shown.stdout)
    printer = doc["printers"][0]
    assert (
        doc["query"] == "u1"
        and doc["model"]["name"] == "Snapmaker U1"
        and printer["name"] == "u1"
    )
    assert (
        printer["machine"]["host"] == "127.0.0.1"
        and printer["machine"]["port"] == moon.port
    )
    assert [h["usable"] for h in printer["machine"]["heads"]] == [
        True,
        False,
        True,
        True,
    ]
    assert [(w["kind"], w["http_status"]) for w in printer["web_ui"]["urls"]] == [
        ("Fluidd", 200),
        ("Moonraker API", 200),
    ]
    assert (
        printer["live"]["state"] == "idle"
        and printer["live"]["slots"][2]["color"] == "#1B5E20"
    )
    assert [j["filename"] for j in printer["queue"]["jobs"]] == ["next_part.gcode"]
    assert [j["status"] for j in printer["history"]["jobs"]] == [
        "completed",
        "cancelled",
    ]
    assert all(
        printer[k]["status"] == "ok" and printer[k]["error"] is None
        for k in ("web_ui", "live", "queue", "history")
    )


def test_no_live_prints_static_facts_and_touches_no_network(tmp_path: Path) -> None:
    """The user only wants the facts on file (no printer contact): `--no-live` prints them and nothing is sent to the printer or its web UIs."""
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    fluidd = FakeWebServer()
    with moon.serve(), fluidd.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        env = _env(tmp_path, fleet, fluidd.port)
        text = _show(tmp_path, env, "u1", "--no-live")
        as_json = _show(tmp_path, env, "u1", "--no-live", "--json")

    assert text.returncode == 0 and as_json.returncode == 0, (
        text.stderr + as_json.stderr
    )
    assert "Machine: u1" in text.stdout and "UNUSABLE" in text.stdout
    assert (
        "benchy.gcode" not in text.stdout
        and text.stdout.count("skipped (--no-live)") == 4
    )
    doc = json.loads(as_json.stdout)["printers"][0]
    assert {doc[k]["status"] for k in ("web_ui", "live", "queue", "history")} == {
        "skipped"
    }
    assert moon.requests == [] and fluidd.requests == []


def test_an_unreachable_printer_does_not_fail_the_command(tmp_path: Path) -> None:
    """The printer is off: exit 0, static sections print, the four live sections say why."""
    fleet = write_fleet(
        tmp_path / "fleet.yaml", u1_entry(FakeMoonraker(port=free_port()))
    )
    env = _env(tmp_path, fleet)
    shown = _show(tmp_path, env, "u1", "--secs", "1")
    as_json = _show(tmp_path, env, "u1", "--secs", "1", "--json")

    assert shown.returncode == 0, shown.stderr
    assert (
        "Model spec: Snapmaker U1" in shown.stdout
        and "UNUSABLE: T2 is broken" in shown.stdout
    )
    assert shown.stdout.count("unreachable: ") == 4
    doc = json.loads(as_json.stdout)["printers"][0]
    for section in ("web_ui", "live", "queue", "history"):
        assert doc[section]["status"] == "unreachable" and doc[section]["error"]
    assert doc["machine"]["name"] == "u1"


def test_an_offline_fleet_printer_is_never_contacted(tmp_path: Path) -> None:
    """The neighbour's P1S has no address: its dossier is the static profile and says "never contacted"."""
    fleet = write_fleet(tmp_path / "fleet.yaml")
    shown = _show(tmp_path, _env(tmp_path, fleet), "p1s")
    as_json = _show(tmp_path, _env(tmp_path, fleet), "p1s", "--json")

    assert shown.returncode == 0, shown.stderr
    assert "Model spec: Bambu Lab P1S" in shown.stdout and "neighbour" in shown.stdout
    assert (
        shown.stdout.count("never contacted") >= 5
    )  # address line + the four live sections
    doc = json.loads(as_json.stdout)["printers"][0]
    assert {doc[k]["status"] for k in ("web_ui", "live", "queue", "history")} == {
        "skipped"
    }
    assert doc["machine"]["adapter"] is None and doc["machine"]["owner"] == "neighbour"


def test_a_creality_stock_printer_has_no_queue_or_history_but_its_own_web_ui(
    tmp_path: Path,
) -> None:
    """The K1 SE on stock firmware has no queue or history API: the dossier says so plainly, still shows its state and its built-in web page."""
    k1 = FakeCreality()
    with k1.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", k1_entry(k1))
        shown = _show(tmp_path, _env(tmp_path, fleet), "k1se", "--secs", "0.5")

    assert shown.returncode == 0, shown.stderr
    out = shown.stdout
    assert "n/a (creality-stock adapter does not expose a queue)" in out
    assert "n/a (creality-stock adapter does not expose a job history)" in out
    assert f"http://127.0.0.1:{k1.http_port}/" in out and "Creality" in out
    assert (
        "nozzle 210.1/210" in out
    )  # the live status still comes from the same renderer as `printer status`
    assert k1.writes() == []


def test_the_live_status_matches_what_printer_status_prints(tmp_path: Path) -> None:
    """One renderer for both commands: every line of `printer status` body is in the dossier."""
    moon = FakeMoonraker()
    moon.status["print_stats"].update(state="printing", filename="base_1.gcode")
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        env = _env(tmp_path, fleet)
        status = run_cli(tmp_path, "printer", "status", "u1", env_extra=env)
        shown = _show(tmp_path, env, "u1")

    body = [ln.split(None, 1)[1] for ln in status.stdout.splitlines()[1:]]
    assert body and all(
        b in " ".join(shown.stdout.split()) for b in (" ".join(x.split()) for x in body)
    )


def test_an_unknown_name_lists_models_and_fleet_printers(tmp_path: Path) -> None:
    """The user mistypes the printer name and the error lists both the model names and the fleet printers to pick from."""
    moon = FakeMoonraker()
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(tmp_path, _env(tmp_path, fleet), "u2")

    assert shown.returncode == 2 and shown.stdout == ""
    assert "Snapmaker U1" in shown.stderr and "Prusa MK4" in shown.stderr  # model names
    assert "u1" in shown.stderr and "p1s" in shown.stderr  # fleet printers
    assert "3d printers list" in shown.stderr and "3d printer list" in shown.stderr


def test_bad_flag_values_are_structured_usage_errors(tmp_path: Path) -> None:
    """The user passes nonsense (`--history many`, `--secs 0`, an unknown flag, no name) and gets a clear usage error, never a traceback."""
    fleet = write_fleet(tmp_path / "fleet.yaml")
    env = _env(tmp_path, fleet)
    bad = [
        _show(tmp_path, env, "u1", "--history", "many"),
        _show(tmp_path, env, "u1", "--secs", "0"),
        _show(tmp_path, env, "u1", "--bogus"),
        _show(tmp_path, env),
    ]

    assert [r.returncode for r in bad] == [2, 2, 2, 2]
    assert (
        "--history" in bad[0].stderr
        and "--secs" in bad[1].stderr
        and "--bogus" in bad[2].stderr
    )
    assert "Traceback" not in "".join(r.stderr for r in bad)


def test_every_show_flag_is_documented_with_an_example_in_help_and_docs(
    tmp_path: Path,
) -> None:
    """The user reads `3d printers --help` or the docs page and finds every `show` flag with a copy-pasteable example."""
    helped = run_cli(tmp_path, "printers", "--help")
    docs = (REPO_ROOT / "docs" / "commands" / "printers.md").read_text(encoding="utf-8")
    printer_docs = (REPO_ROOT / "docs" / "commands" / "printer.md").read_text(
        encoding="utf-8"
    )

    assert helped.returncode == 0
    for flag, example in (
        ("--json", "printers show u1 --json"),
        ("--no-live", "printers show u1 --no-live"),
        ("--history", "printers show k1se --history 10"),
        ("--secs", "printers show k1se --secs 3"),
    ):
        assert flag in helped.stdout and example in helped.stdout, flag
        assert flag in docs and example in docs, flag
    assert "printers show" in printer_docs


def test_a_configured_camera_snapshot_url_is_listed_without_its_credentials(
    tmp_path: Path,
) -> None:
    """The printer's camera URL carries a password and token in the settings file: the dossier shows the URL without them, in text and in JSON."""
    moon = FakeMoonraker()
    watch = (
        "  watch:\n    exec: \"curl -H 'Authorization: Bearer TOPSECRET' https://hooks.example/x\"\n"
        '    vision:\n      snapshot: "http://admin:hunter2@127.0.0.1:8080/?action=snapshot&token=SNAPTOKEN"\n'
    )
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon) + watch)
        shown = _show(tmp_path, _env(tmp_path, fleet), "u1")
        as_json = _show(tmp_path, _env(tmp_path, fleet), "u1", "--json")

    for r in (shown, as_json):
        assert r.returncode == 0, r.stderr
        assert "http://127.0.0.1:8080/?action=snapshot" in r.stdout.replace(
            "\\u0026", "&"
        )
        for secret in ("TOPSECRET", "hunter2", "SNAPTOKEN", "hooks.example"):
            assert secret not in r.stdout


def test_a_bad_web_ports_override_is_a_structured_error_and_nothing_is_probed(tmp_path: Path) -> None:
    """The user exports a wrong THREED_WEBUI_PORTS: the command refuses with a clear error before sending a single request."""
    moon = FakeMoonraker()
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(tmp_path, {"THREED_FLEET": str(fleet), "THREED_WEBUI_PORTS": "80,99999"}, "u1")

    assert shown.returncode == 2 and "THREED_WEBUI_PORTS" in shown.stderr and "Traceback" not in shown.stderr
    assert moon.requests == []


def test_a_malformed_camera_snapshot_setting_does_not_break_the_dossier(tmp_path: Path) -> None:
    """A typo in the camera URL of the settings file must not hide the rest of the printer's facts."""
    moon = FakeMoonraker()
    watch = '  watch:\n    vision:\n      snapshot: "http://cam:80800/?token=SNAPTOKEN"\n'
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon) + watch)
        shown = _show(tmp_path, _env(tmp_path, fleet), "u1", "--no-live")

    assert shown.returncode == 0, shown.stderr
    assert "Machine: u1" in shown.stdout and "SNAPTOKEN" not in shown.stdout + shown.stderr


def test_json_lists_the_web_ui_candidates_that_did_not_answer(tmp_path: Path) -> None:
    """A script wants to know which web UI places were tried and did not answer: `--json` lists them next to the ones that did."""
    moon = FakeMoonraker()
    fluidd, gone = FakeWebServer(), free_port()
    with moon.serve(), fluidd.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        shown = _show(tmp_path, _env(tmp_path, fleet, fluidd.port, gone), "u1", "--json")

    web = json.loads(shown.stdout)["printers"][0]["web_ui"]
    assert [u["kind"] for u in web["urls"]] == ["Fluidd", "Moonraker API"]
    assert len(web["errors"]) == 1 and str(gone) in web["errors"][0]


def test_show_help_prints_usage_and_contacts_nothing(tmp_path: Path) -> None:
    """The user asks `3d printers show u1 --help` and gets the usage text, not a printer dossier."""
    moon = FakeMoonraker()
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
        helped = _show(tmp_path, _env(tmp_path, fleet), "u1", "--help")

    assert helped.returncode == 0 and "--no-live" in helped.stdout and "--history N" in helped.stdout
    assert moon.requests == []
