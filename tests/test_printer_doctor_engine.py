"""The doctor engine: fix risk classes, verification, idempotency, offline safety, discovery, journal,
JSON schema. Synthetic checks only; the real checks have their own test files."""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from printer_doctor import checks as checks_pkg
from printer_doctor import render
from printer_doctor.context import Context
from printer_doctor.engine import NEVER_CONTACTED, Options, run_printer, summarize
from printer_doctor.journal import JournalEntry, build_line, record
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixChange,
    FixRefused,
    Risk,
    Severity,
    Verdict,
)
from printer_doctor.registry import discover_checks
from printops.client import ApiClient, Response
from printer_doctor_fakes import FakeApi, rig, target


def _boom(_ctx: Context) -> Verdict:
    raise ZeroDivisionError("division by zero")


class DownTransport:
    """A 3d-api that is not there: every request fails at the socket."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        raise OSError("connection refused")

    def stream_lines(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        raise OSError("connection refused")


class Switch:
    """A thing a check inspects and a fix flips; counts applies so idempotency is provable."""

    def __init__(self, broken: bool = True, heals: bool = True) -> None:
        self.broken, self.heals, self.applied = broken, heals, 0

    def check(self, risk: Risk, *, refuse: str | None = None) -> Check:
        def run(_ctx: Context) -> Verdict:
            return (
                Verdict.fail("it is broken", "warn", broken=True)
                if self.broken
                else Verdict.ok("fine")
            )

        def apply(_ctx: Context, _result: CheckResult) -> FixChange:
            if refuse:
                raise FixRefused(refuse)
            self.applied += 1
            before = self.broken
            self.broken = self.broken and not self.heals
            return FixChange(before, self.broken, "flipped")

        return Check(
            "switch",
            "a switch",
            "warn",
            "everything",
            run,
            fix=Fix(
                risk,
                "flip it",
                lambda _c, _r: ["flip it by hand"],
                None if risk == "manual-only" else apply,
            ),
        )


def _run(sw: Switch, risk: Risk, options: Options, **kw: Any):  # noqa: ANN202
    r = rig(target(), **kw)
    report = run_printer(r.ctx, [sw.check(risk)], options)
    return report.records[0], r


def test_read_only_run_never_applies_and_still_describes_the_fix() -> None:
    sw = Switch()
    rec, r = _run(sw, "safe", Options())
    assert rec.result.status == "fail" and sw.applied == 0
    assert rec.fix is not None and rec.fix.applied is False and rec.fix.error is None
    assert rec.fix.manual_steps == ["flip it by hand"] and r.journal == []


def test_safe_fix_applies_with_fix_alone_verifies_and_journals() -> None:
    sw = Switch()
    rec, r = _run(sw, "safe", Options(fix=True))
    assert sw.applied == 1 and rec.result.status == "pass"
    assert (
        rec.fix is not None
        and rec.fix.applied
        and rec.fix.verified is True
        and rec.fix.error is None
    )
    assert rec.fix.journal == "buffered"
    (entry,) = r.journal
    assert (entry.check, entry.risk, entry.before, entry.after) == (
        "switch",
        "safe",
        True,
        False,
    )


def test_needs_yes_fix_is_not_executed_without_yes_and_is_with_it() -> None:
    sw = Switch()
    rec, r = _run(sw, "needs-yes", Options(fix=True))
    assert sw.applied == 0 and r.journal == []
    assert (
        rec.fix is not None
        and not rec.fix.applied
        and rec.fix.error
        and "--yes" in rec.fix.error
    )
    rec, _ = _run(sw, "needs-yes", Options(fix=True, yes=True))
    assert sw.applied == 1 and rec.fix is not None and rec.fix.verified is True


def test_manual_only_fix_is_never_executed_even_with_fix_and_yes() -> None:
    sw = Switch()
    rec, r = _run(sw, "manual-only", Options(fix=True, yes=True))
    assert sw.applied == 0 and r.journal == []
    assert rec.result.status == "fail" and rec.fix is not None
    assert rec.fix.applied is False and rec.fix.manual_steps == ["flip it by hand"]


def test_a_fix_that_does_not_heal_is_reported_not_fixed_after_apply() -> None:
    sw = Switch(heals=False)
    rec, r = _run(sw, "safe", Options(fix=True))
    assert sw.applied == 1 and rec.result.status == "fail"
    assert rec.fix is not None and rec.fix.applied and rec.fix.verified is False
    assert rec.fix.error and rec.fix.error.startswith("not fixed after apply")
    assert len(r.journal) == 1  # applied, so journalled whatever verification said


def test_a_refused_fix_reports_the_reason_and_journals_nothing() -> None:
    sw = Switch()
    r = rig(target())
    report = run_printer(
        r.ctx, [sw.check("safe", refuse="printer is printing")], Options(fix=True)
    )
    fix = report.records[0].fix
    assert (
        fix is not None
        and not fix.applied
        and fix.error == "refused: printer is printing"
    )
    assert sw.applied == 0 and r.journal == []


def test_second_fix_run_is_a_noop() -> None:
    sw = Switch()
    _run(sw, "safe", Options(fix=True))
    rec, r = _run(sw, "safe", Options(fix=True))
    assert (
        sw.applied == 1
        and rec.result.status == "pass"
        and rec.fix is None
        and r.journal == []
    )


def test_offline_printer_is_never_contacted() -> None:
    contacted: list[str] = []

    def run(ctx: Context) -> Verdict:
        contacted.append(ctx.name)
        return Verdict.ok("x")

    net = Check("net", "t", "warn", "x", run)
    local = Check(
        "local",
        "t",
        "warn",
        "x",
        lambda _c: Verdict.ok("config only"),
        contacts_printer=False,
    )
    report = run_printer(rig(target(access="offline")).ctx, [net, local], Options())
    by = {rec.result.check: rec.result for rec in report.records}
    assert (
        contacted == []
        and by["net"].status == "skip"
        and by["net"].summary == NEVER_CONTACTED
    )
    assert by["local"].status == "pass"


def test_applies_reason_becomes_a_skip_and_cannot_see_becomes_skip_or_error() -> None:
    cs = [
        Check(
            "na",
            "t",
            "warn",
            "x",
            lambda _c: Verdict.ok("x"),
            applies=lambda _c: "not for this printer",
        ),
        Check(
            "blind",
            "t",
            "warn",
            "x",
            lambda _c: (_ for _ in ()).throw(CannotSee("tool missing")),
        ),
        Check(
            "broken",
            "t",
            "warn",
            "x",
            lambda _c: (_ for _ in ()).throw(CannotSee("HTTP 500", "error")),
        ),
        Check("crash", "t", "warn", "x", _boom),
    ]
    report = run_printer(rig(target()).ctx, cs, Options())
    got = {r.result.check: (r.result.status, r.result.summary) for r in report.records}
    assert got["na"] == ("skip", "not for this printer")
    assert got["blind"] == ("skip", "tool missing")
    assert got["broken"] == ("error", "HTTP 500")
    assert got["crash"][0] == "error" and "ZeroDivisionError" in got["crash"][1]


def test_check_and_skip_select_checks() -> None:
    a = Check("aa", "t", "warn", "x", lambda _c: Verdict.ok("a"))
    b = Check("bb", "t", "warn", "x", lambda _c: Verdict.ok("b"))
    rep = run_printer(rig(target()).ctx, [a, b], Options(only=frozenset({"aa"})))
    assert [r.result.check for r in rep.records] == ["aa"]
    rep = run_printer(rig(target()).ctx, [a, b], Options(skip=frozenset({"aa"})))
    assert [r.result.check for r in rep.records] == ["bb"]


@pytest.mark.parametrize(
    ("fail_on", "severity", "remaining"),
    [
        ("error", "error", 1),
        ("error", "warn", 0),
        ("warn", "warn", 1),
        ("warn", "info", 0),
        ("never", "error", 0),
    ],
)
def test_summary_remaining_follows_fail_on(
    fail_on: str, severity: Severity, remaining: int
) -> None:
    c = Check("c", "t", "error", "x", lambda _c: Verdict.fail("bad", severity))
    rep = run_printer(rig(target()).ctx, [c], Options())
    assert summarize([rep], fail_on).remaining == remaining


def test_an_errored_check_counts_as_an_error_severity_finding() -> None:
    c = Check("c", "t", "info", "x", _boom)
    rep = run_printer(rig(target()).ctx, [c], Options())
    assert (
        summarize([rep], "error").remaining == 1
        and summarize([rep], "never").remaining == 0
    )


# ---- construction rules ---------------------------------------------------------------------


def test_fix_rules_are_enforced_at_construction() -> None:
    def steps(_c: Context, _r: CheckResult) -> list[str]:
        return []

    with pytest.raises(ValueError, match="manual-only"):
        Fix("manual-only", "d", steps, lambda _c, _r: FixChange(1, 2))
    with pytest.raises(ValueError, match="needs an apply"):
        Fix("safe", "d", steps)
    with pytest.raises(ValueError, match="kebab"):
        Check("Not Kebab", "t", "warn", "x", lambda _c: Verdict.ok("x"))


# ---- discovery ------------------------------------------------------------------------------


def test_discover_finds_every_shipped_check_with_unique_ids_and_a_fix_contract() -> (
    None
):
    checks = discover_checks()
    ids = [c.id for c in checks]
    assert ids == sorted(set(ids)) and len(ids) >= 16
    for c in checks:
        assert c.title and c.applies_to and c.fix is not None, c.id


def test_dropping_a_module_adds_a_check_with_no_other_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "zz_custom.py").write_text(
        "from printer_doctor.model import Check, Verdict\n"
        "CHECKS = (Check('zz-custom', 'custom', 'info', 'all', lambda ctx: Verdict.ok('hi')),)\n"
    )
    monkeypatch.setattr(checks_pkg, "__path__", [*checks_pkg.__path__, str(tmp_path)])
    assert "zz-custom" in [c.id for c in discover_checks()]


def test_duplicate_check_ids_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dupe.py").write_text(
        "from printer_doctor.model import Check, Verdict\n"
        "CHECKS = (Check('reachable', 'again', 'info', 'all', lambda ctx: Verdict.ok('hi')),)\n"
    )
    monkeypatch.setattr(checks_pkg, "__path__", [*checks_pkg.__path__, str(tmp_path)])
    with pytest.raises(ValueError, match="duplicate check id 'reachable'"):
        discover_checks()


def test_a_non_check_in_checks_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bogus.py").write_text("CHECKS = ('nope',)\n")
    monkeypatch.setattr(checks_pkg, "__path__", [*checks_pkg.__path__, str(tmp_path)])
    with pytest.raises(TypeError, match="not a Check"):
        discover_checks()


# ---- journal --------------------------------------------------------------------------------


NOW = datetime.datetime(2026, 10, 23, 12, 0, tzinfo=datetime.timezone.utc)


def test_journal_line_has_the_settings_change_shape() -> None:
    line = build_line(
        JournalEntry("p1", "klippy-state", "needs-yes", "shutdown", "ready", "POST /x"),
        NOW,
    )
    assert line["schema"] == "printops.settings_change/1" and line["printer"] == "p1"
    assert (line["old"], line["new"], line["status"]) == (
        "shutdown",
        "ready",
        "applied",
    )
    assert line["actor"] == "3d printers doctor" and line["key"] == "klippy-state"
    assert line["evidence"] == [
        {"check": "klippy-state", "risk": "needs-yes", "note": "POST /x"}
    ]


def test_journal_is_buffered_offline_and_the_line_is_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printops import history_buffer as hb

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    outcome = record(
        JournalEntry("p1", "active-spool-binding", "safe", 3, 5), None, NOW
    )
    assert outcome == "buffered"
    path = tmp_path / "data" / "3d-cli" / "printer-history" / "p1.jsonl"
    (parsed,) = hb.parse_lines(
        path.read_text().splitlines()
    )  # the importer's own parser accepts it
    assert parsed.data["old"] == 3 and parsed.data["new"] == 5
    record(JournalEntry("p1", "active-spool-binding", "safe", 5, 3), None, NOW)
    assert len(path.read_text().splitlines()) == 2


def test_journal_is_imported_when_3d_api_answers_and_buffered_when_it_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    api = FakeApi()
    entry = JournalEntry("p1", "klippy-state", "needs-yes", "shutdown", "ready")
    assert record(entry, api.client(), NOW) == "imported" and len(api.imported) == 1
    api.import_refuses = True
    assert record(entry, api.client(), NOW) == "buffered"
    assert (
        record(entry, ApiClient("http://down", "t", DownTransport()), NOW) == "buffered"
    )


def test_journal_never_raises_for_an_unusable_printer_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert record(JournalEntry("../x", "c", "safe", 1, 2), None, NOW).startswith(
        "failed:"
    )


# ---- JSON schema ----------------------------------------------------------------------------


def test_json_report_has_the_documented_schema() -> None:
    sw = Switch()
    r = rig(target())
    rep = run_printer(r.ctx, [sw.check("safe")], Options())
    doc: dict[str, Any] = json.loads(
        json.dumps(render.to_json([rep], summarize([rep], "error"), NOW))
    )
    assert (
        doc["schema"] == "printers.doctor/1"
        and doc["generated_at"] == "2026-10-23T12:00:00Z"
    )
    (printer,) = doc["printers"]
    assert set(printer) == {"name", "access", "adapter", "results"}
    (res,) = printer["results"]
    assert set(res) == {"check", "status", "severity", "summary", "evidence", "fix"}
    assert set(res["fix"]) == {
        "risk",
        "description",
        "manual_steps",
        "applied",
        "verified",
        "error",
        "journal",
    }
    assert doc["summary"] == {
        "pass": 0,
        "fail": 1,
        "skip": 0,
        "error": 0,
        "fixed": 0,
        "remaining": 0,
    }


def test_text_report_names_the_fix_outcome() -> None:
    sw = Switch()
    for options, needle in [
        (Options(), "run with --fix to apply it"),
        (Options(fix=True), "fixed, verified"),
    ]:
        rep = run_printer(rig(target()).ctx, [Switch().check("safe")], options)
        assert needle in render.to_text([rep], summarize([rep], "error"))
    rep = run_printer(rig(target()).ctx, [sw.check("needs-yes")], Options())
    assert "run with --fix --yes to apply it" in render.to_text(
        [rep], summarize([rep], "error")
    )
