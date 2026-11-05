"""The lighting-normalised evidence channel of the bed verdict (lib/bed_norm.py, issue #723).

Day/night and light on/off on an EMPTY bed must never read as objects; the real parts of the fixtures
(U1 parts after the cancel, the spaghetti frame, squares pasted on the K1 SE bed) still do, also under
the same lighting changes. The lighting changes are generated on the real frames: a gain (up to x4
brighter, x0.25 darker), a gamma (0.5..2) and a colour cast, with clipping as a camera does it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from test_bed_state import frame, k1_ref, u1_match, u1_ref, u1_z10_ref

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_norm as N  # noqa: E402  (needs numpy + OpenCV)
import bed_state as B  # noqa: E402
from test_bed_robust import u1_ref_1016  # noqa: E402

# (gain, gamma, per-channel cast as B, G, R gains)
NEUTRAL = (1.0, 1.0, 1.0)
CAST = (1.2, 1.0, 0.8)
LIGHTS = [
    (0.25, 1.0, NEUTRAL),
    (0.5, 0.5, NEUTRAL),
    (0.5, 2.0, CAST),
    (2.0, 0.5, NEUTRAL),
    (2.0, 1.0, CAST),
    (4.0, 1.0, NEUTRAL),
    (1.0, 0.5, CAST),
    (1.0, 2.0, NEUTRAL),
    (2.0, 2.0, NEUTRAL),
    (4.0, 2.0, CAST),
]
MODERATE = [
    (0.5, 1.0, NEUTRAL),
    (2.0, 1.0, CAST),
    (1.0, 0.5, CAST),
    (1.0, 2.0, NEUTRAL),
]


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    return tmp_path


def lit(img: Any, gain: float, gamma: float, cast: tuple[float, float, float]) -> Any:
    """`img` under another light: x^gamma, times the gain and the per-channel cast, clipped like a camera."""
    x = (img.astype(np.float32) / 255.0) ** gamma * gain * np.array(cast, np.float32)
    return (np.clip(x, 0.0, 1.0) * 255).astype(np.uint8)


def k1(
    name: str, light: tuple[float, float, tuple[float, float, float]] | None = None
) -> B.Verdict:
    img = frame(name)
    return B.classify(
        img if light is None else lit(img, *light), [k1_ref()], 125.0, "k1se"
    )


def u1(
    name: str, light: tuple[float, float, tuple[float, float, float]] | None = None
) -> B.Verdict:
    img = frame(name)
    return B.classify(
        img if light is None else lit(img, *light),
        [u1_ref()],
        200.0,
        "u1",
        match=u1_match(),
    )


def u1_window(
    name: str, light: tuple[float, float, tuple[float, float, float]] | None = None
) -> B.Verdict:
    img = frame(name)
    refs = [u1_ref_1016()]
    return B.classify(
        img if light is None else lit(img, *light), refs, 200.2, "u1", match=u1_match()
    )


# ---- the channel on its own --------------------------------------------------------------------


def test_light_stats_sees_a_gain_and_a_tone_curve_but_not_a_same_light_frame() -> None:
    img = frame("k1_z125_clear_ref.jpg")
    roi = np.ones(img.shape[:2], bool)
    assert not N.light_stats(img, img, roi).mismatch
    assert N.light_stats(img, lit(img, 0.5, 1.0, NEUTRAL), roi).mismatch
    assert N.light_stats(img, lit(img, 4.0, 1.0, NEUTRAL), roi).ratio > 2
    # a tone curve that leaves the MEAN alone still moves the dark and the bright end
    assert N.light_stats(img, lit(img, 2.0, 2.0, NEUTRAL), roi).mismatch
    # the caller knows the light state differs even when the pixels do not say so
    assert N.light_stats(img, img, roi, light_differs=True).mismatch
    assert "light state differs" in N.light_stats(img, img, roi, light_differs=True).why


def test_a_part_changes_the_texture_and_a_gain_does_not() -> None:
    base = frame("k1_z125_clear_ref.jpg")
    roi = np.ones(base.shape[:2], bool)
    part = base.copy()
    part[180:204, 300:324] = (20, 20, 20)
    assert N.structure_changed(base, lit(base, 0.5, 2.0, CAST), roi, 3).sum() < 50
    changed = N.structure_changed(base, lit(part, 0.5, 2.0, CAST), roi, 3)
    assert (
        changed[180:204, 300:324].sum() > 100
    )  # the rim and the flat inside of the part


def test_confirm_keeps_a_colour_region_only_where_the_texture_changed() -> None:
    colour = np.zeros((60, 80), bool)
    colour[10:30, 10:30] = True  # a colour change with texture evidence around it
    colour[40:55, 50:70] = True  # a glare: colour only
    texture = np.zeros_like(colour)
    texture[10:30, 10:30] = True
    texture[12:28, 12:28] = (
        False  # a flat part has structure at its rim only: filled by `confirm`
    )
    kept = N.confirm(colour, texture)
    assert kept[10:30, 10:30].all()
    assert not kept[40:55, 50:70].any()


def test_no_roi_is_no_evidence() -> None:
    img = frame("k1_z125_clear_ref.jpg")
    none = np.zeros(img.shape[:2], bool)
    assert not N.structure_changed(img, img, none).any()
    assert not N.light_stats(img, img, none).mismatch


# ---- an empty bed under another light is never occupied -----------------------------------------


@pytest.mark.parametrize("light", LIGHTS)
@pytest.mark.parametrize("name", ["k1_z125_clear_ref.jpg", "k1_z125_clear_later.jpg"])
def test_the_empty_k1_bed_in_any_light_is_never_occupied(name: str, light: Any) -> None:
    v = k1(name, light)
    assert v.bed != "occupied", (v.reason, v.metrics)


@pytest.mark.parametrize("light", MODERATE)
@pytest.mark.parametrize("name", ["k1_z125_clear_ref.jpg", "k1_z125_clear_later.jpg"])
def test_the_empty_k1_bed_stays_clear_in_a_moderate_light_change(
    name: str, light: Any
) -> None:
    v = k1(name, light)
    assert v.bed == "clear", (v.reason, v.metrics)


def test_k1_daylight_reference_against_the_dark_room_is_clear_through_the_normalised_channel() -> (
    None
):
    """The fixture pair of the incident: the same empty K1 bed, daylight and a dark room."""
    v = k1("k1_z125_clear_later.jpg")
    assert v.bed == "clear"
    assert not N.light_stats(
        k1_ref().image, frame("k1_z125_clear_later.jpg"), k1_ref().volume
    ).mismatch or (v.metrics.get("light_mismatch") == 1.0)


@pytest.mark.parametrize("light", LIGHTS)
@pytest.mark.parametrize("name", ["u1_z200_clear_1403.jpg", "u1_z200_clear_1426.jpg"])
def test_the_empty_u1_bed_in_any_light_is_never_occupied(name: str, light: Any) -> None:
    v = u1(name, light)
    assert v.bed != "occupied", (v.reason, v.metrics)


@pytest.mark.parametrize("light", MODERATE)
@pytest.mark.parametrize(
    "name",
    ["u1_z200_clear_1403.jpg", "u1_z200_clear_1405.jpg", "u1_z200_clear_1412.jpg"],
)
def test_the_empty_u1_bed_at_a_low_view_in_a_moderate_light_change_is_clear_or_names_the_best_view(
    name: str, light: Any
) -> None:
    """The U1 at Z 200 sees 15 % of the plate: the room behind it is most of the ROI, so the normalised
    channel may rescue a false `occupied` there but not call the bed clear (`unknown` + the way out)."""
    v = u1(name, light)
    assert v.bed in ("clear", "unknown"), (v.reason, v.metrics)
    if v.bed == "unknown":
        assert "3d printer bed check u1" in v.reason


@pytest.mark.parametrize("light", MODERATE)
def test_the_empty_u1_bed_at_a_good_view_stays_clear_in_a_moderate_light_change(
    light: Any,
) -> None:
    """Z 10 after a G28: 98 % of the plate in the frame, the same empty bed under another light."""
    ref = u1_z10_ref()
    v = B.classify(lit(ref.image, *light), [ref], 10.0, "u1", match=u1_match())
    assert v.bed == "clear", (v.reason, v.metrics)
    assert v.metrics["light_mismatch"] == 1.0


@pytest.mark.parametrize("light", [None, *LIGHTS])
@pytest.mark.parametrize("name", ["u1_z200_light_1703.jpg", "u1_z200_light_2013.jpg"])
def test_the_window_light_frames_in_any_light_are_never_occupied(
    name: str, light: Any
) -> None:
    """The preflight incident (U1 Z 200.2, 15 % of the plate in view): never occupied, never a FAIL."""
    v = u1_window(name, light)
    assert v.bed != "occupied", (v.reason, v.metrics)


# ---- a real part is still found, in any light --------------------------------------------------


@pytest.mark.parametrize("light", [None, *LIGHTS])
@pytest.mark.parametrize("size", [24, 16])
def test_a_part_on_the_k1_bed_is_found_in_any_light(size: int, light: Any) -> None:
    img = frame("k1_z125_clear_ref.jpg").copy()
    img[180 : 180 + size, 300 : 300 + size] = (20, 20, 20)
    v = B.classify(
        img if light is None else lit(img, *light), [k1_ref()], 125.0, "k1se"
    )
    assert v.bed == "occupied", (v.reason, v.metrics)


@pytest.mark.parametrize(
    "name",
    ["u1_occupied_1351.jpg", "u1_occupied_1400.jpg", "u1_spaghetti_printing.jpg"],
)
@pytest.mark.parametrize(
    "light", [None, (0.7, 1.0, NEUTRAL), (0.5, 1.0, CAST), (1.0, 0.5, NEUTRAL)]
)
def test_the_u1_parts_and_the_spaghetti_are_found_in_a_changed_light(
    name: str, light: Any
) -> None:
    v = u1(name, light)
    assert v.bed == "occupied", (v.reason, v.metrics)


def test_parts_the_colour_difference_alone_would_report_in_another_light_are_unknown_not_occupied() -> (
    None
):
    """The decision itself: the plain colour difference on a gamma-2 frame of the empty K1 bed is a region (the
    bright plate edge), the texture did not change: unknown at worst, and the verdict says why."""
    img = lit(frame("k1_z125_clear_later.jpg"), 2.0, 2.0, NEUTRAL)
    plain = B.classify(
        img, [k1_ref()], 125.0, "k1se", dataclasses.replace(B.TH, light_norm=0)
    )
    assert plain.bed == "occupied"  # the old classifier, teeth
    v = B.classify(img, [k1_ref()], 125.0, "k1se")
    assert v.bed in ("clear", "unknown"), v.reason
    assert v.metrics["light_mismatch"] == 1.0
    if v.bed == "unknown":
        assert "the light, not a part" in v.reason or "light differs" in v.reason


def test_the_light_norm_threshold_is_a_per_printer_switch() -> None:
    from printer_ops import bed as pbed
    from test_bed_state import device

    cfg = pbed.bed_config(
        device(watch={"bed": {"enabled": True, "thresholds": {"light_norm": 0}}})
    )
    assert B.thresholds_for(cfg).light_norm == 0
