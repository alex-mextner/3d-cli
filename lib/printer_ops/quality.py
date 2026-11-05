"""quality.py — the decision half of the print-quality monitor: is the print GOOD, not just alive?

WHY (2026-11-05): a 10 h sheet of small tags on the U1 stringed from layer 2 on — white thread debris all over the
  part — and nothing flagged it: the spaghetti model (Obico) scored 0.0 on all 12 frames (stringing is not a
  class it knows), and the job watch (jobwatch.py) only asks "is the part still where it was", which a stringy part
  is. The print stayed on the plate, so nothing was lost — only a bad part, hours later. This tier watches
  the QUALITY of what is on the plate.

WHAT (pure, stdlib, no clock, no I/O: feed `QualityReading`s, get `QualityVerdict`s and, at the end, a report):
  `QualityConfig`   every threshold, from `<printer>.watch.quality` in printer-devices.yaml (keys = the fields);
  `QualityJudge`    the state machine over the numbers of lib/print_quality.py (fleck_frac, order):
      * BASELINE  the first `baseline_n` readings with the part on the plate ARE the reference (the clean first
                  layer of THIS job under THIS light on THIS plate); never judged against anything but a floor.
                  A reading drifts the baseline only while it is clean, so a slow legitimate change (infill vs
                  walls) is followed and debris is not absorbed.
      * ISSUES    `stringing` (white specks and strands: fleck_frac against the baseline), `texture` (the regular
                  bead pattern got lost with no debris: stripes, roughness, under- or over-extrusion: `order`
                  dropped against the baseline), `first_layer` (an uneven first layer: `order` under an absolute
                  floor while layer <= first_layer_max).
      * LEVELS    notice (INFO: report only) < warning < severe. A level is REPORTED when `confirm` of the last
                  `window` readings reach it (`severe_confirm` for severe): one blurry frame or a toolhead over the
                  part is not a finding. The level is latched; it clears after `clear_after` clean readings.
      * TREND     the slope of the debris density over the last readings; a rising trend lifts a notice-level
                  debris to warning.
      * ALERTS    one `QualityFinding` per (issue, level) per job (`new` on the verdict): the owner is told once
                  per escalation, never once per frame.
  `QualityReport`   what the monitor saw, for a human: readings per severity, per issue, the worst frame, the
                  alerts, the baseline, a timeline (quality_report.py writes it next to the evidence).

NEVER CRITICAL: a quality finding is at most WARNING. It tells and shows the frame; it does not pause or cancel —
  the print is still attached and a bad surface is the owner's call. Pausing/cancelling stays with the detectors
  that mean it (part gone, spaghetti, the printer's own noodle verdict).

THRESHOLDS are calibrated on one camera (docs/research/print-quality-monitor.md): clean first layer fleck_frac
  0.011-0.025 / order 0.83-0.85, stringing 0.039-0.087 / 0.63-0.74.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# issues
STRINGING, TEXTURE, FIRST_LAYER = "stringing", "texture", "first_layer"
ISSUES = (STRINGING, TEXTURE, FIRST_LAYER)

# levels (rank = index)
OK, NOTICE, WARNING, SEVERE = "ok", "notice", "warning", "severe"
LEVELS = (OK, NOTICE, WARNING, SEVERE)
# readings that carry no judgement
BLIND, ABSENT, LEARNING = "blind", "absent", "learning"

# the severity names of printer_ops.watch / jobwatch (a quality finding is never above WARNING)
WATCH_SEVERITY = {NOTICE: "info", WARNING: "warning", SEVERE: "warning"}

# Owner-facing text (Russian: the owner's language) per issue: what is seen, what to change.
SEEN = {
    STRINGING: "на детали белые ниточки и крошки (стринг)",
    TEXTURE: "поверхность потеряла ровный рисунок линий: полосы и шероховатость (недо- или переэкструзия)",
    FIRST_LAYER: "первый слой неровный: рваные линии, пропуски или ниточки между островами",
}
CHANGE = {
    STRINGING: "снизить температуру сопла на 5-10 °C, подсушить филамент, включить ретракт и вайп, "
    "«не пересекать стенки»",
    TEXTURE: "проверить сухость филамента и сопло, откалибровать поток и температуру",
    FIRST_LAYER: "протереть стол (IPA), проверить Z-offset и поток первого слоя, снизить его скорость",
}
LEVEL_RU = {NOTICE: "заметил", WARNING: "внимание", SEVERE: "усиливается"}


@dataclass(frozen=True)
class QualityConfig:
    enabled: bool = True
    baseline_n: int = 4  # readings with the part on the plate that make the reference
    # debris: a level needs BOTH its ratio to the baseline AND its absolute fleck_frac
    fleck_floor: float = 0.012  # a baseline below this counts as this (a spotless first layer must not make 0.03 a disaster)
    notice_ratio: float = 1.7
    notice_abs: float = 0.03
    warning_ratio: float = 2.0
    warning_abs: float = 0.035
    severe_ratio: float = 3.5
    severe_abs: float = 0.06
    # pattern loss: `order` under the baseline by this much
    notice_drop: float = 0.05
    warning_drop: float = 0.10
    severe_drop: float = 0.18
    # first layer: an absolute floor (no baseline can exist before the first layer is there)
    first_layer_max: int = 3
    first_layer_z: float = 0.8  # without a layer number: Z up to this is "first layers"
    # `order` under this in a first layer is `first_layer` warning (0 = off). Clean beads on the U1 camera read
    # 0.83-0.85, a stringy first layer 0.33-0.53 — but another camera/scene has its own normal (a K1 SE view of
    # a part reads 0.5), so the floor is ON only f.
    first_layer_order: float = 0.0
    first_layer_severe: float = 0.45
    window: int = 4
    confirm: int = 2
    severe_confirm: int = 3
    clear_after: int = 6
    adapt: float = 0.05  # how fast a clean reading moves the baseline ...
    adapt_fleck_cap: float = 1.3  # ... which never rises above this multiple of the first-layer value (debris must not be absorbed)
    adapt_order_cap: float = 0.04  # ... nor falls further than this under it
    trend_n: int = 5  # readings the slope needs
    trend_rise: float = (
        0.5  # ratio-to-baseline gained per 10 minutes that counts as "rising"
    )
    min_gap_s: float = 12.0  # a camera check this soon after the last quality reading is skipped (rechecks of the part check)
    timeline: int = 200  # readings kept in the report


def first_layer_floor(order: float, cfg: QualityConfig) -> str:
    """The absolute first-layer level of an `order` reading (`ok` when the floor is off)."""
    if cfg.first_layer_order <= 0:
        return OK
    if order < min(cfg.first_layer_severe, cfg.first_layer_order):
        return SEVERE
    return WARNING if order < cfg.first_layer_order else OK


PROFILES_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "vision" / "quality_profiles.json"
)


def profile_for(model: str | None) -> dict[str, Any]:
    """The calibrated defaults of a printer model (`device.model`) from lib/data/vision/quality_profiles.json
    (matched on the model name stripped to lowercase letters and digits, by its ending), else none."""
    key = re.sub(r"[^a-z0-9]", "", (model or "").lower())
    if not key:
        return {}
    profiles = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    hit: dict[str, Any] = next(
        (v for k, v in profiles.items() if not k.startswith("_") and key.endswith(k)),
        {},
    )
    return dict(hit)


def quality_config(settings: Any, model: str | None = None) -> QualityConfig:
    """`<printer>.watch.quality` (a mapping of QualityConfig fields; `false` turns the monitor off) -> config.
    Unknown keys and values of the wrong type are an InvalidArgument naming the accepted keys."""
    from dataclasses import fields, replace

    from errors import InvalidArgument

    if settings is False:
        return QualityConfig(enabled=False)
    base = replace(QualityConfig(), **profile_for(model))
    if settings is None or settings is True:
        return base
    defaults = {f.name: getattr(QualityConfig(), f.name) for f in fields(QualityConfig)}
    accepted = sorted(defaults)
    if not isinstance(settings, dict):
        raise InvalidArgument(
            "watch.quality", repr(settings), accepted, command="printer watch"
        )
    for key, value in settings.items():
        want = type(defaults.get(key))
        numeric = (
            want in (int, float)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        if key not in defaults or not (numeric or isinstance(value, want)):
            raise InvalidArgument(
                f"watch.quality.{key}", repr(value), accepted, command="printer watch"
            )
    return replace(base, **settings)


@dataclass
class QualityReading:
    """One camera reading: the numbers of print_quality.py plus where in the job it was taken."""

    now: float
    numbers: dict[str, Any]
    layer: int | None = None
    z: float | None = None
    progress: float | None = None
    present: bool = True  # the part stands on the plate (jobwatch: bed_part says so); False = not judged
    frame: str | None = None  # the picture the numbers came from (kept as evidence)
    annotated: str | None = None

    @property
    def ok(self) -> bool:
        return self.numbers.get("status") == "ok"


@dataclass(frozen=True)
class QualityFinding:
    """One thing to tell the owner. Never critical."""

    kind: str  # quality_<issue>_<level>: one alert per kind per job
    issue: str
    level: str
    text: str  # Russian, short: what is seen and what to change
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return WATCH_SEVERITY[self.level]


@dataclass
class QualityVerdict:
    state: (
        str  # ok | notice | warning | severe | blind | absent | learning (this reading)
    )
    raw: str = OK  # this reading alone
    level: str = OK  # the latched level after this reading
    issue: str | None = None
    new: list[QualityFinding] = field(default_factory=list)  # alerts to send now
    fleck_ratio: float | None = None
    order_drop: float | None = None
    trend: float | None = (
        None  # debris ratio gained per 10 minutes (None: too few readings)
    )
    notes: list[str] = field(default_factory=list)
    worst: bool = (
        False  # the worst reading so far: the caller keeps its frame (`pin_worst`)
    )


def rank(level: str) -> int:
    return LEVELS.index(level)


def _level_of(value: float, steps: tuple[float, float, float]) -> str:
    """`ok` below the first step, else the highest step reached."""
    n = sum(value >= s for s in steps)
    return LEVELS[n]


def _slope_per_10min(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (time s, value), per 10 minutes; None without a usable spread of time."""
    n = len(points)
    if n < 2:
        return None
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    if var <= 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in points) / var * 600.0


@dataclass
class Baseline:
    fleck: float
    order: float
    n: int
    fleck0: float = 0.0  # as first learnt: the drift is bounded around these
    order0: float = 0.0

    def __post_init__(self) -> None:
        self.fleck0, self.order0 = self.fleck, self.order

    def fleck_eff(self, floor: float) -> float:
        return max(self.fleck, floor)

    def fleck_eff0(self, floor: float) -> float:
        return max(self.fleck0, floor)


@dataclass
class QualityReport:
    """What the monitor saw: counts per severity, the worst reading, the alerts, the timeline."""

    printer: str = ""
    job: str = ""
    readings: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # blind / absent / learning
    per_level: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(LEVELS, 0)
    )  # raw, judged readings
    per_issue: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ISSUES, 0))
    max_level: str = OK  # highest REPORTED (latched) level
    alerts: list[dict[str, Any]] = field(default_factory=list)
    worst: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)
    started: float | None = None
    ended: float | None = None
    keep: int = 200

    def to_json(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "job": self.job,
            "readings": self.readings,
            "judged": sum(self.per_level.values()),
            "skipped": self.skipped,
            "per_level": self.per_level,
            "per_issue": self.per_issue,
            "max_level": self.max_level,
            "alerts": self.alerts,
            "worst": self.worst,
            "baseline": self.baseline,
            "started": self.started,
            "ended": self.ended,
            "timeline": self.timeline,
        }


class QualityJudge:
    def __init__(
        self, cfg: QualityConfig | None = None, printer: str = "", job: str = ""
    ) -> None:
        self.cfg = cfg or QualityConfig()
        self.base: Baseline | None = None
        self._learn: list[dict[str, Any]] = []
        self.level = OK
        self._raw: deque[tuple[str, str | None]] = deque(maxlen=self.cfg.window)
        self._clean = 0
        self._alerted: set[str] = set()
        self._ratios: deque[tuple[float, float]] = deque(
            maxlen=max(self.cfg.trend_n, 8)
        )
        self.report = QualityReport(printer=printer, job=job, keep=self.cfg.timeline)

    # -- one reading

    def feed(self, r: QualityReading) -> QualityVerdict:
        v = self._judge(r)
        self._record(r, v)
        return v

    def _judge(self, r: QualityReading) -> QualityVerdict:
        if not r.ok:
            return QualityVerdict(
                BLIND, notes=[str(r.numbers.get("reason") or "no reading")]
            )
        if not r.present:
            return QualityVerdict(ABSENT, notes=["nothing stands on the plate yet"])
        first = self._first_layer(r)
        floor = self._floor_level(r, first)
        if self.base is None:
            return self._learning(r, floor)
        return self._compare(r, floor)

    def _first_layer(self, r: QualityReading) -> bool:
        if r.layer is not None:
            return r.layer <= self.cfg.first_layer_max
        return r.z is not None and r.z <= self.cfg.first_layer_z

    def _floor_level(self, r: QualityReading, first: bool) -> str:
        """The first-layer absolute floor on `order` (needs no baseline)."""
        if not first:
            return OK
        return first_layer_floor(float(r.numbers["order"]), self.cfg)

    def _learning(self, r: QualityReading, floor: str) -> QualityVerdict:
        # A reading that already fails the first-layer floor is judged, but never makes the reference.
        if floor == OK:
            self._learn.append(r.numbers)
            if len(self._learn) >= self.cfg.baseline_n:
                self.base = self._baseline_of(self._learn)
                self.report.baseline = {
                    "fleck_frac": self.base.fleck,
                    "order": self.base.order,
                    "n": self.base.n,
                }
        v = QualityVerdict(
            LEARNING if floor == OK else floor,
            raw=floor,
            notes=["learning the job's own baseline"],
        )
        if floor != OK:
            v.issue = FIRST_LAYER
            self._confirm(v, r)
        else:
            v.level = self.level
        return v

    @staticmethod
    def _baseline_of(readings: list[dict[str, Any]]) -> Baseline:
        def median(key: str) -> float:
            xs = sorted(float(x[key]) for x in readings)
            m = len(xs) // 2
            return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2

        return Baseline(median("fleck_frac"), median("order"), len(readings))

    def _compare(self, r: QualityReading, floor: str) -> QualityVerdict:
        assert self.base is not None
        cfg = self.cfg
        fleck = float(r.numbers["fleck_frac"])
        order = float(r.numbers["order"])
        ratio = fleck / self.base.fleck_eff(cfg.fleck_floor)
        drop = self.base.order - order
        debris = _level_of(
            ratio, (cfg.notice_ratio, cfg.warning_ratio, cfg.severe_ratio)
        )
        absolute = _level_of(fleck, (cfg.notice_abs, cfg.warning_abs, cfg.severe_abs))
        debris = LEVELS[min(rank(debris), rank(absolute))]
        pattern = _level_of(drop, (cfg.notice_drop, cfg.warning_drop, cfg.severe_drop))
        v = QualityVerdict(OK, fleck_ratio=round(ratio, 2), order_drop=round(drop, 3))
        v.trend = self._trend(r.now, fleck / self.base.fleck_eff0(cfg.fleck_floor))
        if debris == NOTICE and v.trend is not None and v.trend >= cfg.trend_rise:
            debris = WARNING
            v.notes.append(f"debris is rising: +{v.trend:.1f} x baseline per 10 min")
        v.issue, v.raw = _pick(debris, pattern, floor)
        v.state = v.raw
        self._confirm(v, r)
        if v.raw == OK and self.level == OK:
            self._adapt(fleck, order)
        return v

    def _trend(self, now: float, ratio: float) -> float | None:
        self._ratios.append((now, ratio))
        pts = list(self._ratios)[-self.cfg.trend_n :]
        return _slope_per_10min(pts) if len(pts) >= self.cfg.trend_n else None

    def _adapt(self, fleck: float, order: float) -> None:
        """A clean reading drifts the baseline a little (a different infill reads differently) — within bounds:
        a debris that grows slowly through the notice band must not be absorbed into 'normal'."""
        assert self.base is not None
        cfg, b = self.cfg, self.base
        b.fleck = min(
            b.fleck + cfg.adapt * (fleck - b.fleck), b.fleck0 * cfg.adapt_fleck_cap
        )
        b.order = max(
            b.order + cfg.adapt * (order - b.order), b.order0 - cfg.adapt_order_cap
        )

    def _confirm(self, v: QualityVerdict, r: QualityReading) -> None:
        """Window the raw levels, latch / clear the reported level, and emit the alert for a new one."""
        cfg = self.cfg
        self._raw.append((v.raw, v.issue))
        self._clean = self._clean + 1 if v.raw == OK else 0
        reached = self._reached()
        if rank(reached) > rank(self.level):
            self.level = reached
            v.new = self._alerts(v, reached, r)
        elif self._clean >= cfg.clear_after and self.level != OK:
            self.level = OK
            v.notes.append("quality is back to normal")
        v.level = self.level

    def _reached(self) -> str:
        """The highest level that `confirm` of the window's readings reach (`severe_confirm` for severe)."""
        cfg = self.cfg
        raws = [x for x, _ in self._raw]
        for level in (SEVERE, WARNING, NOTICE):
            need = cfg.severe_confirm if level == SEVERE else cfg.confirm
            if sum(rank(x) >= rank(level) for x in raws) >= need:
                return level
        return OK

    def _alerts(
        self, v: QualityVerdict, level: str, r: QualityReading
    ) -> list[QualityFinding]:
        issue = self._dominant(v)
        kind = f"quality_{issue}_{level}"
        owed = [kind] if kind not in self._alerted else []
        self._alerted.update(
            f"quality_{issue}_{x}" for x in LEVELS[1 : rank(level) + 1]
        )
        if not owed:
            return []
        finding = QualityFinding(
            kind,
            issue,
            level,
            owner_text(issue, level, v),
            self._data(r, v, issue, level),
        )
        self.report.alerts.append(
            {
                "kind": kind,
                "level": level,
                "issue": issue,
                "now": r.now,
                "layer": r.layer,
                "frame": r.frame,
                "text": finding.text,
            }
        )
        return [finding]

    def _dominant(self, v: QualityVerdict) -> str:
        """The issue of the strongest recent reading (the current one when it ties)."""
        best, issue = -1, v.issue or STRINGING
        for raw, iss in self._raw:
            if iss is not None and rank(raw) >= best:
                best, issue = rank(raw), iss
        return issue

    def _data(
        self, r: QualityReading, v: QualityVerdict, issue: str, level: str
    ) -> dict[str, Any]:
        return {
            "issue": issue,
            "level": level,
            "layer": r.layer,
            "z": r.z,
            "numbers": {
                k: r.numbers.get(k)
                for k in ("fleck_frac", "fleck_per10k", "order", "luma")
            },
            "baseline": self.report.baseline,
            "fleck_ratio": v.fleck_ratio,
            "order_drop": v.order_drop,
            "trend": v.trend,
        }

    # -- the report

    def _record(self, r: QualityReading, v: QualityVerdict) -> None:
        rep = self.report
        rep.readings += 1
        rep.started = r.now if rep.started is None else rep.started
        rep.ended = r.now
        judged = v.state not in (BLIND, ABSENT, LEARNING)
        if v.state in (BLIND, ABSENT, LEARNING):
            rep.skipped[v.state] = rep.skipped.get(v.state, 0) + 1
        if judged:
            rep.per_level[v.raw] += 1
            if v.issue is not None and v.raw != OK:
                rep.per_issue[v.issue] += 1
        if rank(self.level) > rank(rep.max_level):
            rep.max_level = self.level
        v.worst = self._worst(r, v, judged)
        rep.timeline.append(_timeline_row(r, v))
        del rep.timeline[: -self.cfg.timeline]

    def pin_worst(self, path: str) -> None:
        """The caller copied the worst frame to a stable `path` (the camera's own file is overwritten by the next one)."""
        if self.report.worst is not None:
            self.report.worst["frame"] = path

    def _worst(self, r: QualityReading, v: QualityVerdict, judged: bool) -> bool:
        if not judged or v.raw == OK:
            return False
        score = (rank(v.raw), float(r.numbers.get("fleck_frac") or 0.0))
        cur = self.report.worst
        if cur is not None and score <= tuple(cur["score"]):
            return False
        self.report.worst = {
            "score": list(score),
            "level": v.raw,
            "issue": v.issue,
            "now": r.now,
            "layer": r.layer,
            "z": r.z,
            "frame": r.annotated or r.frame,
            "raw_frame": r.frame,
            "numbers": {
                k: r.numbers.get(k)
                for k in ("fleck_frac", "fleck_per10k", "order", "luma")
            },
        }
        return True


def _pick(debris: str, pattern: str, floor: str) -> tuple[str | None, str]:
    """(issue, raw level) of one reading: debris is stringing, pattern loss without debris is texture, the
    first-layer floor is its own issue; the strongest wins, ties go to stringing then first_layer."""
    options = [
        (rank(debris), 3, STRINGING),
        (rank(floor), 2, FIRST_LAYER),
        (rank(pattern), 1, TEXTURE),
    ]
    best = max(options)
    return (best[2], LEVELS[best[0]]) if best[0] > 0 else (None, OK)


def absolute_verdict(
    numbers: dict[str, Any], z: float | None, cfg: QualityConfig
) -> tuple[str | None, str]:
    """(issue, level) of ONE frame with no job history: only what needs no baseline. While the first layers print
    (Z <= first_layer_z) the plate is mostly bare and its own glints read as debris, so only the first-layer
    floor on `order` applies; above them the absolute debris levels do. `vision --once` uses it; a watch never
    does (it has the baseline)."""
    if numbers.get("status") != "ok":
        return None, OK
    if z is not None and z <= cfg.first_layer_z:
        return _pick(OK, OK, first_layer_floor(float(numbers["order"]), cfg))
    debris = _level_of(
        float(numbers["fleck_frac"]), (cfg.notice_abs, cfg.warning_abs, cfg.severe_abs)
    )
    return _pick(debris, OK, OK)


def _timeline_row(r: QualityReading, v: QualityVerdict) -> dict[str, Any]:
    return {
        "now": r.now,
        "layer": r.layer,
        "state": v.state,
        "level": v.level,
        "issue": v.issue,
        "fleck_frac": r.numbers.get("fleck_frac"),
        "order": r.numbers.get("order"),
        "fleck_ratio": v.fleck_ratio,
        "order_drop": v.order_drop,
    }


def owner_text(issue: str, level: str, v: QualityVerdict) -> str:
    """The tg message, Russian, short: what is seen, how bad, what to change (the sender adds that nothing was touched)."""
    numbers = []
    if v.fleck_ratio is not None and issue == STRINGING:
        numbers.append(f"мусора в {v.fleck_ratio:g}× больше, чем на первых слоях")
    if v.order_drop is not None and issue == TEXTURE:
        numbers.append(f"рисунок линий хуже на {v.order_drop:.2f}")
    extra = f" ({', '.join(numbers)})" if numbers else ""
    return f"{LEVEL_RU[level]}: {SEEN[issue]}{extra}. Что менять: {CHANGE[issue]}."
