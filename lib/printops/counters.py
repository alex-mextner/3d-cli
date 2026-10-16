"""counters.py — printer usage counters for maintenance rules: the provider interface and its
Moonraker implementation (issue #166; the job ledger #170 adds a second provider later).

INTERFACE: `CounterProvider.usage(printer, head, since) -> Usage` — print hours, filament metres,
tool changes and jobs on `printer` (one `head`, or the whole printer for None) since the ISO
UTC time `since` (None = since the first job). A counter the source does not know is None,
never 0; an unreachable source is a Usage with `error` set.

MOONRAKER (READ-ONLY; GET only, U1 192.168.0.26):
  since None  → GET /server/history/totals   job_totals.total_print_time (s), total_filament_used (mm), total_jobs
  since T     → GET /server/history/list?since=<epoch T>&order=asc&limit=100&start=N, summing
                print_duration (s) and filament_used (mm) of every job started at or after T
  Moonraker counts per printer, not per head, and not tool changes: those stay None until the
  job ledger (#170) books per-tool usage. One provider instance caches each printer's reads, so
  a rule check costs at most one totals read plus one paged list read per printer.
FleetCounters routes each printer by its adapter (printer_ops.devices): moonraker → Moonraker
history, anything else (Creality stock, offline printers) → no counters.
"""

from __future__ import annotations

import datetime
import math
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

COUNTERS = ("print_hours", "filament_m", "tool_changes", "jobs")
PAGE = 100
MAX_PAGES = 100  # 10 000 jobs: far beyond any printer's history; stops a misbehaving server


@dataclass(frozen=True)
class Usage:
    print_hours: float | None = None
    filament_m: float | None = None
    tool_changes: float | None = None
    jobs: float | None = None
    source: str = ""  # where the numbers come from; "" = no provider for this printer
    error: str | None = None  # the source was asked and failed


class CounterProvider(Protocol):
    def usage(self, printer: str, head: str | None, since: str | None) -> Usage: ...


GetJson = Callable[[str], Any]  # URL -> parsed JSON body (raises OSError/ValueError)


def _finite(value: Any) -> float:
    """A counter from the printer; NaN/inf would compare as "not due" forever, so they are malformed."""
    x = float(value)
    if not math.isfinite(x):
        raise ValueError(f"counter value {value!r} is not a finite number")
    return x


def _epoch(at: str) -> float:
    dt = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).timestamp()


class MoonrakerHistory:
    """Counters of ONE Moonraker printer from its job history (GET only)."""

    def __init__(self, base_url: str, get_json: GetJson) -> None:
        self.base = base_url.rstrip("/")
        self.source = f"moonraker {self.base}/server/history"
        self._get = get_json
        self._totals: Usage | None = None
        self._jobs: tuple[float, list[dict[str, Any]]] | None = None  # (since epoch, jobs started since)
        self._truncated = False  # the cached list hit MAX_PAGES: there are more jobs than it holds

    def _result(self, path: str) -> Any:
        body = self._get(self.base + path)
        return body.get("result", body) if isinstance(body, dict) else body

    def totals(self) -> Usage:
        if self._totals is None:
            t = (self._result("/server/history/totals") or {}).get("job_totals") or {}
            self._totals = Usage(print_hours=_finite(t["total_print_time"]) / 3600,
                                 filament_m=_finite(t["total_filament_used"]) / 1000,
                                 jobs=_finite(t["total_jobs"]), source=self.source)
        return self._totals

    def jobs_since(self, since: float) -> list[dict[str, Any]]:
        # a truncated read misses the newest jobs, which every later window needs: read that window anew
        if self._jobs is None or since < self._jobs[0] or (self._truncated and since > self._jobs[0]):
            jobs: list[dict[str, Any]] = []
            self._truncated = False
            for page in range(MAX_PAGES):
                query = urllib.parse.urlencode({"since": f"{since:.3f}", "order": "asc", "limit": PAGE,
                                                "start": page * PAGE})
                batch = (self._result(f"/server/history/list?{query}") or {}).get("jobs") or []
                jobs.extend(j for j in batch if isinstance(j, dict))
                if len(batch) < PAGE:
                    break
            else:  # every page was full: the history goes on, and a short count would read as "ok"
                self._truncated = True
            self._jobs = (since, jobs)
        # a job whose start is not a finite time cannot be placed in a window: the source is malformed
        return [j for j in self._jobs[1] if _finite(j.get("start_time") or 0) >= since]

    def usage(self, since: str | None) -> Usage:
        if since is None:
            return self.totals()
        jobs = self.jobs_since(_epoch(since))
        if self._truncated:
            return Usage(source=self.source,
                         error=f"job history since {since} is longer than {MAX_PAGES * PAGE} jobs; not counted")
        return Usage(print_hours=sum(_finite(j.get("print_duration") or 0) for j in jobs) / 3600,
                     filament_m=sum(_finite(j.get("filament_used") or 0) for j in jobs) / 1000,
                     jobs=float(len(jobs)), source=self.source)


def _http_get_json(url: str) -> Any:
    from printer_ops.base import http_json

    return http_json("GET", url, timeout=10.0)


class FleetCounters:
    """CounterProvider over the fleet: Moonraker printers by their history, others unknown."""

    def __init__(self, get_json: GetJson | None = None,
                 resolve: Callable[[str], tuple[str, str] | None] | None = None) -> None:
        self._get = get_json or _http_get_json
        self._resolve = resolve or _device_url
        self._printers: dict[str, MoonrakerHistory | None] = {}

    def _history(self, printer: str) -> MoonrakerHistory | None:
        if printer not in self._printers:
            found = self._resolve(printer)
            self._printers[printer] = (MoonrakerHistory(found[1], self._get)
                                       if found is not None and found[0] == "moonraker" else None)
        return self._printers[printer]

    def usage(self, printer: str, head: str | None, since: str | None) -> Usage:
        try:
            history = self._history(printer)
        except Exception as e:  # noqa: BLE001 — a broken device entry must not stop the other rules
            return Usage(error=f"printer {printer!r}: {e}")
        if history is None:
            return Usage()
        if head is not None:  # Moonraker history is per printer: per-head counters need #170
            return Usage(source=history.source)
        try:
            return history.usage(since)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:  # any malformed reply: unknown
            return Usage(source=history.source, error=f"{history.source}: {type(e).__name__}: {e}")


def _device_url(printer: str) -> tuple[str, str] | None:
    """(adapter, base URL) of a contactable printer from printer_ops.devices; None if offline/unknown."""
    from printer_ops.devices import load_devices

    device = load_devices().get(printer)
    if device is None or not device.host:
        return None
    return device.adapter, f"http://{device.host}:{device.port}"
