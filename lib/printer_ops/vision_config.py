"""vision_config.py — the per-printer knobs of `watch.vision` that are not the camera itself.

WHAT: `watch.vision.npu` (read the printer's OWN camera detector from klippy.log) and
  `watch.vision.limits` (stale / dark / NPU thresholds of printer_ops.vision.MonitorLimits) for one
  printer. The defaults in MonitorLimits were measured on the Snapmaker U1; another printer's camera
  needs its own, so they are settings of the printer, never a shared literal.

  watch:
    vision:
      npu: false              # default: true only for a `moonraker-monitor` camera (Snapmaker unisrv
                              #   runs a YOLO detector there and logs its verdicts); a K1 SE has none
      limits: {stale_s: 120, dark_luma: 20, npu_threshold: 0.83, npu_quiet_s: 300}

INVARIANTS: stdlib only; `limits_problems` returns messages (camera.vision_problems / fleet lint),
  `monitor_limits` assumes a validated block.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printer_ops.vision import MonitorLimits

LIMIT_KEYS = ("stale_s", "dark_luma", "npu_threshold", "npu_quiet_s")


def limits_problems(vision: dict[str, Any], where: str) -> list[str]:
    """Problems of `npu` and `limits` in a `watch.vision` block."""
    bad: list[str] = []
    if "npu" in vision and not isinstance(vision["npu"], bool):
        bad.append(f"{where}.npu: true or false")
    if "limits" in vision:
        limits = vision["limits"]
        if not isinstance(limits, dict):
            return [
                *bad,
                f"{where}.limits: must be a mapping of {', '.join(LIMIT_KEYS)} to numbers",
            ]
        bad += [
            f"{where}.limits.{k}: unknown (allowed: {', '.join(LIMIT_KEYS)})"
            for k in sorted(set(limits) - set(LIMIT_KEYS))
        ]
        bad += [
            f"{where}.limits.{k}: a number >= 0"
            for k, v in limits.items()
            if k in LIMIT_KEYS
            and (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v < 0
            )
        ]
    return bad


def monitor_limits(vision: dict[str, Any]) -> MonitorLimits:
    """printer_ops.vision.MonitorLimits with this printer's `limits` applied over the defaults."""
    from printer_ops.vision import MonitorLimits

    base, given = MonitorLimits(), vision.get("limits") or {}
    return replace(
        base,
        stale_s=float(given.get("stale_s", base.stale_s)),
        dark_luma=float(given.get("dark_luma", base.dark_luma)),
        npu_threshold=float(given.get("npu_threshold", base.npu_threshold)),
        npu_quiet_s=float(given.get("npu_quiet_s", base.npu_quiet_s)),
    )


def npu_enabled(device: Device) -> bool:
    """Whether to read the printer's own detector verdicts: `watch.vision.npu`, else on for the
    Snapmaker-style monitor camera (the unisrv process that serves it also runs the detector)."""
    from printer_ops.camera import MONITOR, camera_spec

    vision = (device.watch or {}).get("vision") or {}
    flag = vision.get("npu")
    return flag if isinstance(flag, bool) else camera_spec(device).kind == MONITOR
