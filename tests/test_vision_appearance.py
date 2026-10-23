"""threed_vision.appearance: per-part colours from a photo + mask (numpy + OpenCV only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vision_fit_synth as S  # noqa: E402  (also puts packages/threed-vision/src on sys.path)

from threed_vision.appearance import sample_part_colours  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402
from threed_vision.io import to_jsonable  # noqa: E402

RED = (200, 30, 30)
GREY = (128, 128, 130)
BG = (60, 90, 60)


def tool(angle: float = 30.0, highlight: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """A screwdriver-like tool on a green table: red handle 44 px wide x 110 px, grey shaft 12 px wide x 200 px, rotated by `angle`."""
    img = np.zeros((420, 420, 3), np.uint8)
    img[:] = BG
    layer = np.zeros((420, 420, 3), np.uint8)
    mask = np.zeros((420, 420), np.uint8)
    cv2.rectangle(layer, (186, 60), (230, 170), RED, -1)
    cv2.rectangle(layer, (202, 170), (214, 370), GREY, -1)
    cv2.rectangle(mask, (186, 60), (230, 170), 1, -1)
    cv2.rectangle(mask, (202, 170), (214, 370), 1, -1)
    if highlight:
        cv2.rectangle(
            layer, (196, 80), (200, 130), (255, 255, 255), -1
        )  # specular stripe on the handle
        cv2.rectangle(
            layer, (207, 220), (209, 300), (250, 250, 250), -1
        )  # and on the shaft
    M = cv2.getRotationMatrix2D((208, 215), angle, 1.0)
    layer = cv2.warpAffine(layer, M, (420, 420), flags=cv2.INTER_NEAREST)
    mask = cv2.warpAffine(mask, M, (420, 420), flags=cv2.INTER_NEAREST) > 0
    img[mask] = layer[mask]
    return img, mask


def close(a: list[int], b: tuple[int, int, int], tol: int = 18) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


@pytest.mark.parametrize("angle", [0.0, 30.0, 90.0, 200.0])
def test_two_colour_tool_is_split_at_the_width_discontinuity(angle: float) -> None:
    img, mask = tool(angle)
    out = sample_part_colours(img, mask)
    assert len(out["parts"]) == 2
    first, second = out["parts"]
    assert first["t1"] == second["t0"] and first["t0"] == 0.0 and second["t1"] == 1.0
    assert 0.2 < first["t1"] < 0.5  # handle is ~110 of 310 px
    assert close(first["colour_srgb"], RED) and close(
        second["colour_srgb"], GREY
    )  # highlights did not wash the colour out
    assert (first["role_guess"], second["role_guess"]) == (
        "handle",
        "shaft",
    )  # the wider end comes first
    assert first["hex"] == "#{:02x}{:02x}{:02x}".format(*first["colour_srgb"])
    assert sum(p["area_frac"] for p in out["parts"]) == pytest.approx(1.0)
    assert [e["hex"] for e in out["palette"]] == [first["hex"], second["hex"]]
    assert json.dumps(to_jsonable(out), allow_nan=False)


def test_output_is_deterministic() -> None:
    img, mask = tool()
    a, b = sample_part_colours(img, mask), sample_part_colours(img.copy(), mask.copy())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_boundary_blend_does_not_pull_the_colour() -> None:
    """Pixels on the mask edge carry the background (anti-aliasing / a loose mask): they must not matter."""
    img, mask = tool(0.0, highlight=False)
    ring = mask & ~(cv2.erode(mask.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0)
    img[ring] = (0, 0, 255)  # a saturated blue rim
    out = sample_part_colours(img, mask)
    assert close(out["parts"][0]["colour_srgb"], RED) and close(
        out["parts"][1]["colour_srgb"], GREY
    )


def test_n_parts_and_palette_limits() -> None:
    img, mask = tool()
    three = sample_part_colours(img, mask, n_parts=3)
    assert len(three["parts"]) == 3 and [p["role_guess"] for p in three["parts"]] == [
        "handle",
        "shaft",
        "head",
    ]
    one = sample_part_colours(img, mask, n_parts=1)
    assert len(one["parts"]) == 1 and one["parts"][0]["role_guess"] == "body"
    assert (one["parts"][0]["t0"], one["parts"][0]["t1"]) == (0.0, 1.0)
    assert len(sample_part_colours(img, mask, max_colours=1)["palette"]) == 1
    # three parts of a two-colour tool: the two grey parts are one palette colour
    assert len(three["palette"]) == 2


def test_uniform_colour_gives_one_palette_entry() -> None:
    img, mask = tool(highlight=False)
    img[mask] = (30, 90, 200)
    out = sample_part_colours(img, mask)
    assert len(out["palette"]) == 1 and out["palette"][0]["area_frac"] == pytest.approx(
        1.0
    )
    assert all(close(p["colour_srgb"], (30, 90, 200), 6) for p in out["parts"])


def test_white_tool_stays_white() -> None:
    img, mask = tool(highlight=False)
    img[mask] = (240, 240, 240)
    out = sample_part_colours(img, mask)
    assert all(
        min(p["colour_srgb"]) > 220 for p in out["parts"]
    )  # the highlight filter never empties a part


def test_fit_template_refines_the_role_guess() -> None:
    img, mask = tool()
    fit = {"template": {"name": "box"}}
    assert [
        p["role_guess"] for p in sample_part_colours(img, mask, fit=fit)["parts"]
    ] == ["body", "body"]
    fit = {"template": {"name": "flat"}}
    assert [
        p["role_guess"] for p in sample_part_colours(img, mask, fit=fit)["parts"]
    ] == ["handle", "jaw"]


@pytest.mark.parametrize(
    "name",
    [
        "screwdriver",
        "caliper",
        "multimeter",
        "tape_measure",
        "drill",
        "bit_set",
        "pliers_open",
    ],
)
def test_recorded_carpet_masks_with_a_synthetic_colour_fill(name: str) -> None:
    """Real silhouettes, colours painted along the axis: the cut colours are recovered and nothing is NaN."""
    mask = S.recorded_mask(name)
    ys, xs = np.nonzero(mask)
    img = np.zeros((*mask.shape, 3), np.uint8)
    img[:] = BG
    uniform = img.copy()
    uniform[mask] = (20, 120, 220)
    out = sample_part_colours(uniform, mask)
    assert (
        out["parts"]
        and len(out["palette"]) == 1
        and close(out["palette"][0]["colour_srgb"], (20, 120, 220), 6)
    )
    assert all(np.isfinite(p["t0"]) and np.isfinite(p["t1"]) for p in out["parts"])
    painted = uniform.copy()  # two halves along the image's long side
    long_axis = np.argmax(mask.shape)
    coord = (ys, xs)[long_axis]
    cut = (coord.min() + coord.max()) // 2
    painted[ys[coord < cut], xs[coord < cut]] = RED
    out2 = sample_part_colours(painted, mask, n_parts=2)
    hexes = {p["hex"] for p in out2["parts"]}
    assert len(out2["parts"]) == 2 and len(hexes) >= 1
    assert any(close(p["colour_srgb"], RED, 25) for p in out2["parts"]) or any(
        close(p["colour_srgb"], (20, 120, 220), 25) for p in out2["parts"]
    )
    json.dumps(to_jsonable(out2), allow_nan=False)


def test_bad_input_is_clean() -> None:
    img, mask = tool()
    with pytest.raises(BadInput, match="same height"):
        sample_part_colours(img[:100], mask)
    with pytest.raises(BadInput, match="uint8"):
        sample_part_colours(img[..., 0], mask)
    with pytest.raises(BadInput, match="uint8"):
        sample_part_colours(img.astype(np.float32), mask)
    with pytest.raises(BadInput, match="uint8"):
        sample_part_colours(img.astype(np.uint16) * 257, mask)
    with pytest.raises(BadInput, match="n_parts"):
        sample_part_colours(img, mask, n_parts=9)
    with pytest.raises(BadInput, match="max_colours"):
        sample_part_colours(img, mask, max_colours=0)
    with pytest.raises(BadInput, match="foreground"):
        sample_part_colours(img, np.zeros_like(mask))
    one = np.zeros_like(mask)
    one[10, 10] = True
    with pytest.raises(BadInput, match="foreground"):
        sample_part_colours(img, one)


@pytest.mark.parametrize("shape", [(3, 3), (1, 20), (20, 1)])
def test_tiny_but_valid_masks_never_give_nan(shape: tuple[int, int]) -> None:
    img = np.full((40, 40, 3), 77, np.uint8)
    mask = np.zeros((40, 40), bool)
    mask[10 : 10 + shape[0], 10 : 10 + shape[1]] = True
    if mask.sum() < 8:
        pytest.skip("below the 8-pixel minimum: covered by test_bad_input_is_clean")
    out = sample_part_colours(img, mask)
    assert json.dumps(to_jsonable(out), allow_nan=False)
    assert out["parts"][0]["colour_srgb"] is not None


def test_forced_n_parts_on_a_uniform_width_tool_splits_in_equal_parts() -> None:
    """No width discontinuity: the cut must come from the equal split, not from stair-step noise at the first allowed station."""
    img = np.full((120, 400, 3), BG, np.uint8)
    mask = np.zeros((120, 400), bool)
    mask[40:80, 20:380] = True
    img[40:80, 20:200] = RED
    img[40:80, 200:380] = GREY
    out = sample_part_colours(img, mask, n_parts=2)
    assert out["parts"][0]["t1"] == pytest.approx(0.5, abs=0.05)
    cols = [p["colour_srgb"] for p in out["parts"]]
    assert any(close(c, RED) for c in cols) and any(close(c, GREY) for c in cols)
    three = sample_part_colours(img, mask, n_parts=3)
    assert [p["t1"] for p in three["parts"]][:2] == pytest.approx([0.25, 0.5], abs=0.05)


def test_light_grey_part_is_not_discarded_as_glare() -> None:
    img, mask = tool(highlight=False)
    ys, xs = np.nonzero(mask)
    img[mask] = (205, 205, 208)  # chrome-ish: L ~ 82, nearly colourless
    img[ys[::3], xs[::3]] = (120, 120, 122)  # a minority of dark reflections
    out = sample_part_colours(img, mask)
    assert all(close(p["colour_srgb"], (205, 205, 208), 14) for p in out["parts"])


def test_malformed_fit_is_ignored_not_a_crash() -> None:
    img, mask = tool()
    assert len(sample_part_colours(img, mask, fit={"template": "box"})["parts"]) == 2  # type: ignore[arg-type]
