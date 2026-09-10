"""Real-CLI e2e stories for `3d prep`: repair + auto-orient + print-ready export."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .workflow_helper import (
    CUBE,
    require_cli_python_deps,
    require_working_openscad,
    run_cli,
)

_DEPS = (
    ["trimesh", "numpy", "networkx", "scipy", "rtree", "manifold3d"],
    ["trimesh", "numpy", "networkx", "scipy", "rtree", "manifold3d"],
)

_LSHAPE = (
    "// asymmetric L-bracket with thick arms (not square in XY)\n"
    "union() {\n"
    "  cube([40, 12, 12]);\n"
    "  cube([12, 40, 12]);\n"
    "}\n"
)


def test_prep_cube_scad_writes_ready_stl(tmp_path: Path) -> None:
    """A user preps the sample .scad: it exports, repairs, orients, writes the default
    <input>.prep.stl, prints a READY verdict, and exits 0."""
    require_working_openscad()
    require_cli_python_deps(tmp_path, *_DEPS)
    model = tmp_path / "cube.scad"
    shutil.copyfile(CUBE, model)

    result = run_cli(tmp_path, "prep", str(model), timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ">>> PREP: READY" in result.stdout

    out = tmp_path / "cube.prep.stl"
    assert out.exists() and out.stat().st_size > 0


def test_prep_asymmetric_orients_flat_and_writes_3mf_and_json(tmp_path: Path) -> None:
    """An asymmetric L-bracket is auto-oriented to its flat (lowest build height) rest, and
    both the STL and the print-ready 3MF are written; --json reports a READY summary."""
    require_working_openscad()
    require_cli_python_deps(tmp_path, *_DEPS)
    model = tmp_path / "lshape.scad"
    model.write_text(_LSHAPE, encoding="utf-8")
    out = tmp_path / "l.prep.stl"

    result = run_cli(tmp_path, "prep", str(model), "-o", str(out), "--3mf", "--json", timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr

    assert out.exists() and out.stat().st_size > 0
    assert (tmp_path / "l.prep.3mf").exists()

    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["repair"]["watertight_after"] is True
    # The L-bracket's thinnest extent is 12 mm; a flat rest minimizes build height to it.
    assert payload["orientation"]["build_height_mm"] == 12.0
    assert payload["orientation"]["overhang_frac_after"] < 0.05
    assert payload["outputs"]["3mf"].endswith("l.prep.3mf")
