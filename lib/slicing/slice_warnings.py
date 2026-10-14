"""slice_warnings.py — every warning the slicer printed for a G-code, bound to that G-code.

WHAT: `record` reads the slicer's own output for one slice and writes NAME.slice-warnings.json
  next to NAME.gcode:
    log         the CLI's stdout/stderr (Orca family: boost-log lines `[warning] ...` /
                `[error] ...`, continuation lines included);
    result.json the CLI's exit record in --outputdir: a non-zero `return_code` is an error,
                `sliced_plates[].warning_message` a warning;
    slice_info  `<warning msg= level= error_code=>` in Metadata/slice_info.config of a sliced
                project (.gcode.3mf / --export-3mf).
  Each warning gets a stable id (hash of level + message, so the same message keeps its id
  across re-slices) and is unaccepted until `accept` stores a reason with it. The sidecar
  carries the G-code's SHA-256: a re-sliced or edited G-code makes it stale.
  `evaluate` is the print-preflight gate: FAIL when the sidecar is missing, stale, or holds a
  warning without a reason; PASS lists the accepted ones with their reasons.

WHY: the slicer warns (floating regions, cantilevers, empty layers, temperatures, path
  conflicts) and the warning scrolls past in a log nobody reads; the plate is then printed.
  Binding the list to the G-code's hash makes "the slicer said something" a gate: resolved
  (re-slice without it) or accepted on purpose, per warning, with a reason.

HOST NOISE: a few log lines are about the machine running a headless CLI, not about the
  slice (no display / OpenGL / Wayland). They are kept in the sidecar under "ignored" with
  the rule that matched, never dropped silently.

Stdlib only (print-preflight and `3d slice-warnings` import it in-process).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = 1
SUFFIX = ".slice-warnings.json"
# `[2026-10-14 04:32:11.123456] [0x00007f12] [warning] text` (boost log); the level tag may
# also start the line when the timestamp format is off.
_LOG_LINE = re.compile(r"^(?:\[[^\]]*\]\s*)*?\[(warning|error|fatal)\]\s?(.*)$", re.IGNORECASE)
_LOG_PREFIX = re.compile(r"^\[\d{4}-\d\d-\d\d[ T]")
_PLATE_PREFIX = re.compile(
    r"^plate \d+: found (?:NON_CRITICAL )?slicing warnings: ", re.IGNORECASE
)
_NO_CHECK = re.compile(r", no_check=\d+$")
# `run:2206, can not find ...`: the logging function and its source line, which moves between
# slicer builds; the id must not.
_SOURCE_LINE = re.compile(r"^[A-Za-z_]\w*:\d+,\s*")
_MAX_CONTINUATION = 20
# (rule, pattern): lines of a headless CLI run about the host, not about the plate. Seen in a
# real Snapmaker Orca 01.10.01.50 `--slice` log: the version banner is logged as a warning, and
# without a display the thumbnail renderer's GLFW/Wayland/GLEW/OpenGL init fails as errors.
HOST_NOISE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CLI version banner", re.compile(r"^cli mode, current \S+ version", re.IGNORECASE)),
    ("headless display: OpenGL/GLEW/GLFW", re.compile(r"opengl|glew|glfw", re.IGNORECASE)),
    ("headless display: Wayland/X11", re.compile(r"wayland|xdg_runtime_dir|\bx11\b", re.IGNORECASE)),
)


@dataclass
class SlicerWarning:
    id: str
    level: str  # "warning" or "error"
    message: str
    sources: list[str] = field(default_factory=list)  # "log", "result.json", "slice_info"
    error_code: str = ""
    accepted: dict[str, str] | None = None  # {"reason": ..., "at": ISO time}


@dataclass
class Ignored:
    message: str
    rule: str


class SidecarError(ValueError):
    """The sidecar cannot be read or does not belong to the G-code."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def sidecar_path(gcode: Path) -> Path:
    """NAME.gcode -> NAME.slice-warnings.json (NAME.gcode.3mf -> NAME.slice-warnings.json)."""
    name = gcode.name
    for ext in (".gcode.3mf", ".gcode", ".bgcode"):
        if name.lower().endswith(ext):
            return gcode.with_name(name[: -len(ext)] + SUFFIX)
    return gcode.with_name(gcode.stem + SUFFIX)


def normalise(message: str) -> str:
    text = " ".join(message.split())
    text = _SOURCE_LINE.sub("", _PLATE_PREFIX.sub("", text))
    return _NO_CHECK.sub("", text).strip()


def warning_id(level: str, message: str) -> str:
    level = "error" if level.lower() in ("error", "fatal") else "warning"
    return "w" + hashlib.sha256(f"{level}|{normalise(message)}".encode()).hexdigest()[:8]


def _host_noise(message: str) -> str | None:
    for rule, pattern in HOST_NOISE:
        if pattern.search(message):
            return rule
    return None


def parse_log(text: str) -> list[tuple[str, str]]:
    """(level, message) per `[warning]` / `[error]` entry; a message continues on the
    following lines that carry no log prefix (multi-line slicer messages)."""
    out: list[tuple[str, str]] = []
    current: list[str] | None = None
    level = ""
    extra = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _LOG_LINE.match(line.strip())
        if m:
            if current is not None:
                out.append((level, "\n".join(current)))
            level = "error" if m.group(1).lower() in ("error", "fatal") else "warning"
            current, extra = [m.group(2).strip()], 0
            continue
        if current is None:
            continue
        if not line.strip() or _LOG_PREFIX.match(line) or extra >= _MAX_CONTINUATION:
            out.append((level, "\n".join(current)))
            current = None
            continue
        current.append(line.strip())
        extra += 1
    if current is not None:
        out.append((level, "\n".join(current)))
    return [(lv, msg) for lv, msg in out if msg.strip()]


def parse_result_json(path: Path) -> list[tuple[str, str, str]]:
    """(level, message, error_code) from the Orca-family CLI's result.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SidecarError(f"{path}: not a readable result.json: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{path}: not a slicer result.json object")
    out: list[tuple[str, str, str]] = []
    rc = data.get("return_code", 0)
    if rc not in (0, None):
        out.append(("error", f"slicer exit {rc}: {data.get('error_string', '')}".strip(), str(rc)))
    for plate in data.get("sliced_plates") or []:
        if not isinstance(plate, dict):
            continue
        msgs = plate.get("warning_message")
        for msg in msgs if isinstance(msgs, list) else [msgs]:
            if isinstance(msg, dict):
                text = str(msg.get("message") or msg.get("msg") or "")
                out.append(("warning", text, str(msg.get("error_code", ""))))
            elif msg:
                out.append(("warning", str(msg), ""))
    return [o for o in out if o[1].strip()]


def parse_slice_info(path: Path) -> list[tuple[str, str, str]]:
    """(level, message, error_code) from the `<warning>` entries of a sliced 3MF."""
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read("Metadata/slice_info.config")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise SidecarError(f"{path}: no Metadata/slice_info.config (not a sliced 3MF): {exc}") from exc
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SidecarError(f"{path}: unreadable slice_info.config: {exc}") from exc
    out: list[tuple[str, str, str]] = []
    for w in root.iter("warning"):
        # Bambu levels: 0 info, 1 warning, 2 error, 3 fatal (PrintStateBase::WarningLevel order)
        level = "error" if (w.get("level") or "").strip() in ("2", "3") else "warning"
        out.append((level, w.get("msg") or "", (w.get("error_code") or "").strip()))
    return [o for o in out if o[1].strip()]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _slicer_name(gcode: Path) -> str:
    with open(gcode, "rb") as fh:
        head = fh.read(4096).decode("utf-8", "replace")
    m = re.search(r"^;\s*generated by (.+)$", head, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ""


def load(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SidecarError(f"{path}: unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("warnings"), list):
        raise SidecarError(f"{path}: not a slice-warnings sidecar")
    return data


def warnings_of(data: dict[str, object]) -> list[SlicerWarning]:
    items = data.get("warnings")
    out: list[SlicerWarning] = []
    for w in items if isinstance(items, list) else []:
        if isinstance(w, dict):
            acc = w.get("accepted")
            # an acceptance counts only with a real reason; a hand-edited or blank one stays open
            reason = acc.get("reason") if isinstance(acc, dict) else None
            if not (isinstance(reason, str) and len(" ".join(reason.split())) >= 3):
                acc = None
            out.append(
                SlicerWarning(
                    id=str(w.get("id", "")),
                    level=str(w.get("level", "warning")),
                    message=str(w.get("message", "")),
                    sources=[str(s) for s in w.get("sources") or []],
                    error_code=str(w.get("error_code", "")),
                    accepted={str(k): str(v) for k, v in acc.items()} if isinstance(acc, dict) else None,
                )
            )
    return out


def _write(path: Path, data: dict[str, object]) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise read-modify-write of one sidecar across processes."""
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class Recorded:
    path: Path  # the sidecar written
    warnings: list[SlicerWarning]
    ignored: list[Ignored]
    notes: list[str]

    @property
    def open(self) -> list[SlicerWarning]:
        return [w for w in self.warnings if not w.accepted]


def record(
    gcode: Path,
    *,
    logs: Sequence[tuple[str, str]] = (),
    results: Sequence[Path] = (),
    sliced_3mfs: Sequence[Path] = (),
    accept: dict[str, str] | None = None,
) -> Recorded:
    """Write the sidecar for `gcode` from the slicer's outputs. `logs` are (label, text)
    pairs. Acceptances of the previous sidecar carry over by id (same level + message);
    `accept` adds new ones ({id: reason})."""
    if not (logs or results or sliced_3mfs):
        raise SidecarError("no slicer output given: pass --log, --result or --sliced-3mf")
    found: dict[str, SlicerWarning] = {}
    ignored: list[Ignored] = []
    sources: list[dict[str, str]] = []

    def add(level: str, message: str, source: str, code: str = "") -> None:
        rule = _host_noise(message) if source == "log" else None
        if rule is not None:
            if all(i.message != message for i in ignored):
                ignored.append(Ignored(message, rule))
            return
        wid = warning_id(level, message)
        w = found.setdefault(
            wid, SlicerWarning(wid, "error" if level == "error" else "warning", normalise(message))
        )
        if source not in w.sources:
            w.sources.append(source)
        w.error_code = w.error_code or code

    for label, text in logs:
        sources.append({"kind": "log", "path": label})
        for level, message in parse_log(text):
            add(level, message, "log")
    for res in results:
        sources.append({"kind": "result.json", "path": str(res)})
        for level, message, code in parse_result_json(res):
            add(level, message, "result.json", code)
    for sliced in sliced_3mfs:
        sources.append({"kind": "slice_info", "path": str(sliced)})
        for level, message, code in parse_slice_info(sliced):
            add(level, message, "slice_info", code)

    side = sidecar_path(gcode)
    notes: list[str] = []
    if side.is_file():
        try:
            previous = {w.id: w for w in warnings_of(load(side))}
        except SidecarError as exc:
            previous = {}
            notes.append(f"previous sidecar unreadable, acceptances not carried over: {exc}")
        carried = 0
        for wid, w in found.items():
            old = previous.get(wid)
            if old is not None and old.accepted:
                # same level + message as an earlier slice of this file: the reason still applies
                w.accepted = {k: v for k, v in old.accepted.items() if k in ("reason", "at")}
                carried += 1
        if carried:
            notes.append(f"{carried} acceptance(s) carried over from the previous {side.name} (same message)")
    for wid, reason in (accept or {}).items():
        if wid not in found:
            notes.append(f"--accept-warning {wid}: the slicer did not report it this time")
            continue
        w = found[wid]
        w.accepted = {"reason": _reason(reason), "at": _now()}
    data: dict[str, object] = {
        "schema": SCHEMA,
        "gcode": gcode.name,
        "gcode_sha256": sha256_file(gcode),
        "slicer": _slicer_name(gcode),
        "recorded_at": _now(),
        "sources": sources,
        "warnings": [asdict(w) for w in found.values()],
        "ignored": [asdict(i) for i in ignored],
        "post_processed": [],
    }
    _write(side, data)
    return Recorded(side, list(found.values()), ignored, notes)


def _reason(reason: str) -> str:
    text = " ".join(reason.split())
    if len(text) < 3:
        raise SidecarError(f"an acceptance needs a reason (got {reason!r})")
    return text


def accept_warning(gcode: Path, wid: str, reason: str) -> SlicerWarning:
    """Store `reason` with warning `wid` in the sidecar of `gcode` (which must be current)."""
    side = sidecar_path(gcode)
    with _locked(side):
        data = load(side)
        if data.get("gcode_sha256") != sha256_file(gcode):
            raise SidecarError(
                f"{side.name} is stale: {gcode.name} changed since the warnings were recorded; record them again"
            )
        items = warnings_of(data)
        for w in items:
            if w.id == wid:
                w.accepted = {"reason": _reason(reason), "at": _now()}
                data["warnings"] = [asdict(x) for x in items]
                _write(side, data)
                return w
    raise SidecarError(f"{side.name} has no warning {wid}; ids: {', '.join(x.id for x in items) or 'none'}")


def follow_edit(src: Path, dst: Path, src_sha: str, dst_sha: str, step: str) -> Path | None:
    """A 3d post-process (thumbnails) rewrote `src` as `dst` without touching the toolpath:
    when the sidecar of `src` was current, give `dst` a sidecar with the new hash and note the
    step. A stale or absent sidecar stays as it is."""
    side = sidecar_path(src)
    if not side.is_file():
        return None
    try:
        data = load(side)
    except SidecarError:
        return None
    if data.get("gcode_sha256") != src_sha:
        return None
    steps = data.get("post_processed")
    data["post_processed"] = [*(steps if isinstance(steps, list) else []), {"step": step, "from_sha256": src_sha}]
    data["gcode"] = dst.name
    data["gcode_sha256"] = dst_sha
    out = sidecar_path(dst)
    _write(out, data)
    return out


def _short(text: str, n: int = 160) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def evaluate(origin: Path, gcode: Path | None = None) -> tuple[str, str, list[str]]:
    """print-preflight gate: (status, detail, lines). The sidecar is looked up next to
    `origin`; the hash is taken of `gcode` (a snapshot copy) or of `origin`."""
    side = sidecar_path(origin)
    target = gcode or origin
    record_cmd = f"3d slice-warnings record {origin.name} --log SLICER.log [--result result.json]"
    if not side.is_file():
        return "FAIL", f"no {side.name}: the slicer's warnings for this G-code were never recorded ({record_cmd})", []
    try:
        data = load(side)
    except SidecarError as exc:
        return "FAIL", str(exc), []
    if data.get("gcode_sha256") != sha256_file(target):
        return (
            "FAIL",
            f"{side.name} is stale: recorded for another {origin.name} (re-sliced or edited since); "
            f"record the warnings of this slice ({record_cmd})",
            [],
        )
    items = warnings_of(data)
    open_ = [w for w in items if not w.accepted]
    sources = data.get("sources")
    kinds = ", ".join(
        sorted({str(s.get("kind")) for s in sources if isinstance(s, dict)}) if isinstance(sources, list) else []
    )
    ignored = data.get("ignored")
    n_ignored = len(ignored) if isinstance(ignored, list) else 0
    tail = f" (from {kinds or 'no source'}" + (f"; {n_ignored} host-noise line(s) ignored" if n_ignored else "") + ")"
    lines = [
        f"{'OPEN' if not w.accepted else 'ok  '} {w.id} {w.level}: {_short(w.message)}"
        + (f" — accepted: {w.accepted.get('reason', '')}" if w.accepted else "")
        for w in items
    ]
    if open_:
        return (
            "FAIL",
            f"{len(open_)} of {len(items)} slicer warning(s) not resolved or accepted{tail}; "
            f"fix and re-slice, or: 3d slice-warnings accept {origin.name} ID 'reason'",
            lines,
        )
    if items:
        return "PASS", f"{len(items)} slicer warning(s), each accepted with a reason{tail}", lines
    return "PASS", f"the slicer reported no warnings{tail}", lines
