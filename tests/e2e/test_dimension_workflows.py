"""`3d dimension` renders every part in its own colour with a legend, through bin/3d (#580)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dimension_fixtures import (
    BLUE,
    GREEN,
    RED,
    SCAD_COLOR_REASON,
    THREE_BOXES,
    openscad_exports_3mf_color,
    write_3mf,
    write_obj,
)

from .workflow_helper import run_cli


def _sidecar(image: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        Path(f"{image}.dims.json").read_text(encoding="utf-8")
    )
    return data


def _check(tmp_path: Path, image: Path) -> None:
    checked = run_cli(tmp_path, "dimension", "--check", str(image))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "PASS" in checked.stdout


def test_coloured_3mf_renders_each_part_in_its_colour_and_passes_the_gate(
    tmp_path: Path,
) -> None:
    """A user dimensions a coloured 3MF: each part keeps its colour, one is stood in, the gate passes."""
    model = write_3mf(tmp_path / "box.3mf", THREE_BOXES)
    out = tmp_path / "box.png"
    done = run_cli(
        tmp_path,
        "dimension",
        str(model),
        "-o",
        str(out),
        "--part",
        f"{BLUE}=PCB:stand_in",
    )
    assert done.returncode == 0, done.stderr
    assert "3 part(s)" in done.stdout
    data = _sidecar(out)
    parts = {p["name"]: p for p in data["parts"]}
    assert set(parts) == {"lid", "base", "PCB"}
    assert (
        parts["lid"]["color"] == RED and parts["base"]["color"] == GREEN
    )  # authored colours
    assert parts["PCB"]["muted"] and parts["PCB"]["role"] == "stand_in"
    assert len({p["color"] for p in data["parts"]}) == 3
    assert data["legend"]["rows"] >= 1
    _check(tmp_path, out)


def test_obj_with_mtl_renders_coloured_parts(tmp_path: Path) -> None:
    """A user dimensions an OBJ with an MTL: every object becomes a part in its material colour."""
    model = write_obj(tmp_path / "box.obj", THREE_BOXES)
    out = tmp_path / "obj.png"
    done = run_cli(tmp_path, "dimension", str(model), "-o", str(out))
    assert done.returncode == 0, done.stderr
    assert {p["name"]: p["authored_color"] for p in _sidecar(out)["parts"]} == {
        "lid": RED,
        "base": GREEN,
        "clip": BLUE,
    }
    _check(tmp_path, out)


def test_obj_whose_mtl_is_missing_warns_and_still_colours_every_part(
    tmp_path: Path,
) -> None:
    """A user dimensions an OBJ whose MTL is gone: a warning, and the parts still get distinct colours."""
    model = write_obj(tmp_path / "box.obj", THREE_BOXES)
    model.with_suffix(".mtl").unlink()
    out = tmp_path / "nomtl.png"
    done = run_cli(tmp_path, "dimension", str(model), "-o", str(out))
    assert done.returncode == 0, done.stderr
    assert "warning" in done.stderr and "box.mtl not found" in done.stderr
    assert len({p["color"] for p in _sidecar(out)["parts"]}) == 3


@pytest.mark.skipif(not openscad_exports_3mf_color(), reason=SCAD_COLOR_REASON)
def test_scad_assembly_keeps_its_color_calls_and_takes_define(tmp_path: Path) -> None:
    """A user dimensions a .scad assembly: color() calls become parts and -D reaches openscad."""
    scad = tmp_path / "asm.scad"
    scad.write_text(
        'show = false;\ncolor("red") cube(10);\ncolor("#2222CC") translate([20, 0, 0]) cube(10);\n'
        'if (show) color("#22AA22") translate([40, 0, 0]) cube(10);\n'
    )
    plain = tmp_path / "plain.png"
    done = run_cli(tmp_path, "dimension", str(scad), "-o", str(plain))
    assert done.returncode == 0, done.stderr
    assert {p["name"] for p in _sidecar(plain)["parts"]} == {"#FF0000", "#2222CC"}

    shown = tmp_path / "shown.png"
    done = run_cli(
        tmp_path, "dimension", str(scad), "-Dshow=true", "-o", str(shown),
        "--part", "#FF0000=lid", "--part", "#2222cc=base:vitamin",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    names = {p["name"]: p for p in _sidecar(shown)["parts"]}
    assert set(names) == {"lid", "base", "#22AA22"}
    assert names["base"]["role"] == "vitamin"
    _check(tmp_path, shown)


def test_define_on_a_non_scad_model_is_a_usage_error(tmp_path: Path) -> None:
    """A user passes -D with a 3MF: exit 2 naming .scad, no traceback."""
    model = write_3mf(tmp_path / "box.3mf", THREE_BOXES)
    done = run_cli(
        tmp_path, "dimension", str(model), "-o", str(tmp_path / "x.png"), "-D", "a=1"
    )
    assert done.returncode == 2
    assert ".scad" in done.stderr
    assert "Traceback" not in done.stderr


def test_part_selector_matching_nothing_exits_2_and_names_the_parts(
    tmp_path: Path,
) -> None:
    """A user mistypes a --part selector: exit 2, and the message lists the real parts."""
    model = write_3mf(tmp_path / "box.3mf", THREE_BOXES)
    done = run_cli(
        tmp_path,
        "dimension",
        str(model),
        "-o",
        str(tmp_path / "x.png"),
        "--part",
        "nope=x",
    )
    assert done.returncode == 2
    assert "matches no part" in done.stderr and "lid" in done.stderr


@pytest.mark.parametrize("spec", ["nonsense", "lid=x:nonrole", "#12=x"])
def test_malformed_part_spec_is_a_structured_usage_error(
    tmp_path: Path, spec: str
) -> None:
    """A user writes a malformed --part spec: exit 2 naming --part, no traceback."""
    model = write_3mf(tmp_path / "box.3mf", THREE_BOXES)
    done = run_cli(
        tmp_path, "dimension", str(model), "-o", str(tmp_path / "x.png"), "--part", spec
    )
    assert done.returncode == 2
    assert "--part" in done.stderr
    assert "Traceback" not in done.stderr


def test_help_documents_parts_colours_and_the_new_flags(tmp_path: Path) -> None:
    """A user reads `3d dimension --help` and docs/commands/dimension.md: both document the colour flags."""
    done = run_cli(tmp_path, "dimension", "--help")
    assert done.returncode == 0
    for needle in ("--part", "-D NAME=VALUE", "legend", "stand_in", "OWN colour"):
        assert needle in done.stdout, needle
    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "commands" / "dimension.md"
    ).read_text(encoding="utf-8")
    for needle in ("--part", "-D NAME=VALUE", "legend", "stand_in"):
        assert needle in doc, needle
