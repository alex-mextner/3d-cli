"""A minimal Snapmaker Orca U1 base project and the slots of the mix tester e2e tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

SLOTS = "1=#000000,3=#1B5E20,4=#FFFFFF"


def write_base_project(path: Path) -> None:
    cfg = {
        "filament_colour": ["#FFFFFF"] * 4,
        "layer_height": "0.12",
        "enable_prime_tower": "1",
        "prime_tower_width": "35",
        "different_settings_to_system": [""] * 6,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr(
            "3D/3dmodel.model",
            '<model><metadata name="Application">Snapmaker_Orca-01.10.01.50</metadata></model>',
        )
