"""Fixtures for the slot colour tolerance and `--fix` tests: a G-code, its project 3MF, manifest and
slice-warnings sidecar for the SIMBOL case (beige PETG sliced as #E8D8B0, the U1 reporting #FEE5A5),
and a fake U1 that holds that PETG in head T4 (G-code tool 3). Never a real printer."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from printer_fakes import FakeMoonraker

SLICED = "#E8D8B0"  # what the slicer wrote
LOADED = "#FEE5A5"  # what the U1 screen shows for the same beige PETG
COLOURS = ["#000000", "#808080", "#1B5E20", SLICED]
TOOLPATH = "G28\nT3\nG1 X10 Y10 E1\nG1 X20 Y20 E1\n"


def gcode_text(colour: str = SLICED, material: str = "PETG") -> str:
    colours = ";".join([*COLOURS[:3], colour])
    return (
        "; HEADER_BLOCK_START\n; HEADER_BLOCK_END\n"
        + TOOLPATH
        + "; filament used [mm] = 0.00, 0.00, 0.00, 30.50\n"
        + f"; extruder_colour = {colours}\n"
        + f"; filament_colour = {colours}\n"
        + f"; filament_type = PLA;PLA;PLA;{material}\n"
    )


def write_project(path: Path, colour: str = SLICED) -> None:
    """A slicer project 3MF: project_settings.config (4-space JSON), plate json, one model part."""
    colours = [*COLOURS[:3], colour]
    settings = {
        "filament_colour": colours,
        "extruder_colour": list(colours),
        "filament_type": ["PLA", "PLA", "PLA", "PETG"],
        "layer_height": "0.2",
    }
    plate = {"filament_colors": list(colours), "filament_ids": [0, 1, 2, 3]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model>kept byte for byte</model>")
        z.writestr(
            "Metadata/project_settings.config", json.dumps(settings, indent=4) + "\n"
        )
        z.writestr("Metadata/plate_1.json", json.dumps(plate, indent=4))


def write_manifest(path: Path, colour: str = SLICED) -> None:
    filaments = [
        {"slot": i + 1, "key": f"f{i}", "name": f"F{i}", "hex": c, "tool": i}
        for i, c in enumerate([*COLOURS[:3], colour])
    ]
    path.write_text(
        json.dumps({"file": "plate.3mf", "filaments": filaments}, indent=1),
        encoding="utf-8",
    )


def write_plate(directory: Path, name: str = "plate") -> Path:
    """NAME.gcode + NAME.3mf + NAME.manifest.json + NAME.slice-warnings.json; the G-code's path."""
    from slicing import slice_warnings as sw

    gcode = directory / f"{name}.gcode"
    gcode.write_text(gcode_text(), encoding="utf-8")
    write_project(directory / f"{name}.3mf")
    write_manifest(directory / f"{name}.manifest.json")
    sw.record(gcode, logs=[("slice.log", "")])
    return gcode


def load_beige_petg(
    moon: FakeMoonraker, head: int = 3, colour: str = LOADED, material: str = "PETG"
) -> None:
    """The fake U1 holds `material` of `colour` in head index `head` (label T<head+1>)."""
    cfg = moon.status["print_task_config"]
    cfg["filament_exist"][head] = True
    cfg["filament_color_rgba"][head] = colour.lstrip("#") + "FF"
    cfg["filament_type"][head] = material
