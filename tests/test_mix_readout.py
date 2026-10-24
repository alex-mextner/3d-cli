"""`3d mix readout` (#416): synthetic photos of the tester card, read back against the colours drawn.

The photos are rendered from the tester's own manifest and card (tests/mix_photo_fixtures.py) with
a colour cast, perspective and an uneven light; the true swatch colours are never passed to the
reader, which gets only the photo and the manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "ArucoDetector"):
    pytest.skip("OpenCV >= 4.7 with aruco needed", allow_module_level=True)

import numpy as np  # noqa: E402
import calib_rectify as cr  # noqa: E402
from mix_photo_fixtures import (  # noqa: E402
    phone_photo,
    scene,
    shoot,
    top_camera_frame,
    truths,
)
from mix_tester_fixtures import build_tester  # noqa: E402
from PIL import Image  # noqa: E402

from printops.color import hex_delta_e  # noqa: E402
from slicing import mix_card  # noqa: E402
from slicing.mix_readout import SCHEMA, ReadoutError, _patch_pixels, read_photo  # noqa: E402


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    d = tmp_path_factory.mktemp("tester")
    return d, build_tester(d)


def _lin(made: tuple[Path, dict[str, Any]], **kw: Any) -> Any:
    d, m = made
    return scene(d / "tester.card.png", m, truths(m), **kw)


def _check_every_swatch(
    record: dict[str, Any], m: dict[str, Any], limit: float = 2.0
) -> None:
    true = truths(m)
    assert len(record["swatches"]) == len(true)
    for r in record["swatches"]:
        err = hex_delta_e(r["measured_hex"], true[r["number"]])
        assert err < limit, (r["number"], r["measured_hex"], true[r["number"]], err)


def test_phone_photo_with_perspective_cast_and_uneven_light_is_read_within_de2(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made))
    rec = read_photo(m, photo, tmp_path / "out", manifest_name="tester.mix.json")
    _check_every_swatch(rec, m)
    assert rec["schema"] == SCHEMA
    assert rec["fiducials"]["found"] == 4 and rec["fiducials"]["rms_mm"] < 0.5
    assert rec["qr"]["status"] == "match"
    assert rec["white"]["model"] == "plane" and len(rec["white"]["regions"]) == 3


def test_top_camera_frame_16bit_png_is_read_within_de2(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    photo = top_camera_frame(tmp_path / "frame.png", _lin(made))
    rec = read_photo(m, photo, tmp_path / "out")
    _check_every_swatch(rec, m)
    assert rec["photo"]["size"] == [1536, 1600]


def test_the_prediction_error_is_what_the_true_colours_differ_by(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """The record's ΔE to the prediction is the real ΔE between the drawn truth and the model's hex."""
    _, m = made
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made))
    rec = read_photo(m, photo, tmp_path / "out")
    entries = {e["number"]: e for e in m["candidates"] + m["references"]}
    for r in rec["swatches"]:
        truth_err = hex_delta_e(truths(m)[r["number"]], entries[r["number"]]["hex"])
        assert r["delta_e_predicted"] == pytest.approx(truth_err, abs=2.0)
        assert r["delta_e_target"] == pytest.approx(
            hex_delta_e(r["measured_hex"], m["target"]), abs=0.01
        )
    assert rec["summary"]["closest_to_target"]["number"] in {
        c["number"] for c in m["candidates"]
    }


def test_files_are_written_and_the_json_is_the_record(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made))
    rec = read_photo(m, photo, tmp_path / "o" / "wall", manifest_name="tester.mix.json")
    assert json.loads((tmp_path / "o" / "wall.readout.json").read_text()) == rec
    rect = Image.open(tmp_path / "o" / "wall.rectified.png")
    assert rect.size == (
        round(m["readout"]["card_mm"][0] * 10),
        round(m["readout"]["card_mm"][1] * 10),
    )
    ann = Image.open(tmp_path / "o" / "wall.readout.png")
    assert ann.size[0] > rect.size[0] and ann.size[1] >= rect.size[1]
    assert rec["photo"]["sha256"] and rec["manifest"]["id"] == m["id"]


def test_a_card_of_another_tester_is_refused(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    other = build_tester(tmp_path / "other", target="#B5654F", count=3)
    assert other["id"] != m["id"]
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made))
    with pytest.raises(
        ReadoutError,
        match=r"names '" + m["id"] + r"'.*not this manifest \(" + other["id"],
    ):
        read_photo(other, photo, tmp_path / "out")


def test_a_photo_without_a_readable_qr_is_refused_unless_the_check_is_skipped(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    lin = _lin(made)
    qr = m["readout"]["qr"]["box_mm"]
    h_mm = m["readout"]["card_mm"][1]
    pad = 6.0
    lin[
        round((h_mm - qr[3] - pad) * 10) : round((h_mm - qr[1] + pad) * 10),
        round((qr[0] - pad) * 10) : round((qr[2] + pad) * 10),
    ] = 0.83  # the QR is smeared out
    photo = phone_photo(tmp_path / "phone.jpg", lin)
    with pytest.raises(ReadoutError, match="no QR code"):
        read_photo(m, photo, tmp_path / "out")
    rec = read_photo(m, photo, tmp_path / "out", check_qr=False)
    assert rec["qr"]["status"] == "skipped"
    _check_every_swatch(rec, m)


def test_fewer_than_three_fiducials_says_what_to_reshoot(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    lin = _lin(made)
    h_mm = m["readout"]["card_mm"][1]
    for f in m["readout"]["fiducials"][:2]:
        cx, cy = f["center_mm"]
        lin[
            round((h_mm - cy - 8) * 10) : round((h_mm - cy + 8) * 10),
            round((cx - 8) * 10) : round((cx + 8) * 10),
        ] = 0.83
    photo = phone_photo(tmp_path / "phone.jpg", lin)
    with pytest.raises(ReadoutError, match=r"2 of 4 card fiducials found.*reshoot"):
        read_photo(m, photo, tmp_path / "out")


def test_a_blank_photo_has_no_fiducials(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    path = tmp_path / "blank.png"
    Image.fromarray(np.full((600, 800, 3), 120, np.uint8)).save(path)
    with pytest.raises(ReadoutError, match=r"0 of 4 card fiducials"):
        read_photo(m, path, tmp_path / "out")


def test_a_card_too_small_in_the_frame_is_refused(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    small = cv2.resize(_lin(made), None, fx=0.24, fy=0.24, interpolation=cv2.INTER_AREA)
    quad = [
        (10, 10),
        (10 + small.shape[1], 10),
        (10 + small.shape[1], 10 + small.shape[0]),
        (10, 10 + small.shape[0]),
    ]
    shot = shoot(
        small,
        quad,
        (small.shape[1] + 20, small.shape[0] + 20),
        cast=(1, 1, 1),
        noise=0.0,
        blur=0.0,
    )
    path = tmp_path / "far.png"
    cv2.imwrite(
        str(path),
        np.round(np.clip(shot, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)[:, :, ::-1],
    )
    with pytest.raises(ReadoutError, match=r"(px/mm|fiducials found)"):
        read_photo(m, path, tmp_path / "out")


def test_a_shaded_paper_region_is_refused(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    lin = _lin(made)
    h_mm = m["readout"]["card_mm"][1]
    b = m["readout"]["white_boxes"][0]
    lin[
        round((h_mm - b[3]) * 10) : round((h_mm - b[1]) * 10),
        round(b[0] * 10) : round(b[2] * 10),
    ] = 0.002
    photo = phone_photo(tmp_path / "phone.jpg", lin)
    with pytest.raises(ReadoutError, match="paper region"):
        read_photo(m, photo, tmp_path / "out")


def test_white_balance_on_a_pure_reference_swatch(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """--white N maps the white spool's swatch to the spool's colour; the others follow it."""
    d, m = made
    white = next(r for r in m["references"] if r["hex"] == "#FFFFFF")
    true = dict(truths(m))
    true[white["number"]] = white["hex"]  # this one prints exactly as the spool says
    lin = scene(d / "tester.card.png", m, true)
    photo = phone_photo(tmp_path / "phone.jpg", lin)
    rec = read_photo(m, photo, tmp_path / "out", white=str(white["number"]))
    assert rec["white"]["mode"] == "swatch" and rec["white"]["maps_to"] == "#FFFFFF"
    for r in rec["swatches"]:
        assert hex_delta_e(r["measured_hex"], true[r["number"]]) < 3.0, r
    with pytest.raises(ReadoutError, match="not a pure reference"):
        read_photo(m, photo, tmp_path / "out", white="1")
    with pytest.raises(ReadoutError, match="card, or the number"):
        read_photo(m, photo, tmp_path / "out", white="grey")


def test_a_swatch_half_in_shadow_is_flagged_not_trusted(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made, half_shadow=2))
    rec = read_photo(m, photo, tmp_path / "out")
    assert any("swatch 2: the patch is not flat" in w for w in rec["warnings"])
    assert not any("swatch 1:" in w for w in rec["warnings"])


def test_the_card_layout_the_reader_trusts_is_the_one_printed(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """The card PNG's ArUco markers decode to the manifest's fiducial ids at the manifest's centres."""
    d, m = made
    img = cv2.imread(str(d / "tester.card.png"))
    found = cr.detect(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), m["readout"]["dictionary"])
    assert sorted(found) == [0, 1, 2, 3]
    h_px = img.shape[0]
    for f in m["readout"]["fiducials"]:
        c = found[f["id"]].mean(axis=0)
        assert c[0] / 10 == pytest.approx(f["center_mm"][0], abs=0.15)
        assert (h_px - c[1]) / 10 == pytest.approx(f["center_mm"][1], abs=0.15)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(
        cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    )
    assert text == mix_card.payload(m["id"])


def test_markers_that_do_not_fit_the_manifests_card_refuse_the_fit(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """A marker of another size (a plate or card printed at another scale in the frame) is not data."""
    _, m = made
    wrong = json.loads(json.dumps(m))
    wrong["readout"]["fiducials"][0]["size_mm"] = 4.0
    photo = phone_photo(tmp_path / "phone.jpg", _lin(made))
    with pytest.raises(ReadoutError, match=r"fit a flat card with \d+\.\d+ mm rms"):
        read_photo(wrong, photo, tmp_path / "out")


def test_a_patch_outside_the_photo_is_never_read_as_black() -> None:
    """The warp fills what the camera did not see with zeros; the 4th channel says so."""
    lay = {"px_per_mm": 10, "card_mm": [20.0, 20.0]}
    rect = np.full((200, 200, 4), 255, np.uint8)
    rect[:, 100:, 3] = 0  # the right half of the card was outside the frame
    rect[:, 100:, :3] = 0
    assert _patch_pixels(rect, lay, [2.0, 2.0, 8.0, 8.0]).shape == (3600, 3)
    with pytest.raises(ReadoutError, match=r"outside the photo.*reshoot"):
        _patch_pixels(rect, lay, [8.0, 2.0, 14.0, 8.0])
    with pytest.raises(ReadoutError, match="outside the card image"):
        _patch_pixels(rect, lay, [18.0, 2.0, 25.0, 8.0])


def test_a_photo_pillow_cannot_decode_is_a_reshoot_message_not_a_traceback(
    made: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    _, m = made
    junk = tmp_path / "heic.jpg"
    junk.write_bytes(b"ftypheic not a jpeg")
    with pytest.raises(
        ReadoutError, match="cannot be read as an image.*send a JPEG or PNG"
    ):
        read_photo(m, junk, tmp_path / "out")
