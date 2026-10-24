"""vision_cycle.py — ONE poll cycle of `3d printer vision`, with every collaborator injected.

WHAT: `VisionCycle.cycle()` is the body of the live loop in lib/vision_detect.py `run_live`, lifted out so
  the supervision rules are testable without a camera, a model or a printer:
    adapter.status -> monitor.set_job -> reset_print (a new print) -> camera keepalive + grab ONLY while
    printing -> print-context tap -> monitor.step -> the printer's own detector (NPU tap) -> the status
    file -> dispatcher.process(events).
  `run_live` builds the real collaborators and loops `cycle()` + sleep(`next_delay_s`).

HOW A JOB IS FOUND: the cycle only ever asks the printer what it is doing (`adapter.status()`); it never
  reads who started the print. A job begun from `3d printer start`, Fluidd/Mainsail, the touchscreen,
  Orca/Creality Print, Home Assistant or a G-code macro is the same state change (idle -> printing) and is
  supervised from its first poll (#641). `VisionMonitor.set_job` tells a NEW print from a pause/resume or a
  network blip (job identity: file, state before, progress, how long the printer was gone).

A PRINTER THAT DOES NOT ANSWER (#640): a `PrinterCommError` from `adapter.status()` is not an alarm and not
  a crash. The cycle records `unreachable_since` (first failure), keeps writing the status file
  (`reachable: false`), logs ONE stderr line when the printer goes away and ONE when it is back (never one
  per cycle; none at all while the `NAME.intentional-off` marker is set), polls at most every UNREACHABLE_MAX_S
  and touches neither the camera nor the printer's detector (a switched-off printer has nothing to look at;
  a half-open camera stream must not raise "camera stale" for a printer that is off). The first answer after
  a start or an outage clears the marker (`clear_quiet`).

INVARIANTS: stdlib only (no numpy / cv2 / onnxruntime: the scorer behind `monitor` and `reset_print` is
  injected); every error but a `PrinterCommError` propagates; nothing here pauses, cancels or changes a
  printer: events go to the dispatcher, whose AutoPauser alone may pause.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from printer_ops import intentional_off
from printer_ops.base import PAUSED, PRINTING, Adapter, PrinterCommError
from printer_ops.vision import (
    Frame,
    FrameScore,
    NpuVerdict,
    PrintContext,
    VisionMonitor,
)
from printer_ops.vision_status import VisionStatus

UNREACHABLE_MAX_S = (
    30.0  # poll an unreachable printer at least this often: supervision resumes fast
)
MIN_DELAY_S = 0.5


class Camera(Protocol):
    def keepalive(self, now: float | None = None) -> bool: ...

    def grab(self) -> Frame: ...


class ContextTap(Protocol):
    def poll(self) -> PrintContext: ...


class NpuTap(Protocol):
    def poll(self) -> list[NpuVerdict]: ...


class Processor(Protocol):
    def process(self, batch: list[Any]) -> object: ...


@dataclass
class CycleReport:
    """What one cycle saw. `error` is the status poll's failure (the printer did not answer)."""

    state: str | None = None
    file: str | None = None
    error: str | None = None
    camera_error: str | None = None
    context_error: str | None = None
    npu_error: str | None = None
    z: float | None = None
    frame: Frame | None = None
    score: FrameScore | None = None
    events: list[Any] = field(default_factory=list)
    reachable: bool = True
    new_print: bool = False
    next_delay_s: float = MIN_DELAY_S

    def as_json(self, printer: str) -> dict[str, Any]:
        """The dict `3d printer vision --once` / `--verbose` print (key order is part of the output)."""
        out: dict[str, Any] = {"printer": printer}
        if self.error is not None:
            out["error"] = self.error
        else:
            out.update(state=self.state, file=self.file)
        if self.camera_error is not None:
            out["camera_error"] = self.camera_error
        if self.z is not None:
            out["z"] = round(self.z, 2)
        if self.context_error is not None:
            out["context_error"] = self.context_error
        if self.npu_error is not None:
            out["npu_error"] = self.npu_error
        return out


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class VisionCycle:
    """The cycle of one printer; `cycle()` is called every `interval_s` by the loop."""

    def __init__(
        self,
        printer: str,
        *,
        adapter: Adapter,
        camera: Camera,
        monitor: VisionMonitor,
        reset_print: Callable[[], None],
        interval_s: float,
        context_tap: ContextTap | None = None,
        set_context: Callable[[PrintContext | None], None] | None = None,
        npu_tap: NpuTap | None = None,
        status: VisionStatus | None = None,
        dispatcher: Processor | None = None,
        clock: Callable[[], float] = time.time,
        quiet: Callable[[], bool] | None = None,
        clear_quiet: Callable[[], object] | None = None,
        log: Callable[[str], None] = _stderr,
        force_frame: bool = False,
        after_keepalive: Callable[[], None] | None = None,
    ) -> None:
        """`force_frame`: grab a frame even when nothing prints or the printer is silent (`--once`);
        `after_keepalive`: called when a keepalive was sent in such a cycle (`--once` waits for the first
        fresh frame of a sleeping camera). `quiet` / `clear_quiet` default to the printer's
        intentional-off marker (printer_ops.intentional_off)."""
        self.printer = printer
        self.adapter = adapter
        self.camera = camera
        self.monitor = monitor
        self.reset_print = reset_print
        self.interval_s = interval_s
        self.context_tap = context_tap
        self.set_context = set_context
        self.npu_tap = npu_tap
        self.status = status
        self.dispatcher = dispatcher
        self.clock = clock
        self.quiet = quiet or (lambda: intentional_off.is_set(printer))
        self.clear_quiet = clear_quiet or (
            lambda: intentional_off.clear_marker(printer)
        )
        self.log = log
        self.force_frame = force_frame
        self.after_keepalive = after_keepalive
        self.reachable: bool | None = None  # None until the first poll
        self.unreachable_since: float | None = None
        self.last_poll_ok: float | None = None
        self.last_frame_ts: float | None = None
        self.job_file: str | None = None
        self.job_started: float | None = None

    def cycle(self) -> CycleReport:
        t0 = self.clock()
        rep = CycleReport()
        try:
            st = self.adapter.status()
        except PrinterCommError as exc:
            rep.reachable, rep.error = False, str(exc)
            self._lost(t0, str(exc))
        else:
            self._answered(t0)
            rep.new_print = self.monitor.set_job(st.state, st.filename, st.progress, t0)
            if rep.new_print:
                self.reset_print()  # the same file printed again is a new print too
            rep.state, rep.file = st.state, st.filename
            self._track_job(st.state, st.filename, rep.new_print, t0)
        printing = rep.reachable and self.monitor.printing
        if printing or self.force_frame:
            self._grab(rep, t0)
        if rep.reachable:
            rep.score, rep.events = self.monitor.step(rep.frame)
            self._npu(rep)
        self._write_status(rep, printing=bool(printing))
        if rep.events and self.dispatcher is not None:
            self.dispatcher.process(rep.events)
        interval = (
            self.interval_s
            if rep.reachable
            else min(self.interval_s, UNREACHABLE_MAX_S)
        )
        rep.next_delay_s = max(MIN_DELAY_S, interval - (self.clock() - t0))
        return rep

    # ---- reachability ---------------------------------------------------------------------

    def _lost(self, now: float, why: str) -> None:
        self.monitor.lost_contact(now)
        if self.reachable is False:
            return
        self.reachable = False
        self.unreachable_since = now
        if not self.quiet():
            self.log(
                f"{self.printer}: printer does not answer ({why}); polling every <= {UNREACHABLE_MAX_S:g} s"
            )

    def _answered(self, now: float) -> None:
        was, since = self.reachable, self.unreachable_since
        self.reachable, self.unreachable_since, self.last_poll_ok = True, None, now
        if was is True:
            return
        if was is False and since is not None and not self.quiet():
            self.log(
                f"{self.printer}: printer answers again (unreachable for {now - since:.0f} s)"
            )
        self.clear_quiet()  # the first answer after a start or an outage: "switched off on purpose" is over

    def _track_job(
        self, state: str, filename: str | None, new_print: bool, now: float
    ) -> None:
        if state not in (PRINTING, PAUSED):
            self.job_file = self.job_started = None
        elif new_print or self.job_started is None or filename != self.job_file:
            self.job_file, self.job_started = filename, now

    # ---- the parts of a cycle ---------------------------------------------------------------

    def _grab(self, rep: CycleReport, now: float) -> None:
        # Keep the U1 camera awake and fetch frames only while printing (or when forced): the
        # monitor costs the printer a capture every ~0.5 s for as long as it is awake.
        try:
            if (
                self.camera.keepalive(now)
                and self.force_frame
                and self.after_keepalive is not None
            ):
                self.after_keepalive()
            rep.frame = self.camera.grab()
            self.last_frame_ts = rep.frame.ts
        except PrinterCommError as exc:
            rep.camera_error = str(exc)
        if self.context_tap is not None and self.set_context is not None:
            try:
                ctx = self.context_tap.poll()
            except PrinterCommError as exc:
                self.set_context(None)  # no masks this frame rather than stale ones
                rep.context_error = str(exc)
            else:
                self.set_context(ctx)
                rep.z = ctx.z

    def _npu(self, rep: CycleReport) -> None:
        if self.npu_tap is None:
            return
        try:
            rep.events += self.monitor.npu(self.npu_tap.poll(), self.clock())
        except PrinterCommError as exc:
            rep.npu_error = str(exc)

    def _write_status(self, rep: CycleReport, *, printing: bool) -> None:
        if self.status is None:
            return
        job = (
            {"file": self.job_file, "started": self.job_started}
            if rep.reachable and rep.state in (PRINTING, PAUSED)
            else None
        )
        self.status.update(
            state=rep.state,
            filename=rep.file,
            score=rep.score,
            events=rep.events,
            trend=self.monitor.tracker.adjusted,
            camera_error=rep.camera_error,
            now=self.clock(),
            last_poll_ok=self.last_poll_ok,
            unreachable_since=self.unreachable_since,
            reachable=bool(rep.reachable),
            last_frame_ts=self.last_frame_ts,
            supervising=printing,
            job=job,
            interval_s=self.interval_s,
        )
