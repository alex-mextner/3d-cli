"""A minimal Snapmaker Orca U1 base project and the slots of the mix tester e2e tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from slicing.mix_model import Slot, pick, rank
from slicing.mix_pattern import MixRules
from slicing.mix_tester import LoadedSlot, build, read_base

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


LOADED = [
    LoadedSlot(1, "#000000", "black", 11, 21),
    LoadedSlot(3, "#1B5E20", "green", 13, 23),
    LoadedSlot(4, "#FFFFFF", "white", 14, 24),
]


def build_tester(
    out_dir: Path,
    target: str = "#808080",
    count: int = 4,
    height: float = 6.0,
    filament_ids: tuple[int, int, int] = (21, 23, 24),
    measured: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Write a real tester (project, manifest, sheet, card) into `out_dir`; return its manifest.
    `filament_ids` are the Spoolman filament ids of the black, green and white spools; `measured`
    ({head: hex}) builds the tester from measured colours (Spoolman's kept as `nominal_hex`)."""
    loaded = [
        LoadedSlot(
            s.slot,
            (measured or {}).get(s.slot, s.hex),
            s.name,
            s.spool_id,
            fid,
            s.hex if s.slot in (measured or {}) else None,
        )
        for s, fid in zip(LOADED, filament_ids, strict=True)
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / "base.3mf"
    write_base_project(base)
    settings, app = read_base(base)
    ranked = rank(
        target,
        [Slot(s.slot, s.hex, s.name) for s in loaded],
        max_cycle=3,
        layer_h=0.12,
        rules=MixRules(0.12),
    )
    mixes = pick([c for c in ranked if c.cycle > 1], count)
    manifest = build(
        target=target,
        slots=loaded,
        candidates=mixes,
        out=out_dir / "tester",
        layer=0.12,
        height=height,
        base_settings=settings,
        app=app,
        source="base.3mf",
    )
    assert json.loads((out_dir / "tester.mix.json").read_text()) == manifest
    return manifest
