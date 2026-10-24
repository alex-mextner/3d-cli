"""bed_view.py — the auto view of the printer bed: move the bed in Z to where the camera sees the
whole plate, judge it there, put it back (issue #303 follow-up; print-start incident of 2026-10-23).

WHY: the Snapmaker U1 rests at Z 200 after a job, where its camera sees 15 % of the plate (the bed
  is lowered away from the camera; `bed_state.bed_in_view`). A window light there read as "15.7 %
  of the view differs: occupied", the owner confirmed the bed by hand and the print was started
  with --force. At Z 70 the same camera sees 89 % of the plate and a reference of the empty bed
  stands there.

WHAT (stdlib only; the pixels stay in bed_state.py, which wires this module in):
  `pick_inspection_z`  PURE: which Z shows the plate best. Candidates: the Zs of the empty-bed
                       references and a 5 mm grid over the axis. A Z qualifies when the plate share
                       in the frame is >= `threshold` (watch.bed.auto_view_min, default 85 %), it is
                       inside [floor, ceiling] and an empty-bed reference stands for it (ZMatch:
                       the picture moved < match_px). Views within VIEW_TIE of the best count as
                       equally good: the nearest to the current Z wins (least motion, the bed stays
                       as far from the nozzles as it can). No qualifying Z -> status `none` and WHY
                       (too low a share, no reference there, above the clearance floor, ...): the
                       caller keeps its old verdict, it never invents a view.
  `auto_view`          the flow `bed_state.py status` runs for a printer with `watch.bed.auto_view`
                       (or --best-view): read the printer, decide, move (Z only), wait, judge a
                       fresh frame, restore the Z in a `finally`. It decides in this order and
                       returns the old verdict plus the reason when any step refuses:
                         1. printing / paused / busy: NOTHING is moved, the camera is not asked,
                            the verdict is `printing` ("bed not moved, view not judged");
                         2. the share at the current Z >= threshold: nothing to do;
                         3. the printer must read idle/complete/cancelled, Z must be homed (the
                            check NEVER homes: a G28 with parts on the bed can crash the head),
                            the printer must be a full-access one;
                         4. clearance: the bed is raised toward the nozzles, so the floor is the
                            tallest thing that may stand on it + AUTO_VIEW_CLEARANCE (declared:
                            --part-height / watch.bed.auto_view_part_mm, else the last job's
                            `object_height` from Moonraker's file metadata; unknown -> no move);
                         5. ceiling: resume.z_max_mm - AUTO_VIEW_Z_TOP_MARGIN.

WRITES (the only ones): `G90`, `G1 Z<z> F<feed>`, `M400` as one script through the adapter —
  no X/Y word, no home, no macro. Every send re-reads the printer right before it (`guarded_z_move`)
  and refuses unless it reads idle/complete/cancelled with Z homed and Klipper ready; this includes
  the restore (a print started meanwhile never gets a move). The restore runs in a `finally`; a
  marker file (<state>/bed/NAME.view.json) records the displaced Z until it is back, so a check
  killed mid-way is undone by the next one. One check per printer at a time (flock).

COORDINATES: references and the camera calibration are in toolhead Z (`toolhead.position`); G1 is in
  gcode Z. They differ by the gcode offset (Z offset of the last job: the U1 parks at 199.7 or
  200.3). The move converts through `gcode_move.gcode_position`; the restore sends the Z found.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.bed import (  # noqa: E402
    AUTO_VIEW_CLEARANCE,
    AUTO_VIEW_FEED,
    AUTO_VIEW_SETTLE_S,
    AUTO_VIEW_Z_FLOOR,
    AUTO_VIEW_Z_TOP_MARGIN,
    QUIET_STATES,
    BedConfig,
    state_path,
)

GRID_MM = 5.0         # the Z grid searched for a view beside the Zs of the references
VIEW_TIE = 0.10       # views this close (share of the plate) to the best count as equally good
MOVE_TIMEOUT_S = 60.0  # a 270 mm Z move at 10 mm/s takes 27 s
PENDING_MAX_AGE_S = 1800.0  # a displaced-Z marker older than this is not trusted (the bed may have moved since)
ACTIVE_STATES = ("printing", "paused", "busy")  # printer_ops.base: the bed belongs to the job


def _log(msg: str) -> None:
    print(f"bed view: {msg}", file=sys.stderr, flush=True)


def _fz(z: float | None) -> str:
    return "?" if z is None else f"{z:.1f}"


# ---- the picker (pure) ----------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    z: float
    visible: float        # share of the plate inside the frame at this Z
    referenced: bool      # an empty-bed reference stands for this Z
    allowed: bool         # inside [floor, ceiling]


@dataclass(frozen=True)
class ViewPick:
    status: str                  # "stay" (current Z is good) | "move" | "none" (no honest view exists)
    z: float | None              # the Z to judge at (the current one for "stay"); None for "none"
    visible: float | None        # share of the plate in the frame at `z`
    current_visible: float | None
    reason: str
    candidates: tuple[Candidate, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "z": self.z, "visible": _r(self.visible),
                "current_visible": _r(self.current_visible), "reason": self.reason}


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def pick_inspection_z(view: Callable[[float], float | None], ref_zs: Sequence[float],
                      stands: Callable[[float | None, float | None], bool], current_z: float | None, *,
                      z_floor: float, z_ceiling: float, threshold: float) -> ViewPick:
    """The best Z to judge the bed at. `view(z)` = share of the plate in the frame (None: cannot tell),
    `ref_zs` = Zs of the empty-bed references, `stands(ref_z, z)` = the reference stands for the bed at z
    (bed_state.ZMatch). See the module docstring for the rule."""
    refs = sorted({round(float(z), 1) for z in ref_zs})

    def referenced(z: float) -> bool:
        return any(stands(r, z) for r in refs)

    top = max([z_ceiling, *refs, current_z or 0.0])
    grid = {round(i * GRID_MM, 1) for i in range(int(top // GRID_MM) + 1)}
    zs = sorted(grid | set(refs) | ({round(current_z, 1)} if current_z is not None else set()))
    cands = []
    for z in zs:
        v = view(z)
        if v is not None and math.isfinite(v):
            cands.append(Candidate(z, float(v), referenced(z), z_floor <= z <= z_ceiling))
    cur = view(current_z) if current_z is not None else None
    if cur is not None and cur >= threshold and referenced(current_z or 0.0):
        return ViewPick("stay", current_z, cur, cur, f"{cur:.0%} of the plate is in the frame at Z {_fz(current_z)}",
                        tuple(cands))
    good = [c for c in cands if c.allowed and c.referenced and c.visible >= threshold]
    if good:
        best = max(c.visible for c in good)
        near = [c for c in good if c.visible >= best - VIEW_TIE]
        pick = min(near, key=lambda c: (abs(c.z - current_z) if current_z is not None else c.z, -c.visible))
        return ViewPick("move", pick.z, pick.visible, cur,
                        f"{pick.visible:.0%} of the plate is in the frame at Z {pick.z:.1f} "
                        f"(a reference stands there; {_fz(current_z)} shows {_pct(cur)})", tuple(cands))
    return ViewPick("none", None, None, cur, _why_none(cands, current_z, cur, threshold, z_floor, z_ceiling),
                    tuple(cands))


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0%}"


def _span(zs: list[float]) -> str:
    return f"Z {zs[0]:.0f}" if zs[0] == zs[-1] else f"Z {zs[0]:.0f}..{zs[-1]:.0f}"


def _why_none(cands: list[Candidate], current_z: float | None, cur: float | None, threshold: float,
              floor: float, ceiling: float) -> str:
    """Why no Z qualifies, naming the first thing that would have to change."""
    have = f"{_pct(cur)} of the plate is in the frame at Z {_fz(current_z)}, {threshold:.0%} needed"
    if cur is None:
        return f"the camera calibration cannot tell how much of the plate is in the frame at Z {_fz(current_z)}"
    shows = [c for c in cands if c.visible >= threshold]
    if not shows:
        best = max(cands, key=lambda c: c.visible, default=None)
        return f"{have}; no Z shows that much" + (f" (best: Z {best.z:.0f} with {best.visible:.0%})" if best else "")
    inside = [c for c in shows if c.allowed]
    if not inside:
        return (f"{have}; {_span([c.z for c in shows])} would show it but the bed may only move within "
                f"Z {floor:.0f}..{ceiling:.0f} (the floor keeps the bed clear of the tallest part that may stand on it)")
    if not any(c.referenced for c in inside):
        return (f"{have}; {_span([c.z for c in inside])} would show it but no empty-bed reference stands there: "
                "with the bed clear and the plate on, move it to such a Z and run "
                "`3d printer bed reference NAME --capture` (or `--from FRAME.jpg --z Z`)")
    return f"{have}; nothing qualifies"


# ---- the printer (read + guarded Z move) ----------------------------------------------------

class MoveRefused(Exception):
    """A guard stopped a move; nothing was sent."""


@dataclass(frozen=True)
class Motion:
    state: str            # base.py normalized: idle | printing | paused | complete | cancelled | error | busy | unknown
    raw_state: str
    z: float | None       # toolhead Z (what references and the camera calibration use)
    gcode_z: float | None  # the same position in gcode coordinates (what G1 takes)
    homed: str            # homed axes, e.g. "xyz"
    ready: bool           # Klipper (webhooks) is ready
    filename: str | None  # print_stats.filename: the last job

    @property
    def offset(self) -> float:
        """toolhead Z minus gcode Z (the gcode Z offset in force)."""
        return 0.0 if self.z is None or self.gcode_z is None else self.z - self.gcode_z


def _finite(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def read_motion(adapter: Any) -> Motion:
    """One read-only Moonraker query of everything the guards need."""
    from printer_ops.base import UNKNOWN
    from printer_ops.moonraker import _STATE

    st = adapter.query("print_stats&toolhead&webhooks&gcode_move")
    ready = (st.get("webhooks") or {}).get("state", "ready") == "ready"
    ps = st.get("print_stats") or {}
    raw = str(ps.get("state", ""))
    th = st.get("toolhead") or {}
    pos, gpos = th.get("position"), (st.get("gcode_move") or {}).get("gcode_position")
    z = _finite(pos[2]) if isinstance(pos, list) and len(pos) > 2 else None
    gz = _finite(gpos[2]) if isinstance(gpos, list) and len(gpos) > 2 else None
    name = ps.get("filename")
    return Motion(_STATE.get(raw, UNKNOWN) if ready else UNKNOWN, raw, z, gz, str(th.get("homed_axes") or ""),
                  ready, name if isinstance(name, str) and name else None)


def guarded_z_move(adapter: Any, *, to_gcode_z: float, floor: float | None = None, ceiling: float | None = None,
                   target_toolhead_z: float | None = None, feed: float = AUTO_VIEW_FEED,
                   read: Callable[[Any], Motion] = read_motion) -> Motion:
    """THE ONLY WRITE: `G90`, `G1 Z<to_gcode_z> F<feed>`, `M400` as one script, after a fresh read of the
    printer says: Klipper ready, state idle/complete/cancelled, Z homed. `floor`/`ceiling` bound
    `target_toolhead_z`. Returns the Motion read before the move; MoveRefused when a guard stops it."""
    m = read(adapter)
    if not m.ready:
        raise MoveRefused("cannot move: Klipper is not ready")
    if m.state not in QUIET_STATES:
        raise MoveRefused(f"cannot move: printer is {m.state} [{m.raw_state}], the bed belongs to the job")
    if "z" not in m.homed.lower():
        raise MoveRefused(f"cannot move: not homed (homed axes: {m.homed or 'none'}); the bed check never homes")
    if m.z is None:
        raise MoveRefused("cannot move: toolhead Z unknown")
    if not math.isfinite(to_gcode_z) or not math.isfinite(feed) or feed <= 0:
        raise MoveRefused(f"cannot move: Z {to_gcode_z} F{feed} is not a finite move")
    if target_toolhead_z is not None and ((floor is not None and target_toolhead_z < floor)
                                          or (ceiling is not None and target_toolhead_z > ceiling)):
        raise MoveRefused(f"cannot move: Z {target_toolhead_z:.1f} is outside {_fz(floor)}..{_fz(ceiling)}")
    adapter.run_gcode(f"G90\nG1 Z{to_gcode_z:.3f} F{feed:g}\nM400", timeout=MOVE_TIMEOUT_S)
    return m


# ---- clearance --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    z_floor: float
    z_ceiling: float
    part_mm: float
    part_source: str


def part_height(adapter: Any, cfg: BedConfig, m: Motion, override: float | None) -> tuple[float | None, str]:
    """The height of the tallest thing that may stand on the bed: declared (`--part-height`,
    watch.bed.auto_view_part_mm) or the last job's `object_height` (Moonraker metadata of
    print_stats.filename). (None, why) when nothing says."""
    if override is not None:
        return override, "--part-height"
    if cfg.auto_view_part_mm is not None:
        return cfg.auto_view_part_mm, "watch.bed.auto_view_part_mm"
    fetch = getattr(adapter, "file_metadata", None)
    if m.filename is None or fetch is None:
        return None, "the last job is unknown"
    try:
        meta = fetch(m.filename)
    except Exception as exc:  # unreadable metadata is "unknown", never a guess
        return None, f"the metadata of {m.filename} is unreadable ({str(exc)[:120]})"
    h = _finite(meta.get("object_height")) if isinstance(meta, dict) else None
    if h is None or h < 0:
        return None, f"Moonraker has no object_height for {m.filename}"
    return h, f"object_height of {m.filename} (Moonraker file metadata)"


def move_limits(device: Any, adapter: Any, cfg: BedConfig, m: Motion, override: float | None) -> Limits | str:
    """The Z range the bed may be moved within, or why it may not be moved at all."""
    if device.resume is None:
        return (f"{device.name} has no resume.z_max_mm: the Z range of the axis is unknown "
                "(fleet/printers.yaml resume block)")
    part, source = part_height(adapter, cfg, m, override)
    if part is None:
        return (f"the height of what may stand on the bed is unknown ({source}): raising the bed toward the nozzles "
                "could crash it into a part; declare it with --part-height MM or watch.bed.auto_view_part_mm")
    floor = max(AUTO_VIEW_Z_FLOOR, part + AUTO_VIEW_CLEARANCE)
    return Limits(floor, device.resume.z_max_mm - AUTO_VIEW_Z_TOP_MARGIN, part, source)


# ---- the displaced-Z marker and the per-printer lock -----------------------------------------

def _view_path(printer: str, suffix: str) -> Path:
    return state_path(printer).with_name(f"{printer}.view{suffix}")


@contextlib.contextmanager
def view_lock(printer: str) -> Iterator[bool]:
    """One auto view per printer at a time: True when this process holds the lock, False when another does."""
    path = _view_path(printer, ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def view_in_progress(printer: str, now: float) -> bool:
    """A view check of `printer` has the bed away from where it was found (a fresh marker): the watch
    monitor must not judge or report the frames of that time."""
    pending = _read_pending(printer)
    ts = _finite(pending.get("ts")) if pending else None
    return ts is not None and 0 <= now - ts <= PENDING_MAX_AGE_S


def _write_pending(printer: str, m: Motion, now: float) -> None:
    path = _view_path(printer, ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"printer": printer, "z": m.z, "gcode_z": m.gcode_z, "ts": now}), encoding="utf-8")


def _clear_pending(printer: str) -> None:
    _view_path(printer, ".json").unlink(missing_ok=True)


def _read_pending(printer: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_view_path(printer, ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def terminate_as_exit() -> Iterator[None]:
    """SIGTERM (a stopped watcher, `timeout`) unwinds like an exception so the `finally` restores the bed."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _term(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    old = signal.signal(signal.SIGTERM, _term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def restore_z(adapter: Any, before: Motion, *, tol: float = 0.15,
              read: Callable[[Any], Motion] = read_motion) -> str | None:
    """Put the bed back where `before` found it. None = done (or never left); else why it is NOT back."""
    try:
        now = read(adapter)
        if now.z is not None and before.z is not None and abs(now.z - before.z) <= tol:
            return None
        target = before.gcode_z if before.gcode_z is not None else before.z
        if target is None:
            return "the Z it was found at is unknown"
        guarded_z_move(adapter, to_gcode_z=target, read=read)
        after = read(adapter)
        if after.z is not None and before.z is not None and abs(after.z - before.z) > max(tol, 0.5):
            return f"the bed is at Z {after.z:.1f}, it was found at Z {before.z:.1f}"
        return None
    except MoveRefused as exc:
        return str(exc)
    except Exception as exc:  # never hide the bed verdict behind a failed restore: report it
        return f"restore failed: {exc.__class__.__name__}: {exc}"


def recover_pending(adapter: Any, printer: str, now: float,
                    read: Callable[[Any], Motion] = read_motion) -> str | None:
    """A check that died between the move and the restore left a marker: undo it now (only a fresh
    marker, only a quiet printer). -> a note for the verdict, or None."""
    pending = _read_pending(printer)
    if pending is None:
        return None
    z, ts = _finite(pending.get("z")), _finite(pending.get("ts"))
    if z is None or ts is None or not 0 <= now - ts <= PENDING_MAX_AGE_S:
        _clear_pending(printer)
        return None
    with view_lock(printer) as held:
        if not held:
            return None  # the check that wrote the marker is still running: it restores the bed itself
        m = read(adapter)
        if m.state in ACTIVE_STATES:
            return None  # the job owns the bed now; the marker stays for after it
        if m.z is not None and abs(m.z - z) <= 0.15:
            _clear_pending(printer)
            return None
        why = restore_z(adapter, Motion("", "", z, _finite(pending.get("gcode_z")), "", True, None), read=read)
        if why is None:
            _clear_pending(printer)
            return f"put the bed back to Z {z:.1f} (left there by an interrupted view check)"
        return f"an interrupted view check left the bed away from Z {z:.1f}: {why}"


# ---- the flow ---------------------------------------------------------------------------------

def _annotate(v: dict[str, Any], view: dict[str, Any], note: str | None) -> dict[str, Any]:
    v["view"] = {k: x for k, x in view.items() if x is not None}
    if note:
        v["reason"] = f"{v.get('reason', '')}; view: {note}".lstrip("; ")
    return v


def auto_view(device: Any, cfg: BedConfig, *, mode: str, adapter: Any, judge: Callable[[float | None], dict[str, Any]],
              view: Callable[[float], float | None], ref_zs: Sequence[float],
              stands: Callable[[float | None, float | None], bool], max_age: float | None = None,
              part_height_mm: float | None = None, settle_s: float = AUTO_VIEW_SETTLE_S,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
              read: Callable[[Any], Motion] = read_motion) -> dict[str, Any]:
    """The bed verdict of `judge`, at a better Z when the current one shows too little of the plate.
    `mode`: auto (cfg.auto_view decides) | force (--best-view) | never (--no-move). `judge(max_age)` is
    `bed_state.one_shot` (max_age None = a fresh frame). The verdict carries `view`: what was done and why."""
    if mode not in ("auto", "force", "never"):
        raise ValueError(f"mode must be auto, force or never, not {mode!r}")
    m = read(adapter)
    base: dict[str, Any] = {"threshold": cfg.auto_view_min, "z_before": m.z}
    if m.state in ACTIVE_STATES:  # 1. the job owns the bed: no move, no camera
        v = judge(max_age)
        return _annotate(v, {**base, "action": "not moved", "state": m.state},
                         f"{m.state}: bed not moved, view not judged")
    note = recover_pending(adapter, device.name, clock(), read) if mode != "never" else None
    if note:
        m = read(adapter)
        base["z_before"] = m.z
    seen = view(m.z) if m.z is not None else None
    base["visible"] = _r(seen)
    if seen is None or seen >= cfg.auto_view_min:  # 2. already a good view (or no calibration to say)
        return _annotate(judge(max_age), {**base, "action": "none"}, note)
    shown = f"{seen:.0%} of the plate is in the frame at Z {_fz(m.z)}"
    if mode == "never" or (mode == "auto" and not cfg.auto_view):
        off = "--no-move" if mode == "never" else "watch.bed.auto_view is off; --best-view moves it"
        return _annotate(judge(max_age), {**base, "action": "not moved", "why": off},
                         "; ".join(x for x in (note, f"{shown}, bed not moved ({off})") if x))
    why = _may_move(device, adapter, cfg, m, part_height_mm)
    limits = why if isinstance(why, Limits) else None
    if limits is None:  # 3.-5. the guards that stop a move
        return _annotate(judge(max_age), {**base, "action": "not moved", "why": str(why)},
                         "; ".join(x for x in (note, f"{shown}, bed not moved: {why}") if x))
    pick = pick_inspection_z(view, ref_zs, stands, m.z, z_floor=limits.z_floor, z_ceiling=limits.z_ceiling,
                             threshold=cfg.auto_view_min)
    base.update(pick=pick.to_json(), part_mm=limits.part_mm, part_source=limits.part_source,
                z_floor=limits.z_floor, z_ceiling=limits.z_ceiling)
    if pick.status != "move" or pick.z is None:
        return _annotate(judge(max_age), {**base, "action": "not moved", "why": pick.reason},
                         "; ".join(x for x in (note, f"bed not moved: {pick.reason}") if x))
    with view_lock(device.name) as held:
        if not held:
            why = "another view check of this printer is running"
            return _annotate(judge(max_age), {**base, "action": "not moved", "why": why},
                             "; ".join(x for x in (note, f"{shown}, bed not moved: {why}") if x))
        return _moved_check(device, adapter, m, pick, limits, base, note, judge, settle_s, sleep, clock, read)


def _may_move(device: Any, adapter: Any, cfg: BedConfig, m: Motion, override: float | None) -> Limits | str:
    if getattr(device, "access", "full") != "full":
        return f"{device.name} is {device.access}: no writes"
    if not m.ready:
        return "Klipper is not ready"
    if m.state not in QUIET_STATES:
        return f"cannot move: printer is {m.state} [{m.raw_state}]"
    if m.z is None:
        return "cannot move: toolhead Z unknown"
    if "z" not in m.homed.lower():
        return f"cannot move: not homed (homed axes: {m.homed or 'none'}); the bed check never homes"
    return move_limits(device, adapter, cfg, m, override)


def _moved_check(device: Any, adapter: Any, m: Motion, pick: ViewPick, limits: Limits, base: dict[str, Any],
                 note: str | None, judge: Callable[[float | None], dict[str, Any]], settle_s: float,
                 sleep: Callable[[float], None], clock: Callable[[], float],
                 read: Callable[[Any], Motion]) -> dict[str, Any]:
    """Move to `pick.z`, judge a fresh frame, restore. The restore is in the `finally`: it runs after
    any exception and after SIGTERM (terminate_as_exit)."""
    assert pick.z is not None and m.z is not None
    target_gcode = pick.z - m.offset
    attempted = False
    verdict: dict[str, Any] | None = None
    with terminate_as_exit():
        try:
            _write_pending(device.name, m, clock())
            attempted = True  # from here on the bed may be away from where it was found
            guarded_z_move(adapter, to_gcode_z=target_gcode, floor=limits.z_floor, ceiling=limits.z_ceiling,
                           target_toolhead_z=pick.z, read=read)
            _log(f"{device.name}: bed moved Z {m.z:.1f} -> {pick.z:.1f} ({pick.visible:.0%} of the plate in view)")
            sleep(settle_s)  # the camera rewrites its frame every ~4 s; the bed shakes a moment
            verdict = judge(None)
        except MoveRefused as exc:  # a guard fired before anything moved: the old verdict, with the reason
            attempted = False
            _clear_pending(device.name)
            verdict = judge(None)
            return _annotate(verdict, {**base, "action": "not moved", "why": str(exc)},
                             "; ".join(x for x in (note, f"bed not moved: {exc}") if x))
        finally:
            if attempted:
                restore_error = restore_z(adapter, m, read=read)
                if restore_error is None:
                    _clear_pending(device.name)
                    _log(f"{device.name}: bed restored to Z {m.z:.1f}")
                else:
                    _log(f"{device.name}: BED NOT RESTORED to Z {m.z:.1f}: {restore_error}")
                if verdict is not None:
                    verdict["_restore_error"] = restore_error
    assert verdict is not None
    restore_error = verdict.pop("_restore_error", None)
    used = {"action": "moved", "z_used": verdict.get("z"), "moved_from": m.z, "restored": restore_error is None,
            "restore_error": restore_error}
    text = (f"judged at Z {_fz(verdict.get('z'))} after moving the bed from Z {m.z:.1f} "
            f"({_pct(base.get('visible'))} -> {pick.visible:.0%} of the plate in the frame), "
            + ("bed put back" if restore_error is None else f"BED NOT PUT BACK: {restore_error}"))
    return _annotate(verdict, {**base, **used}, "; ".join(x for x in (note, text) if x))


__all__ = ["Candidate", "Limits", "MoveRefused", "Motion", "ViewPick", "auto_view", "guarded_z_move",
           "move_limits", "part_height", "pick_inspection_z", "read_motion", "recover_pending", "restore_z",
           "view_in_progress"]
