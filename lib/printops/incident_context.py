"""incident_context.py — auto-context of an incident: what was printing, from which spool, at which settings.

ACCESSED VIA: 3d-api routes_incidents (POST /v1/incidents, GET /v1/incidents/context) — e.g.
"на U1 голова 3 пошли сопли" becomes printer u1, head T3 + everything below without the
reporter typing it (plan §6).

WHAT IT FILLS, each with its source in `sources` (stored in incident.context):
  job_file, job_id   printer status (read-only) + Moonraker history (the in-progress job_id);
                     an idle printer → its last job, with a warning, and no layer
  layer              print_stats.info.current_layer (Moonraker) / `layer` (Creality ws)
  z_mm, feature,     the G-code at the printer's file position (virtual_sdcard.file_position),
  nozzle_temp,       else at the first extrusion of the current layer — gcode_resume.state_at,
  bed_temp,          the same per-line parser `3d gcode-resume` uses (one parser): target
  chamber_temp,      temperatures (M104/M109 per tool, M140/M190, M141/M191), part fan
  speed_mm_s,        (M106 P0, % of 255), feed rate of the last extruding move, volumetric
  volumetric_mm3_s,  flow = speed × the slicer's ;WIDTH: × ;HEIGHT: bead (Orca's rounded
  fan_pct            rectangle). Without the G-code: the printer's live heater targets.
  head               given; else the printer's only head; else the G-code's active tool
  spool, filament,   the spool bound to printer/head in Spoolman (`printops_slot`), or the
  vendor, lot        spool_id the reporter gave
  days_since_dried,  printops.drying assessment of that spool (Spoolman last_dried_at; the
  dry_box_rh         spool location's humidity sensor via Home Assistant, read-only)

WHERE THE G-CODE COMES FROM: a file of the same name in PRINTOPS_GCODE_DIRS (os.pathsep list;
a printer-side name `<stem>-<16 hex>.gcode` from `3d printer upload` also matches `<stem>.gcode`
whose SHA-256 starts with those 16 hex), with the size the printer reports. With
PRINTOPS_GCODE_FETCH=1 a Moonraker printer's file is downloaded once (GET, read-only) into
<db dir>/gcode-cache/<printer>/. Off by default: a 130 MB read from a printer mid-print is a
deliberate choice, not a side effect.

INVARIANTS: read-only towards printers, Spoolman and HA; a failing source becomes a warning and
an empty field, never a failed report — except an unknown printer or head (404).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from printops import printer_read
from printops.incidents import IncidentError, obj as _obj, spool_snapshot
from printops.spoolman import Spoolman, SpoolmanError

if TYPE_CHECKING:
    from printer_ops.base import PrinterStatus
    from printer_ops.devices import Device

    from printops.fleet import FleetPrinter, Head
    from printops.homeassistant import HaStates
    from printops.settings import Settings

DEADLINE_S = 120.0
_HASHED = re.compile(r"^(?P<stem>.+)-(?P<tag>[0-9a-f]{16})\.gcode$")
_TRUE = ("1", "true", "yes", "on")


@dataclass
class Readers:
    """Where context comes from; tests swap the callables for fakes."""

    status: Callable[[Device], PrinterStatus] = printer_read.status
    moonraker: Callable[[Device, str], Any] = printer_read.moonraker_get
    download: Callable[..., Path] | None = None  # printer_read.download_gcode when fetching is on
    gcode_dirs: tuple[Path, ...] = ()
    cache_dir: Path | None = None
    humidity: HaStates | None = None
    deadline_s: float = DEADLINE_S

    @classmethod
    def from_env(cls, settings: Settings, env: Mapping[str, str] | None = None) -> Readers:
        from printops.homeassistant import HaError, HaStates

        env = os.environ if env is None else env
        dirs = tuple(Path(p).expanduser() for p in (env.get("PRINTOPS_GCODE_DIRS") or "").split(os.pathsep) if p.strip())
        fetch = (env.get("PRINTOPS_GCODE_FETCH") or "").strip().lower() in _TRUE
        try:
            humidity = HaStates.from_env(env)
        except (HaError, OSError):
            humidity = None
        return cls(
            gcode_dirs=dirs,
            download=printer_read.download_gcode if fetch else None,
            cache_dir=settings.db_path.parent / "gcode-cache",
            humidity=humidity,
        )


@dataclass
class Context:
    fields: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    printer_state: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    gcode: dict[str, Any] | None = None
    drying: dict[str, Any] | None = None

    def put(self, key: str, value: Any, source: str) -> None:
        if value is None:
            return
        self.fields[key] = value
        self.sources[key] = source

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- G-code ------------------------------------------------------------------------------------

_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _SHA_CACHE[key] = h.hexdigest()
    return _SHA_CACHE[key]


def locate_gcode(
    readers: Readers, device: Device | None, filename: str, size: int | None
) -> tuple[Path | None, str]:
    """(path of the job's G-code, how it was found) or (None, why not)."""
    name = PurePosixPath(filename).name
    hashed = _HASHED.match(name)
    why: list[str] = []
    for d in readers.gcode_dirs:
        candidates: list[tuple[Path, str | None]] = [(d / name, None)]
        if hashed:
            candidates.append((d / f"{hashed['stem']}.gcode", hashed["tag"]))
        for path, tag in candidates:
            if not path.is_file():
                continue
            if size is not None and path.stat().st_size != size:
                why.append(f"{path}: {path.stat().st_size} bytes, the printer's file has {size}")
                continue
            if tag is not None and not _sha256(path).startswith(tag):
                why.append(f"{path}: SHA-256 does not start with {tag}")
                continue
            return path, f"local file {path}" + (f" (SHA-256 {tag}… matches)" if tag else "")
    if readers.download is not None and device is not None and device.adapter == "moonraker" and readers.cache_dir:
        dest = readers.cache_dir / device.name / name
        if dest.is_file() and (size is None or dest.stat().st_size == size):
            return dest, f"cached download {dest}"
        try:
            readers.download(device, filename, dest, size=size)
        except printer_read.PrinterReadError as e:
            why.append(str(e))
        else:
            return dest, f"downloaded read-only from {device.name} to {dest}"
    if not readers.gcode_dirs and readers.download is None:
        why.append("no PRINTOPS_GCODE_DIRS and PRINTOPS_GCODE_FETCH is off")
    elif not why:
        why.append(f"{name} is not in PRINTOPS_GCODE_DIRS")
    return None, "; ".join(why)


def bead_area(width: float | None, height: float | None) -> float | None:
    """Cross-section (mm²) of an extruded bead: Orca's rectangle with semicircular sides."""
    if not width or not height or width <= 0 or height <= 0:
        return None
    if width < height:
        return math.pi * (width / 2) ** 2
    return (width - height) * height + math.pi * (height / 2) ** 2


def apply_gcode_state(ctx: Context, st: Mapping[str, Any], tool: int | None, where: str) -> None:
    """Fill temperatures / fan / speed / flow / feature from a gcode_resume machine state."""
    src = f"G-code {where}"
    if tool is not None:
        ctx.put("nozzle_temp", st["temp"].get(tool), f"{src}: M104/M109 target of tool T{tool}")
    ctx.put("bed_temp", st["bed"], f"{src}: M140/M190 target")
    ctx.put("chamber_temp", st["chamber"], f"{src}: M141/M191 target")
    fan = st["fan"].get(0)
    if fan is not None:
        ctx.put("fan_pct", round(min(max(float(fan), 0.0), 255.0) / 255 * 100, 1), f"{src}: M106 (part fan)")
    if st["feature"]:
        ctx.put("feature", str(st["feature"]).strip().lower(), f"{src}: ;TYPE:")
    if st["fe"]:
        speed = st["fe"] / 60
        ctx.put("speed_mm_s", round(speed, 1), f"{src}: feed rate of the last extruding move")
        area = bead_area(st["width"], st["height"])
        if area is not None:
            ctx.put("volumetric_mm3_s", round(speed * area, 2), f"{src}: speed × ;WIDTH: × ;HEIGHT: bead")


# -- printer -----------------------------------------------------------------------------------


@dataclass
class Job:
    active: bool
    filename: str | None
    layer: int | None
    file_position: int | None
    file_size: int | None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def job_of(status: PrinterStatus) -> Job:
    from printer_ops.base import ACTIVE_JOB_STATES

    raw = status.raw or {}
    ps, vsd = _obj(raw.get("print_stats")), _obj(raw.get("virtual_sdcard"))
    info = _obj(ps.get("info"))
    layer = _int(info.get("current_layer"))
    if layer is None:
        layer = _int(raw.get("layer"))  # Creality ws
    return Job(
        active=status.state in ACTIVE_JOB_STATES,
        filename=status.filename,
        layer=layer if layer else None,
        file_position=_int(vsd.get("file_position")),
        file_size=_int(vsd.get("file_size")),
    )


def _match_head(printer: FleetPrinter, head: str) -> str:
    for h in printer.heads:
        if h.label.casefold() == head.casefold():
            return h.label
    unit, _, n = head.partition("/")
    for ams in printer.ams or ():
        if ams.id.casefold() == unit.casefold() and n.isdigit() and 1 <= int(n) <= ams.slots:
            return f"{ams.id}/{int(n)}"
    heads = ", ".join(h.label for h in printer.heads) or "-"
    raise IncidentError(404, f"{printer.name} has no head {head!r}; heads: {heads}")


def _head_obj(printer: FleetPrinter | None, label: str | None) -> Head | None:
    if printer is None or label is None:
        return None
    return next((h for h in printer.heads if h.label == label), None)


def _moonraker_job(readers: Readers, device: Device, ctx: Context) -> dict[str, Any] | None:
    try:
        page = readers.moonraker(device, "/server/history/list?limit=1&order=desc")
    except printer_read.PrinterReadError as e:
        ctx.warnings.append(f"job history unreadable: {e}")
        return None
    jobs = page.get("jobs") if isinstance(page, dict) else None
    return jobs[0] if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict) else None


# -- the resolver ------------------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    *,
    printer: str | None,
    head: str | None = None,
    spool_id: int | None = None,
    readers: Readers,
) -> Context:
    """Everything the incident's snapshot can know without asking the reporter."""
    ctx = Context()
    fleet_printer: FleetPrinter | None = None
    device: Device | None = None
    if printer:
        try:
            entry = printer_read.device_for(printer)
        except LookupError as e:
            raise IncidentError(404, str(e)) from e
        except printer_read.PrinterReadError as e:
            ctx.warnings.append(str(e))
        else:
            fleet_printer, device = entry.printer, entry.device
        ctx.put("printer", printer, "reported")
        if head and fleet_printer is not None:
            head = _match_head(fleet_printer, head)
        if head:
            ctx.put("head", head, "reported")
        elif fleet_printer is not None and len(fleet_printer.heads) == 1:
            head = fleet_printer.heads[0].label
            ctx.put("head", head, "fleet: the printer's only head")

    status = None
    if fleet_printer is not None and device is None:
        ctx.warnings.append(f"{printer} is an offline printer: not contacted, no job/layer/temperatures")
    elif device is not None:
        try:
            status = readers.status(device)
        except printer_read.PrinterReadError as e:
            ctx.warnings.append(f"printer status unreadable: {e}")
    if status is not None and device is not None:
        _from_printer(ctx, readers, device, fleet_printer, status)
        head = ctx.fields.get("head", head)

    _from_spool(ctx, conn, spoolman, printer, head, spool_id, readers)
    return ctx


def _from_printer(
    ctx: Context, readers: Readers, device: Device, fleet_printer: FleetPrinter | None, status: PrinterStatus
) -> None:
    ctx.printer_state = status.state
    job = job_of(status)
    src = f"{device.name} status"
    hist = _moonraker_job(readers, device, ctx) if device.adapter == "moonraker" else None
    if job.active:
        ctx.put("job_file", job.filename, src)
        ctx.put("layer", job.layer, f"{src}: current layer")
        if hist and hist.get("status") == "in_progress" and hist.get("filename") == job.filename:
            ctx.put("job_id", str(hist.get("job_id")), f"{device.name} Moonraker history (in-progress job)")
    else:
        last = hist.get("filename") if hist else job.filename
        if last:
            ctx.put("job_file", last, f"{src}: the last job (printer is {status.state})")
            if hist:
                ctx.put("job_id", str(hist.get("job_id")), f"{device.name} Moonraker history (last job, {hist.get('status')})")
            ctx.warnings.append(f"printer is {status.state}: job = the last job, layer and G-code settings unknown")

    head_label = ctx.fields.get("head")
    head = _head_obj(fleet_printer, head_label)
    extruder = head.extruder if head and head.extruder else ("nozzle" if "nozzle" in status.temps else None)
    if extruder and extruder in status.temps:
        actual, target = status.temps[extruder]
        ctx.telemetry["nozzle"] = {"heater": extruder, "actual": actual, "target": target}
    if "bed" in status.temps:
        actual, target = status.temps["bed"]
        ctx.telemetry["bed"] = {"actual": actual, "target": target}

    if job.active and job.filename:
        _from_gcode(ctx, readers, device, fleet_printer, job, head)
    for key, tel in (("nozzle_temp", ctx.telemetry.get("nozzle")), ("bed_temp", ctx.telemetry.get("bed"))):
        if key not in ctx.fields and tel and tel.get("target"):
            ctx.put(key, tel["target"], f"{src}: live heater target")


def _from_gcode(
    ctx: Context, readers: Readers, device: Device, fleet_printer: FleetPrinter | None, job: Job, head: Head | None
) -> None:
    from gcode_resume import state_at

    assert job.filename is not None
    path, how = locate_gcode(readers, device, job.filename, job.file_size)
    if path is None:
        ctx.warnings.append(f"G-code not available ({how}): no temperatures/speed/feature from it")
        ctx.gcode = {"file": job.filename, "found": False, "why": how}
        return
    deadline = time.monotonic() + readers.deadline_s
    try:
        if job.file_position is not None:
            cur = state_at(path, byte=job.file_position, deadline=deadline)
            where = f"at byte {job.file_position} (printer file position)"
        elif job.layer is not None:
            cur = state_at(path, layer=job.layer, deadline=deadline)
            where = f"at the first extrusion of layer {job.layer}"
        else:
            ctx.warnings.append("printer reports neither a file position nor a layer: G-code not read")
            ctx.gcode = {"file": job.filename, "path": str(path), "found": True, "read": False}
            return
    except (OSError, ValueError) as e:
        ctx.warnings.append(f"G-code {path.name} unreadable: {e}")
        ctx.gcode = {"file": job.filename, "path": str(path), "found": True, "error": str(e)}
        return
    st = cur.state
    tool = head.index if head is not None else st["tool"]
    if head is None and st["tool"] is not None and fleet_printer is not None:
        match = next((h for h in fleet_printer.heads if h.index == st["tool"]), None)
        if match is not None:
            ctx.put("head", match.label, f"G-code {where}: active tool T{st['tool']}")
    ctx.put("z_mm", cur.z, f"G-code {where}: ;Z: of layer {cur.layer}")
    if "layer" not in ctx.fields:
        ctx.put("layer", cur.layer or None, f"G-code {where}: ;LAYER_CHANGE count")
    apply_gcode_state(ctx, st, tool, where)
    ctx.gcode = {
        "file": job.filename, "path": str(path), "found": True, "how": how, "line": cur.line + 1,
        "byte": cur.byte, "gcode_layer": cur.layer, "active_tool": st["tool"],
        "object": st["obj"], "feed_mm_min": st["f"],
    }
    if cur.layer and job.layer and abs(cur.layer - job.layer) > 1:
        ctx.warnings.append(f"G-code cursor is on layer {cur.layer}, the printer reports {job.layer}")


def _from_spool(
    ctx: Context,
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    printer: str | None,
    head: str | None,
    spool_id: int | None,
    readers: Readers,
) -> None:
    if spoolman is None:
        if spool_id is not None or head:
            ctx.warnings.append("Spoolman is not configured: spool/filament/lot unknown")
        return
    spool: Mapping[str, Any] | None = None
    try:
        if spool_id is not None:
            spool = spoolman.get("spool", spool_id)
            source = "reported spool_id"
        elif printer and head:
            from printops.spools import slot_of

            key = f"{printer}/{head}"
            bound = [sp for sp in spoolman.find("spool") if slot_of(sp) == key]
            if len(bound) == 1:
                spool, source = bound[0], f"Spoolman: the spool bound to {key}"
            elif bound:
                ctx.warnings.append(f"{len(bound)} spools claim {key}: spool not chosen")
            else:
                ctx.warnings.append(f"no spool bound to {key} in PrintOps (3d printops spool load)")
    except SpoolmanError as e:
        ctx.warnings.append(f"Spoolman unreadable: {e.message}")
        return
    if spool is None:
        return
    for key, value in spool_snapshot(spool).items():
        ctx.put(key, value, source)
    _from_drying(ctx, conn, spoolman, int(spool["id"]), readers)


def _from_drying(ctx: Context, conn: sqlite3.Connection, spoolman: Spoolman, spool_id: int, readers: Readers) -> None:
    from printops import drying, policy

    try:
        pol = policy.section("drying")
    except policy.PolicyError as e:
        ctx.warnings.append(f"drying policy unusable, drying state unknown: {e}")
        return
    try:
        found = drying.assess_all(conn, spoolman, pol, humidity=readers.humidity, spool_ids=[spool_id])
    except SpoolmanError as e:
        ctx.warnings.append(f"drying state unknown: {e.message}")
        return
    if not found:
        return
    a = found[0]
    ctx.drying = {"state": a.state, "clock": a.clock, "since": a.since, "age_days": a.age_days,
                  "rh": a.rh, "rh_entity": a.rh_entity, "location": a.location, "notes": a.notes}
    if a.clock == "dried":
        ctx.put("days_since_dried", a.age_days, f"Spoolman last_dried_at ({a.since})")
    elif a.clock == "opened":
        ctx.warnings.append(f"spool #{spool_id} has not been dried since it was opened {a.age_days:g} d ago")
    else:
        ctx.warnings.append(f"spool #{spool_id}: no last_dried_at / opened_at in Spoolman")
    ctx.put("dry_box_rh", a.rh, f"Home Assistant {a.rh_entity} (humidity sensor of {a.location})")
