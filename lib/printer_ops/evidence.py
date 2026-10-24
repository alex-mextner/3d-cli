"""evidence.py — `3d printer evidence`: keep what a finished print left behind, before it rotates away (#551).

WHY: after a print ends — done, cancelled or failed — the only long-lived record on a Moonraker
  printer is the history job. The console (`/server/gcode_store`) is a RAM ring buffer that the
  1 Hz temperature reports push every event out of within hours, and `klippy.log` rotates. The
  Chekhov cat stand (2026-10-23) stopped at layer 455 of 644 because a head failed `pickup`
  (`err-sta ['1-(FFF)']`); the one trace of it was one line in klippy.log.

WHAT: GET the history job (`/server/history/job?uid=ID`, or the newest finished one of
  `/server/history/list`), GET `/server/files/logs/klippy.log`, cut out the lines around every
  pause / cancel / exception / `pickup failed` / `detached` / `request pause|cancel` /
  `Exiting SD card print` / shutdown event of the job's time window, and write
  `<dir>/<date>-<outcome>.md` plus the raw `<date>-<outcome>.json` next to it. With the sliced G-code
  (`--gcode`) every `file_position` the log mentions is mapped to its layer: the layer it was
  in, its z, and the top z of the last complete layer below it (where a resume has to cut).
  klippy.log of the Snapmaker U1 stamps every line with the UTC time of day only (no date): the dates
  are rebuilt backwards from when its newest line was written (the HTTP Last-Modified of the download). A stock Klipper log has no stamps at all:
  it is read whole, without a time window and without times.

INVARIANTS: only GET requests ever leave this path, so it is safe on a printing printer.
  `--job-file` / `--klippy-log` replace the two fetches with saved copies (the printer is off or
  busy, or the log was copied before it rotated). Missing parts are reported in the evidence and
  make the exit code 1 — after the files are written: partial evidence is kept, never dropped. A
  job that does not exist is a usage error and writes nothing. A G-code whose sha256 / size
  contradicts the printed file is mapped but labelled MISMATCH.
  Stdlib-only: the registry imports `commands/printer.py` on every `3d` run.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import itertools
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from functools import cached_property
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

from errors import GateFailure, InputNotFound, InvalidArgument, UsageError
from printer_ops.base import PrinterCommError

if TYPE_CHECKING:
    from printer_ops.moonraker import MoonrakerAdapter

CMD = "printer evidence"
SCHEMA = "3d-printer-evidence/1"

USAGE = """3d printer evidence NAME [--job ID | --last] [--out DIR] [--gcode FILE.gcode] [--note TEXT] [options]
  Save the evidence of a finished print (done, cancelled or failed) while it still exists:
  the Moonraker history job and the klippy.log lines around every pause / cancel / exception /
  `pickup failed` / `detached` / shutdown of the job, as `DIR/<date>-<outcome>.md` plus the raw
  `DIR/<date>-<outcome>.json`. Read-only on the printer: only GET requests are sent, it works
  on a printer that is printing. Run it after EVERY print: the console is a short RAM buffer and
  klippy.log rotates; the history job is the only long-lived record.

  --job ID             the Moonraker history job (`job_id`, e.g. 000070) — `3d printer status NAME` /
                       the Fluidd history list show it. A job the history does not have is an error
                       (exit 2) and writes nothing
  --last               the default: the newest job that is not still in progress
  --out DIR            where `<date>-<outcome>.md` and `.json` are written, e.g.
                       projects/chekhov/print/runs (created). Without --out the report goes to
                       stdout and nothing is written. <date> is the job's start day (local time);
                       another job already saved under that name gets `-<job id>` appended
  --gcode FILE         the sliced G-code the job printed: every file_position of the log is mapped
                       to its layer, z and the top z of the last complete layer. Its sha256 /
                       size is compared with the printed file; a mismatch is labelled MISMATCH
                       and exits 1 (the mapping then describes another file)
  --note TEXT          what the operator saw (the part that failed, what was done); goes into the .md
  --klippy-log FILE    read this saved copy of klippy.log instead of fetching it (the log was
                       copied before it rotated, or the printer is off). The dates of its lines
                       come from the file's mtime: copy with `cp -p`
  --job-file FILE      read this saved Moonraker job record (the JSON of `/server/history/job`,
                       or just its `job` object) instead of fetching it. With --klippy-log too the
                       printer is not contacted and NAME is only a label
  --context N          non-Stats log lines kept before and after each event (default 6)
  --max-lines N        at most N log entries in the excerpt (default 400); what is cut is counted
  --secs S             timeout of each HTTP request (default 10; klippy.log gets 6x)

Exit: 0 saved; 1 something was missing (job, log or G-code mismatch — the rest is still saved) or
the printer was unreachable; 2 usage error (also: no such job).

Examples:
  3d printer evidence u1 --out projects/chekhov/print/runs
  3d printer evidence u1 --job 000070 --out projects/chekhov/print/runs --gcode print/chekhov.gcode --note "head T2 failed pickup"
  3d printer evidence u1 --job-file job.json --klippy-log klippy.log.copy --gcode print/chekhov.gcode --out runs
  3d printer evidence u1 --last --context 12 --max-lines 800 --secs 30"""

DEFAULT_CONTEXT = 6
DEFAULT_MAX_ENTRIES = 400
MAX_EXTRA_LINES = 12  # continuation lines shown per log entry (the config dump has thousands)
MAX_LOG_BYTES = 128 * 1024 * 1024  # klippy.log larger than this is refused, not read into memory
WINDOW_BEFORE_S = 120.0  # events this long before the job started still belong to it
WINDOW_AFTER_S = 900.0  # ... and this long after it ended (cancel -> idle, cooldown errors)
ROLLOVER_S = 3600.0  # walking back through the log: a clock jump forward this big is midnight
ANCHOR_SLACK = timedelta(minutes=5)
MIN_STAMPED = 1  # a log without one stamped line has no clock: stock Klipper never stamps a line

OUTCOMES = {"completed": "done", "cancelled": "cancelled", "in_progress": "in-progress"}


def outcome_of(status: str | None) -> str:
    """Moonraker job status -> file-name outcome. Everything that is neither completed, cancelled nor still
    running (error, klippy_shutdown, klippy_disconnect, server_exit, interrupted) is a failure; a record
    without any status is `unknown`."""
    if not status:
        return "unknown"
    return OUTCOMES.get(status, "failed")


# ---- klippy.log ----------------------------------------------------------------------------

EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(rx))
    for kind, rx in (
        ("pickup-failed", r"pickup failed"),
        ("detached", r"\bdetached\b"),
        ("exception", r"Raising exception:"),
        ("exception-cleared", r"exception cleared:"),
        ("pause", r"Pausing\.\.\.|request pause"),
        ("resume", r"Resuming\.\.\.|request resume"),
        ("cancel", r"request cancel|Exited to idle state"),
        ("sd-stop", r"Exiting SD card print|exit_print:"),
        ("sd-start", r"Starting SD card print|Finished SD card print"),
        (
            "shutdown",
            r"Transition to shutdown|Lost communication|MCU .* shutdown|Unhandled exception|"
            r"Traceback|Internal error|Timer too close|Klipper state: (?:Shutdown|Disconnect)",
        ),
    )
)
FAULT_KINDS = {"pickup-failed", "detached", "exception", "shutdown"}

_STAMP = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}):")
# U1 firmware: `file_position=N` on its exit lines and `Starting SD card print, file: F, position N`;
# stock Klipper: `Exiting SD card print (position N)` / `Starting SD card print (position N)`
_POSITION = re.compile(
    r"file_position=(\d+)|SD card print \(position (\d+)\)|Starting SD card print.*position (\d+)\s*$"
)


@dataclass
class Entry:
    """One klippy.log message: a line and the unstamped lines that follow it."""

    line: int  # 1-based line number in the log
    secs: float | None  # seconds since midnight; None in a log without a clock
    text: str
    extra: list[str] = field(default_factory=list)
    ts: datetime | None = None  # UTC, dated by `date_entries`

    @property
    def is_stats(self) -> bool:
        return self.text.startswith("Stats ")

    @cached_property
    def kinds(self) -> list[str]:
        """The event kinds of this message (`EVENT_PATTERNS`); the 2 s `Stats` counters are never events."""
        if self.is_stats:
            return []
        return [kind for kind, rx in EVENT_PATTERNS if rx.search(self.text)]


def parse_log(text: str) -> list[Entry]:
    lines = [raw.rstrip("\r") for raw in text.split("\n")]
    stamped = sum(1 for line in lines if _STAMP.match(line))
    if stamped < MIN_STAMPED:  # stock Klipper: no clock in the log, every line is an entry
        return [Entry(n, None, line) for n, line in enumerate(lines, start=1) if line]
    entries: list[Entry] = []
    for n, line in enumerate(lines, start=1):
        m = _STAMP.match(line)
        if m:
            h, mi, s, ms = (int(g) for g in m.groups())
            entries.append(Entry(n, h * 3600 + mi * 60 + s + ms / 1000, line[13:]))
        elif entries and line:
            entries[-1].extra.append(line)
    return entries


def date_entries(entries: list[Entry], anchor: datetime) -> None:
    """Give every entry its UTC date. klippy.log stamps the time of day only, so walk back from the
    newest entry — no later than `anchor`, the moment the log was read — and step one day back
    wherever the clock reads more than ROLLOVER_S later than the entry after it (midnight)."""
    if not entries or entries[-1].secs is None:
        return
    anchor = anchor.astimezone(timezone.utc)
    day = anchor.date()

    def at(d: date, secs: float) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(seconds=secs)

    later = entries[-1].secs
    if at(day, later) > anchor + ANCHOR_SLACK:
        day -= timedelta(days=1)
    for e in reversed(entries):
        if e.secs is None:
            continue
        if e.secs - later > ROLLOVER_S:
            day -= timedelta(days=1)
        e.ts = at(day, e.secs)
        later = e.secs


Window = tuple[datetime | None, datetime | None]


def in_window(entry: Entry, window: Window) -> bool:
    """An entry of a log without a clock belongs to every window."""
    lo, hi = window
    if entry.ts is None:
        return True
    return (lo is None or entry.ts >= lo) and (hi is None or entry.ts <= hi)


@dataclass
class Excerpt:
    entries: list[Entry]  # what is shown, in log order
    hits: dict[int, list[str]]  # entry line number -> event kinds, for the shown events
    skipped_before: dict[int, tuple[int, int]]  # entry line -> (entries left out before it, of them Stats lines)
    events_total: int  # events of the job window, shown or not
    events_cut: int  # events dropped by --max-lines
    outside_window: int  # events in the log that belong to other jobs
    first_fault: Entry | None  # the first fault of the job's window, whatever --max-lines cut

    @property
    def shown_events(self) -> int:
        return len(self.hits)


def build_excerpt(entries: list[Entry], window: Window, context: int, max_entries: int) -> Excerpt:
    """The events inside `window` with `context` non-`Stats` entries around each; windows that touch merge.
    Only what is SHOWN is limited by `max_entries`: the stop positions are read from every entry."""
    events: list[int] = []
    outside = 0
    for i, e in enumerate(entries):
        if e.kinds:
            if in_window(e, window):
                events.append(i)
            else:
                outside += 1

    def span(i: int) -> set[int]:
        keep = {i}
        for step in (-1, 1):
            j, got = i + step, 0
            while 0 <= j < len(entries) and got < context:
                if not entries[j].is_stats:
                    keep.add(j)
                    got += 1
                j += step
        return keep

    first_fault = next((entries[i] for i in events if FAULT_KINDS & set(entries[i].kinds)), None)
    cut = 0
    if len(events) > max_entries:  # more events than the budget: keep the first and last ones
        half = max_entries // 2
        cut = len(events) - max_entries
        events = events[:half] + events[len(events) - (max_entries - half) :]
    keep: set[int] = set()
    for i in events:
        keep |= span(i)
    if len(keep) > max_entries:  # context does not fit: events only
        keep = set(events)
    shown = sorted(keep)
    hits = {entries[i].line: entries[i].kinds for i in events}
    gaps: dict[int, tuple[int, int]] = {}
    for a, b in itertools.pairwise(shown):
        if b > a + 1:
            gaps[entries[b].line] = (
                b - a - 1,
                sum(1 for j in range(a + 1, b) if entries[j].is_stats),
            )
    return Excerpt([entries[i] for i in shown], hits, gaps, len(events) + cut, cut, outside, first_fault)


class StopPosition(NamedTuple):
    byte: int
    ts: datetime | None
    what: str  # 'stop' | 'start'
    line: int


START_PRINT = "Starting SD card print"
SAME_STOP_S = 5.0  # `exit_print:` and `Exiting SD card print` of one stop are written this close
FILE_LOOKAHEAD = 12  # entries after an exit line in which the U1 names the file
_FILE = re.compile(r"\bfile: (.+?)(?:, position \d+)?\s*$")


def _names_other_file(entries: list[Entry], i: int, job_file: str | None) -> bool:
    """The U1 names the file on the `Starting SD card print, file: F, position N` line, and with a `file: F` entry
    a few entries after an exit. A position of another file is not this job's: a print started minutes after this
    one ended is inside the time window too."""
    if not job_file:
        return False
    if entries[i].text.startswith(START_PRINT):
        named = entries[i].text
    else:
        named = next((e.text for e in entries[i + 1 : i + 1 + FILE_LOOKAHEAD] if e.text.startswith("file: ")), "")
    m = _FILE.search(named)
    return m is not None and PurePosixPath(m.group(1)).name != PurePosixPath(job_file).name


def stop_positions(entries: list[Entry], window: Window, job_file: str | None) -> list[StopPosition]:
    """Every `file_position` the job's window gives, from the WHOLE log (never from the shortened excerpt): where
    the printer left the file (`exit_print:` / `Exiting SD card print`) and where a resume re-entered it
    (`Starting SD card print`). The two lines Klipper writes at one stop are one entry here."""
    out: list[StopPosition] = []
    last: dict[tuple[int, str], StopPosition] = {}
    for i, e in enumerate(entries):
        if not e.kinds or not in_window(e, window) or _names_other_file(entries, i, job_file):
            continue
        m = _POSITION.search(e.text)
        if not m:
            continue
        pos = int(next(g for g in m.groups() if g))
        what = "start" if e.text.startswith(START_PRINT) else "stop"
        prev = last.get((pos, what))
        same = prev is not None and (
            e.ts is None or prev.ts is None or abs((e.ts - prev.ts).total_seconds()) <= SAME_STOP_S
        )
        if not same:
            last[(pos, what)] = stop = StopPosition(pos, e.ts, what, e.line)
            out.append(stop)
    return out


# ---- G-code layers -------------------------------------------------------------------------


@dataclass(frozen=True)
class GcodeLayers:
    path: str
    size: int
    sha256: str
    starts: tuple[int, ...]  # byte offset of every `;LAYER_CHANGE`
    zs: tuple[float | None, ...]  # the `;Z:` that follows it = the layer's top z


def scan_gcode(path: Path) -> GcodeLayers:
    """One streaming pass: sha256, size and the byte offset + z of every layer (Orca / PrusaSlicer
    `;LAYER_CHANGE` + `;Z:`). Constant memory: a 130 MB plate is never in RAM."""
    h = hashlib.sha256()
    starts: list[int] = []
    zs: list[float | None] = []
    off = 0
    with path.open("rb") as fh:
        for raw in fh:
            h.update(raw)
            if raw.startswith(b";LAYER_CHANGE"):
                starts.append(off)
                zs.append(None)
            elif raw.startswith(b";Z:") and zs and zs[-1] is None:
                with contextlib.suppress(ValueError):
                    zs[-1] = float(raw[3:])
            off += len(raw)
    if not starts:
        raise InvalidArgument(
            "--gcode",
            str(path),
            ["an Orca / PrusaSlicer G-code with ;LAYER_CHANGE markers"],
            command=CMD,
            extra="Cura-style `;LAYER:n` G-code has no per-layer z to map.",
        )
    return GcodeLayers(str(path), off, h.hexdigest(), tuple(starts), tuple(zs))


@dataclass(frozen=True)
class Where:
    position: int
    layer: int  # 1-based; 0 = before the first layer (start G-code)
    layers: int
    z: float | None  # top z of that layer
    layer_start: int | None
    into_layer: int | None  # bytes of the layer before `position`
    last_complete: int | None  # the layer below, fully printed; None in layer 1 or the start G-code
    last_complete_z: float | None
    past_end: bool  # position beyond the file: the G-code is not the printed one


def locate(g: GcodeLayers, position: int) -> Where:
    """Layer holding byte `position`. A position exactly on a layer's first byte is that layer, with the
    layer below complete: Klipper's file_position is the next byte it would read."""
    n = bisect.bisect_right(g.starts, position)
    past = position > g.size
    below = max(n - 1, 0)  # the layer under the one holding `position` (0: none)
    return Where(
        position=position,
        layer=n,
        layers=len(g.starts),
        z=g.zs[n - 1] if n else None,
        layer_start=g.starts[n - 1] if n else None,
        into_layer=position - g.starts[n - 1] if n else None,
        last_complete=below or None,
        last_complete_z=g.zs[below - 1] if below else None,
        past_end=past,
    )


class Identity(NamedTuple):
    verdict: str  # verified | MISMATCH | unverified
    why: str

    @property
    def mismatch(self) -> bool:
        return self.verdict == "MISMATCH"


_NAME_HASH = re.compile(r"-([0-9a-f]{16})\.gcode$")


def identity(g: GcodeLayers, job: dict[str, Any]) -> Identity:
    """Is `g` the file the job printed? The content hash that `3d printer upload` puts at the end of the stored
    name (`<stem>-<sha256[:16]>.gcode`) decides first, the job's recorded size second."""
    name = str(job.get("filename") or "")
    m = _NAME_HASH.search(name)
    if m:
        if g.sha256.startswith(m.group(1)):
            return Identity(
                "verified",
                f"sha256 {g.sha256[:16]}… matches the hash in the printed file name",
            )
        return Identity(
            "MISMATCH",
            f"sha256 {g.sha256[:16]}… but the printed file name carries {m.group(1)}",
        )
    meta = job.get("metadata")
    size = meta.get("size") if isinstance(meta, dict) else None
    if isinstance(size, int):
        if size == g.size:
            return Identity("verified", f"size {g.size} B matches the job metadata")
        return Identity("MISMATCH", f"size {g.size} B but the job metadata says {size} B")
    return Identity(
        "unverified",
        "the printed file name carries no content hash and the job has no size",
    )


# ---- evidence ------------------------------------------------------------------------------


class Stop(NamedTuple):
    where: Where
    stopped: list[datetime | None]  # when the log says the printer left the file at this position
    reentered: list[datetime | None]  # when a resume entered the file here
    line: int  # first log line that mentions the position


@dataclass
class Evidence:
    printer: str
    source: str  # where the job / log came from, for the report
    captured: datetime
    job: dict[str, Any] | None
    job_problem: str | None
    print_stats: dict[str, Any] | None
    print_stats_problem: str | None
    entries: list[Entry]
    log_problem: str | None
    excerpt: Excerpt | None
    positions: list[StopPosition]
    gcode: GcodeLayers | None
    gcode_identity: Identity | None
    stops: list[Stop]
    note: str | None
    coverage_problem: str | None = None

    @property
    def status(self) -> str | None:
        status = self.job.get("status") if self.job else None
        return str(status) if status else None

    @property
    def outcome(self) -> str:
        return outcome_of(self.status)

    @property
    def problems(self) -> list[str]:
        out = [p for p in (self.job_problem, self.log_problem, self.coverage_problem) if p]
        if self.gcode_identity and self.gcode_identity.mismatch:
            out.append(f"--gcode is not the printed file: {self.gcode_identity.why}")
        if beyond := [s.where.position for s in self.stops if s.where.past_end]:
            out.append(
                f"file_position {', '.join(map(str, beyond))} is past the end of --gcode: the layer map is wrong"
            )
        return out


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc)


def job_window(job: dict[str, Any] | None, now: datetime) -> Window:
    if not job:
        return None, None
    start, end = _epoch(job.get("start_time")), _epoch(job.get("end_time"))
    lo = start - timedelta(seconds=WINDOW_BEFORE_S) if start else None
    hi = end + timedelta(seconds=WINDOW_AFTER_S) if end else (now if start else None)
    return lo, hi


def coverage_problem(entries: list[Entry], window: Window) -> str | None:
    """A log that never overlaps the job (it rotated away, or the copy's mtime dated it wrongly) would
    otherwise read as 'nothing happened'."""
    dated = [e.ts for e in entries if e.ts is not None]
    lo, hi = window
    if not dated or lo is None or hi is None:
        return None
    if dated[-1] < lo or dated[0] > hi:
        return (
            f"klippy.log covers {_utc(dated[0])} to {_utc(dated[-1])}, but the job ran {_utc(lo)} to {_utc(hi)}: "
            "the log has rotated past it, or a --klippy-log copy has the wrong mtime (copy with `cp -p`)"
        )
    return None


def build(
    *,
    printer: str,
    source: str,
    captured: datetime,
    job: dict[str, Any] | None,
    job_problem: str | None,
    print_stats: dict[str, Any] | None,
    print_stats_problem: str | None,
    log_text: str | None,
    log_anchor: datetime | None,
    log_problem: str | None,
    layers: GcodeLayers | None,
    note: str | None,
    context: int,
    max_entries: int,
) -> Evidence:
    """Everything fetched, joined: the excerpt of the log, the layer of every file_position."""
    entries: list[Entry] = []
    excerpt: Excerpt | None = None
    coverage: str | None = None
    positions: list[StopPosition] = []
    if log_text is not None:
        entries = parse_log(log_text)
        if not entries:
            log_problem = "klippy.log is empty"
        else:
            date_entries(entries, log_anchor or captured)
            window = job_window(job, captured)
            excerpt = build_excerpt(entries, window, context, max_entries)
            coverage = coverage_problem(entries, window)
            positions = stop_positions(entries, window, (job or {}).get("filename"))
    ident = identity(layers, job or {}) if layers is not None else None
    stops: list[Stop] = []
    if layers is not None:
        by_pos: dict[int, tuple[list[datetime | None], list[datetime | None], int]] = {}
        for p in positions:
            stopped, reentered, _ = by_pos.setdefault(p.byte, ([], [], p.line))
            (reentered if p.what == "start" else stopped).append(p.ts)
        stops = [Stop(locate(layers, pos), s, r, line) for pos, (s, r, line) in by_pos.items()]
        stops.sort(key=lambda s: s.line)
    return Evidence(
        printer=printer,
        source=source,
        captured=captured,
        job=job,
        job_problem=job_problem,
        print_stats=print_stats,
        print_stats_problem=print_stats_problem,
        entries=entries,
        log_problem=log_problem,
        excerpt=excerpt,
        positions=positions,
        gcode=layers,
        gcode_identity=ident,
        stops=stops,
        note=note,
        coverage_problem=coverage,
    )


# ---- markdown / json ------------------------------------------------------------------------


def _utc(ts: datetime | None, *, ms: bool = False) -> str:
    if ts is None:
        return "?"
    fmt = "%Y-%m-%d %H:%M:%S" + (f".{ts.microsecond // 1000:03d}" if ms else "")
    return ts.astimezone(timezone.utc).strftime(fmt) + " UTC"


def _local(ts: datetime | None) -> str:
    return "?" if ts is None else ts.astimezone().isoformat(timespec="seconds")


def _duration(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "?"
    return f"{seconds:.0f} s ({seconds / 3600:.2f} h)"


def _fence(text: str, lang: str = "") -> str:
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def start_day(ev: Evidence) -> str:
    """The day the print was started, in local time (the capture day when the job is unknown)."""
    start = _epoch((ev.job or {}).get("start_time")) or _epoch((ev.job or {}).get("end_time")) or ev.captured
    return start.astimezone().date().isoformat()


def file_stem(ev: Evidence) -> str:
    """`<date>-<outcome>`. Without a job record there is no start day: `<capture day>-unknown-<HHMMSS>`, which no
    other capture shares."""
    if ev.job is None:
        return f"{start_day(ev)}-unknown-{ev.captured.astimezone():%H%M%S}"
    return f"{start_day(ev)}-{ev.outcome}"


def where_line(w: Where) -> str:
    if w.past_end:
        return f"byte {w.position} is past the end of the G-code ({w.layers} layers): this is not the printed file"
    if w.layer == 0:
        return f"byte {w.position}: before layer 1 (start G-code)"
    parts = [f"layer {w.layer} of {w.layers}"]
    if w.z is not None:
        parts.append(f"z {w.z:g}")
    parts.append(f"layer starts at byte {w.layer_start}, {w.into_layer} B into it")
    if w.last_complete is None:
        tail = "no complete layer yet"
    elif w.last_complete_z is None:
        tail = f"last complete layer {w.last_complete}"
    else:
        tail = f"last complete layer {w.last_complete}, top z {w.last_complete_z:g}"
    return f"{', '.join(parts)}; {tail}"


def summary_lines(ev: Evidence) -> list[str]:
    out: list[str] = []
    if ev.excerpt:
        first = ev.excerpt.first_fault
        if first is not None:
            out.append(f"First fault in klippy.log: {_utc(first.ts, ms=True)} — `{first.text[:200]}`")
    stopped = [s for s in ev.stops if s.stopped]
    if stopped:
        first_stop, last_stop = stopped[0], stopped[-1]
        out.append(
            f"First stop {_utc(first_stop.stopped[0])}: file_position {first_stop.where.position} = "
            f"{where_line(first_stop.where)}"
        )
        if last_stop.where.position != first_stop.where.position:
            out.append(
                f"Last stop {_utc(last_stop.stopped[-1])}: file_position {last_stop.where.position} = "
                f"{where_line(last_stop.where)}"
            )
    return out


def render_markdown(ev: Evidence) -> str:
    job = ev.job or {}
    lines = [
        f"# {start_day(ev)} {Path(str(job.get('filename') or 'unknown job')).name}: {ev.outcome}",
        "",
    ]
    row = lines.append
    row(f"- Printer: {ev.printer}; Moonraker job `{job.get('job_id', '?')}`, file `{job.get('filename', '?')}`")
    start, end = _epoch(job.get("start_time")), _epoch(job.get("end_time"))
    row(
        f"- Start {_utc(start)} ({_local(start)}), end {_utc(end) if end else 'not ended'}"
        + (f" ({_local(end)})" if end else "")
    )
    row(
        f"- `print_duration` {_duration(job.get('print_duration'))}, `total_duration` {_duration(job.get('total_duration'))}"
        + (f"; filament {job['filament_used']:.0f} mm" if isinstance(job.get("filament_used"), (int, float)) else "")
    )
    row(
        f"- Outcome: **{ev.outcome}** (Moonraker status `{ev.status}`)"
        if ev.status
        else f"- Outcome: **unknown** ({'the job record has no status' if ev.job else 'no job record'})"
    )
    for line in summary_lines(ev):
        row(f"- {line}")
    if ev.note:
        row(f"- Operator note: {ev.note}")
    row(f"- Captured {_utc(ev.captured)} from {ev.source}; GET requests only.")
    if ev.print_stats is not None:
        message = f", message {ev.print_stats['message']!r}" if ev.print_stats.get("message") else ""
        row(f"- Printer state at capture: `{ev.print_stats.get('state', '?')}`{message}")
    elif ev.print_stats_problem:
        row(f"- Printer state at capture: unreadable ({ev.print_stats_problem})")
    if ev.problems:
        row("")
        row("## MISSING / WRONG — this evidence is incomplete")
        row("")
        lines.extend(f"- {p}" for p in ev.problems)
    row("")
    row("## Moonraker history record")
    row("")
    row(
        _fence(
            json.dumps(ev.job, indent=2, ensure_ascii=False) if ev.job else "null",
            "json",
        )
    )
    row("")
    if ev.gcode is not None and ev.gcode_identity is not None:
        row("## Where it stopped (G-code layer map)")
        row("")
        row(
            f"G-code `{ev.gcode.path}`: {len(ev.gcode.starts)} layers, {ev.gcode.size} B, sha256 `{ev.gcode.sha256}` — "
            f"**{ev.gcode_identity.verdict}**: {ev.gcode_identity.why}."
        )
        row("")
        if ev.stops:
            for s in ev.stops:
                role = []
                if s.stopped:
                    role.append("stopped " + ", ".join(_utc(t) for t in s.stopped))
                if s.reentered:
                    role.append("re-entered " + ", ".join(_utc(t) for t in s.reentered))
                row(f"- file_position {s.where.position} ({'; '.join(role)}): {where_line(s.where)}")
        else:
            row("- The klippy.log excerpt has no `file_position` to map.")
        row("")
    elif ev.positions:
        row(
            "File positions in the log (pass `--gcode FILE` to map them to layers): "
            + ", ".join(str(p.byte) for p in ev.positions)
        )
        row("")
    row("## klippy.log excerpt (UTC)")
    row("")
    row(excerpt_text(ev))
    return "\n".join(lines).rstrip() + "\n"


def excerpt_text(ev: Evidence) -> str:
    if ev.excerpt is None:
        return f"klippy.log was not collected: {ev.log_problem or 'not requested'}"
    x = ev.excerpt
    dated = [e.ts for e in ev.entries if e.ts is not None]
    if dated:
        head = [
            f"klippy.log covers {_utc(dated[0])} to {_utc(dated[-1])} ({len(ev.entries)} entries); "
            f"`!` marks an event line, line numbers are the log's own."
        ]
    else:
        head = [
            f"klippy.log has no time stamps (stock Klipper): all {len(ev.entries)} lines are searched, "
            "without a time window; `!` marks an event line, line numbers are the log's own."
        ]
    job_start = _epoch((ev.job or {}).get("start_time"))
    if job_start and dated and dated[0] > job_start:
        head.append(f"The job started {_utc(job_start)}, before the log begins: its start was already rotated away.")
    if x.outside_window:
        head.append(f"{x.outside_window} event line(s) of other jobs outside the job's time window are not shown.")
    if x.events_cut:
        head.append(f"--max-lines cut {x.events_cut} of {x.events_total} events (the first and last are kept).")
    if not x.entries:
        head.append("No pause / cancel / exception / pickup / shutdown event in the job's time window.")
    body: list[str] = []
    for e in x.entries:
        if gap := x.skipped_before.get(e.line):
            body.append(f"      ... {gap[0]} entries left out ({gap[1]} of them Stats) ...")
        mark = "!" if e.line in x.hits else " "
        stamp = _utc(e.ts, ms=True)[:-4] if e.ts else "(no time)"
        body.append(f"{mark}{e.line:>6}  {stamp}  {e.text}")
        body.extend(f"{'':>8}  | {s}" for s in e.extra[:MAX_EXTRA_LINES])
        if len(e.extra) > MAX_EXTRA_LINES:
            body.append(f"{'':>8}  | ... {len(e.extra) - MAX_EXTRA_LINES} more lines")
    return "\n".join(head) + ("\n\n" + _fence("\n".join(body)) if body else "")


def evidence_json(ev: Evidence) -> dict[str, Any]:
    x = ev.excerpt
    dated = [e.ts for e in ev.entries if e.ts is not None]
    return {
        "schema": SCHEMA,
        "printer": ev.printer,
        "source": ev.source,
        "captured": _utc(ev.captured),
        "outcome": ev.outcome,
        "problems": ev.problems,
        "note": ev.note,
        "job": ev.job,
        "print_stats": ev.print_stats,
        "klippy": None
        if x is None
        else {
            "covers": [_utc(dated[0]), _utc(dated[-1])] if dated else None,
            "events_in_job_window": x.events_total,
            "events_cut": x.events_cut,
            "events_outside_window": x.outside_window,
            "entries": [
                {
                    "line": e.line,
                    "utc": e.ts.isoformat() if e.ts else None,
                    "text": e.text,
                    "extra": e.extra[:MAX_EXTRA_LINES],
                    "event": x.hits.get(e.line),
                }
                for e in x.entries
            ],
        },
        "gcode": None
        if ev.gcode is None or ev.gcode_identity is None
        else {
            "path": ev.gcode.path,
            "size": ev.gcode.size,
            "sha256": ev.gcode.sha256,
            "layers": len(ev.gcode.starts),
            "identity": ev.gcode_identity.verdict,
            "identity_why": ev.gcode_identity.why,
            "positions": [
                {
                    "file_position": s.where.position,
                    "layer": s.where.layer,
                    "z": s.where.z,
                    "layer_start": s.where.layer_start,
                    "into_layer": s.where.into_layer,
                    "last_complete_layer": s.where.last_complete,
                    "last_complete_z": s.where.last_complete_z,
                    "past_end": s.where.past_end,
                    "stopped_utc": [_utc(t) for t in s.stopped],
                    "reentered_utc": [_utc(t) for t in s.reentered],
                }
                for s in ev.stops
            ],
        },
    }


def _saved_job(sidecar: Path) -> tuple[str, str]:
    """(printer, job id) of an evidence JSON: a job id is a per-installation counter, so printers sharing one
    `--out` can both have a 000070."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        job = data.get("job")
        return str(data.get("printer") or ""), str(job.get("job_id") or "") if isinstance(job, dict) else ""
    except (OSError, ValueError, AttributeError):
        return "", ""


def _safe_id(job_id: str) -> str:
    """A job id as a file-name part: a record from `--job-file` is not trusted to stay inside `--out`."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id).lstrip(".")[:64] or "job"


def free_stem(out: Path, stem: str, printer: str, job_id: str) -> str:
    """`stem`, else `stem-<job id>`, else `stem-<job id>-2`, ...: the first whose saved record is absent or is this
    very job's (saved again). Nothing of another job is ever replaced."""

    def taken(name: str) -> bool:
        sidecar = out / f"{name}.json"
        return sidecar.exists() and not (job_id and _saved_job(sidecar) == (printer, job_id))

    if not taken(stem):
        return stem
    tagged = f"{stem}-{_safe_id(job_id)}"
    if not taken(tagged):
        return tagged
    n = 2
    while taken(f"{tagged}-{n}"):
        n += 1
    return f"{tagged}-{n}"


def earlier_saves(out: Path, printer: str, job_id: str, keep: Path) -> list[Path]:
    """Other evidence files of the same job in `out` (it was saved while still running, or under another outcome)."""
    if not job_id:
        return []
    return sorted(p for p in out.glob("*.json") if p != keep and _saved_job(p) == (printer, job_id))


def write_evidence(ev: Evidence, out: Path) -> tuple[Path, Path]:
    """Write `<date>-<outcome>.md/.json` into `out` (`free_stem` picks the name)."""
    out.mkdir(parents=True, exist_ok=True)
    stem = free_stem(out, file_stem(ev), ev.printer, str((ev.job or {}).get("job_id") or ""))
    md_path, json_path = out / f"{stem}.md", out / f"{stem}.json"
    _atomic(json_path, json.dumps(evidence_json(ev), indent=2, ensure_ascii=False) + "\n")
    _atomic(md_path, render_markdown(ev))
    return md_path, json_path


def _atomic(path: Path, text: str) -> None:
    """Write beside `path` (a fresh, exclusively created name: a planted symlink is never followed), then rename: a
    crash never leaves half a file where evidence should be. The kernel applies the umask: these files are committed."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---- command ---------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, command=CMD, remediation=["See `3d printer evidence --help`."])


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="3d printer evidence", add_help=False)
    p.add_argument("printer")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--job")
    sel.add_argument("--last", action="store_true")
    p.add_argument("--out")
    p.add_argument("--gcode")
    p.add_argument("--note")
    p.add_argument("--klippy-log")
    p.add_argument("--job-file")
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT)
    p.add_argument("--max-lines", type=int, default=DEFAULT_MAX_ENTRIES)
    p.add_argument("--secs", type=float, default=10.0)
    return p


def _load_job(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(path, command=CMD)
    accepted = ["the JSON of /server/history/job, or its `job` object"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InvalidArgument("--job-file", path, accepted, command=CMD, extra=str(exc)) from exc
    if isinstance(data, dict):
        result = data.get("result")
        for candidate in (
            data,
            data.get("job"),
            result.get("job") if isinstance(result, dict) else None,
        ):
            if isinstance(candidate, dict) and "status" in candidate and "filename" in candidate:
                return candidate
    raise InvalidArgument(
        "--job-file",
        path,
        accepted,
        command=CMD,
        extra="no object with `status` and `filename` found in it",
    )


def _moonraker(name: str, secs: float) -> MoonrakerAdapter:
    """The configured Moonraker printer `name` — resolved from the config only, nothing is contacted."""
    from printer_ops import adapter_by_name
    from printer_ops.base import UnsupportedOperation
    from printer_ops.moonraker import MoonrakerAdapter

    adapter = adapter_by_name(name, command="printer")
    if not isinstance(adapter, MoonrakerAdapter):
        raise UnsupportedOperation(
            f"{name} is a {adapter.kind} printer: it has no Moonraker job history or klippy.log",
            command=CMD,
            remediation=["Evidence needs a Moonraker printer; see `3d printer list`."],
        )
    adapter.timeout = secs
    return adapter


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    args = _parser().parse_args(argv)
    if args.context < 0 or args.max_lines < 1 or args.secs <= 0:
        raise UsageError("--context must be >= 0, --max-lines >= 1, --secs > 0", command=CMD)
    if args.job and args.job_file:
        raise UsageError("--job and --job-file both name the job: give one", command=CMD)
    gcode = Path(args.gcode) if args.gcode else None
    if gcode is not None and not gcode.is_file():
        raise InputNotFound(args.gcode, command=CMD)
    log_path = Path(args.klippy_log) if args.klippy_log else None
    if log_path is not None and not log_path.is_file():
        raise InputNotFound(args.klippy_log, command=CMD)
    layers = scan_gcode(gcode) if gcode is not None else None  # a bad G-code must fail before anything is fetched
    offline = bool(args.job_file and args.klippy_log)  # NAME is then only a label
    adapter = None if offline else _moonraker(args.printer, args.secs)
    captured = datetime.now(timezone.utc)
    sources: list[str] = []

    job: dict[str, Any] | None = None
    job_problem = None
    if args.job_file:
        job = _load_job(args.job_file)
        sources.append(f"job record {args.job_file}")
    elif adapter is not None:
        try:
            job = adapter.history_job(args.job) if args.job else adapter.history_finished_job()
        except PrinterCommError as exc:
            job_problem = f"history job not collected: {exc.message}"
        else:
            if job is None:  # the printer answered: there is no such job, nothing to save
                what = f"job {args.job}" if args.job else "finished job in its newest 20 records"
                raise UsageError(
                    f"{args.printer}'s history has no {what}",
                    command=CMD,
                    remediation=[f"List its jobs: 3d printers show {args.printer} --history 20"],
                )
            sources.append(f"{adapter.base} /server/history")

    log_text: str | None = None
    anchor: datetime | None = None
    log_problem = None
    if log_path is not None:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        anchor = datetime.fromtimestamp(log_path.stat().st_mtime, timezone.utc)
        sources.append(f"klippy.log copy {log_path} (dated from its mtime)")
    elif adapter is not None:
        try:
            # a printer that just failed the job request is not given six times the patience again
            log_text, anchor = adapter.klippy_log(
                timeout=args.secs * (1 if job_problem else 6), max_bytes=MAX_LOG_BYTES
            )
            sources.append(f"{adapter.base} /server/files/logs/klippy.log")
        except PrinterCommError as exc:
            log_problem = f"klippy.log not collected: {exc.message}"

    stats = stats_problem = None
    if adapter is not None:
        try:
            stats = adapter.print_stats()
        except PrinterCommError as exc:  # klippy down is exactly when evidence is wanted
            stats_problem = exc.message

    ev = build(
        printer=args.printer,
        source="; ".join(sources) or "no source",
        captured=captured,
        job=job,
        job_problem=job_problem,
        print_stats=stats,
        print_stats_problem=stats_problem,
        log_text=log_text,
        log_anchor=anchor,
        log_problem=log_problem,
        layers=layers,
        note=args.note,
        context=args.context,
        max_entries=args.max_lines,
    )
    if args.out:
        md_path, json_path = write_evidence(ev, Path(args.out))
        print(
            f"evidence of {ev.printer} job {(job or {}).get('job_id', '?')} ({ev.outcome}): {md_path} and {json_path}"
        )
        for line in summary_lines(ev):
            print(f"  {line}")
        for older in earlier_saves(Path(args.out), ev.printer, str((job or {}).get("job_id") or ""), json_path):
            print(
                f"  note: the same job was saved before as {older.with_suffix('.md')}: delete it if this one replaces it"
            )
    else:
        print(render_markdown(ev), end="")
        print("(nothing written: pass --out DIR to save it)", file=sys.stderr)
    if ev.problems:
        raise GateFailure(
            "evidence is incomplete: " + "; ".join(ev.problems),
            command=CMD,
            remediation=[
                "What could be collected is saved; collect the rest and run it again "
                "(the same job overwrites its own files; a file named `unknown` is from "
                "a run without the job record: delete it once the job is saved)."
            ],
        )
    return 0
