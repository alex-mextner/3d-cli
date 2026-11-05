"""bed_refresh.py — keep the empty-bed reference current (issue #723, item 3).

`3d printer bed reference NAME --refresh [--assume-empty "why"]` re-captures the reference from the best view
with the printer's light on and replaces the stale one of the same plate type, light state and view
(bed_refs.py). The frame must earn it: it is judged against the existing references first and stored only
when that very frame is a confident `clear` with the plate on, in a good view (`watch.bed.auto_view_min` of the
plate in the frame), with the light on; otherwise nothing is stored and the reason is said. `--assume-empty
"why"` is the owner's word instead of that verdict (the reason is kept in the reference and in the journal).

`watch.bed.auto_reference: true` does the same by itself: `bed_state.status_view` (`bed status`, `bed check`,
the preflight `bed` line, the one-shot `bed-free` check at the job's end) calls `auto_refresh` with the
verdict it just made; an idle printer, a confident clear in a good view with the light on, and the frame of
that verdict becomes the reference. Every refresh, manual or automatic, is one JSON line in
~/.local/state/3d-cli/bed/NAME.refs.log.

WRITES: the reference files (data dir) and the journal, plus what `status_view` already does (the guarded
Z-only move and back, the light). Imports bed_state (numpy + OpenCV) like bed_part.py does.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bed_refs as R
import bed_state as B
from printer_ops.bed import AUTO_VIEW_SETTLE_S, PRINTING, BedConfig
from printer_ops.bed_light import LightSession, write_block

if TYPE_CHECKING:
    from printer_ops.devices import Device


def _fz(z: object) -> str:
    return f"{z:.1f}" if isinstance(z, (int, float)) else "?"


def _light_state_of(light: LightSession) -> Callable[[], str]:
    return lambda: R.light_state(light.report.to_dict())


def _auto_refusal(cfg: BedConfig, verdict: dict[str, Any]) -> str | None:
    """Why this verdict does not refresh the reference by itself, or None."""
    if verdict.get("source") != "camera now":
        return f"the verdict is not from a fresh frame (source {verdict.get('source')})"
    view = verdict.get("view") or {}
    if view.get("action") == "moved" and not view.get("restored"):
        return "the bed was not put back where it was found"
    if not verdict.get("raw_frame") or not isinstance(verdict.get("z"), (int, float)):
        return "the verdict has no camera frame to store"
    return R.refresh_refusal(verdict, cfg.auto_view_min)


def auto_refresh(
    device: Device, cfg: BedConfig, verdict: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """`watch.bed.auto_reference`: store the camera frame of `verdict` as the reference when it is a confident
    clear (see the module docstring). -> {refreshed, reason | path}; never raises: the verdict stands."""
    why = _auto_refusal(cfg, verdict)
    if why is not None:
        return {"refreshed": False, "reason": why}
    try:
        frame = Path(str(verdict["raw_frame"])).read_bytes()
        ref = B.capture_reference(
            device,
            cfg,
            plate=True,
            frame=frame,
            frame_z=float(verdict["z"]),
            now=now,
            light_state=lambda: R.light_state(verdict.get("light")),
        )
    except Exception as exc:  # noqa: BLE001 — a failed refresh never changes the verdict it came from
        return {"refreshed": False, "reason": f"{type(exc).__name__}: {exc}"}
    R.journal_refresh(
        device.name,
        why="a confident clear check in a good view with the light on",
        z=ref["z"],
        light=ref["light"],
        verdict=f"{verdict.get('bed')}/{verdict.get('plate')}",
        now=now,
    )
    return {
        "refreshed": True,
        "path": ref["path"],
        "z": ref["z"],
        "light": ref["light"],
    }


def _printing_refusal(device: Device) -> dict[str, Any]:
    return {
        "refreshed": False,
        "refused": f"{device.name} is printing: a reference needs the empty bed of an idle printer",
    }


def _take(
    device: Device,
    cfg: BedConfig,
    *,
    assume: str | None,
    light: LightSession,
    judge: Callable[[], dict[str, Any]],
    now: float | None,
) -> dict[str, Any]:
    """Judge a fresh frame (`judge`), then store it as the reference when it may be. The frame that is judged is
    the frame that is stored."""
    v = {**judge(), "light": light.report.to_dict()}  # what the light did for THIS frame
    if v.get("bed") == PRINTING:
        return {**v, **_printing_refusal(device)}
    refusal = None if assume is not None else R.refresh_refusal(v, cfg.auto_view_min)
    if refusal is None and not (
        v.get("raw_frame") and isinstance(v.get("z"), (int, float))
    ):
        refusal = "the check returned no camera frame to store"
    if refusal is not None:
        return {**v, "refreshed": False, "refused": refusal}
    ref = B.capture_reference(
        device,
        cfg,
        plate=True,
        frame=Path(str(v["raw_frame"])).read_bytes(),
        frame_z=float(v["z"]),
        now=now,
        light_state=_light_state_of(light),
        assumed=assume,
    )
    R.journal_refresh(
        device.name,
        why=assume or "a confident clear check in a good view with the light on",
        z=ref["z"],
        light=ref["light"],
        verdict=f"{v.get('bed')}/{v.get('plate')}",
        now=now,
        mode="assumed" if assume is not None else "manual",
    )
    return {**v, "refreshed": True, "new_reference": ref}


def refresh_reference(
    device: Device,
    cfg: BedConfig,
    *,
    assume_empty: str | None = None,
    part_height: float | None = None,
    adapter: Any = None,
    camera: Any = None,
    now: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    settle_s: float | None = None,
) -> dict[str, Any]:
    """`bed reference --refresh`: the bed check's flow (best view, light on, bed back), and the frame it judges
    becomes the reference when it is a confident clear (or the owner says `assume_empty`). Without
    `assume_empty` a Z with a reference is needed (the verdict is judged against it); with it any Z that shows
    the plate will do. The result is the bed verdict with `refreshed`, `refused` (why nothing was stored) and
    `new_reference`."""
    import bed_view
    from printer_ops import make_adapter

    adapter = adapter if adapter is not None else make_adapter(device, timeout=10.0)
    camera = camera if camera is not None else B.camera_for(device)
    match = B.z_match(device, cfg)
    refs = B.RefStore(device.name).load()
    light = LightSession(
        adapter,
        cfg.light,
        note="the reference frame",
        blocked=write_block(device),
        sleep=sleep,
    )

    def printer_fn(_: Device) -> tuple[str, float | None]:
        m = bed_view.read_motion(adapter)
        return m.state, m.z

    def judge(_: float | None) -> dict[str, Any]:
        return _take(
            device,
            cfg,
            assume=assume_empty,
            light=light,
            now=now,
            judge=lambda: B.one_shot(
                device,
                cfg,
                max_age=None,
                camera=camera,
                printer_fn=printer_fn,
                now=now,
                sleep=sleep,
                before_frame=light.on,
                light_state=_light_state_of(light),
            ),
        )

    def view(z: float) -> float | None:
        return 1.0 if match.cam is None else match.in_view(z)

    with light:
        light.on()  # before the move: the settle overlaps the Z travel
        res = bed_view.auto_view(
            device,
            cfg,
            mode="force",
            adapter=adapter,
            judge=judge,
            view=view,
            ref_zs=[r.z for r in refs if r.z is not None],
            stands=match,
            part_height_mm=part_height,
            sleep=sleep,
            need_reference=assume_empty is None,
            settle_s=AUTO_VIEW_SETTLE_S if settle_s is None else settle_s,
        )
    res["light"] = light.report.to_dict()
    return res


def _text(device: Device, res: dict[str, Any]) -> str:
    light = res.get("light") or {}
    tail = f"\n  light: {light['text']}" if light.get("text") else ""
    if not res.get("refreshed"):
        return (
            f"{device.name}: the reference was NOT refreshed, nothing was stored: {res.get('refused')}\n"
            f"  bed check: bed {res.get('bed')}, plate {res.get('plate')} (Z {_fz(res.get('z'))}): "
            f"{res.get('reason', '')}{tail}"
        )
    new = res["new_reference"]
    owner = (
        f"; the owner says it is empty: {new['assumed_empty']}"
        if new.get("assumed_empty")
        else ""
    )
    return (
        f"{device.name}: {new['kind']} reference refreshed at Z {new['z']:.1f} (plate {new['plate']}, light "
        f"{new['light']}){owner}\n  {new['path']}{tail}"
    )


def cmd_refresh(args: argparse.Namespace, device: Device, cfg: BedConfig) -> int:
    """`bed reference NAME --refresh`: exit 0 when the reference was replaced, 1 when nothing was stored."""
    res = refresh_reference(
        device, cfg, assume_empty=args.assume_empty, part_height=args.part_height
    )
    print(json.dumps(res, indent=1, default=str) if args.json else _text(device, res))
    return 0 if res.get("refreshed") else 1
