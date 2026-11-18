"""The glue-stick reminder (printer_ops/glue.py, issue #762): due after max_days OR max_prints since the last
application, whichever comes first, and while none is on record; prints from the printer's Moonraker history
(once per job_id), else 3d's own start log. One record per printer.

Deterministic: a fixed clock, job lists in memory or the loopback FakeMoonraker, temp XDG dirs. No printer.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any

import pytest

from errors import UsageError
from printer_fakes import FakeMoonraker, u1_status
from printer_ops import glue as G
from printer_ops import jobwatch as J
from printer_ops import jobwatch_actions as A
from printer_ops import print_log
from printer_ops.moonraker import MoonrakerAdapter

NOW = 1_800_000_000.0
DAY = 86400.0
LIMITS = G.Limits(5, 7.0, "default")


@pytest.fixture(autouse=True)
def _xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


_ids = iter(range(1, 10**6))


def job(
    start: float,
    status: str = "completed",
    duration: float = 600.0,
    filament: float = 900.0,
    job_id: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id or f"{next(_ids):06X}",
        "start_time": start,
        "end_time": start + duration,
        "status": status,
        "print_duration": duration,
        "filament_used": filament,
    }


def history(jobs: list[dict[str, Any]]) -> G.HistoryFor:
    """A Moonraker history in memory: the jobs started at or after `since`, like MoonrakerHistory.jobs_since."""
    return lambda printer: (
        lambda since: [j for j in jobs if j["start_time"] >= since],
        "moonraker fake",
    )


def unreachable(printer: str) -> G.History:
    def jobs_since(since: float) -> list[dict[str, Any]]:
        raise ConnectionRefusedError(111, "Connection refused")

    return jobs_since, "moonraker http://192.0.2.1:7125/server/history"


# -- the rule


def test_time_limit_fires_first_while_the_print_count_is_below_its_limit() -> None:
    assert G.fired_limits(NOW - 8 * DAY, NOW, 2, LIMITS) == ("days",)


def test_print_limit_fires_first_while_the_time_is_below_its_limit() -> None:
    assert G.fired_limits(NOW - 1 * DAY, NOW, 5, LIMITS) == ("prints",)


def test_both_limits_reported_when_both_are_reached() -> None:
    assert G.fired_limits(NOW - 9 * DAY, NOW, 7, LIMITS) == ("days", "prints")


def test_limits_are_inclusive_at_the_boundary_and_quiet_just_below() -> None:
    assert G.fired_limits(NOW - 7 * DAY, NOW, 4, LIMITS) == ("days",)
    assert G.fired_limits(NOW - 7 * DAY + 1, NOW, 4, LIMITS) == ()
    assert G.fired_limits(NOW - DAY, NOW, 5, LIMITS) == ("prints",)


def test_no_application_on_record_is_due() -> None:
    assert G.fired_limits(None, NOW, 0, LIMITS) == ("never",)


def test_only_jobs_that_printed_count() -> None:
    assert G.printed(job(0, "completed"))
    assert G.printed(
        job(0, "cancelled", duration=20.0, filament=16.5)
    )  # a few lines on the plate
    assert G.printed(job(0, "error", duration=0.0, filament=3.0))
    assert G.printed(
        {"status": "in_progress", "print_duration": 0.0}
    )  # printing on the glue right now
    assert not G.printed(
        job(0, "interrupted", duration=0.0, filament=0.0)
    )  # never reached the plate
    assert not G.printed(job(0, "cancelled", duration=0.0, filament=0.0))


def test_a_history_record_seen_twice_counts_once_but_a_reprint_is_a_new_print() -> None:
    first = job(NOW - 3600, job_id="000070")
    reprint = job(
        NOW - 600, job_id="000071"
    )  # same file again: a new job id, a new print
    assert G.count_printed([first, dict(first), reprint]) == 2


# -- counting


def test_prints_come_from_the_printer_history_after_the_glue_only() -> None:
    jobs = [
        job(NOW - 3 * DAY),
        job(NOW - 2 * DAY),
        job(NOW - DAY, "cancelled"),
        job(NOW - 3600, "interrupted", 0.0, 0.0),
    ]
    count = G.count_prints("k1se", NOW - 2.5 * DAY, history(jobs)("k1se"))
    assert (count.count, count.exact, count.source) == (2, True, "moonraker fake")


def test_unreachable_moonraker_falls_back_to_the_start_log_as_a_lower_bound() -> None:
    print_log.record_start("k1se", "old.gcode", at=NOW - 3 * DAY)
    print_log.record_start("k1se", "a.gcode", at=NOW - DAY)
    print_log.record_start("k1se", "b.gcode", at=NOW - 60)
    count = G.count_prints("k1se", NOW - 2 * DAY, unreachable("k1se"))
    assert (count.count, count.exact) == (2, False)
    assert (
        "start log" in count.source
        and "unreachable: ConnectionRefusedError" in count.note
    )


def test_a_printer_without_moonraker_counts_from_the_start_log() -> None:
    print_log.record_start("k1", "a.gcode", at=NOW - 10)
    count = G.count_prints("k1", NOW - DAY, (None, ""))
    assert (count.count, count.exact, count.note) == (
        1,
        False,
        "the printer has no Moonraker job history",
    )


def test_start_log_skips_broken_lines() -> None:
    path = print_log.log_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"at": 5}\nnot json\n{"at": "x"}\n[1]\n{"at": 0}\n{"at": 1e999}\n{"at": NaN}\n'
        + '{"at": 1'
        + "0" * 400
        + "}\n",
        encoding="utf-8",
    )
    assert print_log.starts_since("k1se", 0.0) == 2


def test_a_corrupted_utf8_line_skips_only_itself() -> None:
    """Issue #768: invalid UTF-8 in one line must not drop the valid starts before and after it."""
    path = print_log.log_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b'{"at": 1}\n{"at": 2, "remote": "\xff\xfe"}\n\x80\x81\n{"at": 3}\n'
    )
    assert print_log.starts_since("k1se", 0.0) == 2


def test_a_start_after_a_partial_log_line_is_still_counted() -> None:
    """A failed write can leave an unterminated line; the next start must not be glued onto it."""
    path = print_log.log_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_text('{"at": 1}\n{"at": 2', encoding="utf-8")
    print_log.record_start("k1se", "a.gcode", at=3)
    assert print_log.starts_since("k1se", 0.0) == 2


# -- records and the verdict


def test_a_printer_with_no_glue_ever_recorded_is_due_never_by_default() -> None:
    v = G.evaluate("u1", now=NOW, history_for=history([]))
    assert v is not None and v.fired == ("never",) and v.limits == LIMITS
    assert G.warning_lines(v) == ["GLUE DUE on u1: no glue application on record"]


def test_only_explicit_config_turns_the_reminder_off() -> None:
    G.configure("u1", on=False)
    assert G.evaluate("u1", now=NOW, history_for=history([])) is None
    G.configure("u1", on=True)
    assert G.evaluate("u1", now=NOW, history_for=history([])) is not None
    assert G.load_config() == {}  # on is the default: nothing left in glue.json


def test_configured_limits_apply_and_reset_goes_back_to_the_defaults() -> None:
    G.configure("k1se", max_prints=4)
    assert G.limits_for("k1se") == G.Limits(4, 7.0, str(G.config_path()))
    G.configure("k1se", max_days=2.5)
    assert (G.limits_for("k1se").max_prints, G.limits_for("k1se").max_days) == (4, 2.5)
    G.configure("k1se", reset=True)
    assert G.limits_for("k1se") == LIMITS


def test_count_first_verdict_names_the_limit_and_the_values() -> None:
    G.record_applied("k1se", NOW - DAY, now=NOW - DAY)
    jobs = [job(NOW - DAY + 600 * i) for i in range(5)]
    v = G.evaluate("k1se", now=NOW, history_for=history(jobs))
    assert v is not None and v.due and v.fired == ("prints",)
    assert G.reasons(v) == ["5 prints since the glue (limit 5)"]
    assert v.to_json()["confirm_command"] == "3d glue applied --printer k1se"


def test_time_first_verdict_names_the_limit_and_the_values() -> None:
    G.record_applied("k1se", NOW - 8 * DAY, now=NOW)
    v = G.evaluate("k1se", now=NOW, history_for=history([job(NOW - DAY)]))
    assert v is not None and v.fired == ("days",)
    assert G.reasons(v) == ["8d 00h since the glue (limit 7d)"]


def test_glue_applied_resets_the_time_and_the_count_together() -> None:
    jobs = [job(NOW - 9 * DAY + 60 * i) for i in range(6)]
    G.record_applied("k1se", NOW - 10 * DAY, now=NOW)
    before = G.evaluate("k1se", now=NOW, history_for=history(jobs))
    assert before is not None and before.fired == ("days", "prints")
    G.record_applied("k1se", NOW - 3600, now=NOW)
    after = G.evaluate("k1se", now=NOW, history_for=history(jobs))
    assert (
        after is not None
        and after.fired == ()
        and after.elapsed_s == 3600
        and after.prints is not None
    )
    assert after.prints.count == 0
    assert not list(
        G.state_path("k1se").parent.glob(".*.tmp")
    )  # the write-then-rename left nothing behind


def test_unreachable_moonraker_verdict_says_at_least() -> None:
    G.record_applied("k1se", NOW - DAY, now=NOW)
    for i in range(5):
        print_log.record_start("k1se", f"{i}.gcode", at=NOW - 3600 + i)
    v = G.evaluate("k1se", now=NOW, history_for=unreachable)
    assert v is not None and v.fired == ("prints",)
    assert G.reasons(v) == ["~5 prints since the glue (limit 5)"]
    assert any(
        "an estimate, starts 3d sent, not verified as printed" in line
        for line in G.warning_lines(v)
    )


def test_state_is_one_record_per_printer(tmp_path: Path) -> None:
    G.record_applied("k1se", NOW - 60, now=NOW)
    data = json.loads(
        (tmp_path / "state" / "3d-cli" / "glue" / "k1se.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(data) == {"applied_at", "recorded_at"}
    assert G.applied_at("k1se") == NOW - 60 and G.applied_at("u1") is None


def test_bad_limits_in_the_config_are_named(tmp_path: Path) -> None:
    path = tmp_path / "config" / "3d-cli" / "glue.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"k1se": {"max_prints": 0}}', encoding="utf-8")
    with pytest.raises(G.ThreeDError, match="max_prints must be a whole number"):
        G.limits_for("k1se")


def test_at_is_local_time_without_an_offset_and_never_in_the_future() -> None:
    assert G.parse_at("2026-11-17T23:35Z") == 1_794_958_500.0
    assert G.parse_at("2026-11-17T23:35+00:00") == G.parse_at("2026-11-17T23:35Z")
    with pytest.raises(UsageError, match="in the future"):
        G.parse_at("2026-11-18T00:35Z", now=1_794_958_500.0)
    with pytest.raises(UsageError, match="ISO date"):
        G.parse_at("yesterday")


@pytest.mark.parametrize(
    "raw", ["0001-01-01", "0001-01-01T00:00:00+00:00", "9999-12-31T23:59+00:00"]
)
def test_at_out_of_the_local_time_range_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("TZ", "Europe/Belgrade")
    time.tzset()
    try:
        with pytest.raises(UsageError, match="out of the range|in the future"):
            G.parse_at(raw)
    finally:
        monkeypatch.undo()
        time.tzset()


# -- the check before a print (it reads the real clock, like `3d printer start`)


def _due_k1se() -> None:
    G.record_applied("k1se", time.time() - 8 * DAY)


def test_check_without_a_terminal_warns_and_prints_the_confirm_command() -> None:
    _due_k1se()
    out: list[str] = []
    G.check_before_print(
        "k1se", interactive=False, echo=out.append, history_for=history([])
    )
    assert (
        out[0].startswith("GLUE DUE on k1se: ")
        and "since the glue (limit 7d)" in out[0]
    )
    assert out[-1] == "  Glue applied? Record it with: 3d glue applied --printer k1se"


def test_check_on_a_terminal_yes_records_the_glue_now() -> None:
    _due_k1se()
    out: list[str] = []
    asked: list[str] = []

    def yes(question: str) -> str:
        asked.append(question)
        return "y"

    G.check_before_print(
        "k1se", interactive=True, ask=yes, echo=out.append, history_for=history([])
    )
    assert asked == ["Glue applied? [y/N] "] and "glue recorded on k1se" in out[-1]
    after = G.evaluate("k1se", history_for=history([]))
    assert after is not None and not after.due


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_check_on_a_terminal_no_continues_with_the_warning(answer: str) -> None:
    _due_k1se()
    out: list[str] = []
    G.check_before_print(
        "k1se",
        interactive=True,
        ask=lambda q: answer,
        echo=out.append,
        history_for=history([]),
    )
    assert "continuing without fresh glue" in out[-1]
    assert G.evaluate("k1se", history_for=history([])).due  # type: ignore[union-attr]


def test_check_on_a_closed_stdin_is_a_no() -> None:
    _due_k1se()
    out: list[str] = []

    def eof(question: str) -> str:
        raise EOFError

    G.check_before_print(
        "k1se", interactive=True, ask=eof, echo=out.append, history_for=history([])
    )
    assert "continuing without fresh glue" in out[-1]


@pytest.mark.parametrize("stdin", ["none", "closed"])
def test_check_without_stdin_is_not_interactive(
    monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    import io
    import sys

    _due_k1se()
    stream = None
    if stdin == "closed":
        stream = io.StringIO()
        stream.close()
    monkeypatch.setattr(sys, "stdin", stream)
    out: list[str] = []
    G.check_before_print("k1se", echo=out.append, history_for=history([]))
    assert out[-1] == "  Glue applied? Record it with: 3d glue applied --printer k1se"


def test_check_when_not_due_prints_one_status_line() -> None:
    G.record_applied("k1se", time.time() - 3600)
    out: list[str] = []
    G.check_before_print(
        "k1se",
        interactive=False,
        echo=out.append,
        history_for=history([job(time.time() - 60)]),
    )
    assert len(out) == 1 and out[0].startswith("glue k1se: ok, 1 of 5 prints, ")


def test_check_never_breaks_a_print_on_a_broken_record() -> None:
    path = G.state_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    out: list[str] = []
    G.check_before_print(
        "k1se", interactive=False, echo=out.append, history_for=history([])
    )
    assert out and out[0].startswith("glue check skipped: ThreeDError")


def test_switched_off_printer_check_is_silent_and_never_asks_the_printer() -> None:
    def boom(printer: str) -> G.History:
        raise AssertionError("a printer with the reminder off must not be asked")

    G.configure("u1", on=False)
    out: list[str] = []
    G.check_before_print("u1", interactive=False, echo=out.append, history_for=boom)
    assert out == []


# -- Telegram


def test_tg_note_is_russian_with_the_limit_and_the_command_only_when_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(G.time, "time", lambda: NOW)  # tg_note reads the clock itself
    G.record_applied("k1se", NOW - DAY, now=NOW)
    assert G.tg_note("k1se", history_for=history([])) is None
    jobs = [job(NOW - DAY + 60 * i) for i in range(6)]
    note = G.tg_note("k1se", history_for=history(jobs))
    assert note is not None and note.startswith("Клей на столе k1se пора обновить:")
    assert "печатей с нанесения: 6 (лимит 5)" in note
    assert note.endswith("Клей нанесён — отметьте: 3d glue applied --printer k1se")
    assert G.tg_note("u1", history_for=history([])) == (
        "Клей на столе u1 пора обновить: нанесение клея не записано. "
        "Клей нанесён — отметьте: 3d glue applied --printer u1"
    )


def _watched(tmp_path: Path, sent: list[list[str]], **kw: Any) -> None:
    m = FakeMoonraker(status=u1_status("printing", "part.gcode", 0.3))
    m.history = [
        {
            "job_id": "000042",
            "filename": "part.gcode",
            "status": "in_progress",
            "start_time": 1.0,
        }
    ]

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def runner(argv: list[str], **_: Any) -> Any:
        sent.append(argv)
        return Done()

    with m.serve():
        adapter = MoonrakerAdapter("k1se", "127.0.0.1", m.port)
        A.Responder(
            adapter,
            A.bind(adapter),
            out_dir=tmp_path / "runs",
            runner=runner,
            sleep=lambda s: None,
            verify_s=0.0,
            echo=lambda s: None,
            evidence_fn=lambda p, j, d: "fake",
            **kw,
        ).respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=2,
            files={},
            extra={},
        )


def test_job_watch_message_carries_the_glue_line(tmp_path: Path) -> None:
    sent: list[list[str]] = []
    _watched(tmp_path, sent, glue_note=lambda printer: f"Клей на столе {printer}: пора")
    assert sent[-1][:3] == ["tg", "--tag", "problem"]
    assert "\nКлей на столе k1se: пора\nEvidence: " in sent[-1][-1]
    assert "Клей" not in sent[0][-1]  # the call stays short


def test_job_watch_counts_glue_prints_from_the_watched_printer(tmp_path: Path) -> None:
    """The default glue line reads the job history of the printer being watched (the fake), never the device
    registry (no device is configured here): the fake's one in-progress job, started after the glue, is an exact
    "1" (the fallback start log would say "~0")."""
    G.record_applied("k1se", 0.5)  # just before the fake job's start_time 1.0
    G.configure("k1se", max_prints=1)
    sent: list[list[str]] = []
    _watched(tmp_path, sent)
    assert "Клей на столе k1se пора обновить: с нанесения прошло" in sent[-1][-1]
    assert "печатей с нанесения: 1 (лимит 1)" in sent[-1][-1]


# -- review findings (#762): malformed or truncated history never reads as an exact count; bounded limits


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        ({"result": {}}, "unreachable: "),
        ({"result": {"jobs": None}}, "unreachable: "),
        ({"error": {"code": 503}}, "unreachable: "),
        ({"result": {"jobs": [{"job_id": "1"}]}}, "has no start time"),
    ],
)
def test_malformed_history_falls_back_to_the_start_log(
    monkeypatch: pytest.MonkeyPatch, reply: Any, why: str
) -> None:

    monkeypatch.setattr(G, "_get_json", lambda *a, **k: reply)
    print_log.record_start("k1se", "a.gcode", at=NOW - 60)
    count = G.count_prints(
        "k1se",
        NOW - DAY,
        G.adapter_history(type("M", (), {"kind": "moonraker", "base": "http://x"})()),
    )
    assert (count.count, count.exact) == (1, False) and why in count.note


def test_history_longer_than_the_page_cap_is_not_an_exact_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    rows = [
        job(NOW - 60, "cancelled", 0.0, 0.0)
        for _ in range(G.HISTORY_PAGE * G.HISTORY_MAX_PAGES + 1)
    ]
    monkeypatch.setattr(G, "_get_json", _moonraker_table(rows))
    count = G.count_prints(
        "k1se",
        NOW - DAY,
        G.adapter_history(type("M", (), {"kind": "moonraker", "base": "http://x"})()),
    )
    assert not count.exact and "longer than 10000 jobs" in count.note


def test_a_server_repeating_the_same_full_page_is_not_followed_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    page = [job(NOW - 60, "cancelled", 0.0, 0.0) for _ in range(G.HISTORY_PAGE)]
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": page}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "changed while it was read" in count.note


def test_limit_changes_of_two_printers_both_survive() -> None:
    G.configure("u1", on=False)
    G.configure("k1se", max_days=5)
    assert G.load_config() == {"u1": {"enabled": False}, "k1se": {"max_days": 5}}


def test_oversized_limits_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "config" / "3d-cli" / "glue.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"k1se": {"max_days": 1e308}}', encoding="utf-8")
    with pytest.raises(G.ThreeDError, match="max_days must be a number in"):
        G.limits_for("k1se")


def _moon() -> Any:
    return type("M", (), {"kind": "moonraker", "base": "http://x"})()


def test_a_job_without_print_metrics_is_unknown_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(
        G,
        "_get_json",
        lambda *a, **k: {"result": {"jobs": [{"job_id": "1", "start_time": NOW}]}},
    )
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "no print metrics" in count.note


def test_an_interrupted_job_recovered_without_metrics_is_unknown_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moonraker's restart recovery marks an in-progress job interrupted, keeping zero metrics and no end time."""

    lost = {
        "job_id": "1",
        "start_time": NOW,
        "status": "interrupted",
        "end_time": None,
        "print_duration": 0.0,
        "filament_used": 0.0,
    }
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": [lost]}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "interrupted before its metrics were saved" in count.note


def test_an_uncertain_job_keeps_the_confirmed_prints_as_the_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    done = [job(NOW - 3600 * (i + 1)) for i in range(5)]
    lost = {
        "job_id": "X",
        "start_time": NOW - 60,
        "status": "interrupted",
        "end_time": None,
        "print_duration": 0.0,
        "filament_used": 0.0,
    }
    monkeypatch.setattr(
        G, "_get_json", lambda *a, **k: {"result": {"jobs": [*done, lost]}}
    )
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "5 confirmed" in count.note


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "12"])
def test_invalid_print_metrics_are_unknown_not_exact(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:

    odd = {
        "job_id": "1",
        "start_time": NOW,
        "status": "completed",
        "print_duration": bad,
        "filament_used": None,
    }
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": [odd]}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "invalid print metrics" in count.note


def test_a_trickling_reply_is_cut_at_the_deadline_and_its_connection_closed() -> None:
    """The deadline bounds the whole request, not each read: a server sending a byte at a time, each well
    inside a socket timeout, still gets cut off, and its connection is closed."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    closed = threading.Event()

    def trickle() -> None:
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        try:
            for _ in range(200):
                conn.sendall(b" ")
                time.sleep(0.05)
        except OSError:
            closed.set()
        finally:
            conn.close()

    worker = threading.Thread(target=trickle, daemon=True)
    worker.start()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="within 0.3 s"):
        G._get_json("GET", f"http://127.0.0.1:{server.getsockname()[1]}/x", timeout=0.3)
    assert time.monotonic() - started < 2
    assert closed.wait(5)
    worker.join(5)
    server.close()


def test_a_history_read_cut_at_the_deadline_keeps_the_prints_already_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def first_page_then_deadline(
        *a: object, timeout: float, **k: object
    ) -> dict[str, Any]:
        timeouts.append(timeout)
        if len(timeouts) > 1:
            raise TimeoutError(f"no complete reply within {timeout:g} s")
        return {"result": {"jobs": [job(NOW - 60) for _ in range(G.HISTORY_PAGE)]}}

    monkeypatch.setattr(G, "_get_json", first_page_then_deadline)
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (G.HISTORY_PAGE, False)
    assert "TimeoutError" in count.note
    assert all(t <= G.HTTP_TIMEOUT_S for t in timeouts)


def test_a_retry_after_a_shift_that_fails_keeps_the_prints_seen_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prints = [job(NOW - 3600 + i) for i in range(5)]
    table = [*prints, *(job(NOW - 600 + i, "cancelled", 0.0, 0.0) for i in range(96))]
    serve = _moonraker_table(table)
    calls: list[int] = []

    def shift_then_reset(method: str, url: str, **k: object) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 2:
            table.remove(table[5])  # an idle job deleted between the two pages
        if len(calls) > 2:
            raise ConnectionResetError("reset")
        return serve(method, url, **k)

    monkeypatch.setattr(G, "_get_json", shift_then_reset)
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)


def test_a_positive_metric_counts_even_when_the_other_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    jobs = [{**job(NOW - 3600 * (i + 1)), "filament_used": "unknown"} for i in range(5)]
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": jobs}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, True)


def test_the_glue_baseline_keeps_fractional_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    G.record_applied("k1se", NOW + 0.75, now=NOW + 1)
    assert G.applied_at("k1se") == pytest.approx(NOW + 0.75, abs=1e-5)
    before, after = job(NOW + 0.25), job(NOW + 0.9)
    monkeypatch.setattr(
        G, "_get_json", lambda *a, **k: {"result": {"jobs": [before, after]}}
    )
    count = G.count_prints(
        "k1se", G.applied_at("k1se") or 0, G.adapter_history(_moon())
    )
    assert (count.count, count.exact) == (1, True)


@pytest.mark.parametrize("entry", [7, "weekly", [[1, 2]]])
def test_a_malformed_config_entry_is_a_clear_error(entry: object) -> None:
    G.config_path().parent.mkdir(parents=True, exist_ok=True)
    G.config_path().write_text(json.dumps({"k1se": entry}), encoding="utf-8")
    with pytest.raises(G.ThreeDError, match="k1se must be an object"):
        G.limits_for("k1se")
    with pytest.raises(G.ThreeDError, match="k1se must be an object"):
        G.configure("k1se", max_days=4)
    G.configure("k1se", reset=True)  # --reset repairs it
    assert G.limits_for("k1se").source == "default"


def _moonraker_table(table: list[dict[str, Any]], after_reply: Any = None) -> Any:
    """A /server/history/list double like Moonraker's: start_time > since, job_id order, offset pages."""
    import urllib.parse

    def server(_method: str, url: str, **_k: object) -> dict[str, Any]:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        since, limit = float(q["since"][0]), int(q["limit"][0])
        start = int(q.get("start", ["0"])[0])
        rows = sorted(
            (j for j in table if j["start_time"] > since), key=lambda j: j["job_id"]
        )
        page = rows[start : start + limit]
        if after_reply is not None:
            after_reply()
        return {"result": {"jobs": page}}

    return server


def test_a_job_deleted_between_two_pages_does_not_shift_a_print_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moonraker pages by offset: deleting an early job between two requests is noticed and read again."""

    idle = [job(NOW - 7200 + i, "cancelled", 0.0, 0.0) for i in range(96)]
    table = [*idle, *(job(NOW - 3600 + i) for i in range(5))]

    def delete_once() -> None:
        if idle[0] in table:
            table.remove(idle[0])

    monkeypatch.setattr(G, "_get_json", _moonraker_table(table, delete_once))
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, True)


def test_a_print_deleted_between_two_pages_still_counts_but_not_as_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a shift keeps the prints the first attempt saw: they were printed since the glue."""

    prints = [job(NOW - 7200 + i) for i in range(5)]
    table = [*prints, *(job(NOW - 3600 + i, "cancelled", 0.0, 0.0) for i in range(96))]

    def delete_once() -> None:
        if prints[0] in table:
            table.remove(prints[0])

    monkeypatch.setattr(G, "_get_json", _moonraker_table(table, delete_once))
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "deleted" in count.note


def test_slow_headers_are_cut_at_the_deadline() -> None:
    """A server dripping header bytes keeps every socket read alive; the overall deadline still holds."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def drip_headers() -> None:
        conn, _ = server.accept()
        conn.recv(4096)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            for _ in range(100):
                conn.sendall(b"X: y\r\n")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=drip_headers, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="within 0.3 s"):
        G._get_json("GET", f"http://127.0.0.1:{server.getsockname()[1]}/x", timeout=0.3)
    assert time.monotonic() - started < 1
    server.close()


@pytest.mark.parametrize("name", ["/x/3d-cli/web", "..", ".", "a/b", ""])
def test_printer_names_that_are_paths_are_refused(name: str) -> None:
    with pytest.raises(UsageError, match="plain printer name"):
        G.state_path(name)
    with pytest.raises(UsageError, match="plain printer name"):
        G.configure(name, max_days=4)


def test_path_keys_in_glue_json_are_not_printers() -> None:
    G.config_path().parent.mkdir(parents=True, exist_ok=True)
    G.config_path().write_text(json.dumps({"/x/3d-cli/web": {}, "..": {}, "k1se": {}}))
    names = G.known_printers()
    assert "k1se" in names
    assert "/x/3d-cli/web" not in names and ".." not in names


def test_a_reused_job_id_after_a_tail_deletion_is_a_new_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moonraker can give a new print the id of a deleted tail job: id plus start time tells them apart."""
    idle = [job(NOW - 7200 + i, "cancelled", 0.0, 0.0) for i in range(99)]
    first = job(NOW - 3600)
    table = [*idle, first, job(NOW - 3000)]

    def delete_tail_and_reuse_the_id() -> None:
        if first in table:
            del table[-2:]
            table.append(job(NOW - 60, job_id=first["job_id"]))

    monkeypatch.setattr(
        G, "_get_json", _moonraker_table(table, delete_tail_and_reuse_the_id)
    )
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (2, False)


def test_the_overlap_row_is_read_again_with_its_newer_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job printing at the end of page one that cancels before extruding is not a print on page two."""
    done = [job(NOW - 7200 + i) for i in range(3)]
    idle = [job(NOW - 3600 + i, "cancelled", 0.0, 0.0) for i in range(96)]
    active = job(NOW - 120, "in_progress", 0.0, 0.0)
    table = [*done, *idle, active]

    def cancel_and_start_another() -> None:
        if active in table:
            table[table.index(active)] = {**active, "status": "cancelled"}
            table.append(job(NOW - 30, "in_progress", 0.0, 0.0))

    monkeypatch.setattr(
        G, "_get_json", _moonraker_table(table, cancel_and_start_another)
    )
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (4, True)


def test_prints_on_a_shifted_page_are_kept_when_the_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idle = [job(NOW - 7200 + i, "cancelled", 0.0, 0.0) for i in range(100)]
    table = [*idle, *(job(NOW - 3600 + i) for i in range(5))]
    serve = _moonraker_table(table)
    calls: list[int] = []

    def shift_then_reset(method: str, url: str, **k: object) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 2:
            table.remove(idle[0])  # shifts page two: it starts after the overlap row
        if len(calls) > 2:
            raise ConnectionResetError("reset")
        return serve(method, url, **k)

    monkeypatch.setattr(G, "_get_json", shift_then_reset)
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)


def test_prints_after_a_clock_rollback_are_still_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moonraker orders by job_id, not start time: later jobs with an earlier clock are on later pages."""

    idle = [job(NOW - 600 + i, "cancelled", 0.0, 0.0) for i in range(100)]
    table = [
        *idle,
        *(job(NOW - 3600 + i) for i in range(5)),
    ]  # the clock went back an hour
    monkeypatch.setattr(G, "_get_json", _moonraker_table(table))
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, True)


def test_an_unreadable_start_log_keeps_the_confirmed_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = print_log.log_path("k1se")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"at": 1}\n', encoding="utf-8")
    path.chmod(0)
    try:
        jobs = [job(NOW - 3600 * (i + 1)) for i in range(5)] + [{"job_id": "X"}]

        monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": jobs}})
        count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    finally:
        path.chmod(0o600)
    assert (count.count, count.exact) == (5, False)
    assert "start log unreadable" in count.note


def test_an_oversized_integer_metric_is_invalid_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    huge = {**job(NOW), "print_duration": 10**400, "filament_used": None}
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": [huge]}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "invalid print metrics" in count.note


def test_a_failed_later_page_keeps_the_confirmed_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    replies = iter(
        [{"result": {"jobs": [job(NOW - 60) for _ in range(G.HISTORY_PAGE)]}}]
    )

    def page(*a: object, **k: object) -> dict[str, Any]:
        reply = next(replies, None)
        if reply is None:
            raise ConnectionResetError("reset")
        return reply

    monkeypatch.setattr(G, "_get_json", page)
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (G.HISTORY_PAGE, False)
    assert "unreachable: ConnectionResetError" in count.note


def test_a_job_without_start_time_keeps_the_confirmed_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    jobs = [
        *(job(NOW - 3600 * (i + 1)) for i in range(5)),
        {"job_id": "X", "status": "completed"},
    ]
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": jobs}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "has no start time" in count.note


@pytest.mark.parametrize("other", [{}, {"filament_used": None}])
def test_a_zero_metric_without_its_pair_is_unknown(
    monkeypatch: pytest.MonkeyPatch, other: dict[str, Any]
) -> None:

    half = {
        "job_id": "1",
        "start_time": NOW,
        "status": "completed",
        "print_duration": 0.0,
        **other,
    }
    monkeypatch.setattr(G, "_get_json", lambda *a, **k: {"result": {"jobs": [half]}})
    count = G.count_prints("k1se", NOW - DAY, G.adapter_history(_moon()))
    assert not count.exact and "incomplete print metrics" in count.note


def test_an_offline_fleet_printer_takes_glue_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline fleet printer is never contacted, but its glue can still be recorded and checked."""
    fleet = tmp_path / "printers.yaml"
    fleet.write_text(
        "p1s:\n  model: Bambu Lab P1S\n  owner: own\n  access: offline\n  heads:\n"
        "    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: hardened, type: std}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    assert "p1s" in G.known_printers()


def test_a_job_started_exactly_at_the_glue_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moonraker returns start_time > since: the query asks from before the glue, the local filter is >=."""

    asked: list[str] = []

    def moonraker(method: str, url: str, **_: Any) -> Any:
        asked.append(url)
        since = float(url.split("since=")[1].split("&")[0])
        return {"result": {"jobs": [j for j in [job(NOW)] if j["start_time"] > since]}}

    monkeypatch.setattr(G, "_get_json", moonraker)
    count = G.count_prints("k1se", NOW, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (1, True)


def test_an_unformattable_record_never_stops_a_print() -> None:
    path = G.state_path("k1se")
    path.parent.mkdir(parents=True)
    path.write_text('{"applied_at": "9999-12-31T23:59:59-12:00"}', encoding="utf-8")
    out: list[str] = []
    G.check_before_print(
        "k1se", interactive=False, echo=out.append, history_for=history([])
    )
    assert out and out[0].startswith("glue check skipped: ")


def test_prints_after_the_glue_on_a_printer_clock_an_hour_behind_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #770: job start times are on the printer's clock; an hour behind hid every print after the glue."""
    glued = NOW - 1800
    behind = 3600.0
    table = [job(glued - behind + 60 * (i + 1)) for i in range(5)]
    history = _moonraker_table(table)

    def server(method: str, url: str, **k: Any) -> dict[str, Any]:
        clock = k.get("clock")
        if clock is not None:
            clock.append((NOW - behind, NOW))
        return history(method, url, **k)

    monkeypatch.setattr(G, "_get_json", server)
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "printer clock is 1h 00m behind" in count.note


def test_a_printer_clock_within_a_minute_keeps_the_count_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = [job(NOW - 600 + i) for i in range(3)]
    history = _moonraker_table(table)

    def server(method: str, url: str, **k: Any) -> dict[str, Any]:
        clock = k.get("clock")
        if clock is not None:
            clock.append((NOW - 30, NOW))
        return history(method, url, **k)

    monkeypatch.setattr(G, "_get_json", server)
    count = G.count_prints("k1se", NOW - 3600, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (3, True)


def test_the_reply_date_header_is_read_as_the_printer_clock() -> None:
    clock: list[tuple[float, float]] = []
    G._record_date("Thu, 15 Jan 2027 08:00:00 GMT", clock)
    G._record_date("not a date", clock)
    G._record_date(None, clock)
    assert [c[0] for c in clock] == [
        datetime.datetime(2027, 1, 15, 8, tzinfo=datetime.timezone.utc).timestamp()
    ]


def _behind_printer(history: Any, behind: float, calls: list[str]) -> Any:
    """A Moonraker double whose Date header says its clock is `behind` seconds behind this computer."""

    def server(method: str, url: str, **k: Any) -> dict[str, Any]:
        calls.append(url)
        clock = k.get("clock")
        if clock is not None:
            clock.append((NOW - behind, NOW))
        return history(method, url, **k)

    return server


def test_a_failed_read_on_the_printer_clock_keeps_the_prints_the_first_read_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glued = NOW - 7200
    table = [job(NOW - 600 + i) for i in range(5)]  # after the glue on either clock
    history = _moonraker_table(table)
    calls: list[str] = []

    def reset_after_the_first(method: str, url: str, **k: Any) -> dict[str, Any]:
        if len(calls) > 1:  # the double records the call before it gets here
            raise ConnectionResetError("reset")
        return history(method, url, **k)

    monkeypatch.setattr(
        G, "_get_json", _behind_printer(reset_after_the_first, 3600, calls)
    )
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "ConnectionResetError" in count.note


def test_the_read_on_the_printer_clock_shares_the_history_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _moonraker_table([job(NOW - 600)])
    calls: list[str] = []

    def slow(method: str, url: str, **k: Any) -> dict[str, Any]:
        time.sleep(0.3)
        return history(method, url, **k)

    monkeypatch.setattr(G, "HISTORY_DEADLINE_S", 0.2)
    monkeypatch.setattr(G, "_get_json", _behind_printer(slow, 3600, calls))
    count = G.count_prints("k1se", NOW - 7200, G.adapter_history(_moon()))
    assert len(calls) == 1
    assert not count.exact and "did not answer" in count.note


def test_an_overflowing_date_header_is_ignored() -> None:
    clock: list[tuple[float, float]] = []
    G._record_date("Fri, 01 Jan 999999999999999999999 00:00:00 GMT", clock)
    assert clock == []


def test_a_printer_clock_ahead_never_hides_prints_after_the_glue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock may have been right when it printed and jumped ahead later: keep the host cutoff."""
    glued = NOW - 3600
    table = [job(glued + 60 * (i + 1)) for i in range(5)]
    calls: list[str] = []
    monkeypatch.setattr(
        G, "_get_json", _behind_printer(_moonraker_table(table), -3600, calls)
    )
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)
    assert "printer clock is 1h 00m ahead of" in count.note


def test_a_print_just_after_the_glue_on_a_clock_seconds_behind_is_counted_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glued = NOW - 600
    calls: list[str] = []
    table = [job(glued + 10 - 30)]  # 10 s after the glue, on a clock 30 s behind
    monkeypatch.setattr(
        G, "_get_json", _behind_printer(_moonraker_table(table), 30, calls)
    )
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (1, True)


def test_a_retry_row_without_metrics_keeps_the_print_the_first_read_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glued = NOW - 7200
    table = [job(NOW - 600 + i) for i in range(5)]
    history = _moonraker_table(table)
    calls: list[str] = []

    def metrics_gone_on_retry(method: str, url: str, **k: Any) -> dict[str, Any]:
        reply = history(method, url, **k)
        if len(calls) > 1:  # the double records the call before it gets here
            jobs = reply["result"]["jobs"]
            reply["result"]["jobs"] = [
                {
                    key: v
                    for key, v in j.items()
                    if key not in ("print_duration", "filament_used")
                }
                for j in jobs
            ]
        return reply

    monkeypatch.setattr(
        G, "_get_json", _behind_printer(metrics_gone_on_retry, 3600, calls)
    )
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (5, False)


def test_date_header_rounding_keeps_the_exact_glue_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whole-second Date makes a right clock look up to a second behind: not a skew to correct."""
    glued = NOW - 600
    calls: list[str] = []
    table = [job(glued - 0.5), job(glued + 0.15)]
    monkeypatch.setattr(
        G, "_get_json", _behind_printer(_moonraker_table(table), 0.9, calls)
    )
    count = G.count_prints("k1se", glued, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (1, True)
    assert len(calls) == 1


def test_a_first_read_failure_stays_after_a_clean_read_on_the_printer_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _moonraker_table([job(NOW - 600 + i) for i in range(3)])
    calls: list[str] = []

    def reset_first(method: str, url: str, **k: Any) -> dict[str, Any]:
        if len(calls) == 1:  # the double records the call before it gets here
            raise ConnectionResetError("reset")
        return history(method, url, **k)

    monkeypatch.setattr(G, "_get_json", _behind_printer(reset_first, 30, calls))
    count = G.count_prints("k1se", NOW - 7200, G.adapter_history(_moon()))
    assert (count.count, count.exact) == (3, False)
    assert "ConnectionResetError" in count.note
