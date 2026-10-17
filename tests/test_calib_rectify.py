"""calib_rectify: ArUco homography and the external scale reference on synthetic photos with
known geometry (the expected values are never passed to the code under test)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "ArucoDetector"):
    pytest.skip("OpenCV >= 4.7 with aruco needed", allow_module_level=True)

import calib_rectify as cr  # noqa: E402
from calib_scene import BED_TO_IMAGE, FIDUCIALS, render_scene, write_card  # noqa: E402

FID_ARG = ";".join(
    f"{f['id']}:{f['center_mm'][0]},{f['center_mm'][1]}" for f in FIDUCIALS
)


def _run(
    tmp_path: Path, *args: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    code = cr.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_homography_recovers_the_hidden_camera(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png")
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        capsys=capsys,
    )
    assert code == 0 and r["status"] == "ok" and r["ids"] == [0, 1, 2, 3]
    assert r["residual_mm"]["rms"] < 0.15
    h = np.array(r["homography"])
    # points the fit never saw: project bed points with the hidden camera, map them back
    probe = np.array([[50.0, 50.0], [110.0, 110.0], [180.0, 40.0], [30.0, 190.0]])
    hom = np.hstack([probe, np.ones((4, 1))]) @ BED_TO_IMAGE.T
    px = hom[:, :2] / hom[:, 2:3]
    back = cr.apply_h(h, px)
    assert np.abs(back - probe).max() < 0.2
    rect = cv2.imread(r["rectified"], cv2.IMREAD_UNCHANGED)
    assert rect.dtype == np.uint16 and rect.shape[:2] == (
        2160,
        2160,
    )  # 2..218 mm at 10 px/mm
    assert r["area_mm"] == [2.0, 2.0, 218.0, 218.0]
    assert Path(r["annotated"]).is_file()


def test_rectified_image_has_bed_plus_y_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png")
    _, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        "--px-per-mm",
        "5",
        capsys=capsys,
    )
    rect = cv2.imread(r["rectified"], cv2.IMREAD_GRAYSCALE)
    found = cr.detect(rect, "DICT_4X4_50")
    centre = {i: c.mean(axis=0) for i, c in found.items()}
    # id 3 is at bed (12, 208): left and at the top of the rectified image
    assert centre[3][0] < 100 and centre[3][1] < 100
    assert centre[1][0] > 900 and centre[1][1] > 900  # (208, 12): right, bottom
    assert (
        abs(centre[0][0] - (12 - 2) * 5) < 1.5
        and abs(centre[0][1] - (218 - 12) * 5) < 1.5
    )


@pytest.mark.parametrize("scale", [(1.004, 0.997), (1.0, 1.0), (0.995, 1.002)])
def test_scale_reference_measures_the_printer_scale_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scale: tuple[float, float],
) -> None:
    img = render_scene(tmp_path / "p.png", card_at=(70.0, 80.0), printer_scale=scale)
    card = write_card(tmp_path / "card.json")
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        "--scale-ref",
        str(card),
        capsys=capsys,
    )
    assert code == 0
    s = r["scale"]
    assert (
        s["status"] == "ok"
        and s["markers_found"] == [40, 41, 42, 43]
        and s["fit_model"] == "affine"
    )
    assert s["true_mm_per_bed_mm"]["x"] == pytest.approx(scale[0], abs=0.001)
    assert s["true_mm_per_bed_mm"]["y"] == pytest.approx(scale[1], abs=0.001)
    assert r["other_markers"] == [40, 41, 42, 43]


def test_card_thickness_parallax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png", card_at=(70.0, 80.0))
    card = write_card(tmp_path / "card.json", thickness=3.0)
    _, plain = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        "--scale-ref",
        str(card),
        capsys=capsys,
    )
    assert (
        plain["scale"]["parallax_corrected"] is False
        and "uncorrected" in plain["scale"]["warning"]
    )
    _, corr = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        "--scale-ref",
        str(card),
        "--camera-height-mm",
        "300",
        capsys=capsys,
    )
    ratio = (
        corr["scale"]["true_mm_per_bed_mm"]["x"]
        / plain["scale"]["true_mm_per_bed_mm"]["x"]
    )
    assert ratio == pytest.approx(300 / 297)


def test_two_marker_reference_is_isotropic() -> None:
    true = np.array([[0.0, 0.0], [100.0, 0.0]])
    measured = np.array(
        [[10.0, 10.0], [10.0, 10.0 + 100 / 1.002]]
    )  # rotated 90°, 0.2 % short
    fit = cr.fit_scale(true, measured)
    assert fit["model"].startswith("isotropic")
    assert fit["ex"] == pytest.approx(1.002) and fit["ey"] == pytest.approx(1.002)
    assert fit["residual_mm"]["max"] < 1e-9


def test_too_few_fiducials_fails_with_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png", drop=(1, 2))
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        capsys=capsys,
    )
    assert code == 1 and r["status"] == "failed" and r["missing"] == [1, 2]
    assert "2 of 4 fiducials found; need 3" in r["reason"]


def test_three_fiducials_are_enough(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png", drop=(2,))
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        capsys=capsys,
    )
    assert code == 0 and r["fiducials_found"] == 3 and r["residual_mm"]["rms"] < 0.15


def test_scale_reference_not_seen_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png")
    card = write_card(tmp_path / "card.json")
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        "--scale-ref",
        str(card),
        capsys=capsys,
    )
    assert code == 1 and "0 of 4 markers seen" in r["reason"]


def test_manifest_fiducials_and_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = render_scene(tmp_path / "p.png")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"schema": "calib-plate/1", "fiducials": FIDUCIALS}))
    code, r = _run(
        tmp_path,
        str(img),
        "--out-dir",
        str(tmp_path / "o"),
        "--manifest",
        str(manifest),
        capsys=capsys,
    )
    assert code == 0 and r["fiducials_found"] == 4
    code, r = _run(
        tmp_path,
        str(tmp_path / "none.png"),
        "--out-dir",
        str(tmp_path / "o"),
        "--fiducials",
        FID_ARG,
        capsys=capsys,
    )
    assert code == 2 and "cannot read" in r["reason"]


@pytest.mark.parametrize("bad", [["--px-per-mm", "0"], ["--px-per-mm", "nan"], ["--area", "10,10,5,5"],
                                 ["--area", "1,2,3"]])
def test_bad_output_geometry_is_a_json_failure_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: list[str]
) -> None:
    img = render_scene(tmp_path / "p.png")
    code, r = _run(tmp_path, str(img), "--out-dir", str(tmp_path / "o"), "--fiducials", FID_ARG, *bad,
                   capsys=capsys)
    assert code == 2 and r["status"] == "failed"


def test_coincident_scale_markers_and_a_too_low_camera_fail_cleanly() -> None:
    h = np.eye(3)
    found = {0: np.zeros((4, 2)), 1: np.zeros((4, 2))}
    ref: dict[str, Any] = {"markers": {"0": [10.0, 10.0], "1": [10.0, 10.0]}}
    out = cr.scale_reference(ref, found, np.zeros((8, 8), np.uint8), h, None)
    assert out["status"] == "failed" and "coincident" in out["reason"]
    found = {0: np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float) + 10,
             1: np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float) + 60}
    ref = {"markers": {"0": [10.5, 10.5], "1": [60.5, 60.5]}, "thickness_mm": 3}
    out = cr.scale_reference(ref, found, np.zeros((8, 8), np.uint8), h, 3.0)
    assert out["status"] == "failed" and "must exceed" in out["reason"]


def test_marker_corner_convention() -> None:
    c = cr.marker_corners_mm((10.0, 20.0), 4.0)
    assert c.tolist() == [[8.0, 22.0], [12.0, 22.0], [12.0, 18.0], [8.0, 18.0]]
    r = cr.marker_corners_mm((0.0, 0.0), 2.0, 90.0)
    assert np.allclose(r, [[-1, -1], [-1, 1], [1, 1], [1, -1]])
    assert cr.parse_fiducials("0:1,2; 1:3,4", 5)[1] == {
        "id": 1,
        "center_mm": [3.0, 4.0],
        "size_mm": 5,
    }
