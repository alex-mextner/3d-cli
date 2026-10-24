"""lib/backdrop_mask.py: the subject of a studio render is everything that is not the smooth neutral backdrop."""

from __future__ import annotations

import numpy as np
import pytest
from backdrop_mask import backdrop_mask
from errors import UsageError
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFilter

SIZE = 427
BACKDROP = (178, 173, 170)


def _scene(*, shadow: bool = True) -> tuple[NDArray[np.uint8], NDArray[np.bool_]]:
    """A grey backdrop with a vertical gradient, a soft floor shadow, and a subject of black, white and pink parts."""
    ramp = np.linspace(0.88, 1.06, SIZE)[:, None, None]
    arr = np.array(BACKDROP, dtype=np.float64) * ramp * np.ones((SIZE, SIZE, 3))
    if shadow:  # soft, like a rendered contact shadow: a hard-edged blob would be its own texture ring and island
        blob = Image.new("L", (SIZE, SIZE), 0)
        ImageDraw.Draw(blob).ellipse([60, 340, 360, 400], fill=255)
        alpha = (
            np.asarray(blob.filter(ImageFilter.GaussianBlur(10)), dtype=np.float64)
            / 255.0
        )
        arr = arr * (1.0 - 0.40 * alpha[..., None])
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    for box, colour in (
        ([110, 70, 320, 330], (24, 24, 24)),
        ([170, 190, 260, 320], (252, 252, 252)),
        ([200, 120, 230, 150], (240, 150, 160)),
    ):
        d.ellipse(box, fill=colour)
    truth = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(truth).ellipse([110, 70, 320, 330], fill=255)
    return np.asarray(img), np.asarray(truth) > 0


def _iou(a: NDArray[np.bool_], b: NDArray[np.bool_]) -> float:
    return float((a & b).sum() / (a | b).sum())


def test_subject_with_black_white_and_pink_parts_is_one_solid_mask() -> None:
    rgb, truth = _scene()
    mask = backdrop_mask(rgb)
    assert mask.dtype == np.uint8 and set(np.unique(mask)) <= {0, 255}
    assert _iou(mask > 0, truth) > 0.97
    assert (
        mask[135, 215] == 255
    )  # the pink patch is subject (it is saturated: not backdrop)
    assert (
        mask[250, 215] == 255
    )  # the white patch is subject (value above the backdrop range)


def test_a_mid_grey_floor_shadow_stays_backdrop() -> None:
    rgb, truth = _scene(shadow=True)
    mask = backdrop_mask(rgb) > 0
    assert (
        rgb[380, 210].max() < 0.62 * 255
    )  # the shadow is clearly darker than the backdrop around it
    assert (
        not mask[380, 90] and not mask[380, 330]
    )  # inside the shadow ellipse, outside the subject
    assert _iou(mask, truth) > 0.97


def test_speckle_noise_in_the_backdrop_does_not_leak_into_the_mask() -> None:
    rgb, truth = _scene()
    noisy = rgb.copy()
    rng = np.random.default_rng(3)
    ys, xs = rng.integers(5, 60, 40), rng.integers(5, SIZE - 5, 40)
    noisy[ys, xs] = (20, 20, 20)  # dark specks in the empty top strip
    mask = backdrop_mask(noisy) > 0
    assert _iou(mask, truth) > 0.97
    assert not mask[:62].any()


def test_a_hole_inside_the_subject_is_filled() -> None:
    rgb, truth = _scene()
    holed = rgb.copy()
    holed[190:215, 140:160] = BACKDROP  # a window of backdrop colour inside the body
    mask = backdrop_mask(holed) > 0
    assert mask[200, 150]
    assert _iou(mask, truth) > 0.97


def test_kernel_sizes_scale_with_the_frame() -> None:
    rgb, truth = _scene()
    big = np.asarray(
        Image.fromarray(rgb).resize((SIZE * 2, SIZE * 2), Image.Resampling.BILINEAR)
    )
    mask = backdrop_mask(big) > 0
    small = (
        np.asarray(
            Image.fromarray((mask * 255).astype(np.uint8)).resize(
                (SIZE, SIZE), Image.Resampling.NEAREST
            )
        )
        > 0
    )
    assert _iou(small, truth) > 0.96


def test_backdrop_thresholds_are_options_not_constants() -> None:
    rgb, truth = _scene()
    # a value window that does not contain the backdrop: nothing along the border qualifies
    with pytest.raises(UsageError, match="no backdrop along the frame border"):
        backdrop_mask(rgb, value_range=(0.05, 0.30))
    # a zero saturation limit excludes the slightly warm backdrop (saturation 0.045) as well
    with pytest.raises(UsageError, match="no backdrop along the frame border"):
        backdrop_mask(rgb, max_saturation=0.0)
    assert backdrop_mask(rgb, max_saturation=0.9)[250, 215] == 255  # a wide limit still keeps the subject


def test_no_backdrop_at_the_border_is_an_input_error() -> None:
    rgb = np.full(
        (SIZE, SIZE, 3), (230, 40, 40), dtype=np.uint8
    )  # saturated red everywhere
    with pytest.raises(UsageError, match="no backdrop along the frame border"):
        backdrop_mask(rgb)


def test_a_frame_that_is_all_backdrop_has_no_subject() -> None:
    rgb = np.full((SIZE, SIZE, 3), BACKDROP, dtype=np.uint8)
    with pytest.raises(UsageError, match="no subject"):
        backdrop_mask(rgb)


def test_wrong_shape_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="HxWx3"):
        backdrop_mask(np.zeros((10, 10), dtype=np.uint8))
