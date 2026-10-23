"""`3d photo-scale` through bin/3d: every scale route (reference object, two points, honeycomb grid, another photo)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")
pytest.importorskip("PIL")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import vision_calib_fixtures as fx  # noqa: E402

from .workflow_helper import run_cli  # noqa: E402

LIGHT_ENV = {"THREED_VISION_PYTHON": sys.executable}


def _json(tmp_path: Path, *argv: str) -> dict:
    r = run_cli(tmp_path, "photo-scale", *argv, "--json", env_extra=LIGHT_ENV)
    assert r.returncode == 0, r.stderr
    doc: dict = json.loads(r.stdout)
    return doc


def test_photo_scale_two_points(tmp_path: Path) -> None:
    """The user clicks both ends of a 300 mm ruler: 860 px -> mm/px, depth = f * mm/px."""
    photo = tmp_path / "carpet.png"
    fx.write_png(str(photo), fx.textured((2000, 1500)))
    doc = _json(
        tmp_path,
        str(photo),
        "--points",
        "100,300,960,300",
        "--distance",
        "300",
        "--f-px",
        "937.5",
    )
    assert doc["mm_per_px"] == pytest.approx(300 / 860)
    assert doc["plane_depth_mm"] == pytest.approx(937.5 * 300 / 860)
    assert (
        doc["source"] == "points" and doc["f_px"] == 937.5 and doc["K"][0][2] == 1000.0
    )
    assert "fronto_parallel_assumed" in doc["quality"]
    default_f = _json(
        tmp_path, str(photo), "--points", "100,300,960,300", "--distance", "300"
    )
    assert (
        default_f["f_px"] == pytest.approx(0.375 * 2500)
        and default_f["f_source"] == "default"
    )


def test_photo_scale_reference_object_by_instance_id_and_by_mask_file(
    tmp_path: Path,
) -> None:
    """A segment run wrote instances.json; the user says 'instance 7 is 240 mm long'."""
    photo = tmp_path / "carpet.png"
    fx.write_png(str(photo), fx.textured((800, 600)))
    seg = tmp_path / "seg"
    (seg / "masks").mkdir(parents=True)
    bar = np.zeros((600, 800), np.uint8)
    box = cv2.boxPoints(((400.0, 300.0), (480.0, 30.0), 20.0)).astype(
        np.int32
    )  # a 480 x 30 px bar
    cv2.fillConvexPoly(bar, box, 255)
    fx.write_png(str(seg / "masks" / "007.png"), bar)
    (seg / "instances.json").write_text(
        json.dumps({"instances": [{"id": 7, "mask": "masks/007.png"}]}),
        encoding="utf-8",
    )
    via_id = _json(
        tmp_path,
        str(photo),
        "--ref",
        "7=240",
        "--instances",
        str(seg / "instances.json"),
    )
    assert (
        via_id["mm_per_px"] == pytest.approx(0.5, rel=0.01)
        and via_id["source"] == "ref"
    )
    via_file = _json(tmp_path, str(photo), "--ref", f"{seg / 'masks' / '007.png'}=240")
    assert via_file["mm_per_px"] == pytest.approx(via_id["mm_per_px"])
    missing = run_cli(
        tmp_path,
        "photo-scale",
        str(photo),
        "--ref",
        "9=240",
        "--instances",
        str(seg / "instances.json"),
        env_extra=LIGHT_ENV,
    )
    assert missing.returncode == 2 and "instance 9 not found" in missing.stderr
    no_file = run_cli(
        tmp_path, "photo-scale", str(photo), "--ref", "7=240", env_extra=LIGHT_ENV
    )
    assert no_file.returncode == 2 and "--instances" in no_file.stderr


def test_photo_scale_grid_detects_or_reuses_a_calibration(tmp_path: Path) -> None:
    """Chain: calibrate-grid once, then photo-scale reuses its calibration.json at any pixel."""
    photo = tmp_path / "wall.png"
    cam = fx.write_wall_photo(str(photo))
    truth_depth = cam["t"][2] + (
        cam["R"][2, :2] @ np.array([11 * fx.PITCH[0], 8.25 * fx.PITCH[1]])
    )  # plane depth at the panel centre
    detected = _json(tmp_path, str(photo), "--grid", "honeycomb")
    assert detected["source"] == "grid"
    assert detected["plane_depth_mm"] == pytest.approx(truth_depth, rel=0.06)
    assert detected["mm_per_px"] == pytest.approx(truth_depth / 900.0, rel=0.06)
    out = tmp_path / "calib"
    assert (
        run_cli(
            tmp_path, "calibrate-grid", str(photo), "-o", str(out), env_extra=LIGHT_ENV
        ).returncode
        == 0
    )
    reused = _json(
        tmp_path,
        str(photo),
        "--grid",
        "honeycomb",
        "--calibration",
        str(out / "calibration.json"),
    )
    assert reused["mm_per_px"] == pytest.approx(detected["mm_per_px"], rel=1e-6)
    left = _json(
        tmp_path,
        str(photo),
        "--grid",
        "honeycomb",
        "--calibration",
        str(out / "calibration.json"),
        "--at",
        "300,450",
    )
    assert left["mm_per_px"] != pytest.approx(
        reused["mm_per_px"], rel=1e-4
    )  # perspective: the scale varies over the photo
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    r = run_cli(
        tmp_path,
        "photo-scale",
        str(photo),
        "--grid",
        "honeycomb",
        "--calibration",
        str(bad),
        env_extra=LIGHT_ENV,
    )
    assert r.returncode == 2 and "photo_calibration" in r.stderr
    blank = tmp_path / "blank.png"
    fx.write_png(str(blank), np.full((300, 400, 3), 200, np.uint8))
    nogrid = run_cli(
        tmp_path, "photo-scale", str(blank), "--grid", "honeycomb", env_extra=LIGHT_ENV
    )
    assert nogrid.returncode == 3 and "no honeycomb grid found" in nogrid.stderr


def test_photo_scale_transfers_the_scale_of_another_photo_through_the_homography(
    tmp_path: Path,
) -> None:
    """B is A zoomed out 2x about the centre; B shows 2.0 mm/px, so one A pixel (= half a B pixel) is 1.0 mm."""
    a = fx.textured((800, 600))
    zoom = np.array(
        [[0.5, 0, 200.0], [0, 0.5, 150.0], [0, 0, 1.0]]
    )  # about the image centre (400, 300) -> (400, 300)
    b = cv2.warpPerspective(a, zoom, (800, 600), flags=cv2.INTER_AREA)
    fx.write_png(str(tmp_path / "a.png"), a)
    fx.write_png(str(tmp_path / "b.png"), b)
    doc = _json(
        tmp_path,
        str(tmp_path / "a.png"),
        "--homography-to",
        str(tmp_path / "b.png"),
        "--other-mm-per-px",
        "2.0",
    )
    assert doc["source"] == "homography"
    assert doc["mm_per_px"] == pytest.approx(1.0, rel=0.03)
    assert doc["reg"]["inliers"] > 50 and doc["reg"]["median_reproj_px"] < 1.0
    neither = run_cli(
        tmp_path,
        "photo-scale",
        str(tmp_path / "a.png"),
        "--homography-to",
        str(tmp_path / "b.png"),
        env_extra=LIGHT_ENV,
    )
    assert neither.returncode == 2 and "--other-mm-per-px" in neither.stderr


def test_photo_scale_other_grid_transfers_the_wall_scale_to_a_photo_of_the_same_plane(tmp_path: Path) -> None:
    """A is a close-up of the wall (no panel visible), B shows the honeycomb: B's grid gives the plane, the register
    homography carries it to A. B is A seen through a known homography, so the scale at A's centre is checkable."""
    wall = tmp_path / "wall.png"
    fx.write_wall_photo(str(wall))
    bgr = cv2.imread(str(wall))
    assert bgr is not None
    b = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    zoom = np.array([[1.4, 0, -240.0], [0, 1.4, -180.0], [0, 0, 1.0]])  # A = B zoomed 1.4x about the middle
    a = cv2.warpPerspective(b, zoom, (1200, 900), flags=cv2.INTER_CUBIC)
    fx.write_png(str(tmp_path / "a.png"), a)
    direct = _json(tmp_path, str(wall), "--grid", "honeycomb", "--at", "600,450")
    doc = _json(tmp_path, str(tmp_path / "a.png"), "--homography-to", str(wall), "--other-grid", "honeycomb")
    assert doc["source"] == "homography" and doc["reg"]["inliers"] > 20
    # A's centre pixel (600,450) shows B's pixel zoom^-1 (600,450)+...: 1.4x magnification -> 1/1.4 of B's mm/px there
    assert doc["mm_per_px"] == pytest.approx(direct["mm_per_px"] / 1.4, rel=0.08)
    blank = tmp_path / "blank.png"
    fx.write_png(str(blank), fx.textured((1200, 900)))
    nogrid = run_cli(tmp_path, "photo-scale", str(tmp_path / "a.png"), "--homography-to", str(blank), "--other-grid",
                     "honeycomb", env_extra=LIGHT_ENV)
    assert nogrid.returncode == 3


def test_photo_scale_argument_errors(tmp_path: Path) -> None:
    """Forgetting --distance or the source flag is reported by name, and --help / bare invocation print the usage."""
    photo = tmp_path / "p.png"
    fx.write_png(str(photo), fx.textured((200, 100)))
    no_distance = run_cli(
        tmp_path,
        "photo-scale",
        str(photo),
        "--points",
        "0,0,10,10",
        env_extra=LIGHT_ENV,
    )
    assert no_distance.returncode == 2 and "--distance" in no_distance.stderr
    assert (
        run_cli(tmp_path, "photo-scale", str(photo), env_extra=LIGHT_ENV).returncode
        == 2
    )  # a source is required
    help_run = run_cli(tmp_path, "photo-scale", "--help")
    assert help_run.returncode == 0
    assert "3d photo-scale" in help_run.stdout
    bare = run_cli(tmp_path, "photo-scale")
    assert bare.returncode == 1
    assert "--points" in bare.stdout
