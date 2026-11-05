"""bed_override.py — the owner's recorded word that the bed is empty (issue #723, item 5). Stdlib only.

WHY: the camera can be wrong about an empty bed (a false `occupied`, a stubborn `unknown`) and the only way
  out used to be `3d printer start --force`, which leaves no trace of WHO said so and why. `3d printer bed
  check NAME --assume-empty "why"` runs the normal check and then records the confirmation:
  * `assume-empty-<ts>.json` in the evidence dir (~/.cache/3d-cli/vision/bed/), next to the frames it is
    about: who ($USER), when, why, the printer, the verdict the camera gave, the frame paths, Z, the light,
    the job it is bound to;
  * a small state record ~/.local/state/3d-cli/bed/NAME.assumed.json {ts, why, ttl, job_marker, ...} that the
    `bed` line of the preflight (`printer_ops/preflight.py`) reads.

WHAT THE PREFLIGHT DOES WITH IT (`honoured`): the line is PASS with the note "owner confirmed empty at
  <time>: <why>" while the record is younger than its ttl (`--assume-empty-ttl`, minutes, default 60) AND no
  job has started since: the record carries a `job_marker` (Moonraker's newest history job id, its start
  time, print_stats.filename) and any other marker later means a job started. It never overrides a printing
  printer, and a camera that saw the plate missing is not overridden (the word is about the bed, not the
  plate). Without Moonraker's history component the marker is the file name only: a re-run of the SAME file
  is then not noticed (the ttl still ends the confirmation).

READ-ONLY on the printer: the marker is one history query + one objects query.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TTL_MIN = 60.0
BUSY_STATES = (
    "printing",
    "paused",
    "busy",
)  # printer_ops.base states: the bed belongs to the job


def assumed_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "bed" / f"{printer}.assumed.json"


def who() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def read_marker(adapter: Any) -> dict[str, Any]:
    """The job the printer is at: {job_id, start_time, filename} of Moonraker's newest history job and
    print_stats.filename. Raises what the adapter raises when the printer cannot be read; an adapter without a
    history (or a printer without the component) gives job_id None."""
    status = adapter.query("print_stats")
    filename = (status.get("print_stats") or {}).get("filename") or None
    job_id: str | None = None
    start: float | None = None
    history = getattr(adapter, "history", None)
    if history is not None:
        try:
            jobs = history(1)
        except Exception:  # noqa: BLE001 — a printer without [history]: the file name has to do
            jobs = []
        if jobs:
            job_id = None if jobs[0].job_id is None else str(jobs[0].job_id)
            start = jobs[0].start_time
    return {"job_id": job_id, "start_time": start, "filename": filename}


def printer_state(adapter: Any) -> str:
    """print_stats.state as the printer says it ('' when it says nothing)."""
    status = adapter.query("print_stats")
    return str((status.get("print_stats") or {}).get("state") or "")


def new_job_since(recorded: dict[str, Any], now_marker: dict[str, Any]) -> str | None:
    """Why a job counts as started since `recorded` was taken, or None."""
    if recorded.get("job_id") is not None or now_marker.get("job_id") is not None:
        if recorded.get("job_id") != now_marker.get("job_id"):
            return f"a new job ran since (history job {recorded.get('job_id')} -> {now_marker.get('job_id')})"
        return None
    if recorded.get("filename") != now_marker.get("filename"):
        return f"another file was printed since ({recorded.get('filename')} -> {now_marker.get('filename')})"
    return None


@dataclass(frozen=True)
class Confirmation:
    """The owner's word, as the preflight reads it."""

    ts: float
    why: str
    who: str
    ttl_s: float
    evidence: str | None

    def note(self, now: float) -> str:
        """The preflight's words: `owner confirmed empty at <time>: <why>`, who said so and how long it holds."""
        left = max(0.0, self.ts + self.ttl_s - now) / 60.0
        return f"owner confirmed empty at {_iso(self.ts)}: {self.why[:300]} ({self.who}, holds {left:.0f} more min)"


def confirm_empty(
    device: Any,
    verdict: dict[str, Any],
    *,
    why: str,
    ttl_min: float,
    adapter: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """`bed check --assume-empty WHY`: record the owner's confirmation next to the check `verdict` that was just
    made. -> {recorded: True, evidence, state, until} or {recorded: False, refused: why}. Refused for a printing
    printer, an unreadable one, and a camera that saw the plate missing (the word is about the bed)."""
    now = time.time() if now is None else now
    if verdict.get("bed") == "printing":
        return {
            "recorded": False,
            "refused": f"{device.name} is printing: the bed belongs to the job",
        }
    if verdict.get("plate") == "missing":
        return {
            "recorded": False,
            "refused": "the camera sees the build plate missing: put it on first (the confirmation is about "
            "the bed, not the plate)",
        }
    if adapter is None:
        from printer_ops import make_adapter

        adapter = make_adapter(device, timeout=10.0)
    try:
        state, marker = printer_state(adapter), read_marker(adapter)
    except Exception as exc:  # noqa: BLE001 — the confirmation is bound to the job the printer is at
        return {
            "recorded": False,
            "refused": f"the printer could not be read to bind the confirmation to its job: {exc}",
        }
    if state in BUSY_STATES:
        return {
            "recorded": False,
            "refused": f"{device.name} is {state}: the bed belongs to the job",
        }
    evidence, path = record(
        device.name, why=why, ttl_min=ttl_min, marker=marker, verdict=verdict, now=now
    )
    return {
        "recorded": True,
        "evidence": str(evidence),
        "state": str(path),
        "ttl_min": ttl_min,
        "until": now + ttl_min * 60.0,
        "job_marker": marker,
    }


def record(
    printer: str,
    *,
    why: str,
    ttl_min: float,
    marker: dict[str, Any],
    verdict: dict[str, Any],
    now: float | None = None,
) -> tuple[Path, Path]:
    """Write the evidence JSON and the state record of the owner's confirmation. -> (evidence path, state
    path). `why` must say something; the ttl must be positive."""
    from cli.paths import cache_dir

    if not why.strip():
        raise ValueError("--assume-empty needs the reason: why is the bed empty?")
    if not ttl_min > 0:
        raise ValueError("--assume-empty-ttl must be a positive number of minutes")
    now = time.time() if now is None else now
    out_dir = cache_dir() / "vision" / "bed"
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = _unique(out_dir, f"assume-empty-{_stamp(now)}")
    doc = {
        "printer": printer,
        "who": who(),
        "ts": now,
        "when": _iso(now),
        "why": why.strip(),
        "ttl_min": ttl_min,
        "job_marker": marker,
        "verdict": {k: verdict.get(k) for k in ("bed", "plate", "reason", "metrics")},
        "z": verdict.get("z"),
        "light": verdict.get("light"),
        "frames": {"annotated": verdict.get("frame"), "raw": verdict.get("raw_frame")},
    }
    _write(evidence, doc)
    state = assumed_path(printer)
    state.parent.mkdir(parents=True, exist_ok=True)
    _write(
        state,
        {
            "ts": now,
            "why": why.strip(),
            "ttl": ttl_min * 60.0,
            "job_marker": marker,
            "who": who(),
            "plate": verdict.get("plate"),
            "evidence": str(evidence),
        },
    )
    return evidence, state


def _unique(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.json"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"{stem}-{n}.json"
    return path


def _write(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def read_record(printer: str) -> dict[str, Any] | None:
    try:
        data = json.loads(assumed_path(printer).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _valid_fields(rec: dict[str, Any]) -> bool:
    return (
        isinstance(rec.get("ts"), (int, float))
        and isinstance(rec.get("ttl"), (int, float))
        and isinstance(rec.get("why"), str)
        and bool(rec["why"].strip())
        and isinstance(rec.get("job_marker"), dict)
    )


def honoured(
    printer: str, adapter: Any, now: float | None = None
) -> tuple[Confirmation | None, str | None]:
    """(the confirmation the preflight may rely on, or None; why not when there is a record that does not
    count). No record: (None, None). A printing printer, an unreadable printer, an expired ttl, a new job
    since and a recorded `plate` of `missing` all mean the record does not count."""
    rec = read_record(printer)
    if rec is None:
        return None, None
    now = time.time() if now is None else now
    if not _valid_fields(rec):
        return None, "the assume-empty record is malformed"
    age = now - float(rec["ts"])
    if age < 0 or age > float(rec["ttl"]):
        return (
            None,
            f"the owner's confirmation of {_iso(float(rec['ts']))} expired ({float(rec['ttl']) / 60:g} min ttl)",
        )
    if rec.get("plate") == "missing":
        return (
            None,
            "the owner's confirmation was given while the camera saw the plate missing",
        )
    try:
        busy = printer_state(adapter)
        marker = read_marker(adapter)
    except Exception as exc:  # noqa: BLE001 — an unreadable printer is no basis for an override
        return (
            None,
            f"the printer could not be read to check the owner's confirmation ({exc})",
        )
    if busy in BUSY_STATES:
        return (
            None,
            f"the printer is {busy}: the owner's confirmation never applies to a running job",
        )
    since = new_job_since(rec["job_marker"], marker)
    if since is not None:
        return (
            None,
            f"the owner's confirmation of {_iso(float(rec['ts']))} no longer applies: {since}",
        )
    return Confirmation(
        float(rec["ts"]),
        str(rec["why"]),
        str(rec.get("who") or "?"),
        float(rec["ttl"]),
        rec.get("evidence") if isinstance(rec.get("evidence"), str) else None,
    ), None
