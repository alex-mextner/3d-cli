"""Printer-card resource recovery and the deterministic browser bundle."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations/homeassistant"


def _cards(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [card for item in value for card in _cards(item)]
    if not isinstance(value, dict):
        return []
    if str(value.get("type", "")).startswith("custom:"):
        return [value]
    return [card for item in value.values() for card in _cards(item)]


def test_printer_card_recovery(tmp_path: Path) -> None:
    """A registry/network failure cannot permanently strand the printer cards."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI must install Node for the Home Assistant custom-card regression")
        pytest.skip("Install Node.js 22+ to run the Home Assistant custom-card regression")
    dashboard = yaml.safe_load((INTEGRATION / "dashboards/ui-3d-printers.yaml").read_text(encoding="utf-8"))
    configs = tmp_path / "cards.json"
    configs.write_text(json.dumps(_cards(dashboard)), encoding="utf-8")
    result = subprocess.run(
        [node, "--experimental-vm-modules", str(INTEGRATION / "tests/printer_cards.cjs"),
         str(INTEGRATION), str(configs)],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] == 15


def test_printer_bundle_is_reproducible() -> None:
    """Changes to pinned vendor inputs or source cards must rebuild the output."""
    vendor = json.loads((INTEGRATION / "vendor/webrtc-3.6.1/sources.json").read_text(encoding="utf-8"))
    integration = yaml.safe_load((INTEGRATION / "vendor.lock").read_text(encoding="utf-8"))["webrtc"]
    assert vendor["repository"] == integration["repo"]
    assert vendor["commit"] == integration["commit"]
    assert vendor["version"] == integration["version"].removeprefix("v")
    result = subprocess.run(
        [sys.executable, str(INTEGRATION / "scripts/build_printer_cards.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not os.environ.get("CI"), reason="Locale failure regression runs in CI")
def test_printer_builder_uses_utf8_under_ascii_locale() -> None:
    """A non-UTF-8 shell must not corrupt Cyrillic source or its content hashes."""
    environment = {
        **os.environ,
        "LC_ALL": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    result = subprocess.run(
        [sys.executable, str(INTEGRATION / "scripts/build_printer_cards.py"), "--check"],
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
