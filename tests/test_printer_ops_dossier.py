"""printer_ops.dossier / dossier_render / the Moonraker queue+history reads: what `3d printers show`
knows about a printer. Only loopback fakes; nothing here can reach a real printer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fleet_fixtures import BAD_FLEET, k1_entry, u1_entry, write_fleet
from printer_fakes import (
    FLUIDD_HTML,
    HISTORY_JOBS,
    HISTORY_TOTALS,
    MAINSAIL_HTML,
    FakeCreality,
    FakeMoonraker,
    FakeWebServer,
    free_port,
)

from errors import InvalidArgument
from printer_ops import dossier, dossier_render
from printops.fleet import FleetError


def _registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *entries: str
) -> dossier.Registry:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(
        "THREED_FLEET", str(write_fleet(tmp_path / "fleet.yaml", *entries))
    )
    monkeypatch.chdir(tmp_path)
    return dossier.load_registry()


def _web(monkeypatch: pytest.MonkeyPatch, *servers: FakeWebServer) -> None:
    monkeypatch.setenv(
        "THREED_WEBUI_PORTS", ",".join(str(s.port) for s in servers) or str(free_port())
    )


# ---- name resolution ----------------------------------------------------------------------


def test_a_machine_name_resolves_to_its_machine_and_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)))
    got = dossier.resolve("u1", reg)

    assert [m.name for m in got.machines] == ["u1"]
    assert (
        got.model is not None
        and got.model.name == "Snapmaker U1"
        and got.model_error is None
    )
    assert got.machines[0].device is not None and got.machines[0].fleet is not None


def test_a_model_name_lists_every_machine_of_that_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = u1_entry(FakeMoonraker(port=7126)).replace("u1:", "u1b:", 1)
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)), second)

    assert [m.name for m in dossier.resolve("Snapmaker U1", reg).machines] == [
        "u1",
        "u1b",
    ]
    assert [m.name for m in dossier.resolve("Bambu Lab P1S", reg).machines] == ["p1s"]
    prusa = dossier.resolve("Prusa MK4", reg)
    assert prusa.machines == [] and prusa.model is not None


def test_names_match_case_insensitively_only_when_unambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)))

    assert [m.name for m in dossier.resolve("U1", reg).machines] == ["u1"]
    assert dossier.resolve("snapmaker u1", reg).model_name == "Snapmaker U1"
    reg.models["snapmaker u1"] = reg.models[
        "Snapmaker U1"
    ]  # two models differing only by case
    with pytest.raises(InvalidArgument):
        dossier.resolve("SNAPMAKER U1", reg)


def test_an_unknown_name_lists_model_names_and_fleet_printers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)))
    with pytest.raises(InvalidArgument) as err:
        dossier.resolve("u2", reg)

    accepted = set(err.value.accepted)
    assert {"Snapmaker U1", "Prusa MK4", "u1", "p1s", "boris"} <= accepted
    assert (
        "3d printers list" in err.value.remediation[0]
        and "3d printer list" in err.value.remediation[0]
    )


def test_an_unreadable_fleet_keeps_model_names_working_and_explains_machine_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(bad))
    monkeypatch.chdir(tmp_path)
    reg = dossier.load_registry()

    assert isinstance(reg.error, FleetError)
    assert dossier.resolve("Prusa MK4", reg).machines == []
    with pytest.raises(FleetError):
        dossier.resolve("u1", reg)  # the real reason, not "unknown printer"


# ---- collecting a dossier -----------------------------------------------------------------


def test_collect_reads_status_queue_history_and_web_uis_with_gets_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker(
        history=HISTORY_JOBS,
        totals=HISTORY_TOTALS,
        queue={
            "queue_state": "ready",
            "queued_jobs": [{"filename": "next.gcode", "time_in_queue": 65.0}],
        },
    )
    fluidd, mainsail = FakeWebServer(FLUIDD_HTML), FakeWebServer(MAINSAIL_HTML)
    with moon.serve(), fluidd.serve(), mainsail.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        _web(monkeypatch, fluidd, mainsail)
        d = dossier.collect(reg.machine("u1"), live=True, history_n=2)

    assert (
        d.live.status == dossier.OK
        and d.live.value is not None
        and d.live.value.state == "idle"
    )
    assert d.queue.value is not None and [j.filename for j in d.queue.value.jobs] == [
        "next.gcode"
    ]
    assert d.history.value is not None
    assert [j.filename for j in d.history.value.jobs] == ["benchy.gcode", "cube.gcode"]
    assert d.history.value.totals is not None and d.history.value.totals.jobs == 127
    assert d.web_ui.value is not None
    assert [w.kind for w in d.web_ui.value.found] == [
        "Fluidd",
        "Mainsail",
        "Moonraker API",
    ]
    assert moon.writes() == []
    history_asks = [r.query for r in moon.requests if r.path == "/server/history/list"]
    assert history_asks == [{"limit": ["2"], "order": ["desc"]}]


def test_history_zero_asks_only_for_the_lifetime_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    with moon.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        _web(monkeypatch)
        d = dossier.collect(reg.machine("u1"), live=True, history_n=0)

    assert (
        d.history.value is not None
        and d.history.value.jobs == []
        and d.history.value.totals is not None
    )
    assert not [r for r in moon.requests if r.path == "/server/history/list"]


def test_an_unreachable_printer_degrades_every_live_section_but_not_the_static_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=free_port())))
    _web(monkeypatch)
    machine = reg.machine("u1")
    d = dossier.collect(machine, live=True, secs=1.0)

    for section in (d.web_ui, d.live, d.queue, d.history):
        assert section.status == dossier.UNREACHABLE and section.note
    text = dossier_render.render(dossier.resolve("u1", reg), [d])
    assert text.count("unreachable: ") == 4
    assert (
        "T2" in text and "UNUSABLE" in text and "Snapmaker U1" in text
    )  # static sections still print
    as_json = dossier.to_json(dossier.resolve("u1", reg), [d])["printers"][0]
    assert all(
        as_json[k]["status"] == "unreachable" and as_json[k]["error"]
        for k in ("web_ui", "live", "queue", "history")
    )
    assert as_json["machine"]["heads"][1]["usable"] is False


def test_history_zero_on_an_unreachable_printer_is_unreachable_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=free_port())))
    _web(monkeypatch)
    d = dossier.collect(reg.machine("u1"), live=True, history_n=0, secs=1.0)

    assert d.history.status == dossier.UNREACHABLE and d.history.value is None


def test_history_keeps_what_could_be_read_when_only_one_of_jobs_and_totals_fails() -> None:
    from printer_ops.base import HistoryTotals, JobRecord

    jobs = dossier.Section(dossier.OK, [JobRecord("a.gcode", "completed")])
    totals_down: dossier.Section[HistoryTotals] = dossier.Section(dossier.UNREACHABLE, None, "timed out")
    both = dossier._combine_history(jobs, totals_down)
    assert both.status == dossier.OK and both.value is not None
    assert [j.filename for j in both.value.jobs] == ["a.gcode"] and both.value.totals_error == "timed out"

    jobs_down: dossier.Section[list[JobRecord]] = dossier.Section(dossier.UNREACHABLE, None, "refused")
    totals = dossier.Section(dossier.OK, HistoryTotals(jobs=3))
    only_totals = dossier._combine_history(jobs_down, totals)
    assert only_totals.value is not None and only_totals.value.jobs == [] and only_totals.value.jobs_error == "refused"

    assert dossier._combine_history(jobs_down, totals_down).status == dossier.UNREACHABLE


def test_a_creality_stock_printer_reports_queue_and_history_as_not_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k1 = FakeCreality()
    with k1.serve():
        reg = _registry(tmp_path, monkeypatch, k1_entry(k1))
        d = dossier.collect(reg.machine("k1se"), live=True, secs=0.5)

    assert d.live.status == dossier.OK
    assert (d.queue.status, d.queue.note) == (
        dossier.UNSUPPORTED,
        "n/a (creality-stock adapter does not expose a queue)",
    )
    assert (d.history.status, d.history.note) == (
        dossier.UNSUPPORTED,
        "n/a (creality-stock adapter does not expose a job history)",
    )
    assert d.web_ui.value is not None and [w.kind for w in d.web_ui.value.found] == [
        "Creality"
    ]


def test_no_live_and_offline_printers_make_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker()
    fluidd = FakeWebServer()
    with moon.serve(), fluidd.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        _web(monkeypatch, fluidd)
        no_live = dossier.collect(reg.machine("u1"), live=False)
        offline = dossier.collect(reg.machine("p1s"), live=True)

    assert moon.requests == [] and fluidd.requests == []
    assert {
        s.status for s in (no_live.live, no_live.queue, no_live.history, no_live.web_ui)
    } == {dossier.SKIPPED}
    assert no_live.live.note == "skipped (--no-live)"
    assert offline.machine.device is None and offline.live.note is not None
    assert offline.live.note.startswith("never contacted")


# ---- JSON and secrets ---------------------------------------------------------------------


def test_json_dossier_shape_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    secret_watch = (
        "  watch:\n"
        "    exec: \"curl -H 'Authorization: Bearer TOPSECRET' https://hooks.example/x\"\n"
        "    vision:\n"
        '      snapshot: "http://admin:hunter2@127.0.0.1:8080/?action=snapshot&token=SNAPTOKEN"\n'
    )
    with moon.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon) + secret_watch)
        _web(monkeypatch)
        resolved = dossier.resolve("Snapmaker U1", reg)
        d = dossier.collect(reg.machine("u1"), live=True, history_n=1, secs=1.0)
        doc = dossier.to_json(resolved, [d])
        text = dossier_render.render(resolved, [d])

    blob = json.dumps(doc) + text
    for secret in ("TOPSECRET", "hunter2", "SNAPTOKEN", "hooks.example"):
        assert secret not in blob
    assert doc["model"] == {
        "name": "Snapmaker U1",
        "build_volume_mm": [270.0, 270.0, 270.0],
        "nozzle_mm": 0.4,
        "firmware": "klipper",
        "material": "PLA",
    }
    printer = doc["printers"][0]
    assert set(printer) == {"name", "machine", "web_ui", "live", "queue", "history"}
    assert "raw" not in printer["live"] and printer["live"]["state"] == "idle"
    assert printer["machine"]["watch"]["keys"] == ["exec", "vision"]
    assert printer["machine"]["watch"]["camera_snapshot"].startswith(
        "http://127.0.0.1:8080/?action=snapshot"
    )
    assert [j["filename"] for j in printer["history"]["jobs"]] == ["benchy.gcode"]
    assert printer["history"]["jobs"][0]["started_at"] == "2023-11-14T22:13:20+00:00"
    assert printer["history"]["totals"]["jobs"] == 127
    assert (
        printer["web_ui"]["camera_snapshot"]
        == printer["machine"]["watch"]["camera_snapshot"]
    )


def test_an_offline_machine_keeps_its_static_profile_in_the_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch)
    facts = dossier.machine_facts(reg.machine("boris"))

    assert facts.access == "offline" and facts.adapter is None and facts.host is None
    assert facts.owner == "boris"
    assert [(u.id, u.kind, u.slots) for u in facts.ams or ()] == [("lite", "ams_lite", 4)]


# ---- rendering ----------------------------------------------------------------------------


def test_render_prints_every_section_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker(
        history=HISTORY_JOBS,
        totals=HISTORY_TOTALS,
        queue={
            "queue_state": "ready",
            "queued_jobs": [{"filename": "next.gcode", "time_in_queue": 65.0}],
        },
    )
    fluidd = FakeWebServer()
    with moon.serve(), fluidd.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        _web(monkeypatch, fluidd)
        resolved = dossier.resolve("u1", reg)
        text = dossier_render.render(
            resolved, [dossier.collect(reg.machine("u1"), live=True)]
        )

    order = [
        text.index(h)
        for h in (
            "Model spec: Snapmaker U1",
            "Machine: u1",
            "Web UI",
            "Live status",
            "Queue",
            "History",
        )
    ]
    assert order == sorted(order)
    for fact in (
        "build volume   270 x 270 x 270 mm",
        f"127.0.0.1:{moon.port}",
        "Fluidd",
        "next.gcode",
        "benchy.gcode",
        "127 jobs",
        "412.3 m",
        "2h 14m",
    ):
        assert fact in text


def test_render_of_a_model_without_machines_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch)
    text = dossier_render.render(dossier.resolve("Prusa MK4", reg), [])

    assert (
        "Model spec: Prusa MK4" in text
        and "No fleet or configured printer uses the model 'Prusa MK4'" in text
    )


def test_duration_and_filament_formats() -> None:
    f = dossier_render
    assert [f.fmt_duration(s) for s in (None, 5, 65, 3725, 8040)] == [
        "-",
        "5s",
        "1m 05s",
        "1h 02m",
        "2h 14m",
    ]
    assert (f.fmt_filament(None), f.fmt_filament(12400)) == ("-", "12.4 m")




def test_a_fleet_model_that_differs_from_the_registry_only_by_case_still_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)).replace("Snapmaker U1", "snapmaker u1"))

    by_model = dossier.resolve("Snapmaker U1", reg)
    by_machine = dossier.resolve("u1", reg)
    assert [m.name for m in by_model.machines] == ["u1"]
    assert by_machine.model is not None and by_machine.model.name == "Snapmaker U1" and by_machine.model_error is None


def test_a_reply_of_the_wrong_shape_is_unreadable_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS)
    moon.status["print_stats"] = ["not", "an", "object"]  # parse_status trips over it
    with moon.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        _web(monkeypatch)
        d = dossier.collect(reg.machine("u1"), live=True, secs=2.0)

    assert d.live.status == dossier.UNREACHABLE and d.live.note is not None
    assert d.live.note.startswith("unreadable reply")
    assert d.queue.status == dossier.OK and d.history.status == dossier.OK  # the other reads are unaffected


def test_a_bad_snapshot_setting_cannot_break_the_static_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch = '  watch:\n    vision:\n      snapshot: "http://cam:80800/?token=SECRETTOKEN"\n'
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)) + watch)
    d = dossier.collect(reg.machine("u1"), live=False)

    assert d.facts.watch.camera_snapshot == "(invalid URL)"
    assert "SECRETTOKEN" not in dossier_render.render(dossier.resolve("u1", reg), [d])


def test_a_bare_snapshot_path_is_resolved_against_the_printer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch = "  watch:\n    vision:\n      snapshot: webcam/?action=snapshot\n"
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)) + watch)

    assert dossier.camera_snapshot(reg.machine("u1")) == "http://127.0.0.1:7125/webcam/?action=snapshot"


def test_a_bad_web_ports_setting_is_reported_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon = FakeMoonraker()
    with moon.serve():
        reg = _registry(tmp_path, monkeypatch, u1_entry(moon))
        monkeypatch.setenv("THREED_WEBUI_PORTS", "80,70000")
        with pytest.raises(InvalidArgument, match="THREED_WEBUI_PORTS"):
            dossier.collect(reg.machine("u1"), live=True)

    assert moon.requests == []


def test_a_timestamp_no_clock_can_show_does_not_break_text_or_json() -> None:
    from printer_ops.base import JobRecord

    history = dossier.History([JobRecord("a.gcode", "completed", start_time=1e300, print_s=60.0)], None)
    sec = dossier.Section(dossier.OK, history)

    assert dossier._history_json(history)["jobs"][0]["started_at"] is None
    assert "a.gcode" in "\n".join(dossier_render._history(sec))


def test_a_model_the_registry_does_not_know_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)).replace("Snapmaker U1", "Prusa XL"))
    resolved = dossier.resolve("u1", reg)
    d = dossier.collect(reg.machine("u1"), live=False)
    doc = dossier.to_json(resolved, [d])

    assert resolved.model is None and resolved.model_error is not None and "Prusa XL" in resolved.model_error
    assert doc["model"] is None and doc["model_error"] == resolved.model_error
    text = dossier_render.render(resolved, [d])
    assert "Model spec: Prusa XL" in text and "not in the printer registry" in text and "Machine: u1" in text


def test_a_device_no_adapter_can_be_built_for_degrades_only_that_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printer_ops.base import PrinterRefused

    def refuse(*_a: object, **_k: object) -> None:
        raise PrinterRefused("u1 cannot be driven", command="printer")

    reg = _registry(tmp_path, monkeypatch, u1_entry(FakeMoonraker(port=7125)))
    monkeypatch.setattr("printer_ops.make_adapter", refuse)
    d = dossier.collect(reg.machine("u1"), live=True)

    assert {s.status for s in (d.web_ui, d.live, d.queue, d.history)} == {dossier.UNREACHABLE}
    assert d.live.note == "u1 cannot be driven" and d.facts.name == "u1"


def test_a_broken_fleet_error_names_the_command_the_user_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(bad))
    monkeypatch.chdir(tmp_path)
    reg = dossier.load_registry()

    with pytest.raises(FleetError) as err:
        dossier.resolve("u1", reg)
    assert err.value.command == "printers"
