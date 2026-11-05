"""jobwatch_run.py — `3d printer watch NAME --job`: the loop, the camera checks and the command line.

An agent starts it RIGHT AFTER `3d printer start NAME FILE --yes` (or `printer start ... --watch` does):

    3d printer watch u1 --job --yes --gcode print/part.gcode --out print/runs

It watches that ONE job until it ends. Every `--interval` (60 s; 15 s while layer 1 prints) and at the
checkpoints (a quarter, half and all of the first layer, 25/50/100 % of the job) it looks at the camera and at
the printer's own signals; on a lost print it stops the job (PAUSE or CANCEL, see `--on-fail`), saves the
frames and the klippy.log lines into `--out`, rings `tg call me --say ...` and sends the frame with `tg`.
Decisions are in jobwatch.py, the stop/alert steps in jobwatch_actions.py, the readings in jobwatch_sources.py.
The print-QUALITY monitor (stringing debris, a lost bead pattern, an uneven first layer: warnings only, never a
stop) runs on the same frames (jobwatch_quality.py) and writes its monitoring report when the watch ends.

NEVER: starts a print, moves an axis, resumes, sends G-code. The camera sees the plate at the print's own Z, so
nothing is moved to look. Exit codes: 0 the job ended and nothing was lost, 3 a lost print was found (the stop
was sent, or only attempted: see the outcome), 1 the watch could not run (no job, printer unreadable).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from errors import InvalidArgument, UsageError
from printer_ops.base import PAUSED, PRINTING, PrinterCommError
from printer_ops.jobwatch import (
    DEFAULT_CHECKPOINTS,
    ON_FAIL,
    CMD,
    Checkpoint,
    Finding,
    JobConfig,
    JobJudge,
    Observation,
    PartReading,
    Verdict,
    decide,
    parse_checkpoints,
)
from printer_ops.jobwatch_actions import Bound, Responder, bind, find_tg
from printer_ops.jobwatch_quality import QualityWatch, run_quality_tool
from printer_ops.quality import QualityConfig, quality_config

USAGE = """3d printer watch NAME --job --yes [--on-fail auto|pause|cancel] [--gcode FILE.gcode] [--out DIR]
    [--interval S] [--checkpoints L1@25%,L1@50%,L1@100%,25%,50%,100%] [--dry-run | --tg-dry-run] [--no-quality]
    [--fixture SCENARIO.json] [--duration S] [--wait-start S]
  Watch ONE running print and ABORT IT when it is lost. Start it right after `3d printer start NAME FILE --yes`
  (or give `start` --watch): it follows that job until it ends. Unlike the always-on `3d printer watch NAME`
  (read-only, alerts an agent) this one ACTS, so it needs --yes: you authorise a pause/cancel of this job.

  Every --interval (default 60 s, 15 s while layer 1 prints) and at the checkpoints (a quarter, half and all of
  the first layer — the first-layer marks need --gcode — and 25 / 50 / 100 % of the job) it checks:
    camera    the plate against an empty-plate baseline taken at the start of the job: the part's footprint is
              learnt from layer 3 on, then `part_gone` (little of it left), `part_moved`, `spaghetti` (much new
              material beyond it). A finding needs 2 readings of the last 4 (a toolhead over the part is not
              a finding). With the plate out of view, no baseline or no camera calibration to warp it, the
              camera says `blind` — never "gone".
    signals   the same Moonraker events as the always-on watch (filament runout, heater loss, bed off, exception,
              G-code error, homing lost) and the printer's own defect detection (`noodle` verdicts, p >= 0.8).
    quality   the PRINT-QUALITY monitor (printer_ops/quality.py, lib/print_quality.py): white stringing debris,
              a lost bead pattern (stripes, roughness, under-/over-extrusion) and an uneven first layer, judged
              against the job's OWN first layers (the first 4 readings with the part on the plate are the
              reference). It sends `tg --photo FRAME "<russian text>"` once per issue and level (warning, then
              severe) and NEVER pauses or cancels: a bad surface is the owner's call. A `notice` is only
              written to the report. When the job ends (or the watch stops) the MONITORING REPORT is written
              to DIR/<date>-NAME-monitor-report/ (report.json, report.md, worst-frame.jpg): readings per
              severity and issue, the alerts, the baseline, the worst frame. `--no-quality` or
              `<printer>.watch.quality: false` turns it off; `<printer>.watch.quality: {key: value}` tunes it
              (the QualityConfig fields).
  On a critical finding: the job is stopped, the frames + finding.json + the klippy.log lines of the job are saved
  to DIR/<date>-NAME-jobwatch/ (default ./print/runs), `tg call me --say "<russian text>"` rings the owner, then
  `tg --photo FRAME "<text>"` sends the picture. A warning (part moved, nothing seen, a heater drift) is a tg
  message with the frame and nothing is touched. The owner is called only for a lost print.

  --on-fail auto     (default) CANCEL when the part is gone, or a camera finding in the first layers (layer <= 3);
                     PAUSE for everything else (runout, heater, later spaghetti: resumable). `pause` / `cancel`:
                     always that.
  --gcode FILE       the sliced G-code: first-layer checkpoints and the layer of a stop
  --out DIR          evidence folder (default ./print/runs, else ~/.local/state/3d-cli/jobwatch/NAME/runs)
  --interval S       seconds between checks (60); --first-layer-interval S (15); --poll S between cheap polls (5)
  --checkpoints LIST extra checks (see above); `--checkpoints none` for the periodic one only
  --dry-run          nothing is sent to the printer or the phone: the stop and both tg commands are printed
  --tg-dry-run       the job is really stopped, the tg commands are only printed
  --no-quality       skip the print-quality monitor (and its report)
  --fixture FILE     no network at all: the printer and the camera come from a scenario file with saved frames
                     (implies --dry-run for the phone; a fixture printer records the stop it is given)
  --duration S       stop watching after S seconds (default: until the job ends)
  --wait-start S     wait this long for the job to be printing (120)
  --yes              required (not with --dry-run / --fixture): authorises pause/cancel of this job
  Config: `<printer>.watch.job: {interval_s, on_fail, min_view, confirm, window, arm_layer, out_dir, ...}` in
  printer-devices.yaml (the JobConfig fields, printer_ops/jobwatch.py); flags win.

  Exit: 0 job ended, nothing lost; 3 a lost print was found and the stop attempted; 1 could not run.

Examples:
  3d printer start u1 print/part.gcode --yes && 3d printer watch u1 --job --yes --gcode print/part.gcode
  3d printer watch u1 --job --yes --on-fail pause --out projects/chekhov/print/runs
  3d printer watch k1se --job --fixture tests/data/jobwatch/detached.json --dry-run"""

STATE_FILES = (
    "baseline.jpg",
    "baseline.json",
    "footprint.png",
    "candidate.png",
    "track.json",
    "candidate-track.json",
    "quality-frame.jpg",
    "quality-worst.jpg",
)


@dataclass
class Args:
    name: str
    yes: bool
    on_fail: str | None
    gcode: str | None
    out: str | None
    interval: float | None
    first_layer_interval: float | None
    poll: float | None
    checkpoints: str | None
    dry_run: bool
    tg_dry_run: bool
    fixture: str | None
    duration: float | None
    wait_start: float | None
    no_quality: bool = False


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(
            message, command=CMD, remediation=["See `3d printer watch --help`."]
        )


def parse_args(argv: list[str]) -> Args:
    p = _Parser(prog="3d printer watch NAME --job", add_help=False)
    p.add_argument("name")
    p.add_argument("--job", action="store_true", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--on-fail", choices=ON_FAIL)
    for flag in ("--gcode", "--out", "--checkpoints", "--fixture"):
        p.add_argument(flag)
    for flag in (
        "--interval",
        "--first-layer-interval",
        "--poll",
        "--duration",
        "--wait-start",
    ):
        p.add_argument(flag, type=float)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--tg-dry-run", action="store_true")
    p.add_argument("--no-quality", action="store_true")
    a = p.parse_args(argv)
    return Args(
        a.name,
        a.yes,
        a.on_fail,
        a.gcode,
        a.out,
        a.interval,
        a.first_layer_interval,
        a.poll,
        a.checkpoints,
        a.dry_run,
        a.tg_dry_run,
        a.fixture,
        a.duration,
        a.wait_start,
        a.no_quality,
    )


def job_config(args: Args, device_job: dict[str, Any], default_out: Path) -> JobConfig:
    """The tuned defaults, then `<printer>.watch.job`, then the flags."""
    known = {f.name for f in fields(JobConfig)}
    bad = sorted(set(device_job) - known)
    if bad:
        raise InvalidArgument("watch.job", ", ".join(bad), sorted(known), command=CMD)
    given: dict[str, Any] = {
        k: Path(v) if k == "out_dir" else v for k, v in device_job.items()
    }
    cfg = replace(JobConfig(out_dir=default_out), **given)
    over: dict[str, Any] = {}
    for flag, field_name in (
        ("interval", "interval_s"),
        ("first_layer_interval", "first_layer_interval_s"),
        ("poll", "poll_s"),
        ("on_fail", "on_fail"),
        ("wait_start", "wait_start_s"),
    ):
        if getattr(args, flag) is not None:
            over[field_name] = getattr(args, flag)
    if args.out:
        over["out_dir"] = Path(args.out)
    text = args.checkpoints if args.checkpoints is not None else DEFAULT_CHECKPOINTS
    over["checkpoints"] = () if text == "none" else parse_checkpoints(text)
    cfg = replace(cfg, **over)
    if (
        cfg.on_fail not in ON_FAIL
        or cfg.interval_s <= 0
        or cfg.poll_s <= 0
        or cfg.confirm < 1
    ):
        raise InvalidArgument(
            "watch.job",
            str(device_job),
            ["on_fail auto|pause|cancel, interval_s > 0, confirm >= 1"],
            command=CMD,
        )
    return cfg


def default_out_dir(name: str) -> Path:
    """`./print/runs` of the project when there is a `print/` folder, else the user state dir."""
    if Path("print").is_dir():
        return Path("print") / "runs"
    from cli.paths import state_dir

    return state_dir() / "jobwatch" / name / "runs"


# ---- checkpoints ------------------------------------------------------------------------------


class CheckpointTracker:
    """Which of the configured checkpoints this observation has just crossed (each once)."""

    def __init__(
        self, points: tuple[Checkpoint, ...], layer1: tuple[int, int] | None
    ) -> None:
        self.points, self.layer1 = points, layer1
        self.fired: set[Checkpoint] = set()

    def reached(self, p: Checkpoint, o: Observation) -> bool:
        if p.kind == "progress":
            return o.progress is not None and o.progress >= p.fraction
        if p.fraction >= 1.0:
            return (o.layer is not None and o.layer >= 2) or (
                self.layer1 is not None
                and o.file_position is not None
                and o.file_position >= self.layer1[1]
            )
        if self.layer1 is None or o.file_position is None:
            return False
        start, end = self.layer1
        return o.file_position >= start + p.fraction * (end - start)

    def crossed(self, o: Observation) -> list[str]:
        out = []
        for p in self.points:
            if p not in self.fired and self.reached(p, o):
                self.fired.add(p)
                out.append(str(p))
        return out


def first_layer_range(gcode: str | None) -> tuple[int, int] | None:
    """Byte range of layer 1 in the G-code (`;LAYER_CHANGE` offsets), None without a G-code or a second layer."""
    if not gcode:
        return None
    from printer_ops.evidence import scan_gcode

    starts = scan_gcode(Path(gcode)).starts
    return (starts[0], starts[1]) if len(starts) > 1 else None


# ---- the camera -------------------------------------------------------------------------------

PartTool = Callable[[list[str]], dict[str, Any]]


def run_part_tool(argv: list[str]) -> dict[str, Any]:
    """bed_part.py through cli.pyrun (numpy + OpenCV): its JSON answer, or a blind reading saying why not."""
    from cli.pyrun import tool_argv
    from printer_ops.bed import BED_DEPS

    try:
        r = subprocess.run(
            tool_argv(BED_DEPS, "bed_part.py", argv),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (
            dict(json.loads(r.stdout))
            if r.returncode == 0
            else {
                "status": "blind",
                "reason": f"the pixel tool failed (exit {r.returncode}): {(r.stderr or '')[-200:]}",
            }
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"status": "blind", "reason": f"the pixel tool failed: {exc}"}


class CameraCheck:
    """The job's files (baseline, footprint, frames) and the calls of the pixel tool."""

    def __init__(
        self, name: str, run_dir: Path, tool: PartTool, cfg: JobConfig
    ) -> None:
        self.name, self.dir, self.tool, self.cfg = name, run_dir, tool, cfg
        run_dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    @property
    def baseline(self) -> tuple[Path, float] | None:
        meta = self.dir / "baseline.json"
        if not meta.is_file():
            return None
        return self.dir / "baseline.jpg", float(
            json.loads(meta.read_text(encoding="utf-8"))["z"]
        )

    def ensure_baseline(self, jpeg: bytes, o: Observation) -> str | None:
        """Take the job's empty-plate baseline from the first frames; else the nearest empty-bed reference. -> why not."""
        if self.baseline is not None or o.z is None:
            return None if o.z is not None else "the toolhead Z is unknown"
        if (o.layer is None or o.layer <= 1) and (o.progress or 0.0) < 0.05:
            self.store_baseline(jpeg, o.z)
            return None
        ref = reference_baseline(self.name, o.z)
        if ref is None:
            return (
                "no empty-plate baseline: the watch started after the first layer and there is no empty-bed "
                f"reference (3d printer bed reference {self.name} --capture --best-view)"
            )
        self.store_baseline(ref[0], ref[1])
        return None

    def store_baseline(self, jpeg: bytes, z: float) -> None:
        (self.dir / "baseline.jpg").write_bytes(jpeg)
        (self.dir / "baseline.json").write_text(json.dumps({"z": z}), encoding="utf-8")

    def reading(
        self, jpeg: bytes, o: Observation, why_blind: str | None
    ) -> PartReading:
        base = self.baseline
        if base is None or o.z is None:
            return PartReading("blind", {"reason": why_blind or "no baseline"})
        self.n += 1
        frame, annotated = (
            self.dir / f"frame-{self.n % 6}.jpg",
            self.dir / f"annotated-{self.n % 6}.jpg",
        )
        frame.write_bytes(jpeg)
        argv = [
            "check",
            self.name,
            "--frame",
            str(frame),
            "--baseline",
            str(base[0]),
            "--baseline-z",
            str(base[1]),
            "--z",
            str(o.z),
            "--save-footprint",
            str(self.dir / "candidate.png"),
            "--save-track",
            str(self.dir / "candidate-track.json"),
            "--annotated",
            str(annotated),
            "--label",
            f"layer {o.layer} {o.progress or 0:.0%}",
            "--min-view",
            str(self.cfg.min_view),
            "--z-tol",
            str(self.cfg.z_tol_mm),
            "--json",
        ]
        for flag, name in (("--footprint", "footprint.png"), ("--track", "track.json")):
            if (self.dir / name).is_file():
                argv += [flag, str(self.dir / name)]
        nums = self.tool(argv)
        return PartReading(
            str(nums.get("status", "blind")), nums, str(frame), str(annotated)
        )

    def learn(self, verdict: Verdict) -> None:
        """Keep this reading's changed mask (and the transform that carried the baseline onto it) as the
        part's footprint: armed / refreshed. The two move together or the footprint would not be carried."""
        if not (verdict.arm or verdict.refresh):
            return
        for cand, final in (
            ("candidate.png", "footprint.png"),
            ("candidate-track.json", "track.json"),
        ):
            if (self.dir / cand).is_file():
                os.replace(self.dir / cand, self.dir / final)


def reference_baseline(name: str, z: float) -> tuple[bytes, float] | None:
    """The empty-bed reference nearest to Z (bed reference NAME --capture): (its JPEG, its Z)."""
    from printer_ops.bed import refs_dir

    best: tuple[float, bytes, float] | None = None
    for meta in sorted(refs_dir(name).glob("plate-z*.json")):
        try:
            rz = float(json.loads(meta.read_text(encoding="utf-8"))["z"])
            jpg = meta.with_name(meta.name[: -len(".json")] + ".jpg").read_bytes()
        except (OSError, ValueError, KeyError):
            continue
        if best is None or abs(rz - z) < abs(best[2] - z):
            best = (abs(rz - z), jpg, rz)
    return None if best is None else (best[1], best[2])


# ---- the loop ---------------------------------------------------------------------------------


class JobWatch:
    def __init__(
        self,
        source: Any,
        camera: CameraCheck,
        responder: Responder,
        cfg: JobConfig,
        tracker: CheckpointTracker,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        echo: Callable[[str], None] = print,
        quality: QualityWatch | None = None,
    ) -> None:
        self.source, self.camera, self.responder, self.cfg, self.tracker = (
            source,
            camera,
            responder,
            cfg,
            tracker,
        )
        self.clock, self.sleep, self.echo = clock, sleep, echo
        self.judge = JobJudge(cfg)
        self.quality = quality
        self.next_check = 0.0
        self.recheck_at: float | None = None
        self.ended = 0
        self.alerted: set[str] = set()
        self.unreadable = 0
        self.last_files: dict[str, str | None] = {}
        self.history: list[dict[str, Any]] = []

    def say(self, text: str) -> None:
        self.echo(f"[{time.strftime('%H:%M:%S', time.localtime(self.clock()))}] {text}")

    def due(self, o: Observation, now: float) -> bool:
        crossed = self.tracker.crossed(o)
        for label in crossed:
            self.say(f"checkpoint {label}")
        return (
            bool(crossed)
            or now >= self.next_check
            or (self.recheck_at is not None and now >= self.recheck_at)
        )

    def check(self, o: Observation, now: float) -> Verdict:
        why = None
        jpeg: bytes | None = None
        try:
            jpeg = self.source.grab()
            why = self.camera.ensure_baseline(jpeg, o)
            o.part = self.camera.reading(jpeg, o, why)
        except (PrinterCommError, OSError) as exc:
            o.part = PartReading("blind", {"reason": f"camera: {exc}"})
        o.noodles = self.source.noodles()
        v = self.judge.step(o)
        self.camera.learn(v)
        if self.quality is not None and jpeg is not None:
            v.findings.extend(self.quality.check(jpeg, o))
        first = o.layer is not None and o.layer <= 1
        self.next_check = now + (
            self.cfg.first_layer_interval_s if first else self.cfg.interval_s
        )
        self.recheck_at = now + self.cfg.recheck_s if v.suspicious else None
        self.remember(o, v)
        return v

    def remember(self, o: Observation, v: Verdict) -> None:
        n = o.part.numbers if o.part is not None else {}
        self.say(
            f"layer {o.layer}/{o.total_layer} {o.progress or 0:.0%} z {o.z}: camera {v.label}"
            + (
                f" changed {n.get('changed')} overlap {n.get('overlap')} outside {n.get('outside')}"
                if n.get("changed") is not None
                else ""
            )
            + "".join(f"; {x}" for x in v.notes)
        )
        if self.quality is not None and (note := self.quality.note()):
            self.say(note)
        if o.part is not None and o.part.frame:
            self.last_files = {
                "frame.annotated.jpg": o.part.annotated,
                "frame.raw.jpg": o.part.frame,
                "baseline.jpg": str(self.camera.dir / "baseline.jpg"),
                "footprint.png": str(self.camera.dir / "footprint.png"),
            }
        self.history = [
            *self.history[-20:],
            {
                "t": o.now,
                "layer": o.layer,
                "progress": o.progress,
                "label": v.label,
                "numbers": o.part.numbers if o.part else None,
            },
        ]

    def poll(self, now: float) -> Observation | None:
        try:
            o: Observation = self.source.poll(now)
        except PrinterCommError as exc:
            self.unreadable += 1
            self.say(f"printer unreadable ({self.unreadable}): {exc.message}")
            return None
        self.unreadable = 0
        return o

    def step(self, o: Observation, now: float) -> int | None:
        """One poll's work -> an exit code when the watch is over, else None."""
        if o.state not in (PRINTING, PAUSED):
            self.ended += 1
            if self.ended >= self.cfg.ended_polls:
                self.say(f"the job ended: the printer is {o.state}; nothing was lost")
                return 0
            return None
        self.ended = 0
        v = self.check(o, now) if self.due(o, now) else self.judge.signals_only(o)
        return self.handle(v.findings, o)

    def handle(self, findings: list[Finding], o: Observation) -> int | None:
        critical = [f for f in findings if f.critical]
        if critical:
            action = decide(findings, o.layer, self.cfg)
            self.responder.respond(
                action,
                critical,
                layer=o.layer,
                files=self.last_files,
                extra={"observations": self.history, "action": action},
            )
            return 3
        fresh = [f for f in findings if f.kind not in self.alerted]
        self.alerted.update(f.kind for f in fresh)
        if fresh:
            self.responder.respond(
                "alert",
                fresh,
                layer=o.layer,
                files=self.alert_files(fresh),
                extra={"observations": self.history, "action": "alert"},
            )
        return None

    def alert_files(self, fresh: list[Finding]) -> dict[str, str | None]:
        """The frames sent with an alert: the quality picture (debris marked) for a quality alert."""
        if self.quality is not None and all(
            f.kind.startswith("quality_") for f in fresh
        ):
            return {**self.quality.alert_files(), **self.last_files}
        return self.last_files

    def wait_for_job(self, deadline: float) -> bool:
        while self.clock() < deadline:
            o = self.poll(self.clock())
            if o is not None and o.state in (PRINTING, PAUSED):
                return True
            self.sleep(self.cfg.poll_s)
        return False

    def run(self, duration: float | None) -> int:
        """Watch until the job ends; the monitoring report is written whatever way the watch ends."""
        try:
            return self._run(duration)
        finally:
            self.report()

    def report(self) -> None:
        if self.quality is None:
            return
        folder, line = self.quality.finish(self.cfg.out_dir)
        if folder is not None:
            self.say(f"{line} -> {folder}")

    def _run(self, duration: float | None) -> int:
        start = self.clock()
        if not self.wait_for_job(start + self.cfg.wait_start_s):
            self.say("no job is printing: nothing to watch")
            return 1
        end = None if duration is None else self.clock() + duration
        while end is None or self.clock() < end:
            now = self.clock()
            o = self.poll(now)
            code = self.step(o, now) if o is not None else self.when_unreadable()
            if code is not None:
                return code
            self.sleep(self.cfg.poll_s)
        self.say("duration over: the watch stops, the job goes on unwatched")
        return 0

    def when_unreadable(self) -> int | None:
        if self.unreadable >= 12 and "unreadable" not in self.alerted:
            self.alerted.add("unreadable")
            self.responder.respond(
                "alert",
                [
                    Finding(
                        "printer_unreadable",
                        False,
                        "the printer has not answered for a minute",
                    )
                ],
                layer=None,
                files=self.last_files,
                extra={},
            )
        return None


@contextmanager
def one_watch_per_printer(name: str) -> Iterator[None]:
    """A second job watch for the same printer would double every alert and every stop: refused."""
    from cli.paths import state_dir

    path = state_dir() / "jobwatch" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError(
                f"a job watch for {name} is already running",
                command=CMD,
                remediation=["One watch per printer; it ends with the job."],
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def require_authority(args: Args) -> None:
    if not (args.yes or args.dry_run or args.fixture):
        raise UsageError(
            "this watch PAUSES or CANCELS a physical job and needs --yes",
            command=CMD,
            remediation=[
                f"Authorise it: 3d printer watch {args.name} --job --yes",
                "Or rehearse: add --dry-run",
            ],
        )


def build_live(args: Args, cfg: JobConfig) -> tuple[Any, Any, Responder]:
    from printer_ops import adapter_by_name
    from printer_ops.camera import open_camera, require_camera
    from printer_ops.devices import get_device
    from printer_ops.jobwatch_sources import LiveSource
    from printer_ops.moonraker import MoonrakerAdapter
    from printer_ops.vision import KlippyLogTap

    adapter = adapter_by_name(args.name, command="printer")
    if not isinstance(adapter, MoonrakerAdapter):
        raise UsageError(
            f"{args.name} is a {adapter.kind} printer: the job watch needs Moonraker",
            command=CMD,
        )
    device = get_device(args.name)
    if device.access != "full" and not args.dry_run:
        from printer_ops.base import PrinterRefused

        raise PrinterRefused(
            f"{args.name} is {device.access}: it may not be paused or cancelled",
            command=CMD,
            remediation=["Watch it dry: add --dry-run"],
        )
    camera = open_camera(device, require_camera(device, CMD))
    source = LiveSource(adapter, camera, KlippyLogTap(adapter.host, adapter.port))
    bound = bind(adapter)
    responder = Responder(
        adapter,
        bound,
        out_dir=cfg.out_dir,
        dry_run=args.dry_run,
        tg_dry_run=args.tg_dry_run,
        tg_binary=find_tg(),
        verify_s=cfg.verify_s,
    )
    return adapter, source, responder


def build_fixture(args: Args, cfg: JobConfig, clock: Any) -> tuple[Any, Any, Responder]:
    from printer_ops.jobwatch_sources import (
        FixtureAdapter,
        FixtureSource,
        Scenario,
        require_fixture_printer,
    )

    scenario = Scenario(Path(str(args.fixture)))
    require_fixture_printer(scenario, args.name)
    adapter = FixtureAdapter(args.name, scenario, clock)
    source = FixtureSource(scenario, clock, adapter)
    bound = Bound(args.name, scenario.filename, "FIXTURE")
    responder = Responder(
        adapter,
        bound,
        out_dir=cfg.out_dir,
        dry_run=args.dry_run,
        tg_dry_run=True,
        clock=clock,
        sleep=lambda s: None,
        verify_s=cfg.verify_s,
        evidence_fn=lambda p, j, d: "fixture: there is no klippy.log to fetch",
    )
    return adapter, source, responder


class VirtualClock:
    """Fixture time: `sleep` advances it, nothing waits."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    args = parse_args(argv)
    require_authority(args)
    from printer_ops.devices import get_device

    device = get_device(args.name)
    cfg = job_config(
        args, dict((device.watch or {}).get("job") or {}), default_out_dir(args.name)
    )
    tracker = CheckpointTracker(cfg.checkpoints, first_layer_range(args.gcode))
    quality = quality_config(
        (device.watch or {}).get("quality"), getattr(device, "model", None)
    )
    with one_watch_per_printer(args.name):
        return _run_watch(args, cfg, tracker, quality)


def _run_watch(
    args: Args, cfg: JobConfig, tracker: CheckpointTracker, quality_cfg: QualityConfig
) -> int:
    from cli.paths import state_dir

    run_dir = state_dir() / "jobwatch" / args.name
    if args.fixture:
        clock = VirtualClock()
        _, source, responder = build_fixture(args, cfg, clock)
        sleep: Callable[[float], None] = clock.sleep
        now: Callable[[], float] = clock
    else:
        _, source, responder = build_live(args, cfg)
        sleep, now = time.sleep, time.time
    for name in (
        STATE_FILES
    ):  # a new job starts from nothing: no baseline or footprint of the last one
        (run_dir / name).unlink(missing_ok=True)
    camera = CameraCheck(args.name, run_dir, run_part_tool, cfg)
    quality = (
        None
        if args.no_quality or not quality_cfg.enabled
        else QualityWatch(
            args.name,
            run_dir,
            run_quality_tool,
            quality_cfg,
            cfg,
            responder.bound.filename,
        )
    )
    watch = JobWatch(
        source, camera, responder, cfg, tracker, clock=now, sleep=sleep, quality=quality
    )
    print(
        f"watching {args.name}: {responder.bound.filename} (job {responder.bound.job_id or '?'}), every "
        f"{cfg.interval_s:g} s, on-fail {cfg.on_fail}, evidence -> {cfg.out_dir}"
        + (" [DRY RUN: nothing is sent]" if args.dry_run else ""),
        flush=True,
    )
    return watch.run(args.duration)


__all__ = [
    "USAGE",
    "run",
    "parse_args",
    "job_config",
    "JobWatch",
    "CameraCheck",
    "CheckpointTracker",
    "VirtualClock",
]
