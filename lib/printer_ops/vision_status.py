"""vision_status.py — the last camera verdict of a printer, as one small JSON file.

WHAT: `3d printer vision NAME` (the live loop) calls `VisionStatus.update` once per cycle; the file
  `<state dir>/vision/NAME.json` then holds what Home Assistant (and anything else) shows about the
  camera check of that printer: the printer state, the last frame's p / q / trend / findings, how
  many frames were scored, how many events of which kind were raised and the last one. 3d-api serves
  it as `GET /v1/printers/NAME/vision` (printops/api/routes_vision.py); the generated HA package
  (`3d ha vision`, lib/ha_vision.py) turns that into sensors for EVERY fleet printer with a camera.

INVARIANTS: stdlib only; writes are atomic (temp file + rename) so a reader never sees half a
  file; a failed write is logged on stderr and never stops the vision loop; the document carries
  `ts` (when it was written) so a stale file (the loop died) is told from a live one by its age.
  Nothing here touches a printer.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = 1
MAX_KINDS = 64  # distinct event kinds counted per run (a bound on the file, kinds are a fixed set)


def status_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "vision" / f"{printer}.json"


def read_status(printer: str, path: Path | None = None) -> dict[str, Any] | None:
    """The status document, or None when the loop never ran or the file is unreadable."""
    try:
        data = json.loads((path or status_path(printer)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


class VisionStatus:
    """Counts what one `3d printer vision` run saw and keeps the status file current."""

    def __init__(
        self, printer: str, path: Path | None = None, *, started: float | None = None
    ) -> None:
        self.printer = printer
        self.path = path or status_path(printer)
        self.started = time.time() if started is None else started
        self.frames = 0
        self.events: dict[str, int] = {}
        self.last_event: dict[str, Any] | None = None

    def update(
        self,
        *,
        state: str | None,
        filename: str | None,
        score: Any,
        events: list[Any],
        trend: float | None,
        camera_error: str | None,
        now: float | None = None,
        last_poll_ok: float | None = None,
        unreachable_since: float | None = None,
        reachable: bool = True,
        last_frame_ts: float | None = None,
        supervising: bool = False,
        job: dict[str, Any] | None = None,
        interval_s: float | None = None,
    ) -> dict[str, Any]:
        """Record one cycle and write the file; `score` is the frame's FrameScore (None: no frame).

        The liveness keys (`3d printer supervise status` / doctor read them): `last_poll_ok` (epoch of the
        last answered status poll), `unreachable_since` (epoch of the first unanswered poll of the current
        outage, None while it answers), `reachable`, `last_frame_ts` (epoch of the last grabbed camera
        frame), `supervising` (printing and the camera loop is fetching frames), `job` (`{"file", "started"}`
        while a job prints or is paused, else None) and `interval_s` (the loop's poll interval)."""
        now = time.time() if now is None else now
        if score is not None:
            self.frames += 1
        for event in events:
            kind = str(event.kind)
            if kind in self.events or len(self.events) < MAX_KINDS:
                self.events[kind] = self.events.get(kind, 0) + 1
            self.last_event = {
                "kind": kind,
                "severity": str(event.severity),
                "text": str(event.text),
                "ts": event.ts,
            }
        doc: dict[str, Any] = {
            "schema": SCHEMA,
            "printer": self.printer,
            "ts": now,
            "started": self.started,
            "state": state,
            "file": filename,
            "p": None if score is None else _num(score.p),
            "q": None if score is None else _num(score.q),
            "trend": _num(trend),
            "luma": None if score is None else _num(score.luma),
            "findings": []
            if score is None
            else sorted({str(f.kind) for f in score.findings}),
            "frames": self.frames,
            "events": {
                "total": sum(self.events.values()),
                "by_kind": dict(sorted(self.events.items())),
            },
            "last_event": self.last_event,
            "camera_error": camera_error,
            "last_poll_ok": _epoch(last_poll_ok),
            "unreachable_since": _epoch(unreachable_since),
            "reachable": bool(reachable),
            "last_frame_ts": _epoch(last_frame_ts),
            "supervising": bool(supervising),
            "job": None
            if job is None
            else {"file": job.get("file"), "started": _epoch(job.get("started"))},
            "interval_s": _num(interval_s),
        }
        self._write(doc)
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except (OSError, ValueError) as exc:
            print(f"vision status {self.path}: {exc}", file=sys.stderr, flush=True)


def _num(value: Any) -> float | None:
    """A finite number rounded for the file, else None (JSON has no NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 4) if math.isfinite(value) else None


def _epoch(value: Any) -> float | None:
    """A finite epoch in seconds (milliseconds kept), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3) if math.isfinite(value) else None
