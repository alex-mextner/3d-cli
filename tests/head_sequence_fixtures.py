"""Synthetic project 3MF + G-code builders for the head-sequence check tests."""
from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

# 4 physical filaments; virtual 5 = manual pattern "14"; virtual 6 = ratio (Local-Z) row.
# Row format as Snapmaker Orca writes it: a manual-pattern row ends `cm1,<pattern>`, a
# ratio row ends `cm0` (no pattern field).
MIXED_ROWS = (
    "1,4,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,14;"
    "2,3,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u702,cm0"
)


def project_3mf(path: Path, objects: dict[str, int | list[int]], *, raft_layers: int = 0,
                mixed: str = MIXED_ROWS) -> Path:
    """objects: name -> extruder of its one part, or a list (one part per extruder)."""
    cfg = {
        "filament_colour": ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"],
        "mixed_filament_definitions": mixed,
        "raft_layers": str(raft_layers),
    }
    objs = "".join(
        f'<object id="{i}"><metadata key="name" value="{name}"/>'
        + "".join(
            f'<part id="{j}"><metadata key="extruder" value="{e}"/></part>'
            for j, e in enumerate([ex] if isinstance(ex, int) else ex, start=1)
        )
        + "</object>"
        for i, (name, ex) in enumerate(objects.items(), start=1)
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr("Metadata/model_settings.config", f"<config>{objs}</config>")
    return path


def gcode(
    path: Path,
    layers: Sequence[Mapping[str, int | list[int]]],
    *,
    extra: str = "",
    heights: list[float] | None = None,
) -> Path:
    """layers[i] = {object name: 1-based head that object prints on layer i} (or a list of heads
    printed one after the other). `heights` = the thickness of every layer: each layer then
    announces its Z (`;Z:`) as Orca does, the first layer's Z being its own thickness."""
    out = ["; synthetic", "T1000", "G1 X0 Y0 E1 ; purge outside any object"]
    z = 0.0
    for i, layer in enumerate(layers):
        out.append(";LAYER_CHANGE")
        if heights is not None:
            z += heights[i]
            out.append(f";Z:{z:.4g}")
            out.append(f";HEIGHT:{heights[i]:.4g}")
        for name, head_or_heads in layer.items():
            heads = [head_or_heads] if isinstance(head_or_heads, int) else head_or_heads
            for head in heads:
                out.append(f"T{head - 1}")
                out.append(f"EXCLUDE_OBJECT_START NAME={name}_id_0_copy_0")
                out.append("G1 X1 Y1 F3000")
                if extra:
                    out.append(extra)
                out.append("G1 X10 Y10 E0.5")
                out.append("G1 E-0.8 ; retract")
                out.append(f"EXCLUDE_OBJECT_END NAME={name}_id_0_copy_0")
    path.write_text("\n".join(o for o in out if o) + "\n")
    return path
