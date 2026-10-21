"""dossier_render.py — the text of `3d printers show NAME` (data: printer_ops/dossier.py).

Sections in order: Model spec / Machine / Web UI / Live status / Queue / History. Every live
section says what it is when it has no data: `unreachable: <reason>`, `n/a (<adapter> adapter
does not expose a queue)`, `never contacted`, or `skipped (--no-live)`.
INVARIANTS: stdlib only; pure formatting of an already collected dossier, no I/O.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from printer_ops.dossier import OK, UNREACHABLE, History, MachineFacts, PrinterDossier, Resolved, Section
from printer_ops.render import NAME_W, TEXT_W, WORD_W, clean, status_lines, strip_controls

if TYPE_CHECKING:
    from printer_ops.base import JobQueue
    from printer_ops.webui import WebUiProbe

W = 15  # label column of every section


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s" if m else f"{sec}s"


def fmt_filament(mm: float | None) -> str:
    return "-" if mm is None else f"{mm / 1000:.1f} m"


def _line(label: str, text: str) -> str:
    return f"  {label:<{W}}{text}"


def _unavailable(sec: Section[Any]) -> str | None:
    """The one line of a section that has no data (None when it has)."""
    if sec.status == OK:
        return None
    return f"  {'unreachable: ' if sec.status == UNREACHABLE else ''}{clean(sec.note or '')}"


def _spec(r: Resolved) -> list[str]:
    if r.model is None:
        return [f"Model spec: {r.model_name or '(unknown)'}", f"  {r.model_error}"]
    p = r.model
    x, y, z = p.bed
    return [
        f"Model spec: {p.name}",
        _line("build volume", f"{x:g} x {y:g} x {z:g} mm  (max part that fits)"),
        _line("nozzle", f"{p.nozzle_mm:g} mm"),
        _line("firmware", p.firmware or "(unspecified)"),
        _line("default mat.", p.material or "(unspecified)"),
    ]


def _yes_no(v: bool | None) -> str:
    return "-" if v is None else "yes" if v else "no"


def _heads(f: MachineFacts) -> list[str]:
    ext_w = max((len(h.extruder or "-") for h in f.heads), default=0)
    lines = []
    for i, h in enumerate(f.heads):
        n = h.nozzle
        spec = (f"{n.diameter_mm:g} mm " + " ".join(x for x in (n.material, n.type) if x)).strip() if n \
            else "nozzle not known"
        state = "" if h.usable else f"  UNUSABLE: {h.reason}"
        lines.append(_line("heads" if i == 0 else "", f"{h.label}  {h.extruder or '-':<{ext_w}}  {spec}{state}"))
    return lines


def _machine(f: MachineFacts) -> list[str]:
    address = f"{f.adapter}  {f.host}:{f.port}" if f.adapter else "- (never contacted)"
    if f.ws_port:
        address += f"  ws:{f.ws_port}"
    lines = [f"Machine: {f.name}",
             _line("owner", f.owner or "-"),
             _line("access", f.access),
             _line("address", address),
             _line("model", f.model or "-"),
             *_heads(f),
             _line("ams", "-" if f.ams is None else "none" if not f.ams
                   else ", ".join(f"{u.id} ({u.kind}, {u.slots} slots)" for u in f.ams)),
             _line("enclosure", f.enclosure or "-"),
             _line("chamber heater", _yes_no(f.chamber_heater)),
             _line("plates", ", ".join(f.plates) if f.plates else "-")]
    lim = f.axis_limits
    lines.append(_line("axis limits", "-" if lim is None
                       else f"X {lim['x'][0]:g}..{lim['x'][1]:g}  Y {lim['y'][0]:g}..{lim['y'][1]:g} mm"))
    lines.append(_line("bridge max", "-" if f.bridge_max_mm is None else f"{f.bridge_max_mm:g} mm"))
    if f.trusted_macros:
        lines.append(_line("trusted macros", f"{len(f.trusted_macros)} (listed in the settings file)"))
    if f.slicer:
        lines.append(_line("slicer", "  /  ".join(f"{k}: {v}" for k, v in f.slicer.items())))
    if f.watch.keys:
        lines.append(_line("watch", ", ".join(f.watch.keys)))
    if f.watch.camera_snapshot:
        lines.append(_line("camera", f.watch.camera_snapshot))
    if f.top_camera:
        lines.append(_line("top camera", f.top_camera))
    if f.notes:
        lines.append(_line("notes", f.notes))
    lines += [_line("sources" if i == 0 else "", src) for i, src in enumerate(f.sources)]
    if f.settings.device and f.settings.device == f.settings.fleet:
        lines.append(_line("settings", f.settings.device))
    else:
        lines += [_line(label, path) for label, path in
                  (("device file", f.settings.device), ("fleet file", f.settings.fleet)) if path]
    return lines


def _web_ui(sec: Section[WebUiProbe], camera: str | None) -> list[str]:
    lines = ["Web UI"]
    off = _unavailable(sec)
    if off:
        lines.append(off)
    elif sec.value is not None and not sec.value.found:
        lines.append("  none answered")
    elif sec.value is not None:
        width = max(len(clean(w.url, TEXT_W)) for w in sec.value.found)
        for w in sec.value.found:
            title = f'"{clean(w.title, 60)}"' if w.title else None
            detail = ", ".join(x for x in (f"HTTP {w.http_status}", title, clean(w.note) if w.note else None) if x)
            lines.append(f"  {clean(w.kind, 14):<14}{clean(w.url, TEXT_W):<{width}}  ({detail})")
    if camera:
        lines.append(f"  {'camera':<14}{camera}")
    return lines


def _queue(sec: Section[JobQueue]) -> list[str]:
    off = _unavailable(sec)
    if off or sec.value is None:
        return ["Queue", off or ""]
    q = sec.value
    lines = [f"Queue  ({len(q.jobs)} queued, state {clean(q.state or '?', WORD_W)})"]
    if not q.jobs:
        lines.append("  empty")
    lines += [f"  {i}. {clean(j.filename, NAME_W)}  (waiting {fmt_duration(j.waiting_s)})" for i, j in enumerate(q.jobs, 1)]
    return lines


def _local_time(ts: float | None) -> str:
    try:
        return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts is not None else "-"
    except (OverflowError, ValueError, OSError):
        return "-"


def _history(sec: Section[History]) -> list[str]:
    off = _unavailable(sec)
    if off or sec.value is None:
        return ["History", off or ""]
    h = sec.value
    head = "History"
    if h.totals is not None:
        t = h.totals
        head += (f"  ({t.jobs if t.jobs is not None else '?'} jobs, "
                 f"{fmt_duration(t.print_s)} printing, {fmt_filament(t.filament_mm)} filament)")
    lines = [head]
    if h.totals_error:
        lines.append(f"  totals unavailable: {clean(h.totals_error)}")
    if h.jobs_error:
        lines.append(f"  jobs unavailable: {clean(h.jobs_error)}")
    names = [clean(j.filename, NAME_W) for j in h.jobs]
    name_w = max(map(len, names), default=0)
    lines += [f"  {_local_time(j.start_time)}  {clean(j.status, WORD_W):<12}  {name:<{name_w}}  "
              f"{fmt_duration(j.print_s):>8}  {fmt_filament(j.filament_mm):>8}"
              for j, name in zip(h.jobs, names, strict=True)]
    return lines


def render_printer(d: PrinterDossier) -> list[str]:
    live = _unavailable(d.live)
    return [
        *_machine(d.facts),
        "",
        *_web_ui(d.web_ui, d.facts.watch.camera_snapshot),
        "",
        "Live status",
        *([live] if live else status_lines(d.live.value, W) if d.live.value else []),
        "",
        *_queue(d.queue),
        "",
        *_history(d.history),
    ]


def render(r: Resolved, dossiers: list[PrinterDossier], fleet_error: str | None = None) -> str:
    out = _spec(r)
    if not dossiers:
        out += ["", f"No fleet or configured printer uses the model {r.model_name!r}."]
        if fleet_error:
            out.append(f"  fleet registry unreadable: {fleet_error}")
    for d in dossiers:
        out += ["", *(["-" * 60] if len(dossiers) > 1 else []), *render_printer(d)]
    return "\n".join(strip_controls(line) for line in out)  # net for owner-written strings (notes, reasons, paths)
