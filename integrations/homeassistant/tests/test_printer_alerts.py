"""Every printer alert in packages/3d_printers.yaml carries a fresh frame of the printer's camera.

The phone notification attaches a frame only when the caller passes `printer:` to
script.printer_send_notification, so a new alert without it would silently go out text-only.
The frame grabbers are command_line sensors; their shell commands run here against a fake
`curl`, so the success/failure contract and the retention are exercised, not just read.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "3d_printers.yaml"
# The frame grabbers are generated per fleet printer by `3d ha vision` (lib/ha_vision.py).
GENERATED = Path(__file__).resolve().parents[1] / "packages" / "printer_vision.yaml"
SCRIPT = "script.printer_send_notification"
PRINTERS = {"u1", "k1_se"}


def _package() -> dict[str, Any]:
    data = yaml.safe_load(PACKAGE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads HA's `!secret` tags as plain strings."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node))


def _calls(steps: Any) -> Iterator[dict[str, Any]]:
    """Every action step, including those nested in if/then/else, choose and sequence."""
    if isinstance(steps, list):
        for step in steps:
            yield from _calls(step)
    elif isinstance(steps, dict):
        if "action" in steps:
            yield steps
        for key in ("then", "else", "sequence", "default", "choose", "conditions"):
            if key in steps:
                yield from _calls(steps[key])


def _grabbers() -> dict[str, str]:
    """unique_id -> shell command of each command_line frame grabber."""
    return {
        s["sensor"]["unique_id"]: s["sensor"]["command"]
        for s in yaml.load(GENERATED.read_text(encoding="utf-8"), Loader=_Loader)["command_line"]  # noqa: S506
        if s["sensor"]["unique_id"].endswith("_alert_snapshot")
    }


def test_every_printer_alert_names_its_printer() -> None:
    alerts = {
        automation["id"]: call
        for automation in _package()["automation"]
        for call in _calls(automation["action"])
        if call["action"] == SCRIPT
    }
    assert alerts, "no automation sends a printer notification"
    for automation_id, call in alerts.items():
        printer = str(call["data"].get("printer", ""))
        # A template may pick between printers; every quoted name it can yield must be known.
        named = set(re.findall(r"'([a-z0-9_]+)'", printer)) or {printer}
        assert named <= PRINTERS, (
            f"{automation_id} names an unknown printer: {printer!r}"
        )


def test_offline_alert_maps_every_trigger_to_its_own_printer() -> None:
    offline = next(
        a for a in _package()["automation"] if a["id"] == "printer_offline_alert"
    )
    printer = next(c for c in _calls(offline["action"]) if c["action"] == SCRIPT)[
        "data"
    ]["printer"]
    mapping = dict(
        re.findall(
            r"'(binary_sensor\.printer_[a-z0-9_]+_online)': '([a-z0-9_]+)'", printer
        )
    )
    assert set(mapping) == set(offline["trigger"][0]["entity_id"])
    assert all(
        entity == f"binary_sensor.printer_{key}_online"
        for entity, key in mapping.items()
    )
    # A trigger missing from the map sends no picture rather than another printer's.
    assert ".get(trigger.entity_id, '')" in printer


def test_every_printer_has_a_grabber_the_script_finds() -> None:
    script = _package()["script"]["printer_send_notification"]
    grabber = script["sequence"][0]["variables"]["grabber"]
    assert "'sensor.printer_' ~ (printer | default('')) ~ '_alert_snapshot'" in grabber
    # one grabber per fleet printer with a camera; the hand-written alerts only name printers that have one
    assert {f"printer_{p}_alert_snapshot" for p in PRINTERS} <= set(_grabbers())


def test_script_attaches_only_a_file_written_by_this_run() -> None:
    steps = _package()["script"]["printer_send_notification"]["sequence"]
    check = steps[1]["then"][1]["variables"]["snapshot"]
    assert "startswith('printer-alerts/')" in check
    assert "last_changed.timestamp() >= started" in check
    phone = next(
        s for s in _calls(steps) if s["action"].startswith("notify.mobile_app_")
    )
    assert (
        phone["data"]["data"]
        == "{{ {'image': '/media/local/' ~ snapshot} if snapshot else {} }}"
    )
    update = next(
        s for s in _calls(steps) if s["action"] == "homeassistant.update_entity"
    )
    assert update.get("continue_on_error") is True


JPEG = r"\377\330\377\340jpeg"


def _run(
    command: str, tmp_path: Path, curl_exit: int, body: str = JPEG
) -> tuple[str, list[str]]:
    """Run a grabber with /media mapped into tmp_path and a fake curl that writes `body` (printf
    format) and exits 0, or writes a partial file and exits `curl_exit`."""
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'while [ "$1" != -o ]; do shift; done\n'
        f"[ {curl_exit} -eq 0 ] && printf '{body}' > \"$2\" && exit 0\n"
        'printf partial > "$2"; exit 22\n'
    )
    curl.chmod(0o755)
    local = command.replace("/media/", f"{media}/")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    out = subprocess.run(
        ["sh", "-c", local], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip(), sorted(
        p.name for p in (media / "printer-alerts").glob("*.jpg")
    )


@pytest.mark.parametrize(
    "unique_id", ["printer_u1_alert_snapshot", "printer_k1_se_alert_snapshot"]
)
def test_grabber_reports_the_new_file_or_failed(unique_id: str, tmp_path: Path) -> None:
    command = _grabbers()[unique_id]
    state, files = _run(command, tmp_path, curl_exit=0)
    assert state.startswith("printer-alerts/") and state.endswith(".jpg")
    assert files == [state.removeprefix("printer-alerts/")]
    # A failed fetch leaves no partial file and never names a file.
    state, after = _run(command, tmp_path, curl_exit=22)
    assert state == "failed"
    assert after == files


@pytest.mark.parametrize("body", ["<html>502 Bad Gateway</html>", ""])
def test_grabber_rejects_a_200_that_is_not_a_jpeg(body: str, tmp_path: Path) -> None:
    state, files = _run(
        _grabbers()["printer_k1_se_alert_snapshot"], tmp_path, curl_exit=0, body=body
    )
    assert state == "failed"
    assert files == []


def test_alert_automations_queue_instead_of_dropping() -> None:
    # The script waits for the frame; in the default single mode a trigger meanwhile is lost.
    # The script queues too: the alert automations run independently and all share it.
    assert _package()["script"]["printer_send_notification"]["mode"] == "queued"
    for automation in _package()["automation"]:
        if any(c["action"] == SCRIPT for c in _calls(automation["action"])):
            assert automation.get("mode") == "queued", automation["id"]


def test_grabber_keeps_the_newest_30_frames(tmp_path: Path) -> None:
    command = _grabbers()["printer_u1_alert_snapshot"]
    folder = tmp_path / "media" / "printer-alerts"
    folder.mkdir(parents=True)
    for i in range(40):
        old = folder / f"u1-old-{i:02d}.jpg"
        old.write_text("x")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    (folder / "k1_se-keep.jpg").write_text("x")
    state, files = _run(command, tmp_path, curl_exit=0)
    u1 = [f for f in files if f.startswith("u1-")]
    assert len(u1) == 30
    assert state.removeprefix("printer-alerts/") in u1
    assert "u1-old-39.jpg" in u1 and "u1-old-10.jpg" not in u1
    assert "k1_se-keep.jpg" in files
