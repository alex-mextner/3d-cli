"""The checks that look beyond Moonraker's own state: Spoolman link + CORS, the active spool vs
PrintOps, slots, head flags vs faults, camera, watch services, stale device overrides. Loopback
fakes only (FakeMoonraker, FakeSpoolmanCors, FakeCameraServer, FakeApi)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from printer_doctor.engine import Options, run_printer
from printer_doctor.model import CheckResult, ResultRecord
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import (
    FakeApi,
    FakeCameraServer,
    FakeSpoolmanCors,
    Rig,
    ev,
    printer_view,
    rig,
    target,
)
from printer_fakes import FLUIDD_HTML, FakeMoonraker, FakeWebServer, free_port
from printer_ops.deploy_check import Show
from printer_ops.webui import probe

CHECKS = {c.id: c for c in discover_checks()}


def check(
    cid: str, tgt: Any, options: Options | None = None, **kw: Any
) -> tuple[CheckResult, Rig]:
    r = rig(tgt, **kw)
    rec = run_printer(r.ctx, [CHECKS[cid]], options or Options()).records[0]
    return rec.result, r


def spoolman_moon(**kw: Any) -> FakeMoonraker:
    return FakeMoonraker(spoolman=True, components=["spoolman", "file_manager"], **kw)


# ---- spoolman-link --------------------------------------------------------------------------


def test_spoolman_link_passes_when_connected_and_idle_of_pending() -> None:
    moon = spoolman_moon(
        active_spool=2,
        config={"spoolman": {"server": "http://spool.example:7912", "sync_rate": 5}},
    )
    with moon.serve():
        res, _ = check("spoolman-link", target(port=moon.port))
    assert res.status == "pass" and ev(res)["spool_id"] == 2
    assert ev(res)["spoolman_server"] == "http://spool.example:7912"


def test_spoolman_link_is_an_error_when_not_connected_with_manual_steps() -> None:
    moon = spoolman_moon(
        spoolman_connected=False,
        config={"spoolman": {"server": "http://spool.example:7912"}},
    )
    with moon.serve():
        res, r = check(
            "spoolman-link", target(port=moon.port), Options(fix=True, yes=True)
        )
    assert (res.status, res.severity) == ("fail", "error") and moon.writes() == []
    rec = run_printer(r.ctx, [CHECKS["spoolman-link"]], Options()).records[0]
    assert rec.fix is not None and any(
        "spool.example:7912/api/v1/health" in s for s in rec.fix.manual_steps
    )


def test_spoolman_link_warns_on_queued_usage_reports_with_their_spool_ids() -> None:
    moon = spoolman_moon(
        spoolman_pending=[
            {"spool_id": 4, "filament_used": 12.5},
            {"spool_id": 9, "filament_used": 1.0},
        ]
    )
    with moon.serve():
        res, _ = check("spoolman-link", target(port=moon.port))
    assert (res.status, res.severity) == ("fail", "warn")
    assert ev(res)["pending_reports"] == 2 and ev(res)["pending_spool_ids"] == [4, 9]


def test_spoolman_checks_skip_a_printer_without_the_spoolman_component_and_never_ask() -> (
    None
):
    moon = FakeMoonraker(
        components=["file_manager"]
    )  # like a U1: no [spoolman], its endpoints 404
    with moon.serve():
        for cid in ("spoolman-link", "spoolman-cors"):
            res, r = check(cid, target(port=moon.port))
            assert res.status == "skip" and "no [spoolman] component" in res.summary
            assert not any("spoolman" in u for _, u in r.ctx.requests)


# ---- spoolman-cors --------------------------------------------------------------------------


def _cors_case(
    allowed: tuple[str, ...], options: Options | None = None
) -> tuple[ResultRecord, FakeSpoolmanCors]:
    fluidd, spool = FakeWebServer(FLUIDD_HTML), FakeSpoolmanCors()
    with spool.serve(), fluidd.serve():
        origin = f"http://127.0.0.1:{fluidd.port}"
        spool.allowed = tuple(origin if a == "<ui>" else a for a in allowed)
        moon = spoolman_moon(config={"spoolman": {"server": spool.url}})
        with moon.serve():
            r = rig(
                target(port=moon.port), webui=lambda d: probe(d, ports=(fluidd.port,))
            )
            rec = run_printer(
                r.ctx, [CHECKS["spoolman-cors"]], options or Options()
            ).records[0]
    return rec, spool


def test_spoolman_cors_passes_when_the_web_ui_origin_is_admitted() -> None:
    rec, spool = _cors_case(("<ui>",))
    res = rec.result
    assert res.status == "pass"
    (row,) = ev(res)["origins"]
    assert (
        row["admitted"] is True and row["access_control_allow_origin"] == row["origin"]
    )
    assert spool.requests and all(m == "GET" for m, _, _ in spool.requests)


def test_spoolman_cors_fails_when_spoolman_does_not_admit_the_origin_and_names_the_fix() -> (
    None
):
    rec, spool = _cors_case(("http://somewhere-else:1",), Options(fix=True, yes=True))
    res = rec.result
    assert (res.status, res.severity) == ("fail", "error")
    (row,) = ev(res)["origins"]
    assert (
        row["admitted"] is False
        and row["access_control_allow_origin"] is None
        and row["status"] == 200
    )
    assert ev(res)["missing"] == [row["origin"]]
    assert all(m == "GET" for m, _, _ in spool.requests)
    steps = " ".join(rec.fix.manual_steps) if rec.fix else ""
    assert (
        "PRINTOPS_SPOOLMAN_CORS_ORIGIN" in steps
        and "services/3d-api/compose.yaml" in steps
    )
    assert "--force-recreate spoolman" in steps


def test_spoolman_cors_accepts_a_wildcard_and_skips_without_any_web_ui() -> None:
    rec, _ = _cors_case(("*",))
    assert rec.result.status == "pass"
    spool = FakeSpoolmanCors()
    with (
        spool.serve(),
        spoolman_moon(config={"spoolman": {"server": spool.url}}).serve() as moon,
    ):
        res, _ = check(
            "spoolman-cors",
            target(port=moon.port),
            webui=lambda d: probe(d, ports=(free_port(),)),
        )
    assert (
        res.status == "skip"
        and "no Fluidd/Mainsail" in res.summary
        and spool.requests == []
    )


# ---- active-spool-binding -------------------------------------------------------------------


def _bound_api(bound: dict[int, list[int]] | None, heads: int = 1) -> FakeApi:
    return FakeApi(
        routes={"/v1/printers/p1": printer_view("p1", heads=heads, bound=bound)}
    )


def test_active_spool_is_skipped_for_a_multi_head_printer_with_the_reason_and_no_contact() -> (
    None
):
    moon = spoolman_moon(active_spool=1)
    with moon.serve():
        res, r = check(
            "active-spool-binding",
            target(port=moon.port, heads=4),
            api=_bound_api({0: [5]}, 4).client(),
        )
    assert (
        res.status == "skip"
        and "4 heads" in res.summary
        and "ONE active spool" in res.summary
    )
    assert r.ctx.requests == [] and moon.requests == []


def test_active_spool_passes_when_it_equals_the_bound_spool_or_both_are_empty() -> None:
    moon = spoolman_moon(active_spool=5)
    with moon.serve():
        res, _ = check(
            "active-spool-binding",
            target(port=moon.port),
            api=_bound_api({0: [5]}).client(),
        )
    assert res.status == "pass" and ev(res)["bound_spool_ids"] == [5]
    moon = spoolman_moon(active_spool=None)
    with moon.serve():
        res, _ = check(
            "active-spool-binding", target(port=moon.port), api=_bound_api({}).client()
        )
    assert res.status == "pass"


def test_active_spool_mismatch_is_reported_and_read_only_runs_write_nothing() -> None:
    moon = spoolman_moon(active_spool=2)
    with moon.serve():
        res, _ = check(
            "active-spool-binding",
            target(port=moon.port),
            api=_bound_api({0: [5]}).client(),
        )
    assert (res.status, res.severity) == ("fail", "error")
    assert ev(res)["active_spool_id"] == 2 and ev(res)["bound_spool_ids"] == [5]
    assert moon.writes() == []


def test_active_spool_safe_fix_sets_the_bound_spool_verifies_journals_and_is_idempotent() -> (
    None
):
    moon = spoolman_moon(active_spool=2)
    api = _bound_api({0: [5]})
    with moon.serve():
        res, r = check(
            "active-spool-binding",
            target(port=moon.port),
            Options(fix=True),
            api=api.client(),
        )
        assert res.status == "pass" and moon.active_spool == 5
        assert [w.path for w in moon.writes()] == ["/server/spoolman/spool_id"]
        assert (r.journal[0].before, r.journal[0].after, r.journal[0].risk) == (
            2,
            5,
            "safe",
        )
        res, r = check(
            "active-spool-binding",
            target(port=moon.port),
            Options(fix=True),
            api=api.client(),
        )
    assert (
        res.status == "pass" and len(moon.writes()) == 1 and r.journal == []
    )  # second run: no-op


def test_active_spool_fix_clears_a_stale_active_spool_when_nothing_is_bound() -> None:
    moon = spoolman_moon(active_spool=7)
    with moon.serve():
        res, _ = check(
            "active-spool-binding",
            target(port=moon.port),
            Options(fix=True),
            api=_bound_api({}).client(),
        )
    assert res.status == "pass" and moon.active_spool is None


def test_active_spool_fix_is_refused_mid_print_and_sends_nothing() -> None:
    moon = spoolman_moon(active_spool=2)
    moon.status["print_stats"].update(state="printing")
    with moon.serve():
        r = rig(target(port=moon.port), api=_bound_api({0: [5]}).client())
        rec = run_printer(
            r.ctx, [CHECKS["active-spool-binding"]], Options(fix=True)
        ).records[0]
    assert (
        rec.fix is not None
        and rec.fix.error
        and rec.fix.error.startswith("refused:")
        and not rec.fix.applied
    )
    assert moon.writes() == [] and moon.active_spool == 2 and r.journal == []


def test_active_spool_refuses_to_pick_between_two_bound_spools() -> None:
    moon = spoolman_moon(active_spool=2)
    with moon.serve():
        r = rig(target(port=moon.port), api=_bound_api({0: [5, 6]}).client())
        rec = run_printer(
            r.ctx, [CHECKS["active-spool-binding"]], Options(fix=True)
        ).records[0]
    assert (
        rec.result.status == "fail"
        and rec.fix is not None
        and "refused" in (rec.fix.error or "")
    )
    assert moon.writes() == []


def test_active_spool_skips_without_3d_api_or_without_the_component() -> None:
    moon = spoolman_moon(active_spool=2)
    with moon.serve():
        res, _ = check("active-spool-binding", target(port=moon.port), api=None)
    assert res.status == "skip" and "3d-api is not configured" in res.summary
    moon = FakeMoonraker(components=["file_manager"])
    with moon.serve():
        res, _ = check(
            "active-spool-binding",
            target(port=moon.port),
            api=_bound_api({0: [5]}).client(),
        )
    assert res.status == "skip" and "no [spoolman]" in res.summary


def test_active_spool_skips_when_3d_api_cannot_read_spoolman() -> None:
    api = FakeApi(
        routes={
            "/v1/printers/p1": printer_view(
                "p1", spools_error="SPOOLMAN_URL is not set"
            )
        }
    )
    with spoolman_moon().serve() as moon:
        res, _ = check("active-spool-binding", target(port=moon.port), api=api.client())
    assert res.status == "skip" and "SPOOLMAN_URL" in res.summary


# ---- slot-binding-unknown -------------------------------------------------------------------


def _slots_api(
    view: dict[str, Any], anomalies: list[dict[str, Any]] | None = None
) -> FakeApi:
    return FakeApi(
        routes={
            "/v1/printers/p1": view,
            "/v1/anomalies": {"anomalies": anomalies or []},
        }
    )


def test_slot_binding_passes_when_every_bound_slot_has_a_reading() -> None:
    api = _slots_api(printer_view("p1", bound={0: [5]}))
    res, _ = check("slot-binding-unknown", target(), api=api.client())
    assert res.status == "pass"


def test_slot_binding_warns_for_a_bound_slot_with_no_or_empty_reading() -> None:
    cases: list[tuple[dict[int, dict[str, Any] | None], str]] = [
        ({0: None}, "unknown"),
        ({0: {"present": False}}, "empty"),
    ]
    for live, why in cases:
        res, _ = check(
            "slot-binding-unknown",
            target(),
            api=_slots_api(printer_view("p1", bound={0: [5]}, live=live)).client(),
        )
        assert (res.status, res.severity) == ("fail", "warn") and why in str(
            ev(res)["slots"]
        )
    res, _ = check(
        "slot-binding-unknown",
        target(),
        api=_slots_api(printer_view("p1", bound={}, live={0: None})).client(),
    )
    assert (
        res.status == "pass"
    )  # nothing bound: an unknown reading is not a binding problem


def test_slot_binding_warns_for_an_open_anomaly() -> None:
    api = _slots_api(
        printer_view("p1", bound={0: [5]}),
        [{"id": 3, "kind": "color_mismatch", "subject": "slot:p1/T1"}],
    )
    res, _ = check("slot-binding-unknown", target(), api=api.client())
    assert res.status == "fail" and ev(res)["anomalies"] == [
        {"id": 3, "kind": "color_mismatch", "subject": "slot:p1/T1"}
    ]
    assert ("GET", "/v1/anomalies") in api.requests


def test_slot_binding_skips_unpolled_offline_and_unconfigured() -> None:
    res, _ = check(
        "slot-binding-unknown",
        target(),
        api=_slots_api(printer_view("p1", polled=False)).client(),
    )
    assert res.status == "skip" and "does not poll" in res.summary
    api = _slots_api(printer_view("p1"))
    res, _ = check("slot-binding-unknown", target(access="offline"), api=api.client())
    assert res.status == "skip" and "offline" in res.summary and api.requests == []
    res, _ = check("slot-binding-unknown", target(), api=None)
    assert res.status == "skip" and "not configured" in res.summary


# ---- heads-vs-faults ------------------------------------------------------------------------


def _faults_api(faults: list[dict[str, Any]]) -> FakeApi:
    return FakeApi(routes={"/v1/printers/p1/faults": {"faults": faults}})


def test_heads_vs_faults_passes_when_flags_and_faults_agree() -> None:
    res, _ = check(
        "heads-vs-faults",
        target(heads=2, unusable=(1,)),
        api=_faults_api([{"id": 4, "head": "T2"}]).client(),
    )
    assert res.status == "pass"
    res, _ = check("heads-vs-faults", target(heads=2), api=_faults_api([]).client())
    assert res.status == "pass"


def test_heads_vs_faults_is_info_for_an_unusable_head_without_a_fault() -> None:
    res, _ = check(
        "heads-vs-faults", target(heads=2, unusable=(1,)), api=_faults_api([]).client()
    )
    assert (res.status, res.severity) == ("fail", "info") and ev(res)[
        "unusable_without_fault"
    ] == [{"head": "T2", "reason": "broken"}]


def test_heads_vs_faults_warns_for_an_open_fault_on_a_usable_head() -> None:
    res, _ = check(
        "heads-vs-faults",
        target(heads=2),
        api=_faults_api([{"id": 9, "head": "T1"}, {"id": 10, "head": None}]).client(),
    )
    assert (res.status, res.severity) == ("fail", "warn") and ev(res)[
        "fault_on_usable_head"
    ] == [{"head": "T1", "faults": [9]}]


def test_heads_vs_faults_skips_without_3d_api_and_without_a_fleet_entry() -> None:
    res, _ = check("heads-vs-faults", target(heads=2), api=None)
    assert res.status == "skip"
    res, _ = check(
        "heads-vs-faults", target(with_fleet=False), api=_faults_api([]).client()
    )
    assert res.status == "skip" and "no fleet entry" in res.summary


# ---- camera ---------------------------------------------------------------------------------


def _cam(url: str, **extra: Any) -> dict[str, Any]:
    return {"vision": {"camera": {"kind": "snapshot-url", "url": url, **extra}}}


def test_camera_snapshot_passes_for_a_jpeg_and_fails_for_every_dead_variant() -> None:
    cam = FakeCameraServer()
    with cam.serve():
        res, _ = check(
            "camera-snapshot", target(watch=_cam(f"http://127.0.0.1:{cam.port}/snap"))
        )
        assert (
            res.status == "pass"
            and ev(res)["bytes"] == len(cam.body)
            and ev(res)["http_status"] == 200
        )
        cam.status = 404
        res, _ = check(
            "camera-snapshot", target(watch=_cam(f"http://127.0.0.1:{cam.port}/snap"))
        )
        assert (res.status, res.severity) == (
            "fail",
            "error",
        ) and "HTTP 404" in res.summary
        cam.status, cam.body = 200, b"<html>not an image</html>"
        res, _ = check(
            "camera-snapshot", target(watch=_cam(f"http://127.0.0.1:{cam.port}/snap"))
        )
        assert res.status == "fail" and "not a JPEG" in res.summary
    res, _ = check(
        "camera-snapshot", target(watch=_cam(f"http://127.0.0.1:{free_port()}/snap"))
    )
    assert res.status == "fail" and "does not answer" in res.summary


def test_a_sleeping_camera_is_woken_once_and_asked_again_but_only_when_it_needs_keepalive() -> (
    None
):
    woken: list[str] = []
    cam = FakeCameraServer(wake_after=1)
    with cam.serve():
        url = f"http://127.0.0.1:{cam.port}/snap"
        res, _ = check(
            "camera-snapshot",
            target(watch=_cam(url, keepalive=True)),
            wake_camera=lambda d, s: woken.append(s.kind),
        )
        assert (
            res.status == "pass"
            and ev(res)["woken_with_keepalive"] is True
            and woken == ["snapshot-url"]
        )
        cam.hits = 0
        res, _ = check(
            "camera-snapshot",
            target(watch=_cam(url)),
            wake_camera=lambda d, s: woken.append("again"),
        )
    assert res.status == "fail" and woken == [
        "snapshot-url"
    ]  # no keepalive configured: never woken, one honest 503


def test_camera_checks_skip_a_printer_without_a_camera_and_config_problems_fail_camera_config() -> (
    None
):
    for cid in ("camera-snapshot", "camera-config"):
        res, _ = check(cid, target())
        assert res.status == "skip" and "no camera" in res.summary
    bad = {"vision": {"camera": {"kind": "bogus"}}}
    res, _ = check("camera-config", target(watch=bad))
    assert (res.status, res.severity) == ("fail", "error") and "bogus" in res.summary
    res, _ = check("camera-snapshot", target(watch=bad))
    assert res.status == "skip" and "camera-config" in res.summary
    res, _ = check("camera-config", target(watch=_cam("http://127.0.0.1:1/snap")))
    assert res.status == "pass" and ev(res)["kind"] == "snapshot-url"


# ---- watch-units-dry-run --------------------------------------------------------------------


def _unit(args: str, state: str = "active") -> str:
    return (
        f"LoadState=loaded\nActiveState={state}\n"
        f"ExecStart={{ path=/usr/bin/3d ; argv[]=/usr/bin/3d {args} ; ignore_errors=no ; pid=0 }}\nEnvironmentFiles=\n"
    )


def _systemd(units: dict[str, str]) -> Show:
    return lambda unit: units.get(unit, "LoadState=not-found\nActiveState=inactive\n")


CAMERA = _cam("http://127.0.0.1:1/snap")


def test_watch_units_pass_when_both_run_for_real() -> None:
    show = _systemd(
        {
            "3d-printer-vision@p1.service": _unit("printer vision p1"),
            "3d-printer-watch@p1.service": _unit("printer watch p1"),
        }
    )
    res, _ = check("watch-units-dry-run", target(watch=CAMERA), systemctl_show=show)
    assert res.status == "pass"


def test_watch_units_fail_for_dry_run_with_the_fix_commands_and_never_execute_them() -> (
    None
):
    show = _systemd(
        {
            "3d-printer-vision@p1.service": _unit("printer vision p1 --dry-run"),
            "3d-printer-watch@p1.service": _unit("printer watch p1"),
        }
    )
    r = rig(target(watch=CAMERA), systemctl_show=show)
    rec = run_printer(
        r.ctx, [CHECKS["watch-units-dry-run"]], Options(fix=True, yes=True)
    ).records[0]
    assert (rec.result.status, rec.result.severity) == (
        "fail",
        "error",
    ) and "3d-printer-vision@p1.service" in rec.result.summary
    assert rec.fix is not None and not rec.fix.applied
    assert (
        "$ systemctl --user restart 3d-printer-vision@p1.service"
        in rec.fix.manual_steps
    )


def test_watch_units_fail_when_no_vision_service_runs_and_skip_without_systemctl_or_camera() -> (
    None
):
    show = _systemd({"3d-printer-watch@p1.service": _unit("printer watch p1")})
    res, _ = check("watch-units-dry-run", target(watch=CAMERA), systemctl_show=show)
    assert (res.status, res.severity) == (
        "fail",
        "warn",
    ) and "no running vision service" in res.summary
    res, _ = check(
        "watch-units-dry-run", target(watch=CAMERA), systemctl_show=lambda unit: ""
    )
    assert res.status == "skip" and "systemctl --user is not available" in res.summary
    res, _ = check("watch-units-dry-run", target(), systemctl_show=show)
    assert res.status == "skip" and "no camera" in res.summary


# ---- device-override-stale ------------------------------------------------------------------


def test_stale_override_is_reported_with_both_sides_and_the_winner() -> None:
    res, r = check(
        "device-override-stale",
        target(
            port=7125, override=Path("printer-devices.yaml"), override_host="10.9.9.9"
        ),
    )
    assert (res.status, res.severity) == ("fail", "warn")
    assert ev(res)["winner"] == "override"
    assert (
        ev(res)["fleet"]["host"] == "127.0.0.1"
        and ev(res)["effective"]["host"] == "10.9.9.9"
    )
    assert "replaces the fleet entry" in res.summary
    rec = run_printer(
        r.ctx, [CHECKS["device-override-stale"]], Options(fix=True, yes=True)
    ).records[0]
    assert (
        rec.fix is not None
        and not rec.fix.applied
        and any("printer-devices.yaml" in s for s in rec.fix.manual_steps)
    )


def test_an_override_that_repeats_the_fleet_entry_passes_and_no_override_is_not_applicable() -> (
    None
):
    res, _ = check(
        "device-override-stale", target(override=Path("printer-devices.yaml"))
    )
    assert res.status == "pass"
    res, _ = check("device-override-stale", target())
    assert res.status == "skip" and "no override" in res.summary
    res, _ = check(
        "device-override-stale",
        target(with_fleet=False, override=Path("printer-devices.yaml")),
    )
    assert res.status == "skip" and "no fleet entry" in res.summary
