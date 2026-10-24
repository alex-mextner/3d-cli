"""Synthetic multi-colour print for the gcode-mix-gate tests.

Two 20 x 20 x 10 mm boxes side by side: `base` (x 0..20, pure slot 3 = T2) and `stripe` (x 20..40, mixed "12" =
T0, T1, T0, T1...). Every 0.1 mm layer prints the front outer wall (y = 0) in two runs: T2 up to x = 18 and the
pattern tool from x = 22 to x = 39, far enough from the shared face (x = 20) that every point is unambiguous.
`pocket_gcode` is a second, taller print for the cavity / pause gates (0.1 mm layers around a pocket)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LAYERS = 100
LAYER_H = 0.1
STRIPE_MIX = "12"
FOOTER = (
    "; CONFIG_BLOCK_START\n; wall_loops = 4\n; filament_density = 1.24\n"
    "; filament used [g] = {used}\n; estimated printing time (normal mode) = 1h 2m 3s\n; CONFIG_BLOCK_END\n"
)


def write_parts(root: Path) -> Path:
    import trimesh

    parts = root / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    for name, x0 in (("1_base.stl", 0.0), ("2_stripe.stl", 20.0)):
        box = trimesh.creation.box(extents=(20, 20, 10))
        box.apply_translation((x0 + 10, 10, 5))
        box.export(parts / name)
    return parts


def base_config(**extra: object) -> dict[str, object]:
    cfg: dict[str, object] = {
        "parts": [
            {"name": "base", "assign": {"slot": 3}},
            {"name": "stripe", "assign": {"mix": STRIPE_MIX, "mix_src": "AB"}},
        ],
        "heads_slots": [1, 2, 3],
    }
    cfg.update(extra)
    return cfg


def write_config(path: Path, **extra: object) -> Path:
    path.write_text(json.dumps(base_config(**extra)), encoding="utf-8")
    return path


def stripe_gcode(
    *,
    shift: int = 0,
    wrong_tool: dict[int, int] | None = None,
    short_wall: dict[int, float] | None = None,
    pause_at: int | None = None,
    pause_after_extrusion: bool = False,
    used: str = "1.0, 1.0, 1.0",
    tower: bool = True,
    pattern: str = STRIPE_MIX,
) -> str:
    """`shift` rotates the printed pattern; `wrong_tool[k]` prints layer k's stripe with that tool;
    `short_wall[k]` = length (mm) of the stripe's outer wall in layer k (default 17)."""
    exp = [int(c) - 1 for c in pattern]
    out = ["G90\nM83\n"]
    for k in range(LAYERS):
        z = (k + 1) * LAYER_H
        tool = (wrong_tool or {}).get(k, exp[(k + shift) % len(exp)])
        end_x = 39.0
        start_x = 22.0
        if short_wall and k in short_wall:
            start_x = end_x - short_wall[k]
        out.append(f";LAYER_CHANGE\n;Z:{z:.3f}\n;HEIGHT:{LAYER_H}\n")
        if pause_at == k and not pause_after_extrusion:
            out.append("M600\n")
        out.append(
            f";TYPE:Outer wall\nT2\nG1 X1 Y0 F9000\nG1 X18 Y0 E1\nT{tool}\nG1 X{start_x:g} Y0\nG1 X{end_x:g} Y0 E1\n"
        )
        if pause_at == k and pause_after_extrusion:
            out.append("M600\n")
        if tower:
            out.append(";TYPE:Prime tower\nT0\nG1 X100 Y100\nG1 X110 Y100 E1\n")
    out.append(FOOTER.format(used=used))
    return "".join(out)


def layer_top(k: int) -> float:
    return round((k + 1) * LAYER_H, 3)


# ---- pocket print: 0.1 mm layers, a 20 x 10 mm pocket (object coordinates), shifted onto the bed ----
POCKET: dict[str, Any] = {
    "plan_mm": {"x": [-10.0, 10.0], "y": [-5.0, 5.0], "corner_r": 1.0},
    "floor_z": 0.5,
    "pause_z": 12.0,
    "roof_ridge_z": 14.0,
}
POCKET_SHIFT = (50.0, 50.0, 0.0)
POCKET_LAYERS = 160
POCKET_H = 0.1


def pocket_gcode(
    *, fill_at: int | None = None, pause: bool = True, close_roof: bool = True
) -> str:
    """Wall ring around the pocket on every layer; from the pause layer the roof leaves the central strip open until the
    ridge, above the ridge a line closes it. `fill_at` = a layer that extrudes inside the pocket (a failed void)."""
    cx, cy = 50.0, 50.0
    pause_layer = int(
        round(POCKET["pause_z"] / POCKET_H)
    )  # first layer whose top is above the pause z
    out = ["G90\nM83\nT0\n"]
    for k in range(POCKET_LAYERS):
        z = (k + 1) * POCKET_H
        out.append(f";LAYER_CHANGE\n;Z:{z:.3f}\n;HEIGHT:{POCKET_H}\n")
        if pause and k == pause_layer:
            out.append("M600\n")
        out.append(f";TYPE:Outer wall\nG1 X{cx - 12} Y{cy - 8} F9000\n")
        out.append(
            f"G1 X{cx + 12} Y{cy - 8} E1\nG1 X{cx + 12} Y{cy + 8} E1\nG1 X{cx - 12} Y{cy + 8} E1\nG1 X{cx - 12} Y{cy - 8} E1\n"
        )
        if fill_at == k:
            out.append(f"G1 X{cx - 3} Y{cy}\nG1 X{cx + 3} Y{cy} E1\n")
        if close_roof and z - POCKET["roof_ridge_z"] > -1e-9:
            out.append(
                f";TYPE:Internal solid infill\nG1 X{cx - 3} Y{cy}\nG1 X{cx + 3} Y{cy} E1\n"
            )
    out.append(FOOTER.format(used="1.0"))
    return "".join(out)

