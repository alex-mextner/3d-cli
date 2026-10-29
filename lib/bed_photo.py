"""bed_photo.py — the photo of the finished print on the table: raise the bed to a comfortable viewing height,
take a frame with the printer's camera, put the bed back (issue #579, `3d photo capture --lift`).

WHY: after a job the Snapmaker U1 rests at Z 200, far from its camera (`bed_view` docstring); a photo of the
  printed object taken there shows 15 % of the plate. The same guarded Z move that `3d printer bed status
  --best-view` uses brings the bed to a Z where the object is in view, the frame is taken, the bed returns.

WHAT (stdlib only; the pixels/calibration stay in bed_state.py, loaded lazily by `calibrated_view`):
  `pick_photo_z`        PURE: the Z to photograph at. Candidates are a 5 mm grid inside [floor, ceiling]; a Z
                        qualifies when the plate share in the frame is >= `threshold`; views within VIEW_TIE of
                        the best are equal and the nearest to the current Z wins (least motion). No qualifying Z
                        -> a refusal with the reason, never a guess.
  `capture_table_photo` the flow: unless `dry_run`, take the per-printer view lock FIRST (every read that
                        decides the move happens under it), put back a bed an interrupted view left displaced
                        (a marker that cannot be undone is never overwritten: refuse), refuse unless the
                        printer is idle/complete/cancelled with Z homed, decide the target Z (`--lift MM` or
                        `auto`), write the displaced-Z marker, `guarded_z_move` (refused when the gcode Z
                        offset changed since the plan: the G-code Z must still land at the planned Z), settle,
                        grab a FRESH frame, restore the PHYSICAL Z found (`bed_view.restore_physical_z`, in the
                        offset now in force) in a `finally`. The photo is saved even when the restore fails.

RULES (same as bed_view): the only writes are `G90`, `G1 Z<z> F<feed>`, `M400` through `guarded_z_move` (it
  re-reads the printer right before each send); nothing moves while a job owns the bed; the move is bounded by
  `bed_view.move_limits` (floor = tallest part + clearance, ceiling = resume.z_max_mm - 5); the bed is restored
  after any exception and after SIGTERM; an interrupted run is undone by the next bed check's marker recovery.
  `dry_run` (the default of every caller) reads the printer, decides and reports, and sends nothing.

TESTS: tests/test_bed_photo.py drives it with a simulated Moonraker adapter and camera; no printer is touched.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bed_view
from errors import GateFailure, InvalidArgument, UsageError
from printer_ops.bed import AUTO_VIEW_SETTLE_S, QUIET_STATES, BedConfig

GRID_MM = bed_view.GRID_MM
VIEW_TIE = bed_view.VIEW_TIE
FRESH_TRIES = (
    6  # grabs of the camera for a frame that differs from the one before the move
)
LIFT_TOLERANCE_MM = 0.15  # already this close to the target: no move

# The displaced-Z marker and the "may the bed move" decision are bed_view's own (private) helpers: this module
# is their second user and must not copy them, or the two flows would disagree about what is safe.
_mark_displaced = bed_view._write_pending
_clear_displaced = bed_view._clear_pending
_may_move = bed_view._may_move
_pending = bed_view._read_pending

ViewFn = Callable[[float], float | None]


@dataclass(frozen=True)
class LiftPlan:
    """Where the bed goes for the photo and why."""

    target_z: float  # toolhead Z of the photo
    from_z: float
    floor: float
    ceiling: float
    move: bool  # False: the bed already stands there
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "target_z": round(self.target_z, 2),
            "from_z": round(self.from_z, 2),
            "floor": round(self.floor, 2),
            "ceiling": round(self.ceiling, 2),
            "move": self.move,
            "reason": self.reason,
        }


def pick_photo_z(
    view: ViewFn, current_z: float, *, floor: float, ceiling: float, threshold: float
) -> tuple[float | None, str]:
    """The Z inside [floor, ceiling] that shows the most of the plate (>= threshold), nearest to `current_z`
    among views within VIEW_TIE of the best. (None, why) when no Z qualifies."""
    zs = [floor + i * GRID_MM for i in range(int((ceiling - floor) // GRID_MM) + 1)]
    if ceiling not in zs:
        zs.append(ceiling)
    seen = [(z, v) for z in zs if (v := view(z)) is not None]
    if not seen:
        return (
            None,
            "the camera calibration cannot tell how much of the plate is in the frame at any allowed Z",
        )
    best = max(v for _, v in seen)
    if best < threshold:
        return None, (
            f"no allowed Z (Z {floor:.0f}..{ceiling:.0f}) shows {threshold:.0%} of the plate; "
            f"the best, Z {max(seen, key=lambda s: s[1])[0]:.0f}, shows {best:.0%}"
        )
    near = [(z, v) for z, v in seen if v >= best - VIEW_TIE and v >= threshold]
    z, v = min(near, key=lambda s: (abs(s[0] - current_z), -s[1]))
    return z, f"{v:.0%} of the plate is in the frame at Z {z:.1f} (best {best:.0%})"


def calibrated_view(device: Any, cfg: BedConfig) -> ViewFn | None:
    """`view(z)` from the printer's camera calibration (bed_state, numpy), or None when it has none."""
    import bed_state  # numpy/OpenCV: loaded only when `--lift auto` really needs the calibration

    match = bed_state.z_match(device, cfg)
    return None if match.cam is None else match.in_view


def plan_lift(
    device: Any,
    adapter: Any,
    cfg: BedConfig,
    motion: bed_view.Motion,
    *,
    lift: str | float,
    part_height_mm: float | None,
    view: ViewFn | None,
) -> LiftPlan:
    """Decide the photo Z or refuse (GateFailure, exit 1) with the first thing that has to change."""
    limits = _may_move(device, adapter, cfg, motion, part_height_mm)
    if not isinstance(limits, bed_view.Limits):
        raise GateFailure(
            f"the bed is not lifted for the photo: {limits}",
            command="photo",
            remediation=["Fix the first problem named above; nothing was moved."],
        )
    assert motion.z is not None  # _may_move refuses an unknown Z
    if lift == "auto":
        if view is None:
            raise GateFailure(
                f"{device.name} has no camera calibration, so `--lift auto` cannot know which Z shows the plate",
                command="photo",
                remediation=[
                    "Give the height: `3d photo capture ... --lift MM` (toolhead Z, e.g. 70)."
                ],
            )
        target, why = pick_photo_z(
            view,
            motion.z,
            floor=limits.z_floor,
            ceiling=limits.z_ceiling,
            threshold=cfg.auto_view_min,
        )
        if target is None:
            raise GateFailure(
                f"no Z for the photo: {why}",
                command="photo",
                remediation=["Give the height with --lift MM."],
            )
    else:
        target, why = float(lift), f"--lift {float(lift):g}"
        if not limits.z_floor <= target <= limits.z_ceiling:
            raise GateFailure(
                f"--lift {target:g} is outside the allowed Z {limits.z_floor:.0f}..{limits.z_ceiling:.0f} "
                f"(floor = {limits.part_mm:g} mm part from {limits.part_source} + clearance; ceiling = z_max - 5)",
                command="photo",
                remediation=[
                    f"Use a height between {limits.z_floor:.0f} and {limits.z_ceiling:.0f}, or --part-height MM."
                ],
            )
    return LiftPlan(
        target,
        motion.z,
        limits.z_floor,
        limits.z_ceiling,
        abs(target - motion.z) > LIFT_TOLERANCE_MM,
        why,
    )


def parse_lift(raw: str) -> str | float:
    """`auto` or a Z in mm (a finite number > 0)."""
    if raw == "auto":
        return "auto"
    try:
        z = float(raw)
    except ValueError:
        z = float("nan")
    if not 0 < z < float("inf"):
        raise InvalidArgument(
            "--lift", raw, ["auto", "a toolhead Z in mm, e.g. 70"], command="photo"
        )
    return z


def fresh_frame(
    camera: Any,
    before: bytes | None,
    sleep: Callable[[float], None],
    tries: int = FRESH_TRIES,
) -> tuple[bytes, bool]:
    """(jpeg, fresh): grab until the frame differs from `before` (the camera rewrites its file every ~4 s)."""
    jpeg = b""
    for _ in range(tries):
        camera.keepalive()
        jpeg = bytes(camera.grab().jpeg)
        if before is None or jpeg != before:
            return jpeg, True
        sleep(1.0)
    return jpeg, False


def check_part_height(part_height_mm: float | None) -> None:
    """A declared part height is a finite height >= 0 mm; anything else would lower the clearance floor."""
    if part_height_mm is not None and not 0 <= part_height_mm < float("inf"):
        raise InvalidArgument(
            "--part-height",
            str(part_height_mm),
            ["a finite height in mm, 0 or more"],
            command="photo",
        )


def _refuse_recovery_below_floor(
    device: Any,
    adapter: Any,
    cfg: BedConfig,
    part_height_mm: float | None,
    clock: Callable[[], float],
    read: Callable[[Any], bed_view.Motion],
) -> None:
    """A fresh marker would send the bed back to the Z it was found at; when the part now declared stands
    taller than that Z allows (below the clearance floor), refuse before any move: the marker is kept."""
    pending = _pending(device.name)
    if pending is None:
        return
    z, ts = bed_view._finite(pending.get("z")), bed_view._finite(pending.get("ts"))
    if z is None or ts is None or not 0 <= clock() - ts <= bed_view.PENDING_MAX_AGE_S:
        return
    m = read(adapter)
    if m.state in bed_view.ACTIVE_STATES or (m.z is not None and abs(m.z - z) <= 0.15):
        return  # recovery sends nothing in these cases
    limits = _may_move(device, adapter, cfg, m, part_height_mm)
    if isinstance(limits, bed_view.Limits) and z < limits.z_floor:
        raise GateFailure(
            f"{device.name}: putting the bed back to Z {z:.0f} (left there by an interrupted view) would "
            f"go below the clearance floor Z {limits.z_floor:.0f} of the {limits.part_mm:g} mm part from "
            f"{limits.part_source}; nothing was moved and the marker is kept",
            command="photo",
            remediation=[
                f"Run `3d printer bed status {device.name}` once the part is removed or with the height it "
                "had, then retry."
            ],
        )


def capture_table_photo(
    device: Any,
    cfg: BedConfig,
    *,
    adapter: Any,
    camera: Any,
    out: Path,
    lift: str | float = "auto",
    part_height_mm: float | None = None,
    view: ViewFn | None = None,
    dry_run: bool = True,
    settle_s: float = AUTO_VIEW_SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    read: Callable[[Any], bed_view.Motion] = bed_view.read_motion,
) -> dict[str, Any]:
    """Photograph the print at a lifted bed. -> `{printer, dry_run, plan, path, z_mm, moved, restored, ...}`.

    Nothing is sent to a printer that is not full-access (checked before any marker recovery), nor to one whose
    toolhead Z or G-code Z is missing/non-finite (the Z offset would be guessed): both refuse with GateFailure.
    Raises GateFailure when the printer is not idle (nothing moved, the camera is not asked), when a guard
    refuses the lift, or when the camera gives no frame. A failed restore is NOT raised: the photo exists, so it
    is returned with `restored: false` and `restore_error` for the caller to report and exit 1.

    Everything that decides the move (the Z read, the plan) happens under the per-printer view lock, so a
    concurrent view cannot change the bed between the read and the restore. A displaced-Z marker left by an
    interrupted view is undone first; one that cannot be undone is never overwritten (its original Z would be
    lost) and refuses the capture.
    """
    check_part_height(part_height_mm)
    if (
        not dry_run and out.exists()
    ):  # refused before the bed moves: the photo would have nowhere to go
        raise InvalidArgument(
            "--out",
            str(out),
            ["a path that does not exist yet; a saved photo is never overwritten"],
            command="photo",
        )
    if dry_run:
        m = _quiet_motion(device, adapter, read)
        plan = plan_lift(
            device, adapter, cfg, m, lift=lift, part_height_mm=part_height_mm, view=view
        )
        return _new_doc(device, plan, dry_run=True)
    no_write = bed_view.no_write_access(device)
    if (
        no_write is not None
    ):  # before any recovery: a read-only printer is never sent anything
        raise GateFailure(
            f"the bed is not lifted for the photo: {no_write}",
            command="photo",
            remediation=["Use a full-access printer; nothing was moved."],
        )
    _refuse_recovery_below_floor(device, adapter, cfg, part_height_mm, clock, read)
    note = bed_view.recover_pending(adapter, device.name, clock(), read)
    if note:
        bed_view._log(f"{device.name}: {note}")
    with bed_view.view_lock(device.name) as held:
        if not held:
            raise GateFailure(
                f"another bed view of {device.name} is running",
                command="photo",
                remediation=["Retry in a minute."],
            )
        if _pending(device.name) is not None:
            raise GateFailure(
                f"{device.name}: an interrupted bed view left the bed away from where it was found and it "
                "could not be put back yet; no photo lift",
                command="photo",
                remediation=[
                    f"Check `3d printer status {device.name}`, run `3d printer bed status {device.name}` once the "
                    "printer is idle (it puts the bed back), then retry."
                ],
            )
        m = _quiet_motion(device, adapter, read)
        plan = plan_lift(
            device, adapter, cfg, m, lift=lift, part_height_mm=part_height_mm, view=view
        )
        doc = _new_doc(device, plan, dry_run=False)
        if not plan.move:
            jpeg, fresh = fresh_frame(camera, None, sleep)
            return _saved(doc, out, jpeg, fresh, m.z)
        return _lifted_photo(
            device,
            adapter,
            camera,
            out,
            doc,
            plan,
            m,
            settle_s=settle_s,
            sleep=sleep,
            clock=clock,
            read=read,
        )


def _quiet_motion(
    device: Any, adapter: Any, read: Callable[[Any], bed_view.Motion]
) -> bed_view.Motion:
    """The printer's motion, or GateFailure when a job owns the bed."""
    m = read(adapter)
    if m.state in bed_view.ACTIVE_STATES or m.state not in QUIET_STATES:
        raise GateFailure(
            f"{device.name} is {m.state} [{m.raw_state}]: the bed belongs to the job, no photo lift",
            command="photo",
            remediation=[
                f"Wait for the job to end (`3d printer status {device.name}`), then retry."
            ],
        )
    return m


def _new_doc(device: Any, plan: LiftPlan, *, dry_run: bool) -> dict[str, Any]:
    return {
        "printer": device.name,
        "dry_run": dry_run,
        "plan": plan.to_json(),
        "path": None,
        "z_mm": None,
        "moved": False,
        "restored": True,
        "restore_error": None,
        "fresh": None,
    }


def _lifted_photo(
    device: Any,
    adapter: Any,
    camera: Any,
    out: Path,
    doc: dict[str, Any],
    plan: LiftPlan,
    m: bed_view.Motion,
    *,
    settle_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    read: Callable[[Any], bed_view.Motion],
) -> dict[str, Any]:
    """Lift (marker, guarded move), settle, grab a fresh frame, restore; the caller holds the view lock."""
    before, _ = fresh_frame(
        camera, None, sleep, tries=1
    )  # the frame at the resting Z: not "fresh" later
    attempted = False
    restore_error: str | None = None
    after_z: float | None = None
    jpeg, fresh = b"", False
    with bed_view.terminate_as_exit():
        try:
            _mark_displaced(device.name, m, clock())
            attempted = True
            bed_view.guarded_z_move(
                adapter,
                to_gcode_z=plan.target_z - m.offset,
                floor=plan.floor,
                ceiling=plan.ceiling,
                target_toolhead_z=plan.target_z,
                read=read,
            )
            doc["moved"] = True
            sleep(settle_s)
            moved = read(adapter)
            after_z = moved.z
            jpeg, fresh = fresh_frame(camera, before, sleep)
        except bed_view.MoveRefused as exc:  # a guard fired before anything moved
            attempted = False
            _clear_displaced(device.name)
            raise GateFailure(
                f"the bed was not lifted: {exc}",
                command="photo",
                remediation=["Nothing was moved."],
            ) from exc
        finally:
            if attempted:
                restore_error = (
                    "the Z it was found at is unknown"
                    if m.z is None
                    else bed_view.restore_physical_z(adapter, m.z, read=read)
                )
                if restore_error is None:
                    _clear_displaced(device.name)
                else:
                    bed_view._log(
                        f"{device.name}: BED NOT RESTORED to Z {m.z}: {restore_error}"
                    )
    doc["restored"] = restore_error is None
    doc["restore_error"] = restore_error
    if not jpeg:
        raise GateFailure(
            f"{device.name}: the camera returned no frame"
            + ("" if restore_error is None else f"; BED NOT RESTORED: {restore_error}"),
            command="photo",
        )
    return _saved(
        doc, out, jpeg, fresh, after_z if after_z is not None else plan.target_z
    )


def _saved(
    doc: dict[str, Any], out: Path, jpeg: bytes, fresh: bool, z: float | None
) -> dict[str, Any]:
    if not jpeg:
        raise UsageError("the camera returned no frame", command="photo")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open(
            "xb"
        ) as fh:  # exclusive: a photo already on disk is never overwritten
            fh.write(jpeg)
    except FileExistsError as exc:
        raise UsageError(
            f"{out} already exists; the photo was not saved over it",
            command="photo",
            remediation=["Pass a --out path that does not exist yet."],
        ) from exc
    doc.update(path=str(out), z_mm=None if z is None else round(z, 2), fresh=fresh)
    return doc
