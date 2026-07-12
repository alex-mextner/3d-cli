"""End-to-end `3d generate` workflows driven through the real `bin/3d`.

The AI backend is the deterministic MockBackend ($THREED_AI_MOCK_RESPONSE + --backend
mock) — NEVER a real model. Gate-dependent assertions require a working OpenSCAD and skip
otherwise, mirroring tests/test_cli_smoke.py; the tool-independent assertions (a .scad is
written, the dims-present check is real, an explicit status label is emitted) run always.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .workflow_helper import require_working_openscad, run_cli

CUBE_SCAD = """
width = 20;
depth = 20;
height = 16;
wall = 2;
module hollow_box(w, d, h, t) {
    difference() {
        cube([w, d, h], center = true);
        translate([0, 0, t]) cube([w - 2 * t, d - 2 * t, h], center = true);
    }
}
hollow_box(width, depth, height, wall);
""".strip() + "\n"


def _generate_json(
    tmp_path: Path, *argv: str, mock: str = CUBE_SCAD
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    result = run_cli(
        tmp_path, "generate", *argv, "--backend", "mock", "--json",
        env_extra={"THREED_AI_MOCK_RESPONSE": mock},
    )
    assert result.stdout, result.stderr
    return json.loads(result.stdout), result


def test_generate_writes_scad_and_emits_status_label(tmp_path: Path) -> None:
    """Runs with or without OpenSCAD: the .scad is written, every requested dim is really
    present, and one of the three explicit proof labels is emitted."""
    out = tmp_path / "box.scad"
    payload, result = _generate_json(
        tmp_path, "a hollow box",
        "--dim", "width=20", "--dim", "depth=20", "--dim", "height=16", "--dim", "wall=2",
        "-o", str(out),
    )
    assert out.exists()
    assert payload["status"] in {"ok", "diagnostic", "failure"}
    assert payload["scad_path"].endswith("box.scad")
    assert payload["dims_present_in_scad"] == {
        "width": True, "depth": True, "height": True, "wall": True,
    }
    assert payload["requested_dims"]["wall"] == "2"


def test_generate_ok_when_model_renders_and_passes_gates(tmp_path: Path) -> None:
    """With OpenSCAD present, the known-good cube validates + renders + passes the gates."""
    require_working_openscad()
    payload, result = _generate_json(
        tmp_path, "a hollow box",
        "--dim", "width=20", "--dim", "depth=20", "--dim", "height=16", "--dim", "wall=2",
        "-o", str(tmp_path / "box.scad"),
    )
    assert result.returncode == 0, result.stderr
    assert payload["status"] in {"ok", "diagnostic"}
    gate_names = {g["name"] for g in payload["gate_results"]}
    assert "manifold" in gate_names
    manifold = next(g for g in payload["gate_results"] if g["name"] == "manifold")
    assert manifold["status"] == "pass"


def test_generate_failure_when_output_never_renders(tmp_path: Path) -> None:
    """A model that emits non-OpenSCAD gets label=failure and a nonzero exit."""
    require_working_openscad()
    payload, result = _generate_json(
        tmp_path, "junk", "--dim", "width=20", "--rounds", "2",
        "-o", str(tmp_path / "bad.scad"),
        mock="this is not openscad at all {{{",
    )
    assert result.returncode == 1
    assert payload["status"] == "failure"
    assert payload["rounds"] == 2  # exhausted the budget


def test_generate_flags_a_missing_requested_dimension(tmp_path: Path) -> None:
    """A requested dim absent from the model output is reported (never silently patched)."""
    payload, _ = _generate_json(
        tmp_path, "a box", "--dim", "width=20", "--dim", "radius=5",
        "-o", str(tmp_path / "box.scad"),
    )
    assert payload["status"] == "diagnostic"
    assert payload["dims_present_in_scad"] == {"width": True, "radius": False}


def test_generate_reads_dims_from_a_spec_file(tmp_path: Path) -> None:
    """--spec supplies the description and dims; --dim flags still override."""
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"description": "a hollow box", "dims": {"width": 20, "depth": 20, "height": 16, "wall": 2}}),
        encoding="utf-8",
    )
    payload, _ = _generate_json(
        tmp_path, "--spec", str(spec), "-o", str(tmp_path / "box.scad"),
    )
    assert payload["requested_dims"] == {"width": "20", "depth": "20", "height": "16", "wall": "2"}
    assert payload["dims_present_in_scad"] == {
        "width": True, "depth": True, "height": True, "wall": True,
    }


def test_generate_help_lists_flags_and_labels(tmp_path: Path) -> None:
    """A user reads `3d generate --help` and sees every flag plus the three proof labels."""
    result = run_cli(tmp_path, "generate", "--help")
    assert result.returncode == 0
    for token in ("--dim", "--spec", "--backend", "--rounds", "ok", "diagnostic", "failure"):
        assert token in result.stdout
