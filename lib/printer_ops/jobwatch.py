"""jobwatch.py — the decision half of `3d printer watch NAME --job`: is the running print still alive?

WHY (2026-11-04): a part came off the plate without a trace and the print went on; every later look at the
  bed was a look at nothing. The always-on watchers (`printer watch`, `printer vision`) are read-only by
  design and alert an agent; this guard is the one an agent STARTS for ONE job (right after `3d printer start`)
  and that acts on it: pause or cancel, save the evidence, ring the owner.

WHAT (pure, stdlib, no clock, no I/O: feed `Observation`s, get `Finding`s and a `decide`d action):
  `JobConfig`     everything tunable, from CLI flags and `<printer>.watch.job` in printer-devices.yaml;
  `Checkpoint`    "L1@25%" (a quarter through the first layer), "50%" (half of the job): extra checks on top of
                  the periodic one;
  `JobJudge`      the state machine over the readings. CAMERA: the part is ARMED when its footprint (what
                  differs from the empty plate) showed on two readings from `arm_layer` on; from then on the
                  part is `gone` when little of the footprint is still changed, `partial` when part of it is,
                  `growing` (spaghetti) when much changed beyond it. A finding needs `confirm` such readings
                  within the last `window` (a toolhead over the part, a hand, one bad frame is not a finding).
                  SIGNALS: Moonraker events from the same MoonrakerDetector the always-on watch uses
                  (filament runout, heater loss, bed off, exception, G-code error, homing lost) and the
                  printer's own defect detection (`noodle` verdicts in klippy.log).
  `decide`        pause | cancel | alert from the findings, the layer and `--on-fail`.

BLIND IS NOT GONE: with the plate out of view at this Z, no baseline, or (no camera calibration) a plate that
  cannot be followed on the picture (bed_part.py: the chained baseline), the camera says `blind` and
  contributes nothing; the signals keep running. A job that was never seen on the camera
  (`no_part_seen`) is a WARNING, never a cancel: black PLA on a black plate is invisible to a camera, a part that
  came off during the first layers is not, and the two cannot be told apart from one still frame.

INVARIANTS: never starts a print, never moves an axis (the only writes are PAUSE and CANCEL of the bound job, in
  jobwatch_actions.py). The camera sees the plate at the print's own Z (U1: 98 % up to Z 30, 77 % at Z 100), so
  nothing is ever moved to look: moving a bed under a running print would ruin it.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errors import InvalidArgument
from printer_ops.watch import CRITICAL, WARNING, Event

CMD = "printer watch --job"
ON_FAIL = ("auto", "pause", "cancel")

# camera labels of one reading
BLIND, EMPTY, SEEN, PRESENT, GONE, PARTIAL, GROWING = (
    "blind",
    "empty",
    "seen",
    "present",
    "gone",
    "partial",
    "growing",
)

# finding kinds
PART_GONE, PART_MOVED, SPAGHETTI, NO_PART = (
    "part_gone",
    "part_moved",
    "spaghetti",
    "no_part_seen",
)
NOODLE = "printer_noodle"
HOPELESS = frozenset(
    {PART_GONE}
)  # printing in the air: nothing to resume onto, cancel at any layer
CAMERA_KINDS = frozenset({PART_GONE, PART_MOVED, SPAGHETTI, NO_PART, NOODLE})
NOODLE_CRITICAL_PROB = (
    0.80  # the confidence the firmware itself thresholds (vision.NpuVerdict)
)


@dataclass(frozen=True)
class Checkpoint:
    """An extra check: `layer1` = a share of the first layer (needs the G-code), `progress` = of the job."""

    kind: str
    fraction: float

    def __str__(self) -> str:
        pct = f"{self.fraction * 100:g}%"
        return f"L1@{pct}" if self.kind == "layer1" else pct


_CHECKPOINT = re.compile(r"(?:(?P<l1>L1@))?(?P<pct>\d{1,3}(?:\.\d+)?)%")


def parse_checkpoints(text: str) -> tuple[Checkpoint, ...]:
    """`L1@25%,L1@50%,L1@100%,25%,50%,100%` -> Checkpoints; ValueError-free: InvalidArgument names the bad item."""
    out: list[Checkpoint] = []
    for item in (x.strip() for x in text.split(",") if x.strip()):
        m = _CHECKPOINT.fullmatch(item)
        if m is None or not 0 < float(m["pct"]) <= 100:
            raise InvalidArgument(
                "--checkpoints",
                item,
                [
                    "L1@25% (share of the first layer), 50% (share of the job): 0 < N <= 100"
                ],
                command=CMD,
            )
        out.append(
            Checkpoint("layer1" if m["l1"] else "progress", float(m["pct"]) / 100)
        )
    return tuple(out)


DEFAULT_CHECKPOINTS = "L1@25%,L1@50%,L1@100%,25%,50%,100%"


@dataclass(frozen=True)
class JobConfig:
    interval_s: float = 60.0  # camera + signal check period
    first_layer_interval_s: float = (
        15.0  # ... while layer 1 prints (adhesion failures happen there)
    )
    poll_s: float = (
        5.0  # cheap Moonraker poll between checks (checkpoints fire from it)
    )
    recheck_s: float = 10.0  # a suspicious reading is checked again this soon
    confirm: int = 2  # suspicious readings within `window` that make a finding
    window: int = 4
    arm_layer: int = 3  # the part's footprint is learnt from this layer on
    arm_readings: int = 2  # ... after this many readings that show it
    first_layer_max: int = (
        3  # layers up to this are "first layers": a failure there cancels under `auto`
    )
    on_fail: str = "auto"
    checkpoints: tuple[Checkpoint, ...] = ()
    min_view: float = 0.5  # share of the plate in the frame to judge the part
    z_tol_mm: float = 2.0  # without tracking an uncalibrated baseline stands this far
    present_min: float = (
        0.004  # changed share of the plate region that says "something stands there"
    )
    present_blob_px: int = 150
    gone_overlap: float = (
        0.25  # share of the footprint still changed: under this the part is gone
    )
    partial_overlap: float = 0.6  # ... under this only part of it is left
    heal_overlap: float = (
        0.8  # healthy enough to refresh the footprint (the part grows)
    )
    grow_outside: float = 1.0  # changed beyond the footprint, relative to it: spaghetti
    no_part_s: float = 180.0  # armed never, this long past arm_layer: no_part_seen
    wait_start_s: float = 120.0  # how long to wait for the job to start printing
    ended_polls: int = 2  # polls of a finished printer before the watch ends
    verify_s: float = 20.0  # how long a pause/cancel has to show in the printer state
    out_dir: Path = field(default_factory=lambda: Path("print") / "runs")


@dataclass
class PartReading:
    """One answer of bed_part.py (`status` ok | blind) and the files it came from."""

    status: str
    numbers: dict[str, Any]
    frame: str | None = None
    annotated: str | None = None

    @property
    def changed(self) -> float:
        return float(self.numbers.get("changed") or 0.0)

    @property
    def blob_px(self) -> float:
        return float(self.numbers.get("blob_px") or 0.0)


@dataclass
class Observation:
    now: float
    state: str
    filename: str | None = None
    progress: float | None = None
    layer: int | None = None
    total_layer: int | None = None
    z: float | None = None
    file_position: int | None = None
    events: list[Event] = field(default_factory=list)
    noodles: list[tuple[str, float]] = field(
        default_factory=list
    )  # (label, probability) of defect detection
    part: PartReading | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    kind: str
    critical: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def camera(self) -> bool:
        return self.kind in CAMERA_KINDS


# ---- one camera reading -----------------------------------------------------------------------


def label_reading(part: PartReading | None, armed: bool, cfg: JobConfig) -> str:
    """`blind` | `empty` | `seen` (unarmed, something stands there) | `present` | `partial` | `gone` | `growing`."""
    if part is None or part.status != "ok":
        return BLIND
    if not armed:
        shows = part.changed >= cfg.present_min and part.blob_px >= cfg.present_blob_px
        return SEEN if shows else EMPTY
    overlap, outside = part.numbers.get("overlap"), part.numbers.get("outside")
    if overlap is None:
        return BLIND
    if overlap < cfg.gone_overlap and (outside or 0.0) < cfg.grow_outside:
        return GONE
    if (outside or 0.0) >= cfg.grow_outside:
        return GROWING
    return PARTIAL if overlap < cfg.partial_overlap else PRESENT


def healthy(part: PartReading | None, cfg: JobConfig) -> bool:
    """The footprint may be refreshed from this reading (the part is whole and has not spread)."""
    if part is None or part.status != "ok":
        return False
    overlap, outside = part.numbers.get("overlap"), part.numbers.get("outside")
    return (
        overlap is not None and overlap >= cfg.heal_overlap and (outside or 0.0) < 0.3
    )


# ---- the state machine ------------------------------------------------------------------------


@dataclass
class Verdict:
    """What one `JobJudge.step` says: findings, whether to learn/refresh the footprint, notes for the log."""

    label: str = BLIND
    findings: list[Finding] = field(default_factory=list)
    arm: bool = False  # keep the candidate footprint of this reading as THE footprint
    refresh: bool = (
        False  # the part is healthy: the candidate footprint replaces the old one
    )
    suspicious: bool = False  # check again soon (cfg.recheck_s)
    notes: list[str] = field(default_factory=list)


class JobJudge:
    def __init__(self, cfg: JobConfig) -> None:
        self.cfg = cfg
        self.armed = False
        self.labels: deque[str] = deque(maxlen=cfg.window)
        self._seen_run = 0  # consecutive `seen` readings while unarmed
        self._reported: set[str] = set()
        self._first_arm_layer_at: float | None = None
        self.camera_blind_note: str | None = None

    # -- camera

    def _in_arm_window(self, o: Observation) -> bool:
        if o.layer is not None:
            return o.layer >= self.cfg.arm_layer
        return (o.progress or 0.0) >= 0.03

    def _count(self, label: str) -> int:
        return sum(1 for x in self.labels if x == label)

    def _camera(self, o: Observation, v: Verdict) -> None:
        label = label_reading(o.part, self.armed, self.cfg)
        if not self.armed and label in (SEEN, EMPTY) and not self._in_arm_window(o):
            label = BLIND  # the first layers are learnt about, not judged
        v.label = label
        if label == BLIND:
            self._note_blind(o, v)
            return
        self.labels.append(label)
        if not self.armed:
            self._unarmed(o, label, v)
        else:
            self._armed(o, label, v)

    def _note_blind(self, o: Observation, v: Verdict) -> None:
        why = (
            o.part.numbers.get("reason") if o.part is not None else None
        ) or "no camera reading"
        if why != self.camera_blind_note:
            self.camera_blind_note = why
            v.notes.append(f"camera blind: {why}")

    def _unarmed(self, o: Observation, label: str, v: Verdict) -> None:
        self._seen_run = self._seen_run + 1 if label == SEEN else 0
        if self._seen_run >= self.cfg.arm_readings:
            self.armed, v.arm = True, True
            v.notes.append("armed: the part's footprint is on the camera")
            return
        if label == SEEN:
            v.arm = False
            return
        self._first_arm_layer_at = self._first_arm_layer_at or o.now
        if (
            o.now - self._first_arm_layer_at >= self.cfg.no_part_s
            and NO_PART not in self._reported
        ):
            self._reported.add(NO_PART)
            v.findings.append(
                Finding(
                    NO_PART,
                    False,
                    f"nothing stands on the plate at layer {o.layer} (progress {self._pct(o)}): the first layers did "
                    "not stick, or the part is invisible to the camera (dark on dark)",
                    {"layer": o.layer},
                )
            )

    @staticmethod
    def _pct(o: Observation) -> str:
        return "?" if o.progress is None else f"{o.progress:.0%}"

    def _armed(self, o: Observation, label: str, v: Verdict) -> None:
        v.suspicious = label in (GONE, PARTIAL, GROWING)
        v.refresh = label == PRESENT and healthy(o.part, self.cfg)
        if label == GONE and self._confirmed(GONE, 0):
            v.findings.append(
                self._finding(PART_GONE, True, o, "the part is no longer on the plate")
            )
        elif label == GROWING and self._confirmed(GROWING, 0):
            v.findings.append(
                self._finding(
                    SPAGHETTI,
                    True,
                    o,
                    "a mass of new material around/off the part: spaghetti",
                )
            )
        elif label == PARTIAL and self._count(PARTIAL) + self._count(GONE) >= max(
            3, self.cfg.confirm + 1
        ):
            v.findings.append(
                self._finding(
                    PART_MOVED,
                    False,
                    o,
                    "only part of the footprint is where it was: the part moved",
                )
            )

    def _confirmed(self, label: str, _slack: int) -> bool:
        return self._count(label) >= self.cfg.confirm

    def _finding(self, kind: str, critical: bool, o: Observation, what: str) -> Finding:
        n = o.part.numbers if o.part is not None else {}
        text = (
            f"{what} (layer {o.layer}, progress {self._pct(o)}; footprint still changed "
            f"{_pc(n.get('overlap'))}, new change beyond it {_pc(n.get('outside'))})"
        )
        return Finding(kind, critical, text, {"layer": o.layer, "numbers": n})

    # -- signals

    def _signals(self, o: Observation, v: Verdict) -> None:
        for e in o.events:
            if e.severity == CRITICAL and e.kind not in (
                "snapshot",
                "connection",
                "klippy",
            ):
                v.findings.append(
                    Finding(
                        f"signal_{e.kind}", True, e.text, {"event": e.kind, **e.data}
                    )
                )
            elif e.severity == WARNING and e.kind in ("bed_off", "temperature"):
                v.findings.append(
                    Finding(
                        f"signal_{e.kind}", False, e.text, {"event": e.kind, **e.data}
                    )
                )
        for label, prob in o.noodles:
            if label == "noodle" and prob >= NOODLE_CRITICAL_PROB:
                v.findings.append(
                    Finding(
                        NOODLE,
                        True,
                        f"the printer's own defect detection says noodle (p {prob:.2f})",
                        {"prob": prob},
                    )
                )

    def step(self, o: Observation) -> Verdict:
        """A full check: the camera reading and the signals."""
        v = Verdict()
        self._camera(o, v)
        self._signals(o, v)
        return v

    def signals_only(self, o: Observation) -> Verdict:
        """A cheap poll between two camera checks: only what the printer itself reported."""
        v = Verdict(label=self.labels[-1] if self.labels else BLIND)
        self._signals(o, v)
        return v


def _pc(x: Any) -> str:
    return "?" if x is None else f"{float(x):.0%}"


# ---- the decision -----------------------------------------------------------------------------


def decide(findings: Iterable[Finding], layer: int | None, cfg: JobConfig) -> str:
    """`cancel` | `pause` | `alert` (nothing critical: tell, touch nothing)."""
    critical = [f for f in findings if f.critical]
    if not critical:
        return "alert"
    if cfg.on_fail in ("pause", "cancel"):
        return cfg.on_fail
    first_layers = layer is not None and layer <= cfg.first_layer_max
    for f in critical:
        if f.kind in HOPELESS or (f.camera and first_layers):
            return "cancel"
    return "pause"
