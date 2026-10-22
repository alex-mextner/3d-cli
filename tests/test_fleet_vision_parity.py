"""Fleet parity of vision / watch / guard / bed: nothing generic carries one printer's literal.

Two gates:
  * a scan of the GENERIC modules for U1-only literals in code (an IP, the monitor.jpg path, the
    camera.start_monitor call, a hard-coded printer name), with an explicit, small allowlist of the
    modules that ARE U1-adapter-specific and say why;
  * the fleet registry iterated: every printer with a camera reaches vision, guard and the bed check
    through its own device settings, a printer without one is reported as having none.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET

import bed_state
import vision_detect
from errors import InvalidArgument, UsageError
from printer_ops import camera
from printer_ops.bed import THRESHOLD_KEYS, bed_config
from printer_ops.devices import fleet_devices
from printops.fleet import load_fleet

REPO = Path(__file__).resolve().parents[1]
# Wide: no module may carry a fleet printer's address, the camera path or the keepalive call.
GENERIC = (
    "lib/printer_ops/*.py",
    "lib/printops/*.py",
    "lib/printops/cli/*.py",
    "lib/printops/api/*.py",
    "lib/commands/*.py",
    "lib/vision_*.py",
    "lib/bed_state.py",
    "lib/ha_vision.py",
    "lib/ha_deploy*.py",
)
# Narrow: the vision / watch / guard / bed-check modules also carry no printer NAME ("u1", "Snapmaker U1")
# and no identifier named after one (spool slots, maintenance and the like are other features).
FEATURE = (
    "lib/printer_ops/*.py",
    "lib/printops/watch_incidents.py",
    "lib/commands/printer*.py",
    "lib/vision_*.py",
    "lib/bed_state.py",
    "lib/ha_vision.py",
)
# The modules that implement something genuinely specific to one printer family; each is the
# adapter side of a model check or a data table, not a default of the fleet.
U1_SPECIFIC = {
    "lib/printer_ops/camera.py": "the `moonraker-monitor` camera kind: Snapmaker's /server/files/camera/monitor.jpg",
    "lib/printer_ops/vision.py": "MoonrakerCamera's keepalive (camera.start_monitor) + the U1's own klippy.log NPU detector",
    "lib/vision_geometry.py": "the bundled-calibration table keyed by printer model (u1-camera.yaml)",
    "lib/printer_ops/moonraker.py": "Snapmaker print_task_config head filament, gated by that Klipper object",
    "lib/ha_vision.py": "names the camera-proxy script's own env variables (U1_CAMERA_*, a historical prefix)",
}
FLEET_HOSTS = tuple(re.escape(d.host) for d in fleet_devices(load_fleet(REPO_FLEET)).values())  # type: ignore[union-attr]
ADDRESS = re.compile("|".join([*FLEET_HOSTS, r"monitor\.jpg", "start_monitor", r"server/files/camera"]), re.I)
NAMES = {"u1", "snapmaker u1", "k1se", "k1 se", "creality k1 se"}
IDENT = re.compile(r"(^|_)u1(_|$)", re.I)
HELP_NAME = re.compile(r"^_?[A-Z_]*(USAGE|EXAMPLE|EXAMPLES|EPILOG|HELP)[A-Z_]*$")


def generic_files() -> list[Path]:
    return sorted({p for pattern in GENERIC for p in REPO.glob(pattern) if p.is_file()})


def literals(path: Path, names: bool = True) -> list[str]:
    """What a module hard-codes for one printer: strings (not docstrings, not USAGE/EXAMPLE help
    constants) with an address / the camera path / the keepalive call, and with `names` strings that
    ARE a printer name and identifiers named after a printer."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and HELP_NAME.match(t.id) for t in node.targets
        ):
            skip.update(id(n) for n in ast.walk(node.value))
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and HELP_NAME.match(node.target.id)
            and node.value
        ):
            skip.update(id(n) for n in ast.walk(node.value))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            text = node.value
            if ADDRESS.search(text) or (names and text.strip().lower() in NAMES):
                found.append(
                    f"{path.relative_to(REPO)}:{node.lineno}: string {text[:60]!r}"
                )
        name = (
            getattr(node, "id", None)
            or getattr(node, "name", None)
            or getattr(node, "arg", None)
            or getattr(node, "attr", None)
        )
        if names and isinstance(name, str) and IDENT.search(name):
            found.append(
                f"{path.relative_to(REPO)}:{getattr(node, 'lineno', 0)}: identifier {name}"
            )
    return found


def test_generic_modules_carry_no_u1_literal() -> None:
    feature = {p for pattern in FEATURE for p in REPO.glob(pattern)}
    hits = [
        h
        for path in generic_files()
        if str(path.relative_to(REPO)) not in U1_SPECIFIC
        for h in literals(path, names=path in feature)
    ]

    assert hits == [], (
        "a U1-only literal in a generic module: read it from the printer's fleet entry / watch: block "
        "(printer_ops/camera.py, devices.py) or, if the module is genuinely U1-specific, add it to "
        "U1_SPECIFIC with the reason:\n" + "\n".join(hits)
    )


def test_the_u1_allowlist_is_small_and_every_entry_is_used() -> None:
    assert len(U1_SPECIFIC) <= 5
    for relative in U1_SPECIFIC:
        assert (REPO / relative).is_file(), relative
        assert literals(REPO / relative) or "U1" in (REPO / relative).read_text(
            encoding="utf-8"
        ), f"{relative} needs no allowlist entry"


def test_the_scan_catches_a_planted_literal(tmp_path: Path) -> None:
    """The gate can fail: each kind of literal is found in a scratch module (nothing is committed)."""
    bad = tmp_path / "x.py"
    bad.write_text(
        '"""docstring about the U1 at 192.168.0.26 is fine"""\n'
        'USAGE = "3d printer vision u1 on 192.168.0.26"\n'
        'HOST = "192.168.0.26"\n'
        'PATH = "/server/files/camera/monitor.jpg"\n'
        'CALL = {"method": "camera.start_monitor"}\n'
        'def f(device):\n    return device.name == "u1"\n'
        "U1_MONITOR = 1\n",
        encoding="utf-8",
    )
    # literals() reports paths relative to the repo: use the repo-relative form for the scratch file
    monkeypatched = literals.__globals__["REPO"]
    literals.__globals__["REPO"] = tmp_path
    try:
        found = literals(bad)
    finally:
        literals.__globals__["REPO"] = monkeypatched

    kinds = " ".join(found)
    assert (
        "x.py:3: string '192.168.0.26'" in kinds
        and "monitor.jpg" in kinds
        and "start_monitor" in kinds
    )
    assert "x.py:7: string 'u1'" in kinds and "identifier U1_MONITOR" in kinds
    assert (
        "x.py:2" not in kinds and "x.py:1" not in kinds
    )  # usage text and docstrings are prose


# ---- the fleet iterated -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def devices() -> dict[str, Any]:
    fleet = load_fleet(REPO_FLEET)
    assert fleet is not None
    return fleet_devices(fleet)


def test_every_fleet_camera_reaches_vision_guard_and_bed_through_its_own_settings(
    devices: dict[str, Any],
) -> None:
    with_camera = {n: d for n, d in devices.items() if camera.camera_spec(d).present}
    assert {"u1", "k1se"} <= set(with_camera)
    args = argparse.Namespace(snapshot=None, no_keepalive=False, no_geometry=False)
    for name, device in with_camera.items():
        spec = camera.camera_spec(device)
        expected = spec.snapshot_url(device.host, device.port)
        for source in (
            vision_detect._device_camera(device, args),
            bed_state.camera_for(device),
        ):
            assert source.url == expected, f"{name}: {source.url} != {expected}"
            assert source.keepalive_enabled is spec.keepalive
    assert with_camera["u1"].host != with_camera["k1se"].host


def test_a_fleet_printer_without_a_camera_is_reported_as_having_none_not_crashed(
    devices: dict[str, Any],
) -> None:
    bare = dataclasses.replace(devices["k1se"], watch={"tg_fallback": True})
    args = argparse.Namespace(snapshot=None, no_keepalive=False, no_geometry=False)

    assert not camera.camera_spec(bare).present
    with pytest.raises(UsageError, match="no camera"):
        vision_detect._device_camera(bare, args)
    with pytest.raises(UsageError, match="no camera"):
        bed_state.camera_for(bare)
    with pytest.raises(InvalidArgument) as exc:
        bed_config(dataclasses.replace(bare, watch={"bed": {"enabled": True}}))
    assert "k1se has no camera" in exc.value.render()
    assert (
        bed_config(dataclasses.replace(bare, watch={"bed": {"enabled": False}})).enabled
        is False
    )


def test_the_bed_check_of_every_fleet_printer_loads_from_its_device_config(
    devices: dict[str, Any],
) -> None:
    enabled = {
        n: bed_config(d) for n, d in devices.items() if (d.watch or {}).get("bed")
    }

    assert {"u1", "k1se"} <= set(enabled)
    assert (
        enabled["k1se"].roi is not None and enabled["u1"].roi is None
    )  # K1: its own polygon; U1: the calibration
    for name in enabled:
        assert camera.camera_spec(devices[name]).present, (
            f"{name}: watch.bed without a camera"
        )


def test_bed_thresholds_are_per_printer_and_cover_every_tuned_value(
    devices: dict[str, Any],
) -> None:
    assert set(THRESHOLD_KEYS) == {
        f.name for f in dataclasses.fields(bed_state.Thresholds)
    }
    k1 = dataclasses.replace(
        devices["k1se"],
        watch={
            **devices["k1se"].watch,
            "bed": {
                "enabled": True,
                "roi": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
                "thresholds": {"clear_max": 0.07, "settle_frames": 5},
            },
        },
    )

    cfg = bed_config(k1)
    th = bed_state.thresholds_for(cfg)

    assert (th.clear_max, th.settle_frames) == (0.07, 5) and isinstance(
        th.settle_frames, int
    )
    assert th.level == bed_state.TH.level  # the rest stays at the tuned default
    assert bed_state.thresholds_for(bed_config(devices["u1"])) == bed_state.TH
    assert bed_state.z_match(k1, cfg).max_px == bed_state.TH.match_px
    tuned = dataclasses.replace(
        k1,
        watch={
            **k1.watch,
            "bed": {
                "enabled": True,
                "roi": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
                "thresholds": {"match_px": 3.0},
            }
        },
    )
    assert bed_state.z_match(tuned, bed_config(tuned)).max_px == 3.0


@pytest.mark.parametrize(
    "thresholds",
    [{"clear_maxx": 1}, {"clear_max": -1}, {"clear_max": "high"}, [0.1]],
)
def test_a_broken_bed_thresholds_block_is_refused(
    devices: dict[str, Any], thresholds: Any
) -> None:
    bad = dataclasses.replace(
        devices["k1se"], watch={"bed": {"enabled": True, "thresholds": thresholds}}
    )

    with pytest.raises(InvalidArgument):
        bed_config(bad)


def test_the_guard_reads_its_camera_from_the_device_not_from_a_u1_default(
    devices: dict[str, Any],
) -> None:
    import inspect

    source = inspect.getsource(vision_detect.run_guard)
    assert "_device_camera(device" in source and "monitor.jpg" not in source
    spec = camera.camera_spec(devices["k1se"])
    cam = vision_detect._device_camera(
        devices["k1se"], argparse.Namespace(snapshot=None, no_keepalive=False)
    )
    assert cam.url == spec.snapshot and not cam.keepalive_enabled


def test_the_live_loop_takes_the_npu_tap_and_the_limits_from_the_printer() -> None:
    import inspect

    source = inspect.getsource(vision_detect.run_live)

    assert "npu_enabled(device)" in source and "monitor_limits(cfg" in source
