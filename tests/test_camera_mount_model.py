"""The C920 top-down camera mount model (models/camera_mount, issue #338).

Every printable part must export from the one SCAD source to a closed, single-body mesh that
fits the K1 SE bed (the mount is printed there), the design asserts must hold for both
printers, and the STLs committed next to the source must still be what the source builds —
a changed parameter with a stale STL would print the old part.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

MODEL = Path(__file__).resolve().parents[1] / "models" / "camera_mount"
SCAD = MODEL / "camera_mount.scad"
PARTS = [
    "hub", "beam_a", "beam_b", "foot", "shoe_k1", "shoe_u1", "post", "arm", "cam_plate",
    "wedge", "hanger", "pin_long", "pin_short", "pi_tray", "cable_clip", "led_ring", "dowel",
]
PLATES = ["plate_hub", "plate_beam", "plate_small", "plate_extras", "plate_led"]
# assembly-frame pairs that fits.yaml (`3d fit-check --spec`) reads
FITS = ["fit_foot", "fit_shoe_k1", "fit_shoe_u1", "fit_hub", "fit_post", "fit_arm"]
K1SE_BED_MM = 220.0
OPENSCAD = shutil.which("openscad")

needs_openscad = pytest.mark.skipif(OPENSCAD is None, reason="OpenSCAD not installed")


def _export(part: str, out: Path, *defines: str) -> Path:
    assert OPENSCAD is not None
    args = [OPENSCAD, "-o", str(out), "-D", f'part="{part}"']
    for d in defines:
        args += ["-D", d]
    proc = subprocess.run(
        [*args, str(SCAD)], capture_output=True, text=True, timeout=600, check=False
    )
    # an .echo export writes the echoes and failed asserts into the file, not to stderr
    log = proc.stderr + (
        out.read_text() if out.suffix == ".echo" and out.exists() else ""
    )
    assert proc.returncode == 0, log[-2000:]
    assert "Assertion" not in log and "ERROR" not in log, log[-2000:]
    return out


@needs_openscad
@pytest.mark.parametrize("part", PARTS)
def test_part_exports_to_one_closed_body_on_the_k1se_bed(
    part: str, tmp_path: Path
) -> None:
    mesh = trimesh.load(_export(part, tmp_path / f"{part}.stl"))
    assert mesh.is_watertight
    assert len(mesh.split(only_watertight=False)) == 1
    lo, hi = mesh.bounds
    assert lo[2] == pytest.approx(0.0, abs=1e-3), "part must sit on the bed"
    assert max(hi[0] - lo[0], hi[1] - lo[1]) <= K1SE_BED_MM - 10


@needs_openscad
@pytest.mark.parametrize("printer", ["k1se", "u1"])
def test_design_asserts_hold_on_both_printers(printer: str, tmp_path: Path) -> None:
    out = _export("assembly", tmp_path / "assembly.echo", f'printer="{printer}"')
    assert "lens above printer top" in out.read_text()


def _echo_log(tmp_path: Path, *defines: str) -> str:
    """Evaluate the assembly with `defines` and return everything OpenSCAD said."""
    assert OPENSCAD is not None
    echo = tmp_path / "a.echo"
    args = [OPENSCAD, "-o", str(echo), "-D", 'part="assembly"']
    for d in defines:
        args += ["-D", d]
    proc = subprocess.run(
        [*args, str(SCAD)], capture_output=True, text=True, timeout=600, check=False
    )
    return proc.stderr + (echo.read_text() if echo.exists() else "")


@needs_openscad
@pytest.mark.parametrize(
    ("define", "message"),
    [
        ("pin_hole=9", "pin_hole 9 is out of range"),
        ("pin_hole=1", "pin_hole 1 is out of range"),
        ("pin_hole=2.5", "pin_hole 2.5 is out of range"),
        ('printer="k1"', 'printer "k1" is not k1se or u1'),
    ],
)
def test_bad_view_parameters_are_rejected(
    define: str, message: str, tmp_path: Path
) -> None:
    assert message in _echo_log(tmp_path, define)


@pytest.mark.parametrize("name", [*PARTS, *(f"plates/{p}" for p in PLATES)])
def test_committed_stl_fits_the_k1se_bed(name: str) -> None:
    mesh = trimesh.load(MODEL / "stl" / f"{name}.stl")
    lo, hi = mesh.bounds
    assert mesh.is_watertight
    assert max(hi[0] - lo[0], hi[1] - lo[1]) <= K1SE_BED_MM - 10


@needs_openscad
@pytest.mark.parametrize(
    "name", [*PARTS, *(f"plates/{p}" for p in PLATES), *(f"fit/{f}" for f in FITS)]
)
def test_committed_stl_is_what_the_source_builds(name: str, tmp_path: Path) -> None:
    part = name.rsplit("/", 1)[-1]
    fresh = trimesh.load(_export(part, tmp_path / f"{part}.stl"))
    committed = trimesh.load(MODEL / "stl" / f"{name}.stl")
    assert committed.volume == pytest.approx(fresh.volume, rel=0.01), (
        "rebuild: python3 models/camera_mount/build.py --stl"
    )
    assert committed.bounds == pytest.approx(fresh.bounds, abs=0.05)
