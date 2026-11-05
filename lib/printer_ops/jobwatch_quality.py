"""jobwatch_quality.py — the print-quality monitor inside `3d printer watch NAME --job`.

One camera check of the job watch (jobwatch_run.JobWatch.check) already has the frame and the bed_part reading
(is the part on the plate). `QualityWatch.check` takes the same frame, asks lib/print_quality.py for its numbers
(a subprocess with numpy + OpenCV, like bed_part.py), feeds the pure `QualityJudge` (printer_ops/quality.py) and
returns the NON-critical jobwatch Findings it owes the owner: one per issue and level (warning, severe). A
`notice` is not a finding: it only counts in the report. A quality finding is never critical, so `decide`
answers `alert` for it: the owner gets `tg --photo FRAME "<text>"` and the job is not touched.

`finish` writes the monitoring report (quality_report.py) when the watch ends — also when it ends because a part
was lost: what the monitor saw until then is part of the evidence.

INVARIANTS: read-only like the rest of the camera side; a tool failure is a `blind` reading (counted in the report),
never an exception that stops the watch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from printer_ops.jobwatch import Finding, JobConfig, Observation
from printer_ops.quality import (
    NOTICE,
    QualityConfig,
    QualityFinding,
    QualityJudge,
    QualityReading,
    QualityVerdict,
)
from printer_ops.quality_report import summary_line, write_report

QualityTool = Callable[[list[str]], dict[str, Any]]
ROTATE = 4  # annotated frames kept (the evidence copies live in the alert's own folder)


def run_quality_tool(argv: list[str]) -> dict[str, Any]:
    """print_quality.py through cli.pyrun (numpy + OpenCV): its JSON answer, or a blind reading saying why not."""
    from cli.pyrun import tool_argv
    from printer_ops.bed import BED_DEPS

    try:
        r = subprocess.run(
            tool_argv(BED_DEPS, "print_quality.py", argv),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            tail = (r.stderr or "")[-200:]
            return {
                "status": "blind",
                "reason": f"the quality tool failed (exit {r.returncode}): {tail}",
            }
        return dict(json.loads(r.stdout))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"status": "blind", "reason": f"the quality tool failed: {exc}"}


def part_present(o: Observation, cfg: JobConfig) -> bool:
    """Something stands on the plate: bed_part says so (the same test as the part's `seen`); without a bed_part
    reading (blind) the print is taken to have a part from layer 2 on."""
    part = o.part
    if part is not None and part.status == "ok":
        return part.changed >= cfg.present_min and part.blob_px >= cfg.present_blob_px
    return (o.layer or 0) >= 2


class QualityWatch:
    def __init__(
        self,
        name: str,
        run_dir: Path,
        tool: QualityTool,
        cfg: QualityConfig,
        job_cfg: JobConfig,
        job: str,
    ) -> None:
        self.name, self.dir, self.tool, self.job_cfg = name, run_dir, tool, job_cfg
        self.judge = QualityJudge(cfg, printer=name, job=job)
        self.n = 0
        self.last: QualityReading | None = None
        self.last_verdict: QualityVerdict | None = None

    # -- one reading

    def check(self, jpeg: bytes, o: Observation) -> list[Finding]:
        """Measure the frame, judge it, and return the findings to alert (warning / severe only)."""
        if self.last is not None and o.now - self.last.now < self.judge.cfg.min_gap_s:
            self.last_verdict = None  # a recheck of the part check, seconds after the last look: nothing new to say
            return []
        reading = self.read(jpeg, o)
        v = self.judge.feed(reading)
        self.last, self.last_verdict = reading, v
        if v.worst:
            self.pin(reading)
        return [self.finding(f) for f in v.new if f.level != NOTICE]

    def read(self, jpeg: bytes, o: Observation) -> QualityReading:
        self.n += 1
        frame = self.dir / "quality-frame.jpg"
        annotated = self.dir / f"quality-annotated-{self.n % ROTATE}.jpg"
        frame.write_bytes(jpeg)
        argv = [
            "check",
            self.name,
            "--frame",
            str(frame),
            "--z",
            str(o.z if o.z is not None else 0.0),
            "--annotated",
            str(annotated),
            "--json",
        ]
        numbers = self.tool(argv)
        return QualityReading(
            o.now,
            numbers,
            layer=o.layer,
            z=o.z,
            progress=o.progress,
            present=part_present(o, self.job_cfg),
            frame=str(frame),
            annotated=str(annotated) if numbers.get("annotated") else None,
        )

    def pin(self, r: QualityReading) -> None:
        """Keep the worst frame: the rotating files are overwritten by the next readings."""
        src = r.annotated or r.frame
        if src and Path(src).is_file():
            dst = self.dir / "quality-worst.jpg"
            shutil.copyfile(src, dst)
            self.judge.pin_worst(str(dst))

    @staticmethod
    def finding(f: QualityFinding) -> Finding:
        return Finding(f.kind, False, f.text, dict(f.data))

    def alert_files(self) -> dict[str, str | None]:
        """The frames to send with the alert: the one with the debris marked first, then the plain one."""
        r = self.last
        if r is None:
            return {}
        return {"frame.quality.jpg": r.annotated, "frame.raw.jpg": r.frame}

    # -- the end

    def note(self) -> str | None:
        """The log line for the last reading (None: nothing worth saying)."""
        v, r = self.last_verdict, self.last
        if v is None or r is None:
            return None
        n = r.numbers
        if v.state in ("blind", "absent"):
            return f"quality {v.state}: {'; '.join(v.notes)}"
        return (
            f"quality {v.state} (level {v.level}): fleck {n.get('fleck_frac')} order {n.get('order')}"
            + (
                f" ratio {v.fleck_ratio} drop {v.order_drop}"
                if v.fleck_ratio is not None
                else ""
            )
        )

    def finish(self, out_dir: Path) -> tuple[Path | None, str]:
        """Write the monitoring report; -> (its folder, the one-line summary). No readings: no report."""
        if self.judge.report.readings == 0:
            return None, "print-quality monitor: no camera reading was taken"
        return write_report(self.judge.report, out_dir, self.name), summary_line(
            self.judge.report
        )
