"""vision_quality.py — the print-quality tier of `3d printer vision`: is the print GOOD, not just alive?

Tiers (a) and (b) of the vision stack look for FAILURES (spaghetti, a part off the plate, a foreign object).
None of them knows stringing: on 2026-11-05 the Obico model scored p 0.0 / q 0.0 on all 12 frames of a job that
stringed from layer 2 on (docs/research/print-quality-monitor.md). This tier measures the frame the way the job
watch does (lib/print_quality.py: white debris, bead-pattern order) and judges it with the same pure state
machine (printer_ops/quality.py: the job's OWN first layers are the baseline, persistence, one alert per issue
and level, a debris trend), then hands the result to VisionMonitor as `Finding`s:

  quality_<issue>_<level>   warning / severe  -> a WARNING event: tg --photo FRAME "<russian text>" via the
                            watch dispatcher, once per issue and level; NEVER a pause (only CRITICAL events
                            can reach the auto-pauser, and this tier cannot produce one);
                            notice -> an INFO event (subscribers that asked for info), report only otherwise.

PRESENCE: during the first layers (Z <= `first_layer_z`) the print's first frame is the empty-plate reference and
  the part stands on the plate once a frame differs from it (the same colour difference as the bed check); past
  them a part is always there. A print adopted mid-way has no empty reference: judged from its first layer-above
  frame on, its baseline is whatever it looks like then. Needs the camera
  position context (`ContextTap`: the print's Z and the plate region at that Z); without it the tier is silent
  and says why in `--once`.

`once`: a one-frame look (`printer vision NAME --once`) has no history: it reports the numbers and the ABSOLUTE
  verdict only (printer_ops.quality.absolute_verdict) and says so; a quality finding there is a hint to look at
  the frame, not a confirmed one.

REPORT: `finish()` writes the per-print monitoring report (printer_ops/quality_report.py) into the evidence folder
  when the print ends or another one starts.

INVARIANTS: read-only; heavy imports (numpy / OpenCV, bed_state) are lazy; a measurement error is a `blind`
  reading in the report, never an exception in the vision loop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.quality import (  # noqa: E402
    NOTICE,
    OK,
    QualityConfig,
    QualityFinding,
    QualityJudge,
    QualityReading,
    absolute_verdict,
    owner_text,
    quality_config,
)
from printer_ops.quality_report import summary_line, write_report  # noqa: E402
from printer_ops.vision import Finding, PrintContext  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

PRESENT_PROC_W = 480  # the width the bed check compares at (bed_state.PROC_W)


class QualityTier:
    def __init__(
        self,
        device: Any,
        cfg: QualityConfig,
        evidence_dir: Path | None = None,
        *,
        once: bool = False,
    ) -> None:
        self.device, self.cfg, self.evidence_dir, self.once = (
            device,
            cfg,
            evidence_dir,
            once,
        )
        self.judge = QualityJudge(cfg, printer=device.name)
        self.base: tuple[np.ndarray, float] | None = (
            None  # the first frame of the print at 480 px, its Z
        )
        self.last: dict[str, Any] = {}

    # -- print boundaries

    def reset_print(self) -> None:
        """A new print: the report of the one before is written first, then everything starts over."""
        self.finish()
        self.judge = QualityJudge(self.cfg, printer=self.device.name)
        self.base = None
        self.last = {}

    def finish(self) -> Path | None:
        """Write the monitoring report of the print that just ended (no readings: nothing to write)."""
        report = self.judge.report
        if report.readings == 0 or self.evidence_dir is None:
            return None
        folder = write_report(report, self.evidence_dir, self.device.name)
        print(
            f"{self.device.name}: {summary_line(report)} -> {folder}",
            file=sys.stderr,
            flush=True,
        )
        self.judge = QualityJudge(self.cfg, printer=self.device.name)
        return folder

    # -- one frame

    def update(
        self, bgr: np.ndarray, ctx: PrintContext | None, now: float
    ) -> list[Finding]:
        """Measure, judge, and return the Findings that are owed to the owner right now."""
        if ctx is None:
            self.last = {
                "status": "blind",
                "reason": "no print position context (the plate region needs the Z)",
            }
            return []
        import print_quality as PQ

        img = PQ.prepare(bgr)
        shape = (img.shape[0], img.shape[1])
        region = PQ.shrink(PQ.device_region(self.device, ctx.z, shape))
        numbers, flecks = PQ.measure(
            img, region, f"plate surface of {self.device.name} at Z {ctx.z:g}"
        )
        self.last = dict(numbers)
        if numbers["status"] != "ok":
            self.judge.feed(QualityReading(now, numbers, z=ctx.z))
            return []
        label = f"fleck {numbers['fleck_frac']:.3f} order {numbers['order']:.2f}"
        image = PQ.annotated_jpeg(img, region, flecks, label)
        if self.once:
            return self._once(numbers, ctx, image)
        reading = QualityReading(
            now, numbers, z=ctx.z, present=self._present(img, ctx.z)
        )
        v = self.judge.feed(reading)
        self.last.update(
            state=v.state,
            level=v.level,
            issue=v.issue,
            fleck_ratio=v.fleck_ratio,
            order_drop=v.order_drop,
        )
        if v.worst:
            self._pin(image)
        return [self._finding(f, image) for f in v.new]

    def _once(
        self, numbers: dict[str, Any], ctx: PrintContext, image: bytes
    ) -> list[Finding]:
        issue, level = absolute_verdict(numbers, ctx.z, self.cfg)
        self.last.update(
            basis="absolute: one frame, no job baseline", level=level, issue=issue
        )
        if issue is None or level in (OK, NOTICE):
            return []
        from printer_ops.quality import QualityVerdict

        text = owner_text(issue, level, QualityVerdict(level, issue=issue))
        data = {"issue": issue, "level": level, "numbers": numbers, "basis": "absolute"}
        return [
            self._finding(
                QualityFinding(f"quality_{issue}_{level}", issue, level, text, data),
                image,
            )
        ]

    def _present(self, img: np.ndarray, z: float) -> bool:
        """The part stands on the plate. Past the first layers it certainly does; during them the frame must differ
        from the print's first (empty-plate) frame."""
        import cv2

        if z > self.cfg.first_layer_z:
            return True
        w = PRESENT_PROC_W
        small = cv2.resize(
            img,
            (w, max(1, round(img.shape[0] * w / img.shape[1]))),
            interpolation=cv2.INTER_AREA,
        )
        if self.base is None:
            self.base = (small, z)
            return False
        return self._differs(self.base[0], small, z)

    def _differs(self, base: np.ndarray, small: np.ndarray, z: float) -> bool:
        import bed_part as BP
        import bed_state as B

        cfg = B.bed_config(self.device)
        region = BP.plate_region(self.device, cfg, z, small.shape[:2])
        th = B.thresholds_for(cfg)
        d = B.difference(
            base, small, region, th.level, th.detrend_px, th.shift_px, th.open_px
        )
        return d.frac >= PRESENT_MIN and d.blob * int(region.sum()) >= PRESENT_BLOB_PX

    # -- helpers

    def _pin(self, image: bytes) -> None:
        if self.evidence_dir is None:
            return
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / f"{self.device.name}-quality-worst.jpg"
        path.write_bytes(image)
        self.judge.pin_worst(str(path))

    @staticmethod
    def _finding(f: QualityFinding, image: bytes) -> Finding:
        return Finding(f.kind, f.severity, f.text, dict(f.data), image)

    def once_report(self) -> dict[str, Any]:
        """The `quality` block of `vision --once`: the numbers, the verdict and what it is based on."""
        return {k: v for k, v in self.last.items() if k != "annotated"}


PRESENT_MIN = 0.004  # the changed share of the plate that says "something stands there" (jobwatch.JobConfig.present_min)
PRESENT_BLOB_PX = (
    150  # ... and the largest changed region, px at 480 (JobConfig.present_blob_px)
)


def build_quality(
    device: Any, evidence_dir: Path | None, *, once: bool, disabled: bool
) -> QualityTier | None:
    """The tier of a printer from `<printer>.watch.quality` (False = off); None when it is off."""
    cfg = quality_config(
        (getattr(device, "watch", None) or {}).get("quality"),
        getattr(device, "model", None),
    )
    if disabled or not cfg.enabled:
        return None
    return QualityTier(device, cfg, evidence_dir, once=once)
