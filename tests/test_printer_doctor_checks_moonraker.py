"""The Moonraker-family checks of `3d printers doctor` against loopback FakeMoonraker: reachable,
klippy-state (+ its needs-yes fix), moonraker-components, trusted-clients-width (pinned lists),
disk-space, time-sync, print-stuck, webui-presence. Each has a failing and a passing case."""

from __future__ import annotations

import json
from typing import Any

import pytest
from printer_doctor.checks.trusted_clients import classify
from printer_doctor.engine import Options, run_printer
from printer_doctor.model import CheckResult
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import ev, Rig, rig, target
from printer_fakes import (
    FLUIDD_HTML,
    FakeCreality,
    FakeMoonraker,
    FakeWebServer,
    free_port,
)

CHECKS = {c.id: c for c in discover_checks()}
U1_TRUSTED = [
    "172.18.0.0/16",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/8",
    "FE80::/10",
    "::1/128",
]
K1_TRUSTED = [
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "FE80::/10",
    "::1/128",
]


def check(
    cid: str, port: int, options: Options | None = None, **kw: Any
) -> tuple[CheckResult, Rig]:
    r = rig(target(port=port), **kw)
    rec = run_printer(r.ctx, [CHECKS[cid]], options or Options()).records[0]
    return rec.result, r


# ---- reachable ------------------------------------------------------------------------------


def test_reachable_passes_on_a_moonraker_fake_and_a_creality_fake() -> None:
    with FakeMoonraker().serve() as moon:
        res, _ = check("reachable", moon.port)
    assert (
        res.status == "pass"
        and ev(res)["adapter"] == "moonraker"
        and ev(res)["state"] == "idle"
    )
    with FakeCreality().serve() as k1:
        r = rig(
            target(adapter="creality-stock", port=k1.http_port, ws_port=k1.ws_port),
            listen_s=0.3,
        )
        rec = run_printer(r.ctx, [CHECKS["reachable"]], Options()).records[0]
    assert rec.result.status == "pass" and ev(rec.result)["adapter"] == "creality-stock"


def test_reachable_fails_with_error_severity_and_manual_steps_when_nothing_answers() -> (
    None
):
    res, r = check("reachable", free_port())
    assert (res.status, res.severity) == (
        "fail",
        "error",
    ) and "no answer" in res.summary
    rec = run_printer(
        r.ctx, [CHECKS["reachable"]], Options(fix=True, yes=True)
    ).records[0]
    assert (
        rec.fix is not None
        and not rec.fix.applied
        and any("ping -c 3 127.0.0.1" in s for s in rec.fix.manual_steps)
    )


def test_every_other_moonraker_check_skips_naming_reachable_when_the_host_is_down() -> (
    None
):
    r = rig(target(port=free_port()))
    ids = [
        "klippy-state",
        "moonraker-components",
        "trusted-clients-width",
        "disk-space",
        "time-sync",
        "print-stuck",
    ]
    report = run_printer(r.ctx, [CHECKS[i] for i in ids], Options())
    for rec in report.records:
        assert rec.result.status == "skip" and "reachable" in rec.result.summary, (
            rec.result
        )


# ---- klippy-state ---------------------------------------------------------------------------


def test_klippy_state_passes_when_ready_and_the_run_is_read_only() -> None:
    with FakeMoonraker().serve() as moon:
        res, r = check("klippy-state", moon.port)
    assert res.status == "pass" and moon.writes() == []
    assert all(m == "GET" for m, _ in r.ctx.requests)


def test_klippy_state_fails_for_shutdown_and_reads_only_without_fix() -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"}
    )
    with moon.serve():
        res, _ = check("klippy-state", moon.port)
    assert (res.status, res.severity) == ("fail", "error") and ev(res)[
        "klippy_state"
    ] == "shutdown"
    assert moon.writes() == []


def test_klippy_fix_without_yes_sends_nothing_and_with_yes_restarts_the_firmware_and_verifies() -> (
    None
):
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"}
    )
    with moon.serve():
        res, r = check("klippy-state", moon.port, Options(fix=True))
        assert res.status == "fail" and moon.writes() == []
        assert ev(res)["klippy_state"] == "shutdown"
        res, r = check("klippy-state", moon.port, Options(fix=True, yes=True))
    assert res.status == "pass" and [w.path for w in moon.writes()] == [
        "/printer/firmware_restart"
    ]
    assert r.journal[0].before == "shutdown" and r.journal[0].after == "ready"


def test_klippy_error_restarts_klipper_through_the_klippy_relay() -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "error"}
    )
    with moon.serve():
        res, _ = check("klippy-state", moon.port, Options(fix=True, yes=True))
    assert res.status == "pass" and [w.path for w in moon.writes()] == [
        "/printer/restart"
    ]


def test_klippy_disconnected_restarts_the_klipper_service_not_the_klippy_relay() -> (
    None
):
    """With Klippy disconnected `/printer/restart` cannot work (nothing to relay to): the fix
    restarts the host service through the machine endpoint, with the documented JSON body."""
    moon = FakeMoonraker(
        server_info={"klippy_connected": False, "klippy_state": "disconnected"},
        services={"klipper": True},
    )
    with moon.serve():
        res, r = check("klippy-state", moon.port, Options(fix=True, yes=True))
    assert res.status == "pass"
    (write,) = moon.writes()
    assert write.path == "/machine/services/restart" and json.loads(
        write.fields["body"]
    ) == {"service": "klipper"}
    assert (r.journal[0].before, r.journal[0].after) == ("disconnected", "ready")


def test_klippy_disconnected_without_a_service_provider_fails_without_journal_or_polling() -> (
    None
):
    """The restart POST is answered 400 (a Moonraker with provider `none`): that is `failed`, not
    `applied` — no journal line for a restart that never happened, and no 45 s wait."""
    moon = FakeMoonraker(
        server_info={"klippy_connected": False, "klippy_state": "disconnected"}
    )
    with moon.serve():
        r = rig(target(port=moon.port))
        start = r.clock.now
        rec = run_printer(
            r.ctx, [CHECKS["klippy-state"]], Options(fix=True, yes=True)
        ).records[0]
    assert rec.fix is not None and not rec.fix.applied and rec.fix.verified is None
    assert rec.fix.error and rec.fix.error.startswith(
        "failed: POST /machine/services/restart: HTTP 400"
    )
    assert r.journal == [] and r.clock.now == start and len(moon.writes()) == 1
    assert rec.result.status == "fail"


@pytest.mark.parametrize("code", [401, 500, 503])
def test_a_restart_endpoint_error_is_a_failed_fix_not_an_applied_one(code: int) -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"},
        restart_error=code,
    )
    with moon.serve():
        r = rig(target(port=moon.port))
        start = r.clock.now
        rec = run_printer(
            r.ctx, [CHECKS["klippy-state"]], Options(fix=True, yes=True)
        ).records[0]
    assert rec.fix is not None and rec.fix.error and f"HTTP {code}" in rec.fix.error
    assert not rec.fix.applied and r.journal == [] and r.clock.now == start


@pytest.mark.parametrize("job", ["printing", "paused"])
def test_klippy_fix_is_refused_while_a_job_runs_and_sends_nothing(job: str) -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"}
    )
    moon.status["print_stats"].update(state=job)
    with moon.serve():
        r = rig(target(port=moon.port))
        rec = run_printer(
            r.ctx, [CHECKS["klippy-state"]], Options(fix=True, yes=True)
        ).records[0]
    assert (
        rec.fix is not None
        and rec.fix.error
        and rec.fix.error.startswith("refused:")
        and job in rec.fix.error
    )
    assert not rec.fix.applied and moon.writes() == [] and r.journal == []
    assert any("FIRMWARE_RESTART" in s for s in rec.fix.manual_steps)


def test_klippy_startup_is_not_restarted() -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": False, "klippy_state": "startup"}
    )
    with moon.serve():
        r = rig(target(port=moon.port))
        rec = run_printer(
            r.ctx, [CHECKS["klippy-state"]], Options(fix=True, yes=True)
        ).records[0]
    assert (
        rec.fix is not None
        and rec.fix.error
        and "still starting" in rec.fix.error
        and moon.writes() == []
    )


def test_klippy_restart_that_does_not_recover_is_not_fixed_after_apply() -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"},
        restart_recovers=False,
    )
    with moon.serve():
        r = rig(target(port=moon.port))
        rec = run_printer(
            r.ctx, [CHECKS["klippy-state"]], Options(fix=True, yes=True)
        ).records[0]
    assert rec.fix is not None and rec.fix.applied and rec.fix.verified is False
    assert (
        rec.fix.error
        and rec.fix.error.startswith("not fixed after apply")
        and len(r.journal) == 1
    )
    assert len(moon.writes()) == 1  # bounded: one restart, then polling only


def test_klippy_state_is_not_applicable_to_a_creality_stock_printer() -> None:
    with FakeCreality().serve() as k1:
        r = rig(target(adapter="creality-stock", port=k1.http_port, ws_port=k1.ws_port))
        rec = run_printer(r.ctx, [CHECKS["klippy-state"]], Options()).records[0]
    assert rec.result.status == "skip" and "not Moonraker" in rec.result.summary
    assert k1.http_requests == []


# ---- moonraker-components -------------------------------------------------------------------


def test_components_pass_when_clean_and_warn_naming_the_failed_one() -> None:
    with FakeMoonraker().serve() as moon:
        res, _ = check("moonraker-components", moon.port)
    assert res.status == "pass"
    moon = FakeMoonraker(
        failed_components=["spoolman"],
        server_warnings=["[authorization] cors_domains: odd"],
    )
    with moon.serve():
        res, _ = check("moonraker-components", moon.port)
    assert (res.status, res.severity) == ("fail", "warn") and "spoolman" in res.summary
    assert (
        ev(res)["failed_components"] == ["spoolman"] and len(ev(res)["warnings"]) == 1
    )


# ---- trusted-clients-width ------------------------------------------------------------------


def _flagged(entries: list[str]) -> set[str]:
    return {e for e in entries if classify(e)[0] == "flag"}


def test_trusted_clients_flagged_set_is_pinned_for_the_real_u1_and_k1_lists() -> None:
    assert _flagged(U1_TRUSTED) == {"10.0.0.0/8", "172.16.0.0/12", "192.0.0.0/8"}
    assert _flagged(K1_TRUSTED) == {"10.0.0.0/8", "172.16.0.0/12"}


@pytest.mark.parametrize(
    ("entry", "verdict"),
    [
        ("192.0.0.0/8", "flag"),
        ("10.0.0.0/8", "flag"),
        ("0.0.0.0/0", "flag"),
        ("8.8.8.0/24", "flag"),
        ("2000::/3", "flag"),
        ("::/0", "flag"),
        ("192.168.1.0/24", "ok"),
        ("192.168.0.0/16", "ok"),
        ("172.18.0.0/16", "ok"),
        ("192.168.1.7", "ok"),
        ("127.0.0.0/8", "ok"),
        ("::1/128", "ok"),
        ("FE80::/10", "ok"),
        ("169.254.0.0/16", "ok"),
        ("fd00::/8", "ok"),
        ("printer.local", "unchecked"),
    ],
)
def test_trusted_client_classification(entry: str, verdict: str) -> None:
    assert classify(entry)[0] == verdict


def test_trusted_clients_check_flags_the_u1_list_and_passes_a_lan_list() -> None:
    moon = FakeMoonraker(
        config={
            "authorization": {
                "trusted_clients": U1_TRUSTED,
                "cors_domains": [],
                "force_logins": False,
            }
        }
    )
    with moon.serve():
        res, r = check("trusted-clients-width", moon.port, Options(fix=True, yes=True))
    assert (res.status, res.severity) == ("fail", "warn")
    assert {f["entry"] for f in ev(res)["flagged"]} == {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.0.0.0/8",
    }
    assert moon.writes() == []  # manual-only: never executed
    moon = FakeMoonraker(
        config={
            "authorization": {
                "trusted_clients": ["192.168.1.0/24", "127.0.0.0/8"],
                "cors_domains": ["http://192.168.1.5:4408"],
            }
        }
    )
    with moon.serve():
        res, _ = check("trusted-clients-width", moon.port)
    assert res.status == "pass"


def test_trusted_clients_flags_a_wildcard_cors_domain_and_passes_without_authorization() -> (
    None
):
    moon = FakeMoonraker(
        config={
            "authorization": {
                "trusted_clients": ["192.168.1.0/24"],
                "cors_domains": ["*"],
            }
        }
    )
    with moon.serve():
        res, _ = check("trusted-clients-width", moon.port)
    assert res.status == "fail" and "cors_domains" in res.summary
    moon = FakeMoonraker(config={"server": {}})
    with moon.serve():
        res, _ = check("trusted-clients-width", moon.port)
    assert res.status == "pass"
    with FakeMoonraker().serve() as moon:  # no /server/config at all
        res, _ = check("trusted-clients-width", moon.port)
    assert res.status == "skip" and "404" in res.summary


# ---- disk-space -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "status", "severity"),
    [
        (
            {"free": 23962226688, "total": 27950469120, "used": 3988242432},
            "pass",
            "error",
        ),  # real U1
        (
            {"total": 6945079296, "used": 1962131456, "free": 4630155264},
            "pass",
            "error",
        ),  # real K1 SE
        (
            {"total": 100_000_000_000, "used": 96_000_000_000, "free": 4_000_000_000},
            "fail",
            "warn",
        ),  # < 5 %
        (
            {"total": 6_000_000_000, "used": 5_850_000_000, "free": 150_000_000},
            "fail",
            "warn",
        ),  # < 200 MB
        (
            {"total": 6_000_000_000, "used": 5_990_000_000, "free": 10_000_000},
            "fail",
            "error",
        ),  # < 50 MB
    ],
)
def test_disk_space_thresholds(
    usage: dict[str, int], status: str, severity: str
) -> None:
    moon = FakeMoonraker(disk_usage=usage)
    with moon.serve():
        res, _ = check("disk-space", moon.port)
    assert res.status == status and res.severity == severity, res.summary
    assert ev(res)["free_bytes"] == usage["free"]


def test_disk_space_skips_when_the_gcodes_root_reports_no_usage() -> None:
    with FakeMoonraker().serve() as moon:
        res, _ = check("disk-space", moon.port)
    assert res.status == "skip"


# ---- time-sync ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "status", "severity"),
    [
        (0, "pass", "error"),
        (45, "pass", "error"),
        (300, "fail", "warn"),
        (-7200, "fail", "error"),
    ],
)
def test_time_sync_thresholds(offset: float, status: str, severity: str) -> None:
    moon = FakeMoonraker(date_offset_s=offset)
    with moon.serve():
        res, _ = check("time-sync", moon.port, real_clock=True)
    assert res.status == status and res.severity == severity, res.summary
    assert {"printer_time", "local_time", "skew_s"} <= set(ev(res))
    if status == "fail":
        assert abs(float(ev(res)["skew_s"]) - offset) < 5


# ---- print-stuck ----------------------------------------------------------------------------


def _stats(state: str, extruding: float, total: float) -> dict[str, Any]:
    return {
        "state": state,
        "filename": "a.gcode",
        "print_duration": extruding,
        "total_duration": total,
    }


@pytest.mark.parametrize(
    ("stats", "status"),
    [
        (_stats("printing", 100, 5 * 3600), "pass"),
        (_stats("standby", 0, 0), "pass"),
        (_stats("paused", 1000, 1000 + 1800), "pass"),
        (_stats("paused", 1000, 1000 + 3 * 3600), "fail"),
    ],
)
def test_print_stuck_flags_only_a_long_pause(
    stats: dict[str, Any], status: str
) -> None:
    moon = FakeMoonraker()
    moon.status["print_stats"].update(stats)
    with moon.serve():
        res, _ = check("print-stuck", moon.port)
    assert res.status == status, res.summary
    if status == "fail":
        assert res.severity == "warn" and "3.0 h" in res.summary


# ---- webui-presence -------------------------------------------------------------------------


def test_webui_presence_passes_with_fluidd_and_is_info_without_any_ui() -> None:
    from printer_ops.webui import probe

    fluidd = FakeWebServer(FLUIDD_HTML)
    with FakeMoonraker().serve() as moon, fluidd.serve():
        res, _ = check(
            "webui-presence", moon.port, webui=lambda d: probe(d, ports=(fluidd.port,))
        )
        assert res.status == "pass" and ev(res)["origins"] == [
            f"http://127.0.0.1:{fluidd.port}"
        ]
        res, _ = check(
            "webui-presence", moon.port, webui=lambda d: probe(d, ports=(free_port(),))
        )
    assert (res.status, res.severity) == (
        "fail",
        "info",
    ) and "no Fluidd or Mainsail" in res.summary
