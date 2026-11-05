"""jobwatch_actions.py — what `3d printer watch NAME --job` DOES when the print is lost.

Order, because every second of a lost print costs filament and a failed print can hurt the hotend:
  1. save the frames and the findings (local files, instant);
  2. STOP the job: `adapter.pause()` or `adapter.cancel()`, after re-reading the printer and binding it to the
     job that was watched (printing/paused AND the same file; never another job, never an idle printer),
     once per job, verified in the printer state;
  3. RING the owner: `tg call me --say "<russian text>"`;
  4. the klippy.log lines of the job (`3d printer evidence`, GET only) into the same folder;
  5. TELL the owner: `tg --tag problem --photo FRAME "<text>"` (the frame that showed it).
Every step runs even when an earlier one failed: a failed cancel still calls, a failed call still messages.
The outcome (what was sent, what was verified, which step failed) is printed as one JSON line and saved as
`outcome.json` next to the evidence.

SAFETY: the only printer writes are PAUSE and CANCEL (Moonraker `printer/print/pause|cancel`); no G-code, no
axis, no resume, no start. `dry_run` sends NOTHING (printer or phone) and prints what it would; `tg_dry_run`
prints the two tg command lines instead of running them but still stops the job. Nothing here runs the real `tg`
under pytest (watch_sinks.run_real_tg refuses).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from printer_ops.base import (
    ACTIVE_JOB_STATES,
    CANCELLED,
    COMPLETE,
    IDLE,
    PAUSED,
    Adapter,
    PrinterCommError,
    PrinterRefused,
)
from printer_ops.jobwatch import Finding

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
STOPPED = {"pause": (PAUSED,), "cancel": (CANCELLED, IDLE, COMPLETE)}


@dataclass(frozen=True)
class Bound:
    """The job being watched: the printer asks for nothing else to be stopped."""

    printer: str
    filename: str
    job_id: str | None = None


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Outcome:
    printer: str
    job: str
    action: str  # pause | cancel | alert
    dry_run: bool
    findings: list[dict[str, Any]]
    evidence_dir: str
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def bind(adapter: Adapter) -> Bound:
    """Bind to the job printing now (file, and the history job id when the printer keeps one)."""
    st = adapter.status()
    if st.state not in ACTIVE_JOB_STATES or not st.filename:
        raise PrinterRefused(
            f"{adapter.name} is {st.state} [{st.raw_state}]: there is no job to watch",
            command="printer watch --job",
            remediation=[
                f"Start the job first: 3d printer start {adapter.name} FILE.gcode --yes"
            ],
        )
    job_id = None
    try:
        records = adapter.history(1)
        if (
            records
            and records[0].status == "in_progress"
            and records[0].filename == st.filename
        ):
            job_id = records[0].job_id
    except Exception:  # noqa: BLE001 — a printer without history still gets watched
        job_id = None
    return Bound(adapter.name, st.filename, job_id)


class Responder:
    def __init__(
        self,
        adapter: Adapter,
        bound: Bound,
        *,
        out_dir: Path,
        dry_run: bool = False,
        tg_dry_run: bool = False,
        runner: Runner | None = None,
        tg_binary: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        verify_s: float = 20.0,
        evidence_fn: Callable[[str, str | None, Path], str] | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.adapter, self.bound = adapter, bound
        self.out_dir, self.dry_run, self.tg_dry_run = (
            out_dir,
            dry_run,
            tg_dry_run or dry_run,
        )
        self.runner, self.tg_binary = runner, tg_binary
        self.clock, self.sleep, self.verify_s = clock, sleep, verify_s
        self.evidence_fn = evidence_fn or klippy_evidence
        self.echo = echo
        self.acted = False  # PAUSE/CANCEL goes out once per watch

    # -- 1. files

    def evidence_dir(self, now: float) -> Path:
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = self.out_dir / f"{stamp}-{self.bound.printer}-jobwatch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_files(
        self,
        d: Path,
        files: dict[str, str | None],
        findings: list[Finding],
        extra: dict[str, Any],
    ) -> str | None:
        """Copy the frames in, write finding.json; -> the path of the frame to send (annotated first)."""
        sent: str | None = None
        for name, src in files.items():
            if src and Path(src).is_file():
                shutil.copyfile(src, d / name)
                if sent is None and name.startswith("frame"):
                    sent = str(d / name)
        doc = {
            "printer": self.bound.printer,
            "job": self.bound.filename,
            "job_id": self.bound.job_id,
            "findings": [asdict(f) for f in findings],
            **extra,
        }
        (d / "finding.json").write_text(
            json.dumps(doc, indent=1, default=str) + "\n", encoding="utf-8"
        )
        return sent

    # -- 2. stop

    def stop(self, action: str, out: Outcome) -> None:
        if self.acted:
            out.add(action, False, "already acted on this job")
            return
        try:
            st = self.adapter.status()
        except PrinterCommError as exc:
            out.add(action, False, f"printer unreadable, nothing sent: {exc.message}")
            return
        why = self.refusal(action, st.state, st.filename)
        if why:
            out.add(action, False, why)
            return
        if self.dry_run:
            out.add(
                action, True, f"dry run: would send {action} to {self.bound.printer}"
            )
            return
        self.acted = True
        self.send(action, out)

    def refusal(self, action: str, state: str, filename: str | None) -> str | None:
        if state not in ACTIVE_JOB_STATES:
            return f"the printer is {state}: no job to {action}"
        if filename != self.bound.filename:
            return f"the printer runs {filename}, not the watched {self.bound.filename}: refused"
        if action == "pause" and state == PAUSED:
            return "already paused (by the printer)"
        return None

    def send(self, action: str, out: Outcome) -> None:
        try:
            (self.adapter.pause if action == "pause" else self.adapter.cancel)()
        except PrinterCommError as exc:
            out.add(action, False, f"the printer did not accept it: {exc.message}")
            return
        state = self.wait_for(STOPPED[action])
        out.add(
            action,
            state is not None,
            f"sent; the printer is now {state}"
            if state
            else f"sent, but the printer never showed {action}",
        )

    def wait_for(self, states: tuple[str, ...]) -> str | None:
        deadline = self.clock() + self.verify_s
        while True:
            try:
                state = self.adapter.status().state
            except PrinterCommError:
                state = ""
            if state in states:
                return state
            if self.clock() >= deadline:
                return None
            self.sleep(1.0)

    # -- 3. and 5. the phone

    def tg(self, argv: list[str], out: Outcome, name: str) -> bool:
        line = " ".join(_quote(a) for a in argv)
        if self.tg_dry_run:
            self.echo(json.dumps({"would_run": argv}, ensure_ascii=False))
            out.add(name, True, f"dry run: {line}")
            return True
        runner = self.runner or _real_runner()
        try:
            r = runner(argv, text=True, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            out.add(name, False, f"{line}: {exc}")
            return False
        ok = r.returncode == 0
        out.add(
            name,
            ok,
            line
            if ok
            else f"{line}: exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}",
        )
        return ok

    def call(self, text: str, out: Outcome) -> None:
        self.tg([self.tg_binary or "tg", "call", "me", "--say", text], out, "call")

    def message(self, text: str, frame: str | None, out: Outcome) -> None:
        binary = self.tg_binary or "tg"
        rich = [
            binary,
            "--tag",
            "problem",
            *(["--photo", frame] if frame else []),
            text,
        ]
        if not self.tg(rich, out, "message") and not self.tg_dry_run:
            self.tg([binary, text], out, "message_plain")

    # -- 4. klippy lines

    def job_evidence(self, d: Path, out: Outcome) -> None:
        if self.dry_run:
            out.add("klippy_evidence", True, "dry run: not fetched")
            return
        try:
            out.add(
                "klippy_evidence",
                True,
                self.evidence_fn(self.bound.printer, self.bound.job_id, d),
            )
        except Exception as exc:  # noqa: BLE001 — partial evidence is kept, the alerts still go out
            out.add("klippy_evidence", False, f"{exc.__class__.__name__}: {exc}")

    # -- all of it

    def respond(
        self,
        action: str,
        findings: list[Finding],
        *,
        layer: int | None,
        files: dict[str, str | None],
        extra: dict[str, Any],
    ) -> Outcome:
        now = self.clock()
        d = self.evidence_dir(now)
        out = Outcome(
            self.bound.printer,
            self.bound.filename,
            action,
            self.dry_run,
            [asdict(f) for f in findings],
            str(d),
        )
        frame = self.save_files(d, files, findings, extra)
        out.add("evidence_files", True, str(d))
        if action in ("pause", "cancel"):
            self.stop(action, out)
        text = owner_text(self.bound, action, findings, layer, out)
        if action != "alert":
            self.call(text, out)
        if action in ("pause", "cancel"):
            self.job_evidence(d, out)
        self.message(text + f"\nEvidence: {d}", frame, out)
        (d / "outcome.json").write_text(
            json.dumps(out.to_json(), indent=1, default=str) + "\n", encoding="utf-8"
        )
        self.echo(
            json.dumps({"jobwatch": out.to_json()}, ensure_ascii=False, default=str)
        )
        return out


def owner_text(
    bound: Bound, action: str, findings: list[Finding], layer: int | None, out: Outcome
) -> str:
    """The call/message, in Russian (the owner's language): what is lost, what was done, what to do."""
    stopped = any(
        s.name == action and s.ok and not s.detail.startswith("dry run")
        for s in out.steps
    )
    where = f", слой {layer}" if layer is not None else ""
    what = "; ".join(f.text for f in findings[:2])
    if action == "alert":
        return (
            f"{bound.printer}: печать {bound.filename}{where}: внимание. {what}. "
            "Принтер не трогал, посмотрите на кадр."
        )
    did = {
        "cancel": "Печать ОТМЕНЕНА"
        if stopped
        else "Печать нужно ОТМЕНИТЬ (отмена не подтвердилась)",
        "pause": "Печать на ПАУЗЕ"
        if stopped
        else "Печать нужно поставить на паузу (пауза не подтвердилась)",
    }[action]
    return f"{bound.printer}: печать {bound.filename} провалена{where}. {what}. {did}. Подойдите к принтеру."


def klippy_evidence(printer: str, job_id: str | None, d: Path) -> str:
    """`3d printer evidence` of the job (GET only) into `d`; -> its one-line summary."""
    import contextlib
    import io

    from printer_ops.evidence import run as evidence

    argv = [printer, "--out", str(d)] + (["--job", job_id] if job_id else ["--last"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = evidence(argv)
    return f"rc {rc}: " + (buf.getvalue().strip().splitlines() or ["evidence saved"])[0]


def _real_runner() -> Runner:
    from printer_ops.watch_sinks import run_real_tg

    return run_real_tg


def _quote(a: str) -> str:
    return (
        a
        if a and all(c.isalnum() or c in "-_./:=@" for c in a)
        else json.dumps(a, ensure_ascii=False)
    )


def find_tg() -> str | None:
    from printer_ops.watch_sinks import find_tool

    return find_tool("tg")


__all__ = [
    "Bound",
    "Outcome",
    "Responder",
    "bind",
    "find_tg",
    "klippy_evidence",
    "owner_text",
]
