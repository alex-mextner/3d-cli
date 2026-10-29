"""The always-on supervision path never depends on Home Assistant (#643, owner requirement 2026-10-24: it must
work "with Home Assistant and without it"). Static guards over the modules that run in the units, a runtime
import check, and a check that every camera of the fleet is read from the printer itself. HA stays an OPTIONAL
consumer: the status files `vision_status.py` writes and the generated HA package read them, never the reverse."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
SUPERVISION_PATH = [
    "printer_ops/watch.py",
    "printer_ops/watch_run.py",
    "printer_ops/watch_sinks.py",
    "printer_ops/vision.py",
    "printer_ops/vision_cycle.py",
    "printer_ops/vision_config.py",
    "printer_ops/vision_status.py",
    "printer_ops/auto_pause.py",
    "printer_ops/subscriptions.py",
    "printer_ops/printer_agent.py",
    "printer_ops/camera.py",
    "printer_ops/supervise.py",
    "printer_ops/supervise_units.py",
    "printer_ops/intentional_off.py",
    "printer_doctor/checks/supervision.py",
    "vision_detect.py",
]
HA_MODULES = (
    "homeassistant",
    "ha_vision",
    "commands.ha",
    "hass",
    "hassio",
    "aiohttp_ha",
)
HA_STRINGS = (
    "homeassistant",
    "home-assistant",
    "home_assistant",
    "hassio",
    ":8123",
    "supervisor/ingress",
    "/api/webhook",
)


def _doc_nodes(tree: ast.AST) -> set[int]:
    docs: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
        ):
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docs.add(id(first.value))
    return docs


@pytest.mark.parametrize("relative", SUPERVISION_PATH)
def test_no_module_of_the_supervision_path_imports_or_names_home_assistant(
    relative: str,
) -> None:
    tree = ast.parse((LIB / relative).read_text(encoding="utf-8"))
    docs = _doc_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [
                node.module or "",
                *(f"{node.module}.{a.name}" for a in node.names),
            ]
        else:
            names = []
        for name in names:
            assert not any(
                name == m or name.startswith(m + ".") or name.endswith("." + m)
                for m in HA_MODULES
            ), (
                f"{relative}:{getattr(node, 'lineno', 0)} imports {name}: the supervision path must not need Home Assistant"
            )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
        ):
            low = node.value.lower()
            assert not any(s in low for s in HA_STRINGS), (
                f"{relative}:{getattr(node, 'lineno', 0)} has a Home Assistant string constant {node.value[:60]!r}"
            )


def test_the_units_import_without_pulling_in_home_assistant_code() -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(LIB)!r})\n"
        "import printer_ops.watch_run, printer_ops.vision_cycle, printer_ops.supervise, printer_ops.watch_sinks\n"
        "bad = sorted(m for m in sys.modules if any(t in m.lower() for t in ('homeassistant', 'ha_vision', 'hass')))\n"
        "print(bad)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", r.stdout


def test_every_fleet_camera_is_read_from_the_printer_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The snapshot a unit grabs is the printer's own (its Moonraker origin or its own mjpg-streamer): never an HA
    proxy or entity. `streams:` URLs are for go2rtc/HA consumers only and are not read by the vision loop."""
    from urllib.parse import urlsplit

    from printer_ops.camera import camera_spec
    from printer_ops.devices import load_devices
    from printops.fleet import load_fleet

    monkeypatch.delenv("THREED_FLEET", raising=False)  # the repo's fleet/printers.yaml, not the owner's overrides
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    fleet = load_fleet()
    assert fleet is not None
    checked = 0
    for name, device in load_devices().items():
        spec = camera_spec(device)
        if not spec.present:
            continue
        url = spec.snapshot_url(device.host, device.port)
        assert url is not None, name
        assert urlsplit(url).hostname == device.host, (
            f"{name}: the camera snapshot {url} is not on the printer {device.host}"
        )
        checked += 1
    assert checked >= 1
