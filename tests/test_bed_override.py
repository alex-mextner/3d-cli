"""The owner's recorded word that the bed is empty (issue #723, item 5): `3d printer bed check NAME
--assume-empty "why"` (lib/printer_ops/bed_override.py), and what the `bed` line of the preflight does with it.

The record is bound to the job the printer is at (Moonraker's newest history job, print_stats.filename), to a ttl
and to an idle printer; it never overrides a printing printer. The printer is an in-process fake; the camera is
not asked by the preflight line once a valid confirmation stands.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from printer_ops import bed_override as O
import printer_ops.preflight as preflight
from printer_ops.base import JobRecord
from test_bed_view import device

NOW = 1_800_000_000.0
MIN = 60.0
WATCH = {
    "bed": {"enabled": True, "auto_view": True},
    "vision": {"camera": {"kind": "moonraker-monitor"}},
}


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("USER", "alex")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Printer:
    """The adapter surface the override reads: print_stats and the newest history job."""

    def __init__(
        self,
        state: str = "complete",
        filename: str = "chekhov.gcode",
        job: str | None = "000070",
    ) -> None:
        self.state, self.filename, self.job = state, filename, job
        self.fail: Exception | None = None

    def query(self, objects: str) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return {"print_stats": {"state": self.state, "filename": self.filename}}

    def history(self, limit: int) -> list[JobRecord]:
        return (
            []
            if self.job is None
            else [
                JobRecord(
                    self.filename, "completed", start_time=NOW - 7200, job_id=self.job
                )
            ]
        )


class NoHistory:
    """An adapter of a printer without the history component."""

    def __init__(self, filename: str = "chekhov.gcode") -> None:
        self.filename = filename

    def query(self, objects: str) -> dict[str, Any]:
        return {"print_stats": {"state": "complete", "filename": self.filename}}


OCCUPIED: dict[str, Any] = {
    "printer": "u1",
    "bed": "occupied",
    "plate": "present",
    "z": 70.0,
    "reason": "16.0% of the bed view differs",
    "metrics": {"changed": 0.16},
    "frame": "/cache/u1-status-occupied.jpg",
    "raw_frame": "/cache/u1-status-occupied.raw.jpg",
    "light": {"on_for_frame": True, "text": "light on"},
    "occupied_confident": True,
    "source": "camera now",
}


def confirm(
    why: str = "I looked: the plate is empty", ttl: float = 60.0, **kw: Any
) -> dict[str, Any]:
    printer = kw.pop("adapter", Printer())
    return O.confirm_empty(
        device(),
        kw.pop("verdict", OCCUPIED),
        why=why,
        ttl_min=ttl,
        adapter=printer,
        now=NOW,
        **kw,
    )


# ---- the record -----------------------------------------------------------------------------------------------


def test_the_confirmation_is_recorded_next_to_the_frames_and_in_a_small_state_record() -> (
    None
):
    res = confirm()
    assert (
        res["recorded"] is True
        and res["ttl_min"] == 60.0
        and res["until"] == NOW + 3600
    )
    evidence = json.loads(Path(res["evidence"]).read_text(encoding="utf-8"))
    assert (
        Path(res["evidence"]).parent.name == "bed"
        and Path(res["evidence"]).parent.parent.name == "vision"
    )
    assert (
        Path(res["evidence"]).name.startswith("assume-empty-")
        and Path(res["evidence"]).suffix == ".json"
    )
    assert (evidence["who"], evidence["why"], evidence["printer"]) == (
        "alex",
        "I looked: the plate is empty",
        "u1",
    )
    assert evidence["ts"] == NOW
    assert evidence["when"]
    assert evidence["verdict"] == {
        "bed": "occupied", "plate": "present", "reason": OCCUPIED["reason"], "metrics": {"changed": 0.16},
    }  # fmt: skip
    assert evidence["frames"] == {
        "annotated": OCCUPIED["frame"],
        "raw": OCCUPIED["raw_frame"],
    }
    assert (
        evidence["z"] == 70.0
        and evidence["light"]["on_for_frame"] is True
        and evidence["ttl_min"] == 60.0
    )
    assert evidence["job_marker"] == {
        "job_id": "000070",
        "start_time": NOW - 7200,
        "filename": "chekhov.gcode",
    }
    state = json.loads(O.assumed_path("u1").read_text(encoding="utf-8"))
    assert (state["ts"], state["why"], state["ttl"]) == (
        NOW,
        "I looked: the plate is empty",
        3600.0,
    )
    assert (
        state["job_marker"] == evidence["job_marker"]
        and state["evidence"] == res["evidence"]
    )
    assert (
        O.assumed_path("u1").name == "u1.assumed.json"
        and O.assumed_path("u1").parent.name == "bed"
    )


def test_two_confirmations_in_one_second_get_two_evidence_files() -> None:
    a, b = confirm()["evidence"], confirm()["evidence"]
    assert a != b and Path(a).is_file() and Path(b).is_file()


@pytest.mark.parametrize("why", ["", "   ", "\n"])
def test_an_empty_reason_is_refused_and_records_nothing(why: str) -> None:
    with pytest.raises(ValueError, match="needs the reason"):
        confirm(why=why)
    assert not O.assumed_path("u1").exists()


def test_a_ttl_that_is_not_positive_is_refused() -> None:
    for ttl in (0.0, -5.0, float("nan")):
        with pytest.raises(ValueError, match="positive"):
            confirm(ttl=ttl)


@pytest.mark.parametrize("state", ["printing", "paused", "busy"])
def test_a_printing_printer_is_never_confirmed(state: str) -> None:
    res = confirm(adapter=Printer(state))
    assert (
        res["recorded"] is False
        and state in res["refused"]
        and not O.assumed_path("u1").exists()
    )
    busy = confirm(verdict={**OCCUPIED, "bed": "printing"})
    assert busy["recorded"] is False and "printing" in busy["refused"]


def test_a_camera_that_sees_the_plate_missing_is_not_confirmed() -> None:
    res = confirm(verdict={**OCCUPIED, "plate": "missing"})
    assert res["recorded"] is False and "plate missing" in res["refused"]


def test_an_unreadable_printer_cannot_bind_the_confirmation_to_its_job() -> None:
    p = Printer()
    p.fail = OSError("connection refused")
    res = confirm(adapter=p)
    assert (
        res["recorded"] is False
        and "connection refused" in res["refused"]
        and not O.assumed_path("u1").exists()
    )


# ---- when the preflight may rely on it -----------------------------------------------------------------------------


def test_a_fresh_confirmation_on_the_same_job_is_honoured_with_its_note() -> None:
    confirm()
    conf, why = O.honoured("u1", Printer(), NOW + 10 * MIN)
    assert why is None and conf is not None
    note = conf.note(NOW + 10 * MIN)
    assert (
        note.startswith("owner confirmed empty at ")
        and "I looked: the plate is empty" in note
    )
    assert "alex" in note and "50 more min" in note


def test_there_is_nothing_to_honour_without_a_record() -> None:
    assert O.honoured("u1", Printer(), NOW) == (None, None)


def test_the_confirmation_expires_with_its_ttl() -> None:
    confirm(ttl=30.0)
    assert O.honoured("u1", Printer(), NOW + 29 * MIN)[0] is not None
    conf, why = O.honoured("u1", Printer(), NOW + 31 * MIN)
    assert conf is None and "expired" in str(why) and "30 min ttl" in str(why)
    conf, why = O.honoured(
        "u1", Printer(), NOW - MIN
    )  # a clock that went backwards is no basis either
    assert conf is None and "expired" in str(why)


def test_a_new_job_since_ends_the_confirmation() -> None:
    confirm()
    conf, why = O.honoured("u1", Printer(job="000071"), NOW + MIN)
    assert (
        conf is None
        and "a new job ran since" in str(why)
        and "000070 -> 000071" in str(why)
    )
    # the SAME file printed again is a new history job too
    assert (
        O.honoured("u1", Printer(job="000072", filename="chekhov.gcode"), NOW + MIN)[0]
        is None
    )


def test_without_a_history_the_file_name_tells_a_new_job_and_the_ttl_ends_the_rest() -> (
    None
):
    O.record(
        "u1",
        why="empty",
        ttl_min=60.0,
        marker=O.read_marker(NoHistory()),
        verdict=OCCUPIED,
        now=NOW,
    )
    assert O.honoured("u1", NoHistory(), NOW + MIN)[0] is not None
    conf, why = O.honoured("u1", NoHistory("other.gcode"), NOW + MIN)
    assert conf is None and "another file was printed" in str(why)


@pytest.mark.parametrize("state", ["printing", "paused", "busy"])
def test_it_never_overrides_a_printing_printer(state: str) -> None:
    confirm()
    conf, why = O.honoured("u1", Printer(state), NOW + MIN)
    assert conf is None and "never applies to a running job" in str(why)


def test_an_unreadable_printer_or_a_damaged_record_does_not_count() -> None:
    confirm()
    p = Printer()
    p.fail = OSError("down")
    conf, why = O.honoured("u1", p, NOW + MIN)
    assert conf is None and "could not be read" in str(why)
    O.assumed_path("u1").write_text('{"ts": "x"}', encoding="utf-8")
    assert "malformed" in str(O.honoured("u1", Printer(), NOW + MIN)[1])
    O.assumed_path("u1").write_text("not json", encoding="utf-8")
    assert O.honoured("u1", Printer(), NOW + MIN) == (None, None)


def test_a_record_made_while_the_plate_was_missing_never_counts() -> None:
    O.record(
        "u1",
        why="empty",
        ttl_min=60.0,
        marker=O.read_marker(Printer()),
        verdict={**OCCUPIED, "plate": "missing"},
        now=NOW,
    )
    conf, why = O.honoured("u1", Printer(), NOW + MIN)
    assert conf is None and "plate missing" in str(why)


def test_the_record_of_one_printer_is_not_another_printers() -> None:
    confirm()
    assert O.honoured("k1se", Printer(), NOW + MIN) == (None, None)


# ---- the `bed` line of the preflight ---------------------------------------------------------------------------------


def runner(verdict: dict[str, Any], calls: list[list[str]]) -> Any:
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(verdict), "")

    return run


def bed_line(
    printer: Printer,
    verdict: dict[str, Any],
    calls: list[list[str]],
    now: float = NOW + 5 * MIN,
) -> preflight.Check:
    return preflight.check_bed(
        device(watch=WATCH),
        runner=runner(verdict, calls),
        now=now,
        adapter_for=lambda d: printer,
    )


def test_the_refusal_names_the_command_that_confirms_an_empty_bed() -> None:
    calls: list[list[str]] = []
    fail = bed_line(Printer(), OCCUPIED, calls)
    assert (
        fail.status == preflight.FAIL
        and '3d printer bed check u1 --assume-empty "' in fail.detail
    )
    suspected = bed_line(Printer(), {**OCCUPIED, "occupied_confident": False}, calls)
    assert (
        suspected.status == preflight.WARN
        and '3d printer bed check u1 --assume-empty "' in suspected.detail
    )
    unknown = bed_line(
        Printer(), {**OCCUPIED, "bed": "unknown", "occupied_confident": False}, calls
    )
    assert unknown.status == preflight.WARN and "--assume-empty" in unknown.detail


def test_a_valid_confirmation_makes_the_line_pass_with_the_note_and_the_camera_is_not_asked() -> (
    None
):
    confirm()
    calls: list[list[str]] = []
    line = bed_line(Printer(), OCCUPIED, calls)
    assert line.status == preflight.PASS and calls == []
    assert (
        line.detail.startswith("owner confirmed empty at ")
        and "I looked: the plate is empty" in line.detail
    )


def test_the_line_fails_again_when_the_confirmation_expired_or_a_job_started() -> None:
    confirm(ttl=30.0)
    calls: list[list[str]] = []
    expired = bed_line(Printer(), OCCUPIED, calls, now=NOW + 31 * MIN)
    assert expired.status == preflight.FAIL and len(calls) == 1
    assert any("expired" in line for line in expired.lines)
    confirm()
    newer = bed_line(Printer(job="000071"), OCCUPIED, calls, now=NOW + MIN)
    assert newer.status == preflight.FAIL and any(
        "a new job ran since" in line for line in newer.lines
    )


def test_the_line_never_passes_on_the_owners_word_while_a_job_runs() -> None:
    confirm()
    calls: list[list[str]] = []
    printing = {
        "printer": "u1",
        "bed": "printing",
        "plate": "unknown",
        "reason": "printing: bed not moved",
        "z": 3.0,
    }
    line = bed_line(Printer("printing"), printing, calls, now=NOW + MIN)
    assert (
        line.status == preflight.WARN
        and "owner confirmed" not in line.detail
        and len(calls) == 1
    )
    assert any("never applies to a running job" in text for text in line.lines)


def test_a_printer_with_the_bed_check_off_is_still_skipped() -> None:
    confirm()
    off = preflight.check_bed(
        device(watch={}),
        runner=runner(OCCUPIED, []),
        now=NOW + MIN,
        adapter_for=lambda d: Printer(),
    )
    assert off.status == preflight.SKIP
