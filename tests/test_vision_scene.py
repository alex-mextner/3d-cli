"""Bed-change detectors of `3d printer vision` (lib/vision_scene.py, issue
https://github.com/alex-mextner/3d-cli/issues/150): a foreign object or a moved part must persist
before it is reported, the toolhead volume is ignored, a Z change invalidates the reference, and
the experimental growth check flags a top band that looks like the empty space above it."""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")

import numpy as np  # noqa: E402

from printer_ops.vision import INFO, WARNING  # noqa: E402
from vision_scene import GrowthWatch, SceneParams, SceneWatch  # noqa: E402

H, W = 360, 640


def _scene() -> np.ndarray:
    img = np.full((H, W, 3), 60, np.uint8)  # dark bed
    img[150:260, 250:400] = (40, 160, 40)  # the printed part (green)
    return img


AREA = np.zeros((H, W), np.uint8)
AREA[120:, :] = 255  # bed + part volume
SIL = np.zeros((H, W), np.uint8)
SIL[140:270, 240:410] = 255  # expected part silhouette


def _run(
    watch: SceneWatch,
    frames: list[tuple[float, float, np.ndarray]],
    exclude: np.ndarray | None = None,
) -> list[list[str]]:
    return [
        [f.kind for f in watch.update(img, z, t, AREA, SIL, exclude)]
        for t, z, img in frames
    ]


def _baseline(z: float = 10.0) -> list[tuple[float, float, np.ndarray]]:
    return [(t, z, _scene()) for t in range(0, 601, 60)]


def test_a_new_object_on_the_bed_is_reported_once_it_persists() -> None:
    tool = _scene()
    tool[300:330, 60:200] = (220, 220, 220)  # a scraper left on the bed
    out = _run(
        SceneWatch(), _baseline() + [(610 + i * 10, 10.0, tool) for i in range(5)]
    )
    assert all(k == [] for k in out[:-5])
    # the 3-frame median sees the object from the 2nd frame; then `persist` checks in a row
    assert out[-5:-2] == [[], [], []]
    assert out[-2] == ["vision_foreign_object"]


def test_a_part_that_moved_is_reported_as_part_moved() -> None:
    moved = np.full((H, W, 3), 60, np.uint8)
    moved[180:290, 300:450] = (40, 160, 40)  # knocked sideways and down
    out = _run(
        SceneWatch(), _baseline() + [(610 + i * 10, 10.0, moved) for i in range(4)]
    )
    assert "vision_part_moved" in out[-1]


def test_changes_inside_the_toolhead_volume_are_ignored() -> None:
    head = _scene()
    head[300:330, 60:200] = (220, 220, 220)
    exclude = np.zeros((H, W), np.uint8)
    exclude[280:350, 40:220] = 255
    out = _run(
        SceneWatch(),
        _baseline() + [(610 + i * 10, 10.0, head) for i in range(5)],
        exclude,
    )
    assert all(k == [] for k in out)


def test_no_reference_from_another_z() -> None:
    tool = _scene()
    tool[300:330, 60:200] = (220, 220, 220)
    out = _run(
        SceneWatch(), _baseline(z=5.0) + [(610 + i * 10, 10.0, tool) for i in range(5)]
    )
    assert all(
        k == [] for k in out
    )  # the bed moved 5 mm: the old frames are not comparable


def test_growth_watch_flags_a_band_that_looks_like_the_empty_space_above() -> None:
    band = np.zeros((H, W), np.uint8)
    band[125:148, 250:400] = 255
    below = np.zeros((H, W), np.uint8)
    below[170:250, 250:400] = 255
    above = np.zeros((H, W), np.uint8)
    above[90:120, 250:400] = 255
    gw = GrowthWatch()
    growing = _scene()
    growing[125:148, 250:400] = (40, 160, 40)
    assert [gw.update(growing, z, band, below, above) for z in (10, 11, 12, 13)] == [
        []
    ] * 4
    stalled = (
        _scene()
    )  # band shows the dark background, the part stopped at the old height
    found = [gw.update(stalled, z, band, below, above) for z in (14, 15, 16)]
    assert found[:2] == [[], []]
    (f,) = found[2]
    assert (f.kind, f.severity) == ("vision_no_growth", INFO)
    assert f.data["band_part_share"] < 0.35


def test_findings_are_warnings_for_the_bed_and_info_for_growth() -> None:
    tool = _scene()
    tool[300:330, 60:200] = (220, 220, 220)
    sw = SceneWatch()
    for t, z, img in _baseline():
        sw.update(img, z, t, AREA, SIL)
    found = [sw.update(tool, 10.0, 610 + i * 10, AREA, SIL) for i in range(4)][-1]
    assert [f.severity for f in found] == [WARNING]
    assert 0 < found[0].data["bed_share"] < 0.2


def _creeping_part(minutes: float) -> np.ndarray:
    """The part lifted one pixel row every 5 minutes (a corner curling off the plate)."""
    img = np.full((H, W, 3), 60, np.uint8)
    top = 200 - int(minutes / 5)
    img[top:300, 250:400] = (40, 160, 40)
    return img


def _creep(params: SceneParams) -> list[tuple[float, list[str]]]:
    area = np.zeros((H, W), np.uint8)
    area[120:, :] = 255
    sil = np.zeros((H, W), np.uint8)
    sil[140:320, 240:410] = 255
    watch = SceneWatch(params)
    out = []
    for i in range(0, 91):  # 90 minutes, one frame a minute
        t = i * 60.0
        found = watch.update(_creeping_part(t / 60), 10.0, t, area, sil)
        out.append((t, [f.kind for f in found]))
    return out


def test_a_slow_lift_that_never_changes_within_12_minutes_is_still_reported() -> None:
    out = _creep(SceneParams())
    hits = [t for t, kinds in out if "vision_part_moved" in kinds]
    assert hits, "a part that crept 18 rows in 90 min was never reported"
    assert hits[0] >= 15 * 60  # only the slow reference (15-60 min old) can see it


def test_without_the_slow_reference_the_same_creep_is_invisible() -> None:
    # Regression for the 2026-10-22 U1 case: the 3-12 min reference absorbs a gradual lift.
    blind = SceneParams(slow_min_frames=10_000)
    assert all(kinds == [] for _, kinds in _creep(blind))
