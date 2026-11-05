"""quality_report.py — the per-job MONITORING REPORT: what the print-quality monitor saw, for a human.

`3d printer watch NAME --job` writes it when the watch ends (job complete, lost print, or --duration over) and
`3d printer vision` when a print ends, into a `<stamp>-NAME-monitor-report/` folder next to the other evidence
(`print/runs/` for the job watch, the vision evidence folder for vision):

    report.json       QualityReport.to_json(): readings, counts per severity and per issue, the alerts that were
                      sent, the baseline, the worst reading, the timeline
    report.md         the same in words (English, short): a table a human can read in ten seconds
    worst-frame.jpg   the frame of the worst reading (annotated when there is one: debris in red)

A job nothing was wrong with still gets its report ("0 warnings in 41 judged readings"): the point is that a
human can see what the monitor LOOKED AT, and that "silent" means "checked and fine" rather than "never ran".
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from printer_ops.quality import LEVELS, QualityReport


def summary_line(report: QualityReport) -> str:
    judged = sum(report.per_level.values())
    skipped = sum(report.skipped.values())
    counts = ", ".join(f"{report.per_level[x]} {x}" for x in LEVELS[1:])
    worst = report.worst
    where = (
        f"; worst: {worst['issue']} {worst['level']} at layer {worst['layer']}"
        if worst
        else ""
    )
    return (
        f"print-quality monitor: {report.readings} readings ({judged} judged, {skipped} not: "
        f"{_skips(report)}): {report.per_level['ok']} ok, {counts}; highest reported level {report.max_level}{where}"
    )


def _skips(report: QualityReport) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(report.skipped.items())) or "none"


def render_markdown(report: QualityReport) -> str:
    doc = report.to_json()
    lines = [
        f"# Print-quality monitor: {doc['printer']} {doc['job']}",
        "",
        f"- readings: {doc['readings']} ({doc['judged']} judged; not judged: {_skips(report)})",
        f"- highest level REPORTED to the owner: **{doc['max_level']}**",
        "",
        "| severity (per reading) | readings |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in doc["per_level"].items()],
        "",
        "| issue | readings |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in doc["per_issue"].items()],
        "",
    ]
    lines += _baseline_lines(doc)
    lines += _alert_lines(doc)
    lines += _worst_lines(doc)
    return "\n".join(lines) + "\n"


def _baseline_lines(doc: dict[str, Any]) -> list[str]:
    b = doc["baseline"]
    if b is None:
        return [
            "Baseline: never formed (fewer than the required readings with the part on the plate).",
            "",
        ]
    return [
        f"Baseline (the job's own first layers, {b['n']} readings): fleck_frac {b['fleck_frac']:.4f}, order {b['order']:.3f}.",
        "",
    ]


def _alert_lines(doc: dict[str, Any]) -> list[str]:
    if not doc["alerts"]:
        return ["Alerts sent: none.", ""]
    out = [
        "Alerts (one per issue and level; a quality alert never pauses or cancels):",
        "",
    ]
    out.extend(
        f"- layer {a['layer']}: **{a['level']}** {a['issue']} — {a['text']}"
        for a in doc["alerts"]
    )
    return [*out, ""]


def _worst_lines(doc: dict[str, Any]) -> list[str]:
    w = doc["worst"]
    if w is None:
        return ["Worst reading: none (every judged reading was ok).", ""]
    n = w["numbers"]
    return [
        f"Worst reading: {w['level']} {w['issue']}, layer {w['layer']}, fleck_frac {n.get('fleck_frac')}, "
        f"order {n.get('order')}; frame: worst-frame.jpg",
        "",
    ]


def write_report(report: QualityReport, out_dir: Path, printer: str) -> Path:
    """Write report.json / report.md / worst-frame.jpg into `<out_dir>/<stamp>-<printer>-monitor-report/`."""
    ended = report.ended if report.ended is not None else 0.0
    stamp = datetime.fromtimestamp(ended, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = out_dir / f"{stamp}-{printer}-monitor-report"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(
        json.dumps(report.to_json(), indent=1, default=str) + "\n", encoding="utf-8"
    )
    (d / "report.md").write_text(render_markdown(report), encoding="utf-8")
    worst = report.worst
    src = worst.get("frame") if worst else None
    if src and Path(src).is_file():
        shutil.copyfile(src, d / "worst-frame.jpg")
    return d
