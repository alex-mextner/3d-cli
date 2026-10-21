"""The read-only job queue / job history of the Moonraker adapter (parsing, the unsupported default)
and the status renderer shared by `3d printer status` and `3d printers show`."""
from __future__ import annotations

import pytest
from printer_fakes import HISTORY_JOBS, HISTORY_TOTALS, FakeMoonraker

from printer_ops.base import Adapter, PrinterCommError, PrinterStatus, Slot, UnsupportedOperation
from printer_ops.moonraker import MoonrakerAdapter, parse_history, parse_queue, parse_totals
from printer_ops.render import render_status, status_lines


def test_parse_queue_history_and_totals_read_the_moonraker_payloads() -> None:
    queue = parse_queue(
        {
            "queue_state": "paused",
            "queued_jobs": [
                {"filename": "a.gcode", "job_id": "0A", "time_in_queue": 12.5},
                {"job_id": "no-file"},
            ],
        }
    )
    assert (queue.state, [(j.filename, j.job_id, j.waiting_s) for j in queue.jobs]) == (
        "paused",
        [("a.gcode", "0A", 12.5)],
    )

    jobs = parse_history({"count": 3, "jobs": HISTORY_JOBS})
    assert [(j.filename, j.status, j.print_s, j.filament_mm) for j in jobs] == [
        ("benchy.gcode", "completed", 8040.0, 12400.0),
        ("cube.gcode", "cancelled", 610.0, 800.0),
        ("old.gcode", "completed", 60.0, 100.0),
    ]

    totals = parse_totals(HISTORY_TOTALS)
    assert (totals.jobs, totals.print_s, totals.filament_mm) == (
        127,
        770000.0,
        412300.0,
    )


@pytest.mark.parametrize("parse", [parse_queue, parse_history, parse_totals])
def test_a_reply_of_the_wrong_shape_is_an_error_not_an_empty_answer(
    parse: object,
) -> None:
    with pytest.raises(ValueError, match="expected|no '"):
        parse("garbage")  # type: ignore[operator]


def test_the_creality_adapter_does_not_expose_queue_or_history() -> None:
    adapter = Adapter("k1", "127.0.0.1", 80)
    for call in (adapter.queue, adapter.history_totals, lambda: adapter.history(3)):
        with pytest.raises(UnsupportedOperation):
            call()


def test_status_lines_are_shared_with_printer_status() -> None:
    from printer_ops.base import PrinterStatus, Slot

    st = PrinterStatus(
        "printing",
        "printing",
        "a.gcode",
        0.5,
        {"bed": (60.0, 60.0)},
        slots=[Slot(0, "T1", True, "#000000", "PLA"), Slot(1, "T2", False)],
    )

    assert status_lines(st) == [
        "  state     printing  [printing]",
        "  file      a.gcode  50.0%",
        "  temps     bed 60/60",
        "  heads     T1 #000000 PLA | T2 empty",
    ]
    assert status_lines(st, 15)[0] == "  state          printing  [printing]"


def test_the_moonraker_adapter_reads_queue_history_and_totals_with_gets_only() -> None:
    moon = FakeMoonraker(history=HISTORY_JOBS, totals=HISTORY_TOTALS,
                         queue={"queue_state": "ready", "queued_jobs": [{"filename": "next.gcode", "time_in_queue": 9.0}]})
    with moon.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", moon.port, timeout=2.0)
        queue, jobs, totals = adapter.queue(), adapter.history(1), adapter.history_totals()

    assert (queue.state, [j.filename for j in queue.jobs]) == ("ready", ["next.gcode"])
    assert [(j.filename, j.status) for j in jobs] == [("benchy.gcode", "completed")]
    assert totals.jobs == 127
    assert moon.writes() == []
    assert {r.method for r in moon.requests} == {"GET"}


def test_a_moonraker_that_answers_garbage_is_a_communication_error() -> None:
    moon = FakeMoonraker(queue={"unexpected": True}, totals={"nothing": 1})
    with moon.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", moon.port, timeout=2.0)
        with pytest.raises(PrinterCommError, match="job_queue/status"):
            adapter.queue()
        with pytest.raises(PrinterCommError, match="history/totals"):
            adapter.history_totals()


def test_numbers_that_are_not_finite_are_treated_as_unknown() -> None:
    inf, nan = float("inf"), float("nan")
    jobs = parse_history({"jobs": [{"filename": "a.gcode", "status": "completed", "start_time": nan,
                                    "print_duration": inf, "filament_used": -inf}]})
    totals = parse_totals({"job_totals": {"total_jobs": inf, "total_print_time": nan, "total_filament_used": 5}})
    queue = parse_queue({"queued_jobs": [{"filename": "b.gcode", "time_in_queue": nan}]})

    assert (jobs[0].start_time, jobs[0].print_s, jobs[0].filament_mm) == (None, None, None)
    assert (totals.jobs, totals.print_s, totals.filament_mm) == (None, None, 5.0)
    assert queue.jobs[0].waiting_s is None


def _main_render_status(name: str, adapter: Adapter, st: PrinterStatus) -> str:
    """`3d printer status` text exactly as main printed it before the sanitizer."""

    def temp(v: float | None) -> str:
        return "-" if v is None else f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"

    lines = [f"{name}  ({adapter.kind}  {adapter.host}:{adapter.port}{'  ' + st.model if st.model else ''})"]
    lines.append(f"  state     {st.state}  [{st.raw_state}]")
    if st.filename:
        prog = f"  {st.progress * 100:.1f}%" if st.progress is not None else ""
        lines.append(f"  file      {st.filename}{prog}")
    if st.temps:
        lines.append("  temps     " + "  ".join(f"{k} {temp(a)}/{temp(t)}" for k, (a, t) in st.temps.items()))
    if st.slots:
        heads = [f"{s.label} {s.color or '?'} {s.material or ''}".rstrip() if s.present else f"{s.label} empty"
                 for s in st.slots]
        lines.append("  heads     " + " | ".join(heads))
    if st.message:
        lines.append(f"  message   {st.message}")
    return "\n".join(lines)


def test_printer_status_text_is_byte_identical_to_main_for_normal_input() -> None:
    adapter = MoonrakerAdapter("u1", "192.168.0.26", 7125)
    for st in (
        PrinterStatus("printing", "printing", "base_1-1a2b3c4d5e6f7a8b.gcode", 0.4213,
                      {"extruder": (205.0, 210.0), "bed": (59.6, 60.0)},
                      message="Filament runout on T2", model="Snapmaker U1",
                      slots=[Slot(0, "T1", True, "#000000", "PLA"), Slot(1, "T2", False), Slot(2, "T3", True)]),
        PrinterStatus("idle", "standby", None, None, {}),
        PrinterStatus("error", "klippy:shutdown", None, None, {"nozzle": (None, None)}, message="Lost communication"),
    ):
        assert render_status("u1", adapter, st) == _main_render_status("u1", adapter, st)


def test_printer_status_text_strips_escapes_from_what_the_printer_reports() -> None:
    adapter = MoonrakerAdapter("u1", "127.0.0.1", 7125)
    st = PrinterStatus("printing", "printing\x1b[2J", "a\x07" + "F" * 9000, 0.5, {}, message="m\x1b]0;x\x07",
                       slots=[Slot(0, "T1", True, "\x1b[31m", "PLA\x9b")])
    out = render_status("u1", adapter, st)

    assert not any(c in out for c in ("\x1b", "\x07", "\x9b")) and max(map(len, out.splitlines())) <= 300


def test_an_oversized_slot_list_from_the_printer_cannot_flood_the_heads_line() -> None:
    from printer_ops.moonraker import parse_status

    st = parse_status({"print_stats": {"state": "standby"}, "print_task_config": {"filament_exist": [False] * 10_000}})
    out = render_status("u1", MoonrakerAdapter("u1", "127.0.0.1", 7125), st)
    heads = next(ln for ln in out.splitlines() if ln.lstrip().startswith("heads"))

    assert len(st.slots) == 10_000 and len(heads) <= 300
    assert "T16 empty" in heads and "T17" not in heads and heads.endswith("+9984 more")
