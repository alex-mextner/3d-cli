"""After a print ends the operator saves its evidence with the real `3d printer evidence`, against a loopback
Moonraker that serves the real Chekhov klippy.log of 2026-10-24 (the failed head pickup at layer 455)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from printer_evidence_fakes import (
    READ_AT,
    REAL_STOP,
    FakeEvidenceMoonraker,
    fixture_gcode,
    fixture_position,
    job_for,
    shifted_log,
    write_u1,
)
from printer_fakes import free_port

from .workflow_helper import run_cli


def _cli(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """The job started 17:24 UTC: its file name carries the start day in the host's local time."""
    return run_cli(tmp_path, *argv, env_extra={"TZ": "UTC"})


def _fake() -> FakeEvidenceMoonraker:
    gcode = fixture_gcode()
    running = job_for(
        gcode,
        job_id="000071",
        status="in_progress",
        end_time=None,
        filename="next.gcode",
    )
    return FakeEvidenceMoonraker(
        jobs=[running, job_for(gcode)],
        klippy=shifted_log(),
        klippy_date="Sat, 24 Oct 2026 10:58:00 GMT",
    )


def _log_copy(tmp_path: Path) -> Path:
    """What an operator saved earlier: klippy.log with the mtime of the moment it was read."""
    log = tmp_path / "klippy.log.copy"
    log.write_text(shifted_log(), encoding="utf-8")
    os.utime(log, (READ_AT.timestamp(), READ_AT.timestamp()))
    return log


def test_help_lists_the_subcommand_with_its_examples(tmp_path: Path) -> None:
    """Before the first print ends the operator looks the command up in both help screens.

    Both name `evidence` and its flags with a runnable example; no arguments print usage and exit 1.
    """
    own = _cli(tmp_path, "printer", "evidence", "--help")
    top = _cli(tmp_path, "printer", "--help")
    bare = _cli(tmp_path, "printer", "evidence")
    assert own.returncode == 0
    assert bare.returncode == 1
    assert bare.stdout == own.stdout
    for flag in (
        "--job ID",
        "--last",
        "--out DIR",
        "--gcode FILE",
        "--note TEXT",
        "--klippy-log FILE",
        "--job-file FILE",
        "--context N",
        "--max-lines N",
        "--secs S",
    ):
        assert flag in own.stdout, flag
    assert "3d printer evidence u1 --out projects/chekhov/print/runs" in own.stdout
    assert "evidence NAME [--job ID | --last] [--out DIR]" in top.stdout


def test_evidence_of_a_cancelled_print_is_saved_with_the_layer_of_the_failure(
    tmp_path: Path,
) -> None:
    """The head failed its pickup, the print sat paused for hours and was cancelled. Next morning the operator saves the evidence.

    The run writes `<date>-cancelled.md` and `.json`; the report names the pickup failure, layer 455 of 644 and
    the top z of the last complete layer to cut at; the printer only ever saw GET requests.
    """
    gcode = tmp_path / "chekhov.gcode"
    shutil.copyfile(fixture_gcode(), gcode)
    runs = tmp_path / "projects" / "chekhov" / "print" / "runs"
    with _fake().serve() as fake:
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(
            tmp_path,
            "printer",
            "evidence",
            "u1",
            "--job",
            "000070",
            "--out",
            str(runs),
            "--gcode",
            str(gcode),
            "--note",
            "T2 head failed its pickup, I replaced the nut",
        )
    assert result.returncode == 0, result.stderr
    md = (runs / "2026-10-23-cancelled.md").read_text(encoding="utf-8")
    saved = json.loads((runs / "2026-10-23-cancelled.json").read_text(encoding="utf-8"))
    assert "pickup failed, detected that extruder1 is detached. err-sta: ['1-(FFF)']" in md
    assert "layer 455 of 644, z 65.798" in md
    assert "last complete layer 454, top z 65.698" in md
    assert "Operator note: T2 head failed its pickup, I replaced the nut" in md
    assert "2026-10-24 03:05:19.339 UTC" in md
    assert "Printer state at capture: `standby`" in md
    assert saved["job"]["job_id"] == "000070"
    assert saved["problems"] == []
    assert "layer 455 of 644" in result.stdout
    assert str(runs / "2026-10-23-cancelled.md") in result.stdout
    assert fake.methods() == {"GET"}
    assert ("GET", "/server/history/job?uid=000070") in fake.requests
    assert ("GET", "/server/files/logs/klippy.log") in fake.requests


def test_last_skips_the_job_that_is_still_printing(tmp_path: Path) -> None:
    """A new print has already started when the operator saves the evidence of the previous one.

    Without --job the newest FINISHED job is saved, not the running one; the report goes to stdout when --out is absent.
    """
    with _fake().serve() as fake:
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--last")
    assert result.returncode == 0, result.stderr
    assert "Moonraker job `000070`" in result.stdout
    assert "000071" not in result.stdout
    assert "nothing written" in result.stderr
    assert not (tmp_path / "2026-10-23-cancelled.md").exists()


def test_a_gcode_that_is_not_the_printed_file_is_mapped_but_flagged_and_exits_1(
    tmp_path: Path,
) -> None:
    """The operator passes the wrong slice of the part. The layer would be wrong, so the report says so loudly.

    Everything is still written (evidence is never dropped) but the run exits 1 and names the MISMATCH.
    """
    wrong = tmp_path / "other.gcode"
    wrong.write_bytes(fixture_gcode().read_bytes() + b"; one more line\n")  # another hash than the printed file
    runs = tmp_path / "runs"
    with _fake().serve() as fake:
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(
            tmp_path,
            "printer",
            "evidence",
            "u1",
            "--job",
            "000070",
            "--out",
            str(runs),
            "--gcode",
            str(wrong),
        )
    assert result.returncode == 1
    assert "not the printed file" in result.stderr
    md = (runs / "2026-10-23-cancelled.md").read_text(encoding="utf-8")
    assert "**MISMATCH**" in md
    assert "MISSING / WRONG" in md


def test_an_unreachable_printer_still_leaves_the_evidence_that_was_given(
    tmp_path: Path,
) -> None:
    """The printer was power-cycled and does not answer; the operator saved its log earlier.

    The log copy is dated from its mtime and written up with the missing job marked; the run exits 1 and says what to rerun.
    """
    runs = tmp_path / "runs"
    write_u1(tmp_path / "xdg-config", free_port())
    result = _cli(
        tmp_path,
        "printer",
        "evidence",
        "u1",
        "--klippy-log",
        str(_log_copy(tmp_path)),
        "--out",
        str(runs),
        "--secs",
        "2",
    )
    assert result.returncode == 1
    assert "history job not collected" in result.stderr
    text = next(runs.glob("*-unknown-*.md")).read_text(encoding="utf-8")
    assert "MISSING / WRONG" in text
    assert "2026-10-24 03:05:19.339 UTC" in text


def test_saved_copies_need_no_printer_at_all(tmp_path: Path) -> None:
    """Weeks later the printer is gone, but the job JSON and the log copy are in the project.

    The report is rebuilt from the two files with no printer configured and no network.
    """
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"result": {"job": job_for(fixture_gcode())}}), encoding="utf-8")
    runs = tmp_path / "runs"
    result = _cli(
        tmp_path,
        "printer",
        "evidence",
        "u1",
        "--job-file",
        str(job),
        "--klippy-log",
        str(_log_copy(tmp_path)),
        "--gcode",
        str(fixture_gcode()),
        "--out",
        str(runs),
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads((runs / "2026-10-23-cancelled.json").read_text(encoding="utf-8"))
    first = [p for p in saved["gcode"]["positions"] if p["file_position"] == fixture_position(REAL_STOP)]
    assert [(p["layer"], p["last_complete_z"]) for p in first] == [(455, 65.698)]


def test_a_job_the_history_does_not_have_writes_nothing_and_exits_2(
    tmp_path: Path,
) -> None:
    """A typo in the job id must not leave a junk file in the project's runs directory.

    The printer says it has no such job: exit 2 with the way to list the jobs, and no file.
    """
    runs = tmp_path / "runs"
    with _fake().serve() as fake:
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--job", "00070", "--out", str(runs))
    assert result.returncode == 2
    assert "history has no job 00070" in result.stderr
    assert "3d printers show u1 --history 20" in result.stderr
    assert not runs.exists()


def test_a_printer_without_klippy_log_still_saves_the_job_and_says_what_is_missing(
    tmp_path: Path,
) -> None:
    """Moonraker answers the history but the log was already rotated away (404).

    The job record is saved, the report says klippy.log is missing, and the run exits 1.
    """
    runs = tmp_path / "runs"
    fake = _fake()
    fake.klippy_status = 404
    with fake.serve():
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--job", "000070", "--out", str(runs))
    assert result.returncode == 1
    md = (runs / "2026-10-23-cancelled.md").read_text(encoding="utf-8")
    assert "klippy.log not collected" in md
    assert "HTTP 404" in md
    assert '"job_id": "000070"' in md


def test_bad_input_is_refused_before_any_request(tmp_path: Path) -> None:
    """A typo in a flag or a missing file must not reach the printer.

    Usage errors exit 2 with a pointer to --help; an unknown G-code path names the path; two ways to name the job conflict.
    """
    job = tmp_path / "job.json"
    job.write_text("{}", encoding="utf-8")
    with _fake().serve() as fake:
        write_u1(tmp_path / "xdg-config", fake.port)
        both = _cli(tmp_path, "printer", "evidence", "u1", "--job", "000070", "--last")
        nogcode = _cli(
            tmp_path,
            "printer",
            "evidence",
            "u1",
            "--gcode",
            str(tmp_path / "missing.gcode"),
        )
        negative = _cli(tmp_path, "printer", "evidence", "u1", "--context", "-1")
        twice = _cli(
            tmp_path,
            "printer",
            "evidence",
            "u1",
            "--job",
            "000070",
            "--job-file",
            str(job),
        )
    assert both.returncode == 2
    assert "not allowed with" in both.stderr
    assert nogcode.returncode == 2
    assert "missing.gcode" in nogcode.stderr
    assert negative.returncode == 2
    assert "--context must be >= 0" in negative.stderr
    assert twice.returncode == 2
    assert "--job and --job-file both name the job" in twice.stderr
    assert fake.requests == []


def test_a_creality_printer_has_no_moonraker_history_and_is_refused(
    tmp_path: Path,
) -> None:
    """The operator names the K1 SE, whose stock firmware has no Moonraker job history or klippy.log.

    The command says so and exits 2 before any request; the config is only read.
    """
    d = tmp_path / "xdg-config" / "3d-cli"
    d.mkdir(parents=True)
    (d / "printer-devices.yaml").write_text(
        f"k1se:\n  adapter: creality-stock\n  host: 127.0.0.1\n  port: {free_port()}\n  ws_port: {free_port()}\n",
        encoding="utf-8",
    )
    result = _cli(tmp_path, "printer", "evidence", "k1se", "--out", str(tmp_path / "runs"))
    assert result.returncode == 2
    assert "creality-stock printer: it has no Moonraker job history or klippy.log" in result.stderr


def test_a_log_fetched_days_after_it_was_last_written_is_dated_by_its_last_modified(tmp_path: Path) -> None:
    """The print failed on the 24th and the operator saves the evidence on the 26th. The idle printer wrote nothing since.

    The excerpt dates its lines from the log's Last-Modified, not from the day of the request.
    """
    runs = tmp_path / "runs"
    fake = _fake()
    fake.klippy_date = "Mon, 26 Oct 2026 11:00:00 GMT"
    fake.klippy_last_modified = "Sat, 24 Oct 2026 10:41:15 GMT"
    with fake.serve():
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--job", "000070", "--out", str(runs))
    assert result.returncode == 0, result.stderr
    md = (runs / "2026-10-23-cancelled.md").read_text(encoding="utf-8")
    assert "First fault in klippy.log: 2026-10-24 03:05:19.339 UTC" in md


def test_a_malformed_history_answer_is_a_missing_job_not_a_traceback(tmp_path: Path) -> None:
    """A proxy in front of Moonraker answers 200 with a body that has no job.

    The log is still saved, the report says the job could not be read, and there is no traceback.
    """
    runs = tmp_path / "runs"
    fake = _fake()
    fake.job_reply = {"result": None}
    with fake.serve():
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--job", "000070", "--out", str(runs))
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "history job not collected" in result.stderr
    assert "2026-10-24 03:05:19.339 UTC" in next(runs.glob("*-unknown-*.md")).read_text(encoding="utf-8")


def test_a_404_that_is_not_about_the_job_is_a_missing_history_not_a_wrong_job_id(tmp_path: Path) -> None:
    """The printer has no history component (or a proxy answers 404): the operator's job id is not the problem.

    The report says the history could not be read (exit 1, the log is still saved) instead of blaming the id (exit 2).
    """
    runs = tmp_path / "runs"
    fake = _fake()
    fake.job_status = 404
    fake.job_message = "Not Found"
    with fake.serve():
        write_u1(tmp_path / "xdg-config", fake.port)
        result = _cli(tmp_path, "printer", "evidence", "u1", "--job", "000070", "--out", str(runs))
    assert result.returncode == 1
    assert "history job not collected" in result.stderr
    assert "has no job" not in result.stderr
