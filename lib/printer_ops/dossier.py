"""dossier.py — the one-screen dossier of a printer behind `3d printers show NAME`.

WHAT: resolves NAME to a printer MODEL (lib/data/printers.yaml) and/or the machines of the fleet
  (fleet/printers.yaml + printer-devices.yaml) that carry it, gathers what 3d knows about each
  machine (static facts from the registries) and — read-only — what the machine says right now:
  live status, job queue, job history + lifetime totals, and where its web UI answers.
  `dossier_render.py` prints it; `to_json` is the machine-readable form.
WHY: the model spec alone (bed, nozzle, firmware) says nothing about the machine on the desk:
  its IP, web UIs, what it is printing, what is queued, what it printed before.
INVARIANTS:
  - stdlib only; yaml/registries are reached through the existing loaders.
  - Only reads: adapter.status/queue/history/history_totals (GET) and the web UI probe (GET).
  - A machine that cannot be read never fails the dossier: every live section is a `Section`
    with a status (ok | unreachable | unsupported | skipped) and the reason, static facts always
    print. The live reads run concurrently, so a dead host costs one timeout.
  - `access: offline` printers (other people's) and `live=False` never touch the network
    (load_devices keeps an offline fleet printer out of the device set, whatever the override says).
  - Nothing secret is surfaced: `MachineFacts` is an explicit whitelist, URLs go through
    `redact_url`, watch settings are reduced to key names (an `exec:` command may carry tokens).
"""
from __future__ import annotations

import http.client
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from errors import InvalidArgument, ThreeDError
from printer_ops.base import HistoryTotals, JobQueue, JobRecord, PrinterStatus, UnsupportedOperation
from printer_ops.webui import WebUiProbe, redact_url

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printops.fleet import AmsUnit, FleetPrinter, Head
    from registries.printers import Printer

T = TypeVar("T")

OK, UNREACHABLE, UNSUPPORTED, SKIPPED = "ok", "unreachable", "unsupported", "skipped"
DEFAULT_HISTORY = 5


@dataclass
class Section(Generic[T]):
    """One live part of the dossier: what was read, or why it was not."""

    status: str
    value: T | None = None
    note: str | None = None  # the reason for any status but ok


@dataclass
class History:
    jobs: list[JobRecord]
    totals: HistoryTotals | None
    jobs_error: str | None = None
    totals_error: str | None = None


@dataclass(frozen=True)
class Machine:
    """One physical printer: its fleet entry and/or its device settings."""

    name: str
    fleet: FleetPrinter | None
    device: Device | None

    @property
    def model(self) -> str | None:
        return (self.device.model if self.device else None) or (self.fleet.model if self.fleet else None)

    @property
    def access(self) -> str:
        return self.device.access if self.device else self.fleet.access if self.fleet else "full"


@dataclass
class Registry:
    models: dict[str, Printer]
    fleet: dict[str, FleetPrinter]
    devices: dict[str, Device]
    error: ThreeDError | None = None  # fleet/devices unreadable: the models still resolve

    def machine(self, name: str) -> Machine:
        return Machine(name, self.fleet.get(name), self.devices.get(name))

    @property
    def machine_names(self) -> list[str]:
        return sorted(set(self.fleet) | set(self.devices))


@dataclass
class Resolved:
    query: str
    model_name: str | None
    model: Printer | None
    model_error: str | None
    machines: list[Machine]


@dataclass(frozen=True)
class WatchFacts:
    keys: tuple[str, ...]            # names of the configured `watch:` settings, never their values
    bed_enabled: bool | None
    camera_snapshot: str | None      # credential-free URL


@dataclass(frozen=True)
class SettingsFiles:
    device: str | None
    fleet: str | None


@dataclass(frozen=True)
class MachineFacts:
    """The static facts of a machine that `printers show` prints: an explicit whitelist, no
    free-form device settings. `asdict(facts)` is the JSON form."""

    name: str
    owner: str | None
    access: str
    adapter: str | None
    host: str | None
    port: int | None
    ws_port: int | None
    model: str | None
    heads: tuple[Head, ...]
    ams: tuple[AmsUnit, ...] | None
    enclosure: str | None
    chamber_heater: bool | None
    plates: tuple[str, ...] | None
    axis_limits: dict[str, tuple[float, float]] | None
    bridge_max_mm: float | None
    trusted_macros: tuple[str, ...]
    slicer: dict[str, str]
    watch: WatchFacts
    top_camera: str | None
    notes: str | None
    sources: tuple[str, ...]
    settings: SettingsFiles


@dataclass
class PrinterDossier:
    machine: Machine
    facts: MachineFacts
    web_ui: Section[WebUiProbe]
    live: Section[PrinterStatus]
    queue: Section[JobQueue]
    history: Section[History]


def load_registry() -> Registry:
    from printer_ops.devices import load_devices
    from printers import load_printers

    from printops.fleet import load_fleet

    models = load_printers(command="printers")
    try:
        fleet = load_fleet()
        return Registry(models, dict(fleet.printers) if fleet else {}, load_devices())
    except ThreeDError as exc:
        return Registry(models, {}, {}, exc)


def _find(query: str, names: list[str], *, fold: bool) -> str | None:
    """`query` among `names`: exactly, or (fold) as the one case-insensitive match."""
    if not fold:
        return query if query in names else None
    hits = [n for n in names if n.casefold() == query.casefold()]
    return hits[0] if len(hits) == 1 else None


def resolve(query: str, reg: Registry) -> Resolved:
    """A machine name (`u1`) -> that machine + its model; a model name -> the model + every
    machine of that model. Exact names first, then a unique case-insensitive match; a machine
    name wins over a model of the same name. Unknown -> InvalidArgument listing both kinds."""
    machines, models = reg.machine_names, sorted(reg.models)
    for fold in (False, True):
        name = _find(query, machines, fold=fold)
        if name is not None:
            machine = reg.machine(name)
            return _with_model(query, machine.model, reg, [machine])
        model = _find(query, models, fold=fold)
        if model is not None:
            same = [m for m in map(reg.machine, machines) if (m.model or "").casefold() == model.casefold()]
            return _with_model(query, model, reg, same)
    if reg.error is not None:
        reg.error.command = "printers"  # the fleet loaders name `printer`; the user ran `printers show`
        raise reg.error
    raise InvalidArgument(
        "printer", query, [*models, *machines], command="printers",
        extra=f"Fleet/device printers: {', '.join(machines) or '(none configured)'} (`3d printer list`); "
              "the other names are printer models (`3d printers list`).")


def _with_model(query: str, model_name: str | None, reg: Registry, machines: list[Machine]) -> Resolved:
    """The model of a query: the machine's `model:` may differ from the registry key only by case."""
    names = sorted(reg.models)
    key = (_find(model_name, names, fold=False) or _find(model_name, names, fold=True)) if model_name else None
    model = reg.models.get(key) if key else None
    error = None
    if model is None:
        error = (f"model {model_name!r} is not in the printer registry (`3d printers list`)"
                 if model_name else "no model recorded for this printer")
    return Resolved(query, key or model_name, model, error, machines)


# ---- static facts --------------------------------------------------------------------------

def camera_snapshot(machine: Machine) -> str | None:
    """The configured camera snapshot URL (`watch.vision.snapshot`), credential-free; a bare
    path is resolved against the printer's own address like `3d printer vision` does."""
    device = machine.device
    vision = device.watch.get("vision") if device else None
    snap = vision.get("snapshot") if isinstance(vision, dict) else None
    if not snap or device is None:
        return None
    snap = str(snap)
    return redact_url(snap if "://" in snap else f"http://{device.host}:{device.port}/{snap.lstrip('/')}")


def machine_facts(machine: Machine) -> MachineFacts:
    from printer_ops.devices import offline_device

    fleet, device = machine.fleet, machine.device
    static = device or (offline_device(fleet) if fleet else None)  # an offline entry keeps its static profile
    watch = device.watch if device else {}
    bed = watch.get("bed")
    camera = fleet.photo.get("camera") if fleet and fleet.photo else None
    photo = str(camera) if camera else None
    limits = static.axis_limits if static else None
    return MachineFacts(
        name=machine.name,
        owner=fleet.owner if fleet else None,
        access=machine.access,
        adapter=device.adapter if device else None,
        host=device.host if device else None,
        port=device.port if device else None,
        ws_port=device.ws_port if device else None,
        model=machine.model,
        heads=fleet.heads if fleet else (),
        ams=fleet.ams if fleet else None,
        enclosure=fleet.enclosure if fleet else None,
        chamber_heater=fleet.chamber_heater if fleet else None,
        plates=fleet.plates if fleet else None,
        axis_limits=None if limits is None else {"x": limits[0], "y": limits[1]},
        bridge_max_mm=static.bridge_max_mm if static else None,
        trusted_macros=tuple(sorted(static.trusted_macros)) if static else (),
        slicer=dict(fleet.slicer) if fleet else {},
        watch=WatchFacts(tuple(sorted(watch)), bed.get("enabled") if isinstance(bed, dict) else None,
                         camera_snapshot(machine)),
        top_camera=redact_url(photo) if photo and "://" in photo else photo,
        notes=fleet.notes if fleet else None,
        sources=fleet.sources if fleet else (),
        settings=SettingsFiles(str(device.source) if device else None, str(fleet.source) if fleet else None),
    )


# ---- live reads ----------------------------------------------------------------------------

_WHO = re.compile(r"^\S+ \([\w-]+ [^)]*\): ")  # adapter errors start "u1 (moonraker host:port): "
_REQUEST = re.compile(r"^(?:GET|POST) \S+: ")


def _reason(message: str) -> str:
    """The cause of a failed read without the printer name and request path the section already implies."""
    return _REQUEST.sub("", _WHO.sub("", message))


def _section(kind: str, what: str, read: Callable[[], T]) -> Section[T]:
    """Run one read-only call; an unreadable printer becomes a section, never an exception."""
    try:
        return Section(OK, read())
    except UnsupportedOperation:
        return Section(UNSUPPORTED, None, f"n/a ({kind} adapter does not expose a {what})")
    except ThreeDError as exc:
        return Section(UNREACHABLE, None, _reason(exc.message))
    except OSError as exc:
        return Section(UNREACHABLE, None, str(exc))
    except (http.client.HTTPException, ArithmeticError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # answered, but not like the protocol says (a wrong port, a payload of another shape)
        return Section(UNREACHABLE, None, f"unreadable reply ({type(exc).__name__}: {exc})")


def _combine_history(jobs: Section[list[JobRecord]] | None, totals: Section[HistoryTotals]) -> Section[History]:
    """One History section from the job list (None: not asked for) and the lifetime totals; it
    fails only when nothing that was asked for could be read."""
    asked = [s for s in (jobs, totals) if s is not None]
    if all(s.status != OK for s in asked):
        return Section(asked[0].status, None, asked[0].note)
    return Section(OK, History(jobs.value if jobs and jobs.value else [], totals.value,
                               jobs.note if jobs else None, totals.note))


def collect(machine: Machine, *, live: bool, history_n: int = DEFAULT_HISTORY, secs: float | None = None
            ) -> PrinterDossier:
    """The dossier of `machine`. `live=False` and offline printers make no network call; `secs`
    bounds every read and probe (and is the Creality WebSocket listen window)."""
    from printer_ops import make_adapter
    from printer_ops.webui import PROBE_TIMEOUT_S, probe, web_ports

    facts = machine_facts(machine)
    device = machine.device
    if device is None:
        why = "never contacted (offline fleet printer: 3d does not talk to other people's printers)"
    elif not live:
        why = "skipped (--no-live)"
    else:
        why = None
    if why is not None or device is None:  # `or device is None` narrows the type; why is set whenever it is None
        skip: Section[Any] = Section(SKIPPED, None, why)
        return PrinterDossier(machine, facts, skip, skip, skip, skip)
    ports = web_ports()  # a bad THREED_WEBUI_PORTS is the user's error: reported before any request
    try:
        adapter = make_adapter(device, **({"listen_s": secs, "timeout": secs} if secs is not None else {}))
    except ThreeDError as exc:  # a device entry no adapter can be built from: this machine only
        down: Section[Any] = Section(UNREACHABLE, None, _reason(exc.message))
        return PrinterDossier(machine, facts, down, down, down, down)
    kind = adapter.kind
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_status = pool.submit(_section, kind, "status", adapter.status)
        f_queue = pool.submit(_section, kind, "queue", adapter.queue)
        f_jobs = (pool.submit(_section, kind, "job history", lambda: adapter.history(history_n))
                  if history_n > 0 else None)
        f_totals = pool.submit(_section, kind, "job history", adapter.history_totals)
        f_web = pool.submit(_section, kind, "web UI",
                            lambda: probe(device, ports=ports, timeout=secs or PROBE_TIMEOUT_S))
        web = f_web.result()
        history = _combine_history(f_jobs.result() if f_jobs else None, f_totals.result())
        status, queue = f_status.result(), f_queue.result()
    if web.value is not None and not web.value.found and len(web.value.errors) == web.value.tried:
        web = Section(UNREACHABLE, web.value, web.value.errors[0])
    return PrinterDossier(machine, facts, web, status, queue, history)


# ---- JSON ----------------------------------------------------------------------------------

def _iso(ts: float | None) -> str | None:
    """UTC ISO time of a unix timestamp; None for none or a value no clock can show."""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts is not None else None
    except (OverflowError, ValueError, OSError):
        return None


def _section_json(sec: Section[Any], value: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": sec.status,
        "error": sec.note if sec.status == UNREACHABLE else None,
        "note": sec.note if sec.status != UNREACHABLE else None,
    }
    if sec.value is not None:
        out.update(value(sec.value))
    return out


def _plain(obj: Any) -> Any:
    """`asdict` output with tuples turned into lists (what json.dumps would make of them)."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _status_json(st: PrinterStatus) -> dict[str, Any]:
    d = st.to_json()
    del d["raw"]  # the whole Klipper object dump: `3d printer status --json` has it
    return d


def _history_json(h: History) -> dict[str, Any]:
    return {
        "jobs": [{**asdict(j), "started_at": _iso(j.start_time)} for j in h.jobs],
        "jobs_error": h.jobs_error,
        "totals": asdict(h.totals) if h.totals else None,
        "totals_error": h.totals_error,
    }


def dossier_json(d: PrinterDossier) -> dict[str, Any]:
    return {
        "name": d.machine.name,
        "machine": _plain(asdict(d.facts)),
        "web_ui": {
            **_section_json(d.web_ui, lambda p: {"urls": [asdict(w) for w in p.found], "errors": p.errors}),
            "camera_snapshot": d.facts.watch.camera_snapshot,
        },
        "live": _section_json(d.live, _status_json),
        "queue": _section_json(d.queue, asdict),
        "history": _section_json(d.history, _history_json),
    }


def model_json(r: Resolved) -> dict[str, Any] | None:
    m = r.model
    if m is None:
        return None
    return {"name": m.name, "build_volume_mm": list(m.bed), "nozzle_mm": m.nozzle_mm,
            "firmware": m.firmware, "material": m.material}


def to_json(r: Resolved, dossiers: list[PrinterDossier], registry_error: ThreeDError | None = None
            ) -> dict[str, Any]:
    return {
        "query": r.query,
        "model": model_json(r),
        "model_error": r.model_error,
        "fleet_error": registry_error.message if registry_error else None,
        "printers": [dossier_json(d) for d in dossiers],
    }
