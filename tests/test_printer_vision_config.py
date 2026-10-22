"""printer_ops.vision_config: the per-printer NPU switch and monitor limits of `watch.vision`.

MonitorLimits' defaults were measured on the Snapmaker U1; the printer's own detector exists on the
Snapmaker camera only. Both are settings of the printer, not shared literals.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET

from printer_ops import camera
from printer_ops.devices import fleet_devices
from printer_ops.vision import MonitorLimits
from printer_ops.vision_config import LIMIT_KEYS, limits_problems, monitor_limits, npu_enabled
from printops.fleet import load_fleet


def device(vision: dict[str, Any] | None, model: str | None = None) -> Any:
    watch = {} if vision is None else {"vision": vision}
    return SimpleNamespace(name="p", host="h", port=7125, model=model, watch=watch, source=Path("d.yaml"))


def test_the_npu_is_read_only_where_the_printer_has_one() -> None:
    devices = fleet_devices(load_fleet(REPO_FLEET))  # type: ignore[arg-type]

    assert npu_enabled(devices["u1"]) is True  # the Snapmaker monitor camera: unisrv runs the detector
    assert npu_enabled(devices["k1se"]) is False  # mjpg-streamer: no detector, nothing to read
    assert npu_enabled(device({"npu": False}, "Snapmaker U1")) is False
    assert npu_enabled(device({"npu": True, "camera": {"kind": "mjpg-streamer"}})) is True
    assert npu_enabled(device(None, "Snapmaker U1")) is True and npu_enabled(device(None)) is False


def test_limits_override_the_defaults_per_printer_and_nothing_else() -> None:
    assert monitor_limits({}) == MonitorLimits()
    tuned = monitor_limits({"limits": {"npu_threshold": 0.9, "stale_s": 60}})

    assert (tuned.npu_threshold, tuned.stale_s) == (0.9, 60.0)
    assert (tuned.dark_luma, tuned.npu_quiet_s) == (MonitorLimits().dark_luma, MonitorLimits().npu_quiet_s)
    assert set(LIMIT_KEYS) <= set(MonitorLimits.__dataclass_fields__)


@pytest.mark.parametrize(
    ("vision", "fragment"),
    [
        ({"npu": "yes"}, "npu: true or false"),
        ({"limits": [1]}, "limits: must be a mapping"),
        ({"limits": {"stale": 1}}, "limits.stale: unknown"),
        ({"limits": {"stale_s": -1}}, "limits.stale_s: a number >= 0"),
        ({"limits": {"dark_luma": "dark"}}, "limits.dark_luma: a number >= 0"),
        ({"limits": {"stale_s": float("nan")}}, "limits.stale_s: a number >= 0"),
    ],
)
def test_a_broken_npu_or_limits_setting_is_named(vision: dict[str, Any], fragment: str) -> None:
    assert any(fragment in p for p in limits_problems(vision, "k.watch.vision"))
    assert any(fragment in p for p in camera.vision_problems(vision, "k.watch.vision"))
