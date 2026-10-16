"""The inspector tests import the HA scripts by module name, as the camera proxy does at runtime
(`python /config/scripts/u1_mjpeg_proxy.py` puts its own directory on sys.path)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
