"""glue.py — the glue-stick reminder: when was a printer's bed last glued, and is a fresh layer due.

WHY (issue https://github.com/alex-mextner/3d-cli/issues/762): a PLA part on the K1 SE's textured PEI came
  loose twice on 2026-11-18; a glue-stick layer holds for a few prints and then wears off, washes off with the
  next plate clean, or gathers dust. Nobody remembered when the last layer went on.

DUE: a fresh layer is due when the time since the last application reaches `max_days` OR the prints since it
  reach `max_prints`, whichever comes first (both limits inclusive). Defaults 5 prints / 7 days: a glue-stick
  layer on PEI is usually good for a handful of PLA prints, each part pulls some of it off and the plate wipe
  between prints thins it; after a week it has dried and gathered dust whatever the count. Per printer:
  `3d glue limits --printer NAME --prints N --days D` (~/.config/3d-cli/glue.json).

ON BY DEFAULT: every printer 3d knows (printer-devices.yaml / the fleet) tracks glue with the default limits; a
  printer with no application on record is DUE ("never"). Only explicit config turns it off:
  `3d glue limits --printer NAME --off` writes {"NAME": {"enabled": false}} to glue.json.

PER PRINTER, not per plate: the record is the glue on the bed the printer has now. 3d does not know which of
  several plates is mounted, so it does not pretend to; after swapping to a plate with its own (older) glue,
  record that plate's glue with `3d glue applied --at`.

PRINTS SINCE THE GLUE are read from the printer's own job history (Moonraker GET /server/history/list, so a
  print started from Orca or the touchscreen counts as well as one from `3d printer start`): every job started
  at or after the application that actually printed — completed, cancelled, errored or interrupted jobs with
  print time or filament used, and the job printing now — once per Moonraker job_id (the same history record
  read twice is one print; a reprint has a new job_id and is another). When the printer cannot be asked
  (unreachable, or no Moonraker), 3d-cli's own start log (printer_ops/print_log.py) gives a lower bound.

STATE: ~/.local/state/3d-cli/glue/NAME.json (XDG_STATE_HOME honoured): {"applied_at": ISO, "recorded_at": ISO}.
  `applied_at` is the ONE baseline of both limits (the days and the print count both run from it), written by
  one atomic file replace: recording glue resets both at once.

WHO ASKS: `3d printer upload|start` call `check_before_print` first (a warning, `Glue applied? [y/N]` on a
  TTY, the confirm command otherwise; it never blocks a print), the job watch's Telegram alert carries
  `tg_note`, `3d glue status` shows it all.

INVARIANTS: stdlib only; GET only on the printer; a broken record or an unreachable printer never fails a
  print command.
"""

from __future__ import annotations

import datetime
import http.client
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import ThreeDError, UsageError

DEFAULT_MAX_PRINTS = 5
DEFAULT_MAX_DAYS = 7.0
MAX_LIMIT = 3650  # upper bound of both limits: a due date stays a representable time
FUTURE_SLACK_S = 300  # an --at this far in the future is a typo, not a clock skew
HTTP_TIMEOUT_S = 5.0
DAY_S = 86400.0
CLI = "glue"  # the command the errors name

# unix seconds -> the Moonraker history jobs started since then
JobsSince = Callable[[float], list[dict[str, Any]]]
# (the reader, None when the printer keeps no history 3d can read; where it reads from)
History = tuple[JobsSince | None, str]
HistoryFor = Callable[[str], History]


@dataclass(frozen=True)
class Limits:
    max_prints: int
    max_days: float
    source: str  # "default" or the glue.json path


@dataclass(frozen=True)
class PrintCount:
    count: int
    source: str
    exact: bool  # False: the fallback start log, which misses prints started outside 3d
    note: str = ""  # why the fallback was used


@dataclass(frozen=True)
class Verdict:
    printer: str
    now: float
    limits: Limits
    applied_at: float | None  # None: no application on record
    prints: PrintCount | None = None
    fired: tuple[str, ...] = ()  # "never" | "days" | "prints"

    @property
    def due(self) -> bool:
        return bool(self.fired)

    @property
    def elapsed_s(self) -> float | None:
        return None if self.applied_at is None else max(0.0, self.now - self.applied_at)

    def to_json(self) -> dict[str, Any]:
        p = self.prints
        return {
            "printer": self.printer,
            "due": self.due,
            "fired": list(self.fired),
            "applied_at": _iso(self.applied_at),
            "elapsed_days": None
            if self.elapsed_s is None
            else round(self.elapsed_s / DAY_S, 3),
            "prints": None if p is None else p.count,
            "prints_exact": None if p is None else p.exact,
            "prints_source": None if p is None else p.source,
            "prints_note": None if p is None else p.note or None,
            "max_prints": self.limits.max_prints,
            "max_days": self.limits.max_days,
            "limits_source": self.limits.source,
            "confirm_command": confirm_command(self.printer),
        }


# -- the due rule (pure)


def fired_limits(
    applied_at: float | None, now: float, prints: int, limits: Limits
) -> tuple[str, ...]:
    """Which limits are reached: "never" without an application, else "days" and/or "prints" (inclusive)."""
    if applied_at is None:
        return ("never",)
    out = []
    if now - applied_at >= limits.max_days * DAY_S:
        out.append("days")
    if prints >= limits.max_prints:
        out.append("prints")
    return tuple(out)


def printed(job: dict[str, Any]) -> bool:
    """A Moonraker history job that put plastic on the plate: the job printing now, or any finished one
    (completed, cancelled, error, klippy_shutdown, interrupted, ...) with print time or filament used."""
    if job.get("status") == "in_progress":
        return True
    return _positive(job.get("print_duration")) or _positive(job.get("filament_used"))


def _positive(value: Any) -> bool:
    return _finite_number(value) and value > 0


def count_printed(jobs: list[dict[str, Any]]) -> int:
    """The jobs that printed, once per Moonraker job_id (a job without one: once per file + start time)."""
    return len({_job_key(j) for j in jobs if printed(j)})


def _job_key(job: dict[str, Any]) -> str:
    """job_id plus start time: Moonraker can reuse a deleted tail job's id for a new print."""
    return str((job.get("job_id") or job.get("filename"), job.get("start_time")))


class UncertainHistory(ValueError):
    """The history was read, but some jobs since the glue cannot be classified; `known` jobs surely printed."""

    def __init__(self, message: str, known: int) -> None:
        super().__init__(message)
        self.known = known


def count_prints(printer: str, since: float, history: History) -> PrintCount:
    """Prints on `printer` since `since`: the printer's history, else an estimate from 3d-cli's own start log
    (never below the prints the history did confirm)."""
    from printer_ops import print_log

    jobs_since, source = history
    known = 0
    if jobs_since is None:
        note = "the printer has no Moonraker job history"
    else:
        try:
            return PrintCount(count_printed(jobs_since(since)), source, True)
        except UncertainHistory as exc:
            known = exc.known
            note = f"{source} {exc}" + (
                f"; {known} confirmed print(s)" if known else ""
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            http.client.HTTPException,
        ) as exc:
            note = f"{source} unreachable: {type(exc).__name__}: {exc}"
    try:
        starts = print_log.starts_since(printer, since)
    except (OSError, UnicodeDecodeError) as exc:
        starts = 0
        note += f"; start log unreadable: {type(exc).__name__}: {exc}"
    return PrintCount(
        max(known, starts),
        f"3d-cli start log {print_log.log_path(printer)}",
        False,
        note,
    )


def moonraker_history(printer: str) -> History:
    """The job-history reader of a Moonraker printer from the device registry; (None, "") for any other."""
    from printer_ops.devices import load_devices

    device = load_devices().get(printer)
    if device is None or device.adapter != "moonraker":
        return None, ""
    return _history_at(f"http://{device.host}:{device.port}")


def adapter_history(adapter: Any) -> History:
    """The job-history reader of the printer an open adapter talks to (Moonraker only; else (None, ""))."""
    base = getattr(adapter, "base", None)
    if getattr(adapter, "kind", None) != "moonraker" or not isinstance(base, str):
        return None, ""
    return _history_at(base)


HISTORY_PAGE = 100
HISTORY_MAX_PAGES = (
    100  # 10 000 jobs since one glue: far beyond any plate; more is a broken server
)
HISTORY_DEADLINE_S = 15.0  # the whole paged read: the job-watch alert waits on it


def _history_at(base_url: str) -> History:
    """Paged GET /server/history/list since the glue, bounded by HISTORY_DEADLINE_S in total. A failed,
    malformed, slow or over-long read, or a job since the glue whose start time or metrics are missing or
    invalid, raises UncertainHistory carrying the prints the history did confirm, so the caller falls back
    to an estimate instead of reading a broken or truncated history as an exact count."""
    base = base_url.rstrip("/")

    def jobs_since(since: float) -> list[dict[str, Any]]:
        # Moonraker filters start_time > since: ask from just before it
        jobs, failure = _read_history(base, since - 1.0)
        since_glue = [
            j
            for j in jobs
            if _finite_number(j.get("start_time")) and j["start_time"] >= since
        ]
        problem = failure or _first_problem(jobs, since_glue)
        if problem:
            known = count_printed([j for j in since_glue if not _metrics_problem(j)])
            raise UncertainHistory(problem, known)
        return since_glue

    return jobs_since, f"moonraker {base}/server/history"


def _first_problem(
    jobs: list[dict[str, Any]], since_glue: list[dict[str, Any]]
) -> str | None:
    """The first job that cannot be placed at the glue (no start time) or classified (metrics), if any."""
    for j in jobs:
        if not _finite_number(j.get("start_time")):
            return f"history job {j.get('job_id')!r} has no start time"
    for j in since_glue:
        if problem := _metrics_problem(j):
            return f"history job {j.get('job_id')!r} {problem}"
    return None


def _history_page(
    base: str, since: float, start: int, timeout: float = HTTP_TIMEOUT_S
) -> list[dict[str, Any]]:
    import urllib.parse

    query = urllib.parse.urlencode(
        {"since": f"{since:.3f}", "order": "asc", "limit": HISTORY_PAGE, "start": start}
    )
    body = _get_json("GET", f"{base}/server/history/list?{query}", timeout=timeout)
    batch = body.get("result", {}).get("jobs") if isinstance(body, dict) else None
    if not isinstance(batch, list) or not all(isinstance(j, dict) for j in batch):
        raise ValueError(f"no job list in the history reply: {str(body)[:120]}")
    return batch


HISTORY_MAX_BYTES = 32 * 1024 * 1024  # 100 jobs with metadata are well under 1 MB


def _get_json(method: str, url: str, *, timeout: float) -> Any:
    """One HTTP request -> parsed JSON within `timeout` overall. Socket timeouts bound each read, not the
    exchange (a server can drip header bytes forever), so the request runs in a daemon thread that is
    abandoned at the deadline; it dies with the process."""
    import threading

    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(_get_json_once(method, url, timeout=timeout))
        except BaseException as exc:  # handed to the caller's thread
            error.append(exc)

    worker = threading.Thread(target=run, daemon=True, name="glue-http")
    worker.start()
    worker.join(timeout)
    if error:
        raise error[0]
    if not result:
        raise TimeoutError(f"no complete reply within {timeout:g} s")
    return result[0]


def _get_json_once(method: str, url: str, *, timeout: float) -> Any:
    import urllib.parse

    end = time.monotonic() + timeout
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "", parts.port or 80, timeout=timeout
    )
    try:
        conn.request(
            method, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        )
        if conn.sock is not None:
            conn.sock.settimeout(max(0.001, end - time.monotonic()))
        reply = conn.getresponse()
        data = bytearray()
        while chunk := reply.read1(65536):
            data += chunk
            left = end - time.monotonic()
            if left <= 0 or len(data) > HISTORY_MAX_BYTES:
                raise TimeoutError(f"no complete reply within {timeout:g} s")
            if conn.sock is not None:
                conn.sock.settimeout(left)
    finally:
        conn.close()
    if reply.status >= 400:
        raise OSError(f"HTTP {reply.status} from {method} {url}: {bytes(data[:300])!r}")
    return json.loads(bytes(data) or b"null")


def _read_history(base: str, since: float) -> tuple[list[dict[str, Any]], str | None]:
    """The jobs read and why the read stopped early (None when complete). The whole read, every request
    included, ends by HISTORY_DEADLINE_S: the job-watch alert waits on it."""
    jobs: list[dict[str, Any]] = []
    stop = _read_pages(base, since, time.monotonic() + HISTORY_DEADLINE_S, jobs)
    if stop == "":
        stop = f"did not answer within {HISTORY_DEADLINE_S:g} s"
    return jobs, stop


HISTORY_READ_ATTEMPTS = 3
_SHIFTED = "shifted"


def _read_pages(
    base: str, since: float, deadline: float, jobs: list[dict[str, Any]]
) -> str | None:
    """Fill `jobs` with the history since `since`; None when complete, "" at the deadline, else why it
    stopped. Moonraker pages by offset in job_id order, so a job deleted between two requests shifts the
    rest: each page re-reads the previous page's last job, and when it is not the same job the read starts
    over (a few times) rather than silently skipping one. A read that does not complete keeps every job any
    attempt saw: they were all in the history since the glue."""
    seen: dict[str, dict[str, Any]] = {}
    for _ in range(HISTORY_READ_ATTEMPTS):
        attempt: list[dict[str, Any]] = []
        stop = _read_pass(base, since, deadline, attempt)
        gone = _merge_attempt(seen, attempt)
        jobs[:] = list(seen.values())
        if stop is None:
            # a print an earlier attempt saw was in the history since the glue, even if deleted since
            return (
                f"changed while it was read ({gone} print(s) deleted)" if gone else None
            )
        if stop != _SHIFTED:
            return stop
    return "changed while it was read, on every attempt"


def _merge_attempt(
    seen: dict[str, dict[str, Any]], attempt: list[dict[str, Any]]
) -> int:
    """Add `attempt` to `seen` (newer rows win); how many prints `seen` held that `attempt` lacks."""
    final = {_job_key(j) for j in attempt}
    gone = sum(1 for k, j in seen.items() if k not in final and printed(j))
    seen.update((_job_key(j), j) for j in attempt)
    return gone


def _read_pass(
    base: str, since: float, deadline: float, jobs: list[dict[str, Any]]
) -> str | None:
    try:
        return _read_pass_pages(base, since, deadline, jobs)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        http.client.HTTPException,
    ) as exc:
        return f"unreachable: {type(exc).__name__}: {exc}"


def _read_pass_pages(
    base: str, since: float, deadline: float, jobs: list[dict[str, Any]]
) -> str | None:
    while len(jobs) < HISTORY_PAGE * HISTORY_MAX_PAGES:
        left = deadline - time.monotonic()
        if left <= 0:
            return ""
        batch = _history_page(
            base, since, max(0, len(jobs) - 1), min(HTTP_TIMEOUT_S, left)
        )
        if jobs:
            if not batch or _job_key(batch[0]) != _job_key(jobs[-1]):
                jobs.extend(
                    batch
                )  # still jobs since the glue: kept if every retry fails
                return _SHIFTED
            jobs[-1:] = (
                batch  # the overlap row is newer: its status may have changed since
            )
        else:
            jobs.extend(batch)
        if len(batch) < HISTORY_PAGE:
            return None
    return f"is longer than {HISTORY_PAGE * HISTORY_MAX_PAGES} jobs since the glue"


def _finite_number(value: Any) -> bool:
    import math

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too big for a float
        return False


def _metrics_problem(job: dict[str, Any]) -> str | None:
    """Why a finished job's print metrics cannot tell whether it printed (None when they can). One positive
    metric proves a print; ruling one out needs both metrics present, finite and zero, and not the zeros
    Moonraker's startup recovery leaves when it turns a job left in_progress into `interrupted` with no end
    time. Anything else is unknown, not a measured zero."""
    if job.get("status") == "in_progress" or printed(job):
        return None  # one finite positive metric proves a print, whatever the other one says
    present = [
        m
        for m in (job.get("print_duration"), job.get("filament_used"))
        if m is not None
    ]
    if not all(_finite_number(m) and m >= 0 for m in present):
        return "has invalid print metrics"
    if not present:
        return "has no print metrics"
    if len(present) < 2:
        return "has incomplete print metrics"
    if job.get("status") == "interrupted" and job.get("end_time") is None:
        return "was interrupted before its metrics were saved"
    return None


# -- files


_PRINTER_NAME = re.compile(r"[A-Za-z0-9._-]+")


def plain_name(printer: str) -> bool:
    """A printer name usable as a file name under the glue directory: no path separators, not . or .."""
    return bool(_PRINTER_NAME.fullmatch(printer)) and printer not in {".", ".."}


def _check_name(printer: str) -> str:
    if not plain_name(printer):
        raise UsageError(f"not a plain printer name: {printer!r}")
    return printer


def state_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "glue" / f"{_check_name(printer)}.json"


def config_path() -> Path:
    from cli.paths import config_dir

    return config_dir() / "glue.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ThreeDError(
            f"{path} is not valid JSON: {exc}",
            command=CLI,
            remediation=[f"Fix or delete {path}; `3d glue applied` writes it anew"],
        ) from exc
    if not isinstance(data, dict):
        raise ThreeDError(f"{path}: the top level must be a JSON object", command=CLI)
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write-then-rename: a reader sees the old file or the new one, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config() -> dict[str, Any]:
    return _read_json(config_path())


def _entry(printer: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The printer's glue.json entry ({} when absent); anything but an object is a broken config."""
    entry = (load_config() if config is None else config).get(printer)
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise ThreeDError(
            f"{config_path()}: {printer} must be an object like "
            f'{{"max_prints": 5, "max_days": 7}}, got {entry!r}',
            command=CLI,
        )
    return entry


def enabled(printer: str, config: dict[str, Any] | None = None) -> bool:
    """On unless glue.json says {"NAME": {"enabled": false}}."""
    return _entry(printer, config).get("enabled") is not False


def limits_for(printer: str, config: dict[str, Any] | None = None) -> Limits:
    entry = _entry(printer, config)
    if not ({"max_prints", "max_days"} & set(entry)):
        return Limits(DEFAULT_MAX_PRINTS, DEFAULT_MAX_DAYS, "default")
    prints, days = (
        entry.get("max_prints", DEFAULT_MAX_PRINTS),
        entry.get("max_days", DEFAULT_MAX_DAYS),
    )
    if (
        isinstance(prints, bool)
        or not isinstance(prints, int)
        or not 1 <= prints <= MAX_LIMIT
    ):
        raise ThreeDError(
            f"{config_path()}: {printer}.max_prints must be a whole number 1..{MAX_LIMIT}, got {prints!r}",
            command=CLI,
        )
    if (
        isinstance(days, bool)
        or not isinstance(days, (int, float))
        or not 0 < days <= MAX_LIMIT
    ):
        raise ThreeDError(
            f"{config_path()}: {printer}.max_days must be a number in (0, {MAX_LIMIT}], got {days!r}",
            command=CLI,
        )
    return Limits(prints, float(days), str(config_path()))


def configure(
    printer: str,
    *,
    max_prints: int | None = None,
    max_days: float | None = None,
    on: bool | None = None,
    reset: bool = False,
) -> None:
    """Change the printer's glue.json entry: limits, on/off, or back to the defaults (reset). The whole
    read-modify-write runs under an exclusive lock, so two changes at once never drop each other's entry."""
    import fcntl

    _check_name(printer)

    lock = config_path().with_name("glue.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        config = load_config()
        entry = {} if reset else dict(_entry(printer, config))
        if max_prints is not None:
            entry["max_prints"] = max_prints
        if max_days is not None:
            entry["max_days"] = max_days
        if on is not None:
            entry["enabled"] = on
        if entry.get("enabled") is True:
            del entry["enabled"]  # on is the default: no key
        if entry:
            config[printer] = entry
        else:
            config.pop(printer, None)
        _write_json(config_path(), config)


def known_printers() -> list[str]:
    """Every printer 3d can talk to, every fleet printer (offline ones too: glue can be recorded without
    contacting them), plus any with a glue record or glue config."""
    from cli.paths import state_dir
    from printer_ops.devices import load_devices
    from printops.fleet import load_fleet

    fleet = load_fleet()
    names = {p.stem for p in (state_dir() / "glue").glob("*.json")}
    names |= set(fleet.printers) if fleet is not None else set()
    names = names | set(load_config()) | set(load_devices())
    return sorted(n for n in names if plain_name(n))


# -- records


def record_applied(
    printer: str, applied_at: float, *, now: float | None = None
) -> None:
    """Record a glue application on `printer`: the new baseline of the days AND the print count, replaced in
    one atomic write."""
    _write_json(
        state_path(printer),
        {
            "applied_at": _iso(
                applied_at, "microseconds"
            ),  # the exact print-count baseline
            "recorded_at": _iso(time.time() if now is None else now),
        },
    )


def applied_at(printer: str) -> float | None:
    return _epoch(_read_json(state_path(printer)).get("applied_at"))


def evaluate(
    printer: str,
    *,
    now: float | None = None,
    history_for: HistoryFor = moonraker_history,
    config: dict[str, Any] | None = None,
) -> Verdict | None:
    """The glue verdict of `printer`; None only when glue.json turned it off."""
    now = time.time() if now is None else now
    config = load_config() if config is None else config
    if not enabled(printer, config):
        return None
    limits = limits_for(printer, config)
    applied = applied_at(printer)
    if applied is None:
        return Verdict(printer, now, limits, None, fired=("never",))
    prints = count_prints(printer, applied, history_for(printer))
    return Verdict(
        printer,
        now,
        limits,
        applied,
        prints,
        fired_limits(applied, now, prints.count, limits),
    )


# -- what people read


def confirm_command(printer: str) -> str:
    return f"3d glue applied --printer {printer}"


def reasons(v: Verdict) -> list[str]:
    """One phrase per reached limit, with the values."""
    out = []
    if "never" in v.fired:
        out.append("no glue application on record")
    if "days" in v.fired:
        out.append(
            f"{human(v.elapsed_s or 0)} since the glue (limit {_days(v.limits.max_days)})"
        )
    if "prints" in v.fired and v.prints is not None:
        out.append(
            f"{_count(v.prints)} prints since the glue (limit {v.limits.max_prints})"
        )
    return out


def summary(v: Verdict) -> str:
    """One line: where the bed is between its two limits."""
    if v.applied_at is None or v.prints is None:
        return f"glue {v.printer}: DUE, {'; '.join(reasons(v))}"
    state = "DUE" if v.due else "ok"
    return (
        f"glue {v.printer}: {state}, {_count(v.prints)} of {v.limits.max_prints} prints, "
        f"{human(v.elapsed_s or 0)} of {_days(v.limits.max_days)} since {_local(v.applied_at)}"
    )


def warning_lines(v: Verdict) -> list[str]:
    lines = [f"GLUE DUE on {v.printer}: {'; '.join(reasons(v))}"]
    if v.applied_at is not None:
        lines.append(
            f"  last glue {_local(v.applied_at)}; a fresh glue-stick layer is due before this print"
        )
    if v.prints is not None and not v.prints.exact:
        lines.append(
            f"  print count from {v.prints.source}: an estimate, starts 3d sent, not verified as printed "
            f"({v.prints.note})"
        )
    return lines


def status_lines(v: Verdict) -> list[str]:
    lines = [v.printer]
    if v.applied_at is None:
        lines.append("  last glue       never recorded")
    else:
        lines.append(
            f"  last glue       {_local(v.applied_at)} ({human(v.elapsed_s or 0)} ago)"
        )
        lines.append(
            f"  time since      {human(v.elapsed_s or 0)} of {_days(v.limits.max_days)}"
        )
    if v.prints is not None:
        lines.append(
            f"  prints since    {_count(v.prints)} of {v.limits.max_prints} ({v.prints.source})"
        )
        if v.prints.note:
            lines.append(f"                  {v.prints.note}")
    lines.append(
        f"  limits          {v.limits.max_prints} prints or {_days(v.limits.max_days)}, whichever comes "
        f"first ({v.limits.source})"
    )
    lines.append(f"  status          {_status_word(v)}")
    return lines


def _status_word(v: Verdict) -> str:
    if v.due:
        return (
            f"DUE: {'; '.join(reasons(v))}. After gluing: {confirm_command(v.printer)}"
        )
    if (
        v.applied_at is None or v.prints is None
    ):  # unreachable: no application is always due ("never")
        return "DUE"
    left = v.limits.max_prints - v.prints.count
    by = _local(v.applied_at + v.limits.max_days * DAY_S)
    return f"ok: due after {left} more print{'s' if left != 1 else ''} or on {by}, whichever comes first"


def tg_note(printer: str, *, history_for: HistoryFor = moonraker_history) -> str | None:
    """The glue line of a Telegram alert (Russian, the owner's language) when the glue is due; else None.
    Never raises: an alert must go out whatever the glue record says."""
    try:
        v = evaluate(printer, history_for=history_for)
    except Exception:  # noqa: BLE001 — the alert matters more than the glue line
        return None
    if v is None or not v.due:
        return None
    parts = []
    if "never" in v.fired:
        parts.append("нанесение клея не записано")
    if "days" in v.fired:
        parts.append(
            f"с нанесения прошло {human(v.elapsed_s or 0)} (лимит {_days(v.limits.max_days)})"
        )
    if "prints" in v.fired and v.prints is not None:
        parts.append(
            f"печатей с нанесения: {_count(v.prints)} (лимит {v.limits.max_prints})"
        )
    return (
        f"Клей на столе {v.printer} пора обновить: {'; '.join(parts)}. "
        f"Клей нанесён — отметьте: {confirm_command(v.printer)}"
    )


# -- the check before a print


def check_before_print(
    printer: str,
    *,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
    history_for: HistoryFor = moonraker_history,
) -> None:
    """Warn when the glue is due, before `printer upload|start` sends anything. On a TTY ask
    `Glue applied? [y/N]` (y records it now); elsewhere print the command that records it. Never raises."""
    try:
        v = evaluate(printer, history_for=history_for)
        lines = [] if v is None else ([summary(v)] if not v.due else warning_lines(v))
    except Exception as exc:  # noqa: BLE001 — a broken glue record must never stop a print
        echo(f"glue check skipped: {type(exc).__name__}: {exc}")
        return
    for line in lines:
        echo(line)
    if v is None or not v.due:
        return
    if interactive is None:
        interactive = _is_tty(sys.stdin) and _is_tty(sys.stdout)
    if not interactive:
        echo(f"  Glue applied? Record it with: {confirm_command(printer)}")
        return
    _ask_and_record(v, ask, echo)


def _is_tty(stream: Any) -> bool:
    """False for an absent or closed stream (stdin can be None when the process starts without one)."""
    try:
        return stream is not None and bool(stream.isatty())
    except (ValueError, OSError):
        return False


def _ask_and_record(
    v: Verdict, ask: Callable[[str], str], echo: Callable[[str], None]
) -> None:
    try:
        answer = ask("Glue applied? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        echo(
            f"  continuing without fresh glue; once glued: {confirm_command(v.printer)}"
        )
        return
    now = time.time()
    try:
        record_applied(v.printer, now, now=now)
    except (
        OSError,
        ThreeDError,
    ) as exc:  # the print goes on; the owner records it by hand
        echo(
            f"  could not record the glue ({exc}); record it with: {confirm_command(v.printer)}"
        )
        return
    echo(f"  glue recorded on {v.printer} at {_local(now)}; the count starts again")


# -- formatting


def parse_at(raw: str, *, now: float | None = None) -> float:
    """--at: an ISO date or date-time; without an offset it is local time; refused in the future."""
    try:
        dt = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageError(
            f"--at needs an ISO date/time (2026-11-18 or 2026-11-18T00:35), got {raw!r}",
            command=CLI,
        ) from exc
    try:
        at = (dt if dt.tzinfo else dt.astimezone()).timestamp()
        _iso(at, "microseconds")  # stored and shown in local time: it must convert back
    except (ValueError, OverflowError, OSError) as exc:
        raise UsageError(
            f"--at {raw} is out of the range of local time", command=CLI
        ) from exc
    if at > (time.time() if now is None else now) + FUTURE_SLACK_S:
        raise UsageError(f"--at {raw} is in the future", command=CLI)
    return at


def human(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h"
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def _days(days: float) -> str:
    return f"{days:g}d"


def _count(p: PrintCount) -> str:
    return str(p.count) if p.exact else f"~{p.count}"


def _iso(at: float | None, timespec: str = "seconds") -> str | None:
    return (
        None
        if at is None
        else datetime.datetime.fromtimestamp(at)
        .astimezone()
        .isoformat(timespec=timespec)
    )


def _local(at: float) -> str:
    return (
        datetime.datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    )


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()
