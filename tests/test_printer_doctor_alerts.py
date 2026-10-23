"""`alert-subscribers` (issue #526): does a live omp session receive the printer's alerts? And the
consecutive-failure bookkeeping + health judgement it shares with `3d printer subscriptions`.
Temp registry/state files and a fake `tg-ctl` only: never the real subscription files or sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from printer_doctor import render
from printer_doctor.engine import Options, run_printer, summarize
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import FakeTgCtl, ev, rig, subscription_sources, target
from printer_ops import subscriptions as subs
from printer_ops.watch import Event

CHECK = {c.id: c for c in discover_checks()}["alert-subscribers"]
LIVE = {
    "pid": 4242,
    "sessionId": "0aaa1111-live-session",
    "cwd": "/tmp",
    "title": "main",
}
CAMERA = {
    "vision": {"camera": {"kind": "snapshot-url", "url": "http://127.0.0.1:1/snap"}}
}
DEAD_ERR = "no live omp session matches 'dead0001'"


class Lab:
    """A registry + delivery state in tmp_path and a fake tg-ctl."""

    def __init__(
        self, tmp_path: Path, hosts: list[dict[str, Any]] | None = None
    ) -> None:
        self.tg = FakeTgCtl(hosts)
        self.sources = subscription_sources(tmp_path, self.tg)
        self.registry = self.sources.registry
        self.state = self.sources.state
        assert self.registry is not None and self.state is not None

    def add(self, session: str, *printers: str, severity: str = "warning", failed: int | None = None,
            success: str | None = None) -> str:  # fmt: skip
        assert self.registry is not None and self.state is not None
        entry, _ = self.registry.put(subs.make(session=session, printers=list(printers), min_severity=severity,
                                               created="2026-10-18T10:00:00+00:00"))  # fmt: skip
        if failed is not None or success:
            row: dict[str, Any] = {}
            if failed is not None:
                row |= {"failed": failed, "consecutive_failed": failed, "last_error": DEAD_ERR,
                        "last_error_ts": "2026-10-23T10:00:00+00:00"}  # fmt: skip
            if success:
                row |= {"delivered": 3, "last_ts": success}
            self.state.update(
                lambda data: data["subscriptions"].__setitem__(entry.id, row)
            )
        return entry.id

    def ids(self) -> list[str]:
        assert self.registry is not None
        return sorted(s.id for s in self.registry.load())

    def snapshot(self) -> tuple[str, str]:
        assert self.registry is not None and self.state is not None

        def read(path: Path) -> str:
            return path.read_text() if path.exists() else ""

        return read(self.registry.path), read(self.state.path)

    def run(self, options: Options | None = None, **tgt: Any) -> tuple[Any, Any]:
        r = rig(
            target(watch=tgt.pop("watch", CAMERA), **tgt), subscriptions=self.sources
        )
        return run_printer(r.ctx, [CHECK], options or Options()).records[0], r


# ---- failing / passing ----------------------------------------------------------------------


def test_fails_when_the_only_subscriptions_are_dead_and_reports_each_entry(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    a = lab.add("dead0001", "p1", failed=89, success="2026-10-17T09:00:00+00:00")
    b = lab.add("dead0002", "p1", failed=41)
    c = lab.add("dead0003", "p1", failed=36)
    rec, _ = lab.run()
    res = rec.result
    assert (res.status, res.severity) == (
        "fail",
        "error",
    ) and "no live subscriber" in res.summary
    rows = {row["id"]: row for row in ev(res)["entries"]}
    assert set(rows) == {a, b, c}
    assert (
        rows[a]["consecutive_failed"],
        rows[a]["failed"],
        rows[a]["last_success"],
    ) == (89, 89, "2026-10-17T09:00:00+00:00")
    assert (
        rows[b]["last_error"] == DEAD_ERR
        and rows[b]["live"] is False
        and rows[b]["session"] == "dead0002"
    )
    assert all(row["prunable"] and row["stale"] for row in rows.values())
    assert sorted(ev(res)["prunable"]) == sorted([a, b, c])


def test_passes_with_one_live_subscriber_even_next_to_dead_ones(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("dead0001", "p1", failed=40)
    live = lab.add(
        "0aaa1111", "p1"
    )  # a prefix of the live session id, as `subscribe` stores it
    res = lab.run()[0].result
    assert res.status == "pass" and live in res.summary
    assert {r["id"]: r["live"] for r in ev(res)["entries"]}[live] is True


def test_an_entry_for_every_printer_counts_and_one_for_another_printer_does_not(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("0aaa1111", "other-printer")
    res = lab.run()[0].result
    assert res.status == "fail" and "no subscription covers this printer" in res.summary
    lab.add("0aaa1111")  # no --printer: every printer
    assert lab.run()[0].result.status == "pass"


def test_a_live_subscriber_above_warning_level_does_not_count(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("0aaa1111", "p1", severity="critical")
    res = lab.run()[0].result
    assert res.status == "fail" and "no live subscriber at warning level" in res.summary
    assert not ev(res)["prunable"]  # live: never a candidate for pruning


def test_no_subscriptions_at_all_fails_without_asking_tg_ctl(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    res = lab.run()[0].result
    assert res.status == "fail" and ev(res)["entries"] == [] and lab.tg.calls == []


def test_unknown_liveness_skips_instead_of_guessing(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    lab.tg.missing = True
    lab.add("dead0001", "p1", failed=50)
    res = lab.run()[0].result
    assert (
        res.status == "skip"
        and "cannot tell whether the subscribed sessions are live" in res.summary
    )


def test_a_broken_registry_is_an_error_not_a_pass(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    assert lab.registry is not None
    lab.registry.path.write_text("subscriptions: {not: a list}\n")
    res = lab.run()[0].result
    assert res.status == "error" and res.severity == "error"


# ---- applicability --------------------------------------------------------------------------


def test_skips_a_printer_without_a_watch_or_vision_unit_and_an_offline_one(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    res = lab.run(watch={})[0].result
    assert res.status == "skip" and "no watch or vision unit" in res.summary
    res = lab.run(access="offline")[0].result
    assert res.status == "skip" and "offline" in res.summary


def test_a_watch_block_without_a_camera_is_a_watch_unit(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    assert lab.run(watch={"tg_fallback": True})[0].result.status == "fail"


# ---- the fix --------------------------------------------------------------------------------


def test_prune_removes_only_dead_failing_entries_of_this_printer(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    doomed = lab.add("dead0001", "p1", failed=89)
    keep_few_failures = lab.add(
        "dead0002", "p1", failed=2
    )  # away, may come back with `omp -r`
    keep_never_delivered = lab.add("dead0003", "p1")
    keep_live_failing = lab.add(
        "0aaa1111", "p1", severity="critical", failed=60
    )  # alive: never touched
    keep_other_printer = lab.add("dead0004", "other-printer", failed=90)
    rec, r = lab.run(Options(fix=True))
    assert lab.ids() == sorted(
        [keep_few_failures, keep_never_delivered, keep_live_failing, keep_other_printer]
    )
    assert doomed not in lab.ids()
    assert (
        rec.fix is not None
        and rec.fix.applied
        and rec.fix.verified is True
        and rec.fix.error is None
    )
    (entry,) = r.journal
    assert (
        entry.check == "alert-subscribers"
        and entry.risk == "safe"
        and entry.after == []
    )
    assert entry.before == [f"{doomed} session dead0001 (89 failed in a row)"]
    assert lab.tg.sends() == []  # pruning never sends anything to a session


def test_after_the_prune_the_finding_remains_and_the_report_says_so(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("dead0001", "p1", failed=89)
    rec, _ = lab.run(Options(fix=True))
    assert (
        rec.result.status == "fail" and "no subscription covers" in rec.result.summary
    )
    assert rec.fix is not None and rec.fix.verified is True
    assert any(
        "3d printer subscribe --session SESSION_ID --printer p1" in s
        for s in rec.fix.manual_steps
    )
    from printer_doctor.model import PrinterReport

    rep = PrinterReport("p1", "full", "moonraker", [rec])
    summary = summarize([rep], "error")
    assert (summary.fixed, summary.remaining) == (1, 1)
    text = render.to_text([rep], summary)
    assert "applied and verified, but the finding remains" in text
    assert (
        "        - Subscribe a LIVE omp session" in text
        and "        $ omp-steer list" in text
    )


def test_second_fix_run_is_a_noop(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("dead0001", "p1", failed=89)
    lab.run(Options(fix=True))
    before = lab.snapshot()
    rec, r = lab.run(Options(fix=True))
    assert rec.fix is not None and not rec.fix.applied
    assert rec.fix.error and rec.fix.error.startswith("refused: nothing to prune")
    assert lab.snapshot() == before and r.journal == []


def test_the_fix_rereads_liveness_so_a_session_that_came_back_is_never_pruned(
    tmp_path: Path,
) -> None:
    from printer_doctor.model import FixRefused

    lab = Lab(tmp_path, [])
    sid = lab.add("0aaa1111", "p1", failed=70)
    r = rig(target(watch=CAMERA), subscriptions=lab.sources)
    result = run_printer(r.ctx, [CHECK], Options()).records[0].result
    assert result.status == "fail" and ev(result)["prunable"] == [
        sid
    ]  # dead when the check looked
    lab.tg.hosts.append(LIVE)  # ... and the omp session resumed before the fix ran
    assert CHECK.fix is not None and CHECK.fix.apply is not None
    with pytest.raises(FixRefused, match="nothing to prune"):
        CHECK.fix.apply(r.ctx, result)
    assert lab.ids() == [sid]


def test_pruning_works_on_a_read_only_printer_because_it_touches_no_printer(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("dead0001", "p1", failed=89)
    rec, _ = lab.run(Options(fix=True), access="read-only")
    assert (
        rec.fix is not None
        and rec.fix.applied
        and rec.fix.verified is True
        and lab.ids() == []
    )


def test_a_read_only_run_writes_nothing_and_sends_nothing(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    lab.add("dead0001", "p1", failed=89)
    before = lab.snapshot()
    rec, r = lab.run()
    assert rec.result.status == "fail" and rec.fix is not None and not rec.fix.applied
    assert lab.snapshot() == before and r.journal == [] and lab.tg.sends() == []
    assert [c for c in lab.tg.calls if c[:3] != ["tg-ctl", "omp-live", "status"]] == []


def test_manual_steps_name_the_exact_commands(tmp_path: Path) -> None:
    lab = Lab(tmp_path, [LIVE])
    sid = lab.add("dead0001", "p1", failed=89)
    fix = lab.run()[0].fix
    assert fix is not None
    steps = fix.manual_steps
    assert f"$ 3d printer unsubscribe --id {sid}" in steps
    assert "then: 3d printer subscribe --session SESSION_ID --printer p1" in steps
    assert "$ omp-steer list" in steps


# ---- bookkeeping shared with `3d printer subscriptions` -----------------------------------------


def _event(printer: str = "p1") -> Event:
    return Event(printer, "vision_spaghetti", "warning", "x", {}, key="k")


def test_delivery_state_counts_failures_in_a_row_and_resets_on_success(
    tmp_path: Path,
) -> None:
    state = subs.DeliveryState(tmp_path / "state.json")
    for _ in range(3):
        state.record(
            "s1", [_event(), _event()], 1_800_000_000.0, ok=False, detail="gone"
        )
    row = state.read()["subscriptions"]["s1"]
    assert (row["failed"], row["consecutive_failed"]) == (
        6,
        3,
    )  # `failed` counts events, the streak attempts
    state.record("s1", [_event()], 1_800_000_100.0, ok=True, detail="session x")
    row = state.read()["subscriptions"]["s1"]
    assert (row["consecutive_failed"], row["failed"], row["delivered"]) == (0, 6, 1)
    state.record("s1", [_event()], 1_800_000_200.0, ok=False, detail="gone")
    assert state.read()["subscriptions"]["s1"]["consecutive_failed"] == 1


def test_state_written_before_the_counter_existed_is_read_back_sensibly() -> None:
    cf = subs.consecutive_failures
    assert (
        cf({"failed": 89, "last_error_ts": "2026-10-23T10:00:00+00:00"}) == 89
    )  # never succeeded
    assert (
        cf(
            {
                "failed": 5,
                "last_ts": "2026-10-17T09:00:00+00:00",
                "last_error_ts": "2026-10-23T10:00:00+00:00",
            }
        )
        == 5
    )
    assert (
        cf(
            {
                "failed": 5,
                "last_ts": "2026-10-24T09:00:00+00:00",
                "last_error_ts": "2026-10-23T10:00:00+00:00",
            }
        )
        == 0
    )
    assert (
        cf({"failed": 5, "consecutive_failed": 2}) == 2
        and cf({}) == 0
        and cf({"failed": 0}) == 0
    )
    assert (
        cf({"failed": 4, "last_error_ts": "not a time"}) == 0
    )  # an unreadable time proves nothing


@pytest.mark.parametrize(
    ("live", "failing", "stale", "prunable"),
    [
        (False, 5, True, True),
        (False, 4, True, False),
        (True, 90, True, False),
        (True, 0, False, False),
        (None, 90, True, False),
    ],
)
def test_health_judgement(
    live: bool | None, failing: int, stale: bool, prunable: bool
) -> None:
    sub = subs.make(session="s", printers=["p1"])
    h = subs.healths(
        [sub],
        {sub.id: {"live": live, "detail": "d"}},
        {sub.id: {"consecutive_failed": failing}},
    )[0]
    assert (h.stale, h.prunable) == (stale, prunable)
    assert h.to_dict()["stale_reasons"] == h.reasons


def test_unwatched_lists_printers_without_a_live_warning_level_subscriber() -> None:

    def mk(sub: subs.Subscription, live: bool | None) -> subs.Health:
        return subs.healths([sub], {sub.id: {"live": live, "detail": ""}}, {})[0]

    live_all = mk(subs.make(session="a"), True)
    live_u = mk(subs.make(session="b", printers=["u"]), True)
    dead = mk(subs.make(session="c", printers=["x"]), False)
    crit = mk(subs.make(session="d", printers=["y"], min_severity="critical"), True)
    assert subs.unwatched(["u", "x", "y", "z"], [live_u, dead, crit]) == ["x", "y", "z"]
    assert subs.unwatched(["u", "x"], [live_all]) == []


def test_alert_subscribers_is_registered_once_and_documented() -> None:
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "commands" / "printers-doctor.md"
    ).read_text()
    assert "`alert-subscribers`" in doc and "#526" in doc
    assert [c.id for c in discover_checks()].count("alert-subscribers") == 1
