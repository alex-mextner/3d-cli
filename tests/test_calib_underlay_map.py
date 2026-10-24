"""Tests for calib_underlay_map: the bare underlay of a plate photo as the first-layer map, with
bed-flatness and z-offset hints (#320). Synthetic rectified photos: pixel (u, v) is the bed point
(x0 + u / px_per_mm, y1 - v / px_per_mm)."""

from __future__ import annotations

import numpy as np
import pytest

from calib_underlay_map import analyze

PPM = 4.0
AREA = (0.0, 0.0, 100.0, 100.0)


def _photo(value: float = 0.8) -> np.ndarray:
    return np.full((int(100 * PPM), int(100 * PPM)), value)


def _underlay(
    light: str = "contrast", bare: list[list[float]] | None = None
) -> dict[str, object]:
    return {"bare": bare or [[0.0, 0.0, 100.0, 100.0]], "read": {"light": light}}


def _paint(img: np.ndarray, x0: float, y0: float, x1: float, y1: float, fn) -> None:  # type: ignore[no-untyped-def]
    """Paint the bed rectangle: pixel rows count down from y1 = 100."""
    v0, v1 = round((100 - y1) * PPM), round((100 - y0) * PPM)
    u0, u1 = round(x0 * PPM), round(x1 * PPM)
    img[v0:v1, u0:u1] = fn(img[v0:v1, u0:u1])


def test_an_even_underlay_has_no_hints() -> None:
    rng = np.random.default_rng(1)
    img = _photo() + rng.normal(0, 0.004, (400, 400))
    out = analyze(img, AREA, PPM, _underlay())
    assert out["schema"] == "calib-underlay-map/1" and out["status"] == "proposed"
    assert (
        out["cells_read"] == out["cells_total"] == 81
    )  # whole 10 mm cells inside the 0.6 mm inset
    assert out["hints"] == []
    assert out["uniformity"]["mean_cv"] < 0.01


def test_a_brightness_gradient_is_a_tilt_hint() -> None:
    ramp = np.tile(np.linspace(0.5, 0.9, 400), (400, 1))  # brighter towards +X
    out = analyze(ramp, AREA, PPM, _underlay())
    tilt = [h for h in out["hints"] if h["kind"] == "bed_tilt"]
    assert len(tilt) == 1 and "+X" in tilt[0]["note"]
    assert out["uniformity"]["gradient_per_100mm"]["x"] > 0.3
    assert abs(out["uniformity"]["gradient_per_100mm"]["y"]) < 0.01


def _gappy(patch: np.ndarray) -> np.ndarray:
    out = patch.copy() * 0.7
    out[:, ::2] = 0.15  # the bed shows through every other line
    return out


def test_gaps_under_contrast_light_are_a_nozzle_too_high_hint_at_their_place() -> None:
    rng = np.random.default_rng(2)
    img = _photo() + rng.normal(0, 0.004, (400, 400))
    _paint(
        img, 40.6, 70.6, 50.6, 80.6, _gappy
    )  # exactly one cell (rows count from the top)
    out = analyze(img, AREA, PPM, _underlay("contrast"))
    z = [h for h in out["hints"] if h["kind"] == "z_offset"]
    assert z and all("nozzle too high" in h["note"] for h in z)
    x0, y0, x1, y1 = z[0]["bbox_mm"]
    assert (x0, y0, x1, y1) == (40.6, 70.6, 50.6, 80.6)  # the y axis points up


def test_the_same_roughness_under_raking_light_is_only_to_be_looked_at() -> None:
    img = _photo()
    _paint(img, 40.6, 70.6, 50.6, 80.6, _gappy)
    out = analyze(img, AREA, PPM, _underlay("raking"))
    z = [h for h in out["hints"] if h["kind"] == "z_offset"]
    assert z and all("gaps (too high) or ridges (too low)" in h["note"] for h in z)


def test_a_smooth_dark_cell_is_patchy_not_a_z_offset() -> None:
    rng = np.random.default_rng(3)
    img = _photo() + rng.normal(0, 0.004, (400, 400))
    _paint(img, 10.6, 10.6, 20.6, 20.6, lambda p: p * 0.5)
    out = analyze(img, AREA, PPM, _underlay())
    assert [h["kind"] for h in out["hints"]] == ["patchy"]
    assert "darker" in out["hints"][0]["note"]


def test_cells_only_cover_the_bare_rectangles_and_the_photo() -> None:
    out = analyze(
        _photo(), AREA, PPM, _underlay(bare=[[0, 0, 25, 12], [60, 60, 100, 100]])
    )
    boxes = [tuple(c["bbox_mm"]) for c in out["first_layer_map"]]
    assert (0.6, 0.6, 10.6, 10.6) in boxes and (60.6, 60.6, 70.6, 70.6) in boxes
    assert all(b[3] <= 100 for b in boxes)
    # a photo that covers only a corner reads only the cells inside it
    small = analyze(_photo()[:200, :200], AREA, PPM, _underlay())
    assert small["cells_read"] < small["cells_total"]
    assert all(
        c["bbox_mm"][2] <= 50 and c["bbox_mm"][3] > 49.9 or c["bbox_mm"][1] >= 50
        for c in small["first_layer_map"]
    )


def test_too_few_cells_need_a_person() -> None:
    out = analyze(_photo(), AREA, PPM, _underlay(bare=[[0, 0, 25, 12]]))
    assert (
        out["status"] == "needs_manual"
        and "only 2" in out["reason"]
        and out["hints"] == []
    )


def test_the_light_must_be_known() -> None:
    with pytest.raises(ValueError, match="contrast, raking"):
        analyze(
            _photo(), AREA, PPM, {"bare": [[0, 0, 50, 50]], "read": {"light": "dim"}}
        )
    with pytest.raises(ValueError, match="positive"):
        analyze(_photo(), AREA, 0.0, _underlay())


def test_colour_and_8_bit_photos_are_read_like_grey() -> None:
    grey = _photo(0.8)
    rgb = np.stack([grey * 255] * 3, axis=2).astype(np.uint8)
    a, b = analyze(grey, AREA, PPM, _underlay()), analyze(rgb, AREA, PPM, _underlay())
    assert a["uniformity"]["texture_median"] == b["uniformity"]["texture_median"] == 0.0
    assert a["first_layer_map"][0]["mean"] == pytest.approx(
        b["first_layer_map"][0]["mean"], abs=0.01
    )


def test_cells_tile_the_union_of_the_bare_rectangles_not_each_of_them() -> None:
    """The producer splits the sheet into rectangles whose inner borders are not edges: a 100 x 12
    sheet with a notch is two 6 mm-high rectangles that together hold nine cells."""
    from slicing.calibration_underlay import rect_difference

    bare = rect_difference((0.0, 0.0, 100.0, 12.0), [(99.0, 6.0, 100.0, 12.0)])
    assert len(bare) == 2
    photo = np.full((48, 400), 0.8)
    out = analyze(
        photo,
        (0.0, 0.0, 100.0, 12.0),
        PPM,
        {"bare": [list(r) for r in bare], "read": {"light": "contrast"}},
    )
    assert out["cells_total"] == out["cells_read"] == 9 and out["status"] == "proposed"


def test_a_cell_that_would_touch_a_foot_is_not_read() -> None:
    bare = [
        [0, 0, 40, 10],
        [0, 10, 18, 40],
        [22, 10, 40, 40],
    ]  # a 4 mm foot column between
    out = analyze(_photo(), AREA, PPM, {"bare": bare, "read": {"light": "contrast"}})
    boxes = [c["bbox_mm"] for c in out["first_layer_map"]]
    assert all(b[2] <= 18.6 or b[0] >= 21.4 or b[3] <= 10.6 for b in boxes)
