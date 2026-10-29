"""`3d section` / `3d render --section` through bin/3d: violet hatched cut faces, per-part colours (#586)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from dimension_fixtures import BLUE, GREEN, RED, Box, write_3mf
from PIL import Image

from .workflow_helper import run_cli

VIOLET = (0xB4, 0x8C, 0xDC)
THREE_BOXES = [Box("lid", 0, RED), Box("base", 20, GREEN), Box("clip", 40, BLUE)]


def _violet(path: Path) -> int:
    arr = np.asarray(Image.open(path).convert("RGB")).astype(int)
    return int((np.abs(arr - np.array(VIOLET)).max(axis=2) <= 6).sum())


def test_3d_section_cuts_a_coloured_3mf_with_a_legend(tmp_path: Path) -> None:
    """A user cuts a coloured 3MF assembly with `3d section` and gets a PNG with violet cut faces and a legend."""
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    out = tmp_path / "sec.png"
    done = run_cli(
        tmp_path,
        "section",
        str(model),
        "-o",
        str(out),
        "--plane",
        "XZ",
        "--at",
        "5",
        "--size",
        "900x700",
    )
    assert done.returncode == 0, done.stderr
    assert "3 part(s)" in done.stdout
    assert Image.open(out).size == (900, 700)
    assert _violet(out) > 2000


def test_render_section_alias_and_keep_pos_give_the_same_kind_of_picture(
    tmp_path: Path,
) -> None:
    """A user asks `3d render --section --keep pos` and gets the same kind of violet cut picture as `3d section`."""
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    out = tmp_path / "sec.png"
    done = run_cli(
        tmp_path,
        "render",
        str(model),
        "--section",
        "--keep",
        "pos",
        "--plane",
        "YZ",
        "--at",
        "5",
        "-o",
        str(out),
    )
    assert done.returncode == 0, done.stderr
    assert _violet(out) > 1000


def test_the_old_color_flag_is_still_accepted(tmp_path: Path) -> None:
    """A user's old `3d render --section --color` command keeps working and still writes the picture."""
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    done = run_cli(
        tmp_path,
        "render",
        str(model),
        "--section",
        "--color",
        "--plane",
        "XZ",
        "--at",
        "5",
        "-o",
        str(tmp_path / "s.png"),
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "s.png").stat().st_size > 0


def test_removed_module_flag_and_misplaced_section_flags_are_usage_errors(
    tmp_path: Path,
) -> None:
    """A user passing the removed `--module` or a section flag without `--section` gets a clear usage error."""
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    gone = run_cli(
        tmp_path,
        "render",
        str(model),
        "--section",
        "--module",
        "x();",
        "-o",
        str(tmp_path / "s.png"),
    )
    assert gone.returncode == 2
    assert "--module" in gone.stderr
    assert "Traceback" not in gone.stderr
    stray = run_cli(
        tmp_path, "render", str(model), "--plane", "XZ", "-o", str(tmp_path / "s.png")
    )
    assert stray.returncode == 2 and "belongs to --section" in stray.stderr


def test_plane_outside_the_model_exits_2_with_the_range(tmp_path: Path) -> None:
    """A user cutting outside the model gets exit 2 naming the valid range, not a traceback."""
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    done = run_cli(
        tmp_path,
        "section",
        str(model),
        "-o",
        str(tmp_path / "s.png"),
        "--plane",
        "XZ",
        "--at",
        "500",
    )
    assert done.returncode == 2
    assert "misses the model" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.skipif(shutil.which("openscad") is None, reason="openscad not installed")
def test_scad_assembly_section_keeps_color_calls(tmp_path: Path) -> None:
    """A user sections a coloured OpenSCAD assembly and the `color()` calls become the part colours."""
    scad = tmp_path / "asm.scad"
    scad.write_text(
        'color("red") difference() { cube(20); translate([5, 5, 5]) cube(10); }\n'
        'color("#2222CC") translate([30, 0, 0]) cube(20);\n'
    )
    out = tmp_path / "sec.png"
    done = run_cli(
        tmp_path, "section", str(scad), "-o", str(out), "--plane", "XZ", "--at", "10"
    )
    assert done.returncode == 0, done.stderr
    parts = int(re.search(r"(\d+) part\(s\)", done.stdout).group(1))  # type: ignore[union-attr]
    assert parts >= 2  # the red cube and the blue one; an OpenSCAD build may split the hollow cube further
    assert _violet(out) > 2000
