"""`3d printer evidence` logic on the real Chekhov failure (2026-10-24 03:05:19 UTC, `extruder1 pickup failed`):
dating klippy.log, cutting the excerpt, mapping file_position to a layer, naming and saving the evidence."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from errors import InvalidArgument
from printer_evidence_fakes import (
    LAYER_455_INDEX,
    LAYERS,
    READ_AT,
    REAL_RESTOP,
    REAL_STOP,
    STOP_INTO_LAYER,
    fixture_gcode,
    fixture_position,
    job_for,
    layer_starts,
    shifted_log,
)
from printer_ops import evidence as ev

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _utc_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """File names carry the job's start day in the host's local time; the job started 17:24 UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _dated(text: str, anchor: datetime = READ_AT) -> list[ev.Entry]:
    entries = ev.parse_log(text)
    ev.date_entries(entries, anchor)
    return entries


def _build(**over: Any) -> ev.Evidence:
    gcode = fixture_gcode()
    args: dict[str, Any] = {
        "printer": "u1",
        "source": "test",
        "captured": READ_AT,
        "job": job_for(gcode),
        "job_problem": None,
        "print_stats": None,
        "print_stats_problem": None,
        "log_text": shifted_log(),
        "log_anchor": READ_AT,
        "log_problem": None,
        "layers": ev.scan_gcode(gcode),
        "note": None,
        "context": 6,
        "max_entries": 400,
    }
    args.update(over)
    return ev.build(**args)


# ---- outcome -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("completed", "done"),
        ("cancelled", "cancelled"),
        ("in_progress", "in-progress"),
        ("error", "failed"),
        ("klippy_shutdown", "failed"),
        ("klippy_disconnect", "failed"),
        ("server_exit", "failed"),
        ("interrupted", "failed"),
        ("something_new", "failed"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_job_status_maps_to_the_outcome_in_the_file_name(status: str | None, outcome: str) -> None:
    assert ev.outcome_of(status) == outcome


# ---- dating klippy.log -----------------------------------------------------------------------------------------


def test_klippy_log_times_get_their_utc_date_from_the_moment_the_log_was_read() -> None:
    entries = _dated(shifted_log())
    stop = next(e for e in entries if e.text.startswith("Extruder extruder1 pickup failed"))
    assert stop.ts == datetime(2026, 10, 24, 3, 5, 19, 339000, tzinfo=UTC)
    assert entries[-1].ts is not None and entries[-1].ts <= READ_AT


def test_a_log_that_crosses_midnight_steps_one_day_back() -> None:
    text = "23:58:00.000:a\n23:59:59.900:b\n00:00:01.000:c\n00:30:00.000:d\n"
    a, b, c, d = _dated(text, datetime(2026, 10, 25, 0, 40, tzinfo=UTC))
    assert a.ts == datetime(2026, 10, 24, 23, 58, tzinfo=UTC)
    assert b.ts == datetime(2026, 10, 24, 23, 59, 59, 900000, tzinfo=UTC)
    assert c.ts == datetime(2026, 10, 25, 0, 0, 1, tzinfo=UTC)
    assert d.ts == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_a_last_line_later_than_the_read_moment_belongs_to_the_day_before() -> None:
    (last,) = _dated("23:50:00.000:x\n", datetime(2026, 10, 25, 0, 10, tzinfo=UTC))
    assert last.ts == datetime(2026, 10, 24, 23, 50, tzinfo=UTC)


def test_small_clock_steps_backwards_are_not_midnight() -> None:
    a, b = _dated("12:00:05.000:a\n12:00:04.000:b\n", datetime(2026, 10, 24, 12, 1, tzinfo=UTC))
    assert a.ts is not None and b.ts is not None
    assert a.ts.date() == b.ts.date()


def test_unstamped_lines_belong_to_the_message_above() -> None:
    entries = ev.parse_log("01:00:00.000:head\n  continuation\nmore\n01:00:01.000:next\n")
    assert [(e.text, e.extra) for e in entries] == [
        ("head", ["  continuation", "more"]),
        ("next", []),
    ]


STOCK_KLIPPY = (
    "Starting Klippy...\nArgs: ['klippy.py']\nStats 100.0: gcodein=0  mcu: mcu_awake=0.010\n"
    "Exiting SD card print (position 4242)\nTransition to shutdown state: Lost communication with MCU 'mcu'\n"
    "Stats 102.0: gcodein=0  mcu: mcu_awake=0.010\n"
)


def test_a_stock_klipper_log_without_a_clock_is_read_whole_and_its_stop_position_is_found() -> None:
    entries = _dated(STOCK_KLIPPY)
    assert all(e.ts is None and e.secs is None for e in entries) and len(entries) == 6
    x = ev.build_excerpt(
        entries,
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        0,
        400,
    )
    assert sorted(k for kinds in x.hits.values() for k in kinds) == [
        "sd-stop",
        "shutdown",
    ]  # no window to be outside of
    positions = ev.stop_positions(entries, (None, None), None)
    assert [(p.byte, p.what, p.ts) for p in positions] == [(4242, "stop", None)]


def test_a_stock_klipper_report_says_the_log_has_no_time_stamps_and_still_lists_the_events() -> None:
    md = ev.render_markdown(_build(log_text=STOCK_KLIPPY, layers=None, job=job_for(fixture_gcode())))
    assert "klippy.log has no time stamps (stock Klipper)" in md
    assert "!     4  (no time)  Exiting SD card print (position 4242)" in md
    assert "File positions in the log (pass `--gcode FILE` to map them to layers): 4242" in md


# ---- the excerpt -----------------------------------------------------------------------------------------------


def test_excerpt_holds_the_pickup_failure_with_its_neighbours_and_no_stats_noise() -> None:
    entries = _dated(shifted_log())
    x = ev.build_excerpt(entries, ev.job_window(job_for(fixture_gcode()), READ_AT), 6, 400)
    shown = {e.line: e for e in x.entries}
    failure = next(line for line, kinds in x.hits.items() if "pickup-failed" in kinds and "detached" in kinds)
    assert shown[failure].text.startswith("Extruder extruder1 pickup failed, detected that extruder1 is detached")
    assert "['1-(FFF)']" in shown[failure].text
    assert any("retry attempt 8" in e.text for e in x.entries)  # context before the failure
    assert not any(e.text.startswith("Stats ") for e in x.entries)  # the 2 s counters are not context


def test_routine_pausing_and_config_macros_are_not_events() -> None:
    entries = _dated(shifted_log())
    x = ev.build_excerpt(entries, (None, None), 0, 400)
    texts = [e.text for e in x.entries]
    assert not any(t.startswith("Pausing toolhead to calculate") for t in texts)
    assert not any("Config file" in t for t in texts)
    kinds = {k for ks in x.hits.values() for k in ks}
    assert {
        "pickup-failed",
        "detached",
        "exception",
        "exception-cleared",
        "pause",
        "resume",
        "cancel",
        "sd-stop",
        "sd-start",
    } <= kinds


def test_a_cleared_exception_is_an_event_but_not_a_fault() -> None:
    """The U1 logs `exception cleared` when the operator resumes; as the first line of a job's window it is not
    the failure."""
    log = "10:00:00.000:exception cleared: id:523 index:1 code:46\n10:00:05.000:Resuming...\n"
    e = _build(
        log_text=log,
        layers=None,
        job=job_for(fixture_gcode(), start_time=READ_AT.timestamp() - 3600),
    )
    assert e.excerpt is not None and e.excerpt.shown_events == 2
    assert not any(line.startswith("First fault") for line in ev.summary_lines(e))


def test_events_of_other_jobs_outside_the_time_window_are_counted_not_shown() -> None:
    entries = _dated(shifted_log())
    lo = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    x = ev.build_excerpt(entries, (lo, None), 0, 400)
    assert x.outside_window > 0
    assert all(e.ts is not None and e.ts >= lo for e in x.entries)
    assert not any("pickup failed, detected" in e.text for e in x.entries)


def test_max_lines_cuts_the_middle_and_says_how_many() -> None:
    log = "".join(f"01:00:{i:02d}.000:Raising exception: n={i}\n" for i in range(40))
    x = ev.build_excerpt(_dated(log, datetime(2026, 10, 24, 2, tzinfo=UTC)), (None, None), 0, 10)
    assert (x.events_total, x.events_cut, x.shown_events) == (40, 30, 10)
    shown = [e.text for e in x.entries]
    assert shown[0].endswith("n=0") and shown[-1].endswith("n=39")


def test_stop_positions_come_from_the_whole_log_not_from_the_shortened_excerpt() -> None:
    """--max-lines is a view: with the budget at 2 events the stop in the cut middle still reaches the layer map."""
    chatter = "".join(f"01:00:{i:02d}.000:Raising exception: n={i}\n" for i in range(30))
    log = (
        chatter + "01:01:00.000:Exiting SD card print, lines=1, current_line_gcode=M400, file_position=500\n" + chatter
    )
    job = job_for(fixture_gcode(), start_time=READ_AT.timestamp() - 86400, end_time=None)
    e = _build(
        log_text=log,
        job=job,
        max_entries=2,
        log_anchor=datetime(2026, 10, 24, 2, tzinfo=UTC),
        captured=datetime(2026, 10, 24, 2, tzinfo=UTC),
    )
    assert e.excerpt is not None and e.excerpt.events_cut > 0
    assert [s.where.position for s in e.stops] == [500]


def test_an_event_free_job_window_is_reported_as_such() -> None:
    entries = _dated("01:00:00.000:Stats 1: gcodein=0\n01:00:02.000:Stats 3: gcodein=0\n")
    x = ev.build_excerpt(entries, (None, None), 6, 400)
    assert x.entries == []
    assert x.events_total == 0


def test_a_job_that_is_still_running_has_its_window_end_now() -> None:
    job = job_for(fixture_gcode(), status="in_progress", end_time=None)
    lo, hi = ev.job_window(job, READ_AT)
    assert lo is not None
    assert hi == READ_AT


def test_a_log_that_does_not_overlap_the_job_is_a_problem_not_a_quiet_job() -> None:
    """A week-old job after the log rotated, or a `cp`'d copy dated by the copy's mtime, must not read as 'nothing happened'."""
    old = job_for(
        fixture_gcode(),
        start_time=READ_AT.timestamp() - 7 * 86400,
        end_time=READ_AT.timestamp() - 6 * 86400,
    )
    e = _build(job=old, layers=None)
    assert any("has rotated past it" in p and "cp -p" in p for p in e.problems)
    assert ev.render_markdown(e).count("MISSING / WRONG") == 1


def test_a_log_that_covers_only_the_end_of_the_job_is_normal_not_a_problem() -> None:
    assert _build().problems == []  # the real job started 8 h before the fixture log begins


# ---- file_position -> layer ------------------------------------------------------------------------------------


def test_the_real_stop_is_layer_455_with_last_complete_layer_top_z_65_698() -> None:
    g = ev.scan_gcode(fixture_gcode())
    w = ev.locate(g, fixture_position(REAL_STOP))
    assert (w.layer, w.layers, w.z) == (455, LAYERS, 65.798)
    assert (w.last_complete, w.last_complete_z) == (454, 65.698)
    assert (w.layer_start, w.into_layer) == (
        layer_starts(fixture_gcode())[LAYER_455_INDEX],
        STOP_INTO_LAYER,
    )
    assert not w.past_end


def test_a_position_on_the_first_byte_of_a_layer_is_that_layer_and_the_one_below_is_complete() -> None:
    g = ev.scan_gcode(fixture_gcode())
    on_start = ev.locate(g, g.starts[LAYER_455_INDEX])
    one_before = ev.locate(g, g.starts[LAYER_455_INDEX] - 1)
    assert (on_start.layer, on_start.last_complete) == (455, 454)
    assert (one_before.layer, one_before.last_complete) == (454, 453)


def test_first_layer_and_start_gcode_have_no_complete_layer() -> None:
    g = ev.scan_gcode(fixture_gcode())
    in_first = ev.locate(g, g.starts[0] + 10)
    in_header = ev.locate(g, 5)
    assert (in_first.layer, in_first.last_complete, in_first.last_complete_z) == (
        1,
        None,
        None,
    )
    assert (in_header.layer, in_header.z, in_header.last_complete) == (0, None, None)


def test_a_position_beyond_the_file_means_it_is_not_the_printed_gcode() -> None:
    g = ev.scan_gcode(fixture_gcode())
    assert ev.locate(g, REAL_STOP).past_end  # the real 75 MB file's byte in the 50 KB fixture
    assert not ev.locate(g, g.size).past_end


def test_gcode_without_layer_markers_is_refused_with_what_is_accepted(
    tmp_path: Path,
) -> None:
    cura = tmp_path / "cura.gcode"
    cura.write_text(";LAYER:0\nG1 X1 Y1 E1\n;LAYER:1\nG1 X2 Y2 E2\n", encoding="utf-8")
    with pytest.raises(InvalidArgument) as err:
        ev.scan_gcode(cura)
    assert ";LAYER_CHANGE" in str(err.value.accepted)


def test_scan_gcode_matches_a_byte_for_byte_reading_of_the_fixture() -> None:
    g = ev.scan_gcode(fixture_gcode())
    data = fixture_gcode().read_bytes()
    marker = b";LAYER_CHANGE"
    expected = [i for i in range(len(data)) if data.startswith(marker, i) and (i == 0 or data[i - 1] == 10)]
    assert list(g.starts) == expected
    assert g.size == len(data)
    assert len(g.starts) == LAYERS


# ---- is it the printed file ----------------------------------------------------------------------------------


def test_gcode_identity_follows_the_hash_in_the_printed_file_name() -> None:
    g = ev.scan_gcode(fixture_gcode())
    assert ev.identity(g, job_for(fixture_gcode())).verdict == "verified"
    other = ev.identity(g, {"filename": "x-48b4bf31-48b4bf311a62a332.gcode"})
    assert other.mismatch
    assert "48b4bf311a62a332" in other.why


def test_gcode_identity_falls_back_to_the_recorded_size_then_gives_up() -> None:
    g = ev.scan_gcode(fixture_gcode())
    assert ev.identity(g, {"filename": "plain.gcode", "metadata": {"size": g.size}}).verdict == "verified"
    assert ev.identity(g, {"filename": "plain.gcode", "metadata": {"size": g.size + 1}}).mismatch
    assert ev.identity(g, {"filename": "plain.gcode"}).verdict == "unverified"


# ---- the assembled evidence -----------------------------------------------------------------------------------


def test_evidence_reports_both_stops_and_the_resume_entry_as_layer_455() -> None:
    e = _build()
    assert e.problems == []
    by_pos = {s.where.position: s for s in e.stops}
    assert len(by_pos) == 2  # the stop, and where the resume re-entered
    first, second = (
        by_pos[fixture_position(REAL_STOP)],
        by_pos[fixture_position(REAL_RESTOP)],
    )
    assert (first.where.layer, first.where.last_complete_z) == (455, 65.698)
    assert (second.where.layer, second.where.into_layer) == (
        455,
        REAL_RESTOP - REAL_STOP + STOP_INTO_LAYER,
    )
    assert first.stopped == [
        datetime(2026, 10, 24, 3, 5, 19, 340000, tzinfo=UTC)
    ]  # exit_print + Exiting SD card = one stop
    assert first.reentered == [datetime(2026, 10, 24, 9, 31, 32, 164000, tzinfo=UTC)]
    assert second.stopped == [datetime(2026, 10, 24, 9, 31, 48, 28000, tzinfo=UTC)]


def test_markdown_carries_the_failure_the_layer_and_the_z_to_cut_at() -> None:
    md = ev.render_markdown(_build(note="head T2 failed pickup"))
    assert md.startswith("# 2026-10-23 chekhov_phone_stand-final4-")
    assert "Outcome: **cancelled**" in md
    assert "First fault in klippy.log: 2026-10-24 03:05:19.339 UTC" in md
    assert "layer 455 of 644, z 65.798" in md
    assert "last complete layer 454, top z 65.698" in md
    assert "Operator note: head T2 failed pickup" in md
    assert "err-sta: ['1-(FFF)']" in md
    assert "The job started 2026-10-23 17:24:59 UTC, before the log begins" in md


def test_a_mismatching_gcode_is_labelled_and_counted_as_a_problem() -> None:
    e = _build(job=job_for(fixture_gcode(), filename="x-48b4bf31-48b4bf311a62a332.gcode"))
    assert any("not the printed file" in p for p in e.problems)
    assert "**MISMATCH**" in ev.render_markdown(e)


def test_missing_parts_are_listed_in_the_evidence_itself() -> None:
    e = _build(
        job=None,
        job_problem="history job not collected: unreachable",
        log_text=None,
        log_anchor=None,
        log_problem="klippy.log not collected: HTTP 404",
        layers=None,
    )
    md = ev.render_markdown(e)
    assert e.outcome == "unknown"
    assert "MISSING / WRONG" in md
    assert "history job not collected: unreachable" in md
    assert "klippy.log was not collected: klippy.log not collected: HTTP 404" in md


def test_without_gcode_the_positions_are_listed_for_a_later_mapping() -> None:
    md = ev.render_markdown(_build(layers=None))
    assert "pass `--gcode FILE`" in md
    assert str(fixture_position(REAL_STOP)) in md


# ---- saving it -------------------------------------------------------------------------------------------------


def test_files_are_named_by_start_day_and_outcome_and_the_json_is_the_raw_record(
    tmp_path: Path,
) -> None:
    md, js = ev.write_evidence(_build(), tmp_path / "print" / "runs")
    assert (md.name, js.name) == (
        "2026-10-23-cancelled.md",
        "2026-10-23-cancelled.json",
    )
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["schema"] == ev.SCHEMA
    assert data["job"]["job_id"] == "000070"
    assert data["outcome"] == "cancelled"
    assert {p["layer"] for p in data["gcode"]["positions"]} == {455}
    assert data["klippy"]["events_in_job_window"] == len([x for x in data["klippy"]["entries"] if x["event"]])


def test_saving_the_same_job_again_overwrites_its_own_files_only(
    tmp_path: Path,
) -> None:
    out = tmp_path / "runs"
    ev.write_evidence(_build(note="first"), out)
    md, _ = ev.write_evidence(_build(note="second"), out)
    assert sorted(p.name for p in out.iterdir()) == [
        "2026-10-23-cancelled.json",
        "2026-10-23-cancelled.md",
    ]
    text = md.read_text(encoding="utf-8")
    assert "second" in text
    assert "first" not in text


def test_another_job_of_the_same_day_and_outcome_never_replaces_the_first(
    tmp_path: Path,
) -> None:
    out = tmp_path / "runs"
    first, _ = ev.write_evidence(_build(), out)
    start = datetime(2026, 10, 23, 17, 24, 59, tzinfo=UTC) + timedelta(hours=1)
    other = job_for(fixture_gcode(), job_id="000071", start_time=start.timestamp())
    second, second_json = ev.write_evidence(_build(job=other), out)
    assert second.name == "2026-10-23-cancelled-000071.md"
    assert second_json.name == "2026-10-23-cancelled-000071.json"
    assert "`000070`" in first.read_text(encoding="utf-8")


def test_captures_without_a_job_record_never_overwrite_each_other(
    tmp_path: Path,
) -> None:
    out = tmp_path / "runs"
    first, _ = ev.write_evidence(_build(job=None, job_problem="down"), out)
    second, _ = ev.write_evidence(
        _build(job=None, job_problem="down", captured=READ_AT + timedelta(seconds=5)),
        out,
    )
    assert first != second
    assert first.name.endswith("-unknown-105800.md")


def test_a_failed_write_leaves_no_half_written_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "runs"
    out.mkdir()

    def boom(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        ev.write_evidence(_build(), out)
    assert list(out.iterdir()) == []


def test_saved_files_get_the_umask_mode_of_a_normal_file_not_0600(
    tmp_path: Path,
) -> None:
    old = os.umask(0o022)
    try:
        md, js = ev.write_evidence(_build(), tmp_path / "runs")
    finally:
        os.umask(old)
    assert md.stat().st_mode & 0o777 == 0o644
    assert js.stat().st_mode & 0o777 == 0o644


def test_the_command_doc_gives_every_flag_of_the_help_with_an_example() -> None:
    doc = (Path(__file__).resolve().parents[1] / "docs" / "commands" / "printer-evidence.md").read_text(
        encoding="utf-8"
    )
    flags = sorted(set(re.findall(r"--[a-z]+(?:-[a-z]+)*", ev.USAGE)))
    assert {
        "--job",
        "--last",
        "--out",
        "--gcode",
        "--note",
        "--klippy-log",
        "--job-file",
        "--context",
        "--max-lines",
        "--secs",
    } <= set(flags)
    for flag in flags:
        assert f"`{flag}" in doc, f"{flag} is not documented in docs/commands/printer-evidence.md"


# ---- another job's positions, names, notes -----------------------------------------------------------------------


def test_a_print_started_minutes_after_this_one_ended_is_not_mapped_as_this_jobs_resume() -> None:
    """Job B starts inside the 15 min after job A ended: its `Starting SD card print ... position 0` is B's."""
    filename = job_for(fixture_gcode())["filename"]
    log = (
        f"10:35:11.000:Exiting SD card print, lines=1, current_line_gcode=M400, file_position={fixture_position(REAL_STOP)}\n"
        f"10:35:11.001:file: /home/lava/printer_data/gcodes/{filename}\n"
        "10:40:00.000:Starting SD card print, file: /home/lava/printer_data/gcodes/other-b.gcode, position 0\n"
        "10:40:30.000:Exiting SD card print, lines=9, current_line_gcode=G1, file_position=77\n"
        "10:40:30.001:file: /home/lava/printer_data/gcodes/other-b.gcode\n"
    )
    e = _build(log_text=log)
    assert [s.where.position for s in e.stops] == [fixture_position(REAL_STOP)]
    assert e.stops[0].reentered == []


def test_a_log_without_file_names_maps_every_position() -> None:
    log = "10:35:11.000:Exiting SD card print, lines=1, current_line_gcode=M400, file_position=500\n"
    assert [s.where.position for s in _build(log_text=log).stops] == [500]


def test_a_job_id_from_a_record_cannot_leave_the_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "runs"
    ev.write_evidence(_build(), out)  # job 000070 holds the plain name
    hostile = job_for(fixture_gcode(), job_id="../../victim")
    md, js = ev.write_evidence(_build(job=hostile), out)
    assert md.parent == out
    assert js.parent == out
    assert "/" not in md.name
    assert not (tmp_path / "victim.md").exists()


def test_records_without_a_job_id_each_get_their_own_file_and_a_resave_overwrites_only_its_own(tmp_path: Path) -> None:
    out = tmp_path / "runs"

    def anonymous(start: float) -> dict[str, Any]:
        job = job_for(fixture_gcode(), start_time=start)
        del job["job_id"]
        return job

    day = datetime(2026, 10, 23, 12, 0, tzinfo=UTC).timestamp()
    names = [ev.write_evidence(_build(job=anonymous(day + n)), out)[0].name for n in range(3)]
    assert names == ["2026-10-23-cancelled.md", "2026-10-23-cancelled-job.md", "2026-10-23-cancelled-job-2.md"]


def test_another_save_of_a_finished_job_points_at_the_earlier_one_saved_while_it_ran(tmp_path: Path) -> None:
    out = tmp_path / "runs"
    running, _ = ev.write_evidence(_build(job=job_for(fixture_gcode(), status="in_progress", end_time=None)), out)
    _, finished_json = ev.write_evidence(_build(), out)
    assert running.name == "2026-10-23-in-progress.md"
    assert ev.earlier_saves(out, "u1", "000070", finished_json) == [running.with_suffix(".json")]
    assert ev.earlier_saves(out, "u1", "", finished_json) == []
    assert ev.earlier_saves(out, "k1se", "000070", finished_json) == []


def test_an_exit_line_is_attributed_to_its_file_even_when_the_file_line_comes_a_few_entries_later() -> None:
    """On the U1 the exit line is followed by `Pausing...` and others; the `file:` line is the 8th entry after it."""
    filename = job_for(fixture_gcode())["filename"]
    fillers = "".join(f"10:40:00.{i:03d}:filler {i}\n" for i in range(6))
    log = (
        "10:40:00.000:exit_print: lines=9, current_line_gcode=G1, file_position=77\n"
        f"{fillers}10:40:01.000:file: /home/lava/printer_data/gcodes/other-b.gcode\n"
        f"10:35:00.000:exit_print: lines=1, current_line_gcode=M400, file_position=500\n"
        f"{fillers}10:35:01.000:file: /home/lava/printer_data/gcodes/{filename}\n"
    )
    assert [s.where.position for s in _build(log_text=log).stops] == [500]


def test_the_two_lines_of_one_stop_are_one_stop_even_across_a_second_boundary() -> None:
    log = (
        "10:35:10.900:exit_print: lines=1, current_line_gcode=M400, file_position=500\n"
        "10:35:11.300:Exiting SD card print, lines=1, current_line_gcode=M400, file_position=500\n"
    )
    (stop,) = _build(log_text=log).stops
    assert len(stop.stopped) == 1


def test_a_u1_log_that_is_mostly_a_config_dump_is_still_a_clocked_log() -> None:
    """Just after a klippy restart the file is thousands of unstamped config lines and a handful of stamped messages."""
    dump = "".join(f"[gcode_macro T{i}]\n" for i in range(3000))
    entries = ev.parse_log(
        "10:00:00.000:===== Config file =====\n" + dump + "10:00:01.000:a\n10:00:02.000:Pausing...\n10:00:03.000:b\n"
    )
    assert [e.text for e in entries] == ["===== Config file =====", "a", "Pausing...", "b"]
    assert len(entries[0].extra) == 3000
    assert all(e.secs is not None for e in entries)


def test_the_first_fault_is_the_first_of_the_whole_window_not_of_what_max_lines_kept() -> None:
    chatter = "".join(f"10:00:{i:02d}.000:Pausing...\n" for i in range(30))
    log = chatter + "10:01:00.000:Extruder extruder1 pickup failed\n" + chatter.replace("10:00:", "10:02:")
    e = _build(
        log_text=log, max_entries=4, layers=None, job=job_for(fixture_gcode(), start_time=READ_AT.timestamp() - 3600)
    )
    assert e.excerpt is not None
    assert not any("pickup failed" in x.text for x in e.excerpt.entries)  # the shown events are the first and last two
    assert any("pickup failed" in line for line in ev.summary_lines(e))


def test_a_stop_beyond_the_end_of_the_gcode_is_a_problem_even_when_the_file_hash_matches() -> None:
    log = "10:35:10.000:Exiting SD card print, lines=1, current_line_gcode=M400, file_position=999999999\n"
    e = _build(log_text=log)
    assert any("past the end of --gcode" in p for p in e.problems)


def test_two_printers_with_the_same_job_number_do_not_replace_each_other(tmp_path: Path) -> None:
    out = tmp_path / "runs"
    first, _ = ev.write_evidence(_build(printer="u1"), out)
    second, _ = ev.write_evidence(_build(printer="k1se"), out)
    again, _ = ev.write_evidence(_build(printer="k1se"), out)
    assert first.name == "2026-10-23-cancelled.md"
    assert second.name == "2026-10-23-cancelled-000070.md"
    assert again == second
