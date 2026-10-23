"""Part-detached check of `3d printer vision` (lib/vision_detach.py).

Real evidence: tests/data/vision/u1_part_detached_layer17.jpg is the U1 camera frame of
2026-10-22 20:25 (job 00006E paused at layer 17/53): the honeycomb panel floats above the rim that
stayed on the plate, so bare plate shows where the near wall of the part should be. The "healthy"
twin used below is SYNTHETIC: the same frame with the expected wall band painted part-white (the
part still touching the plate); no real healthy frame with this lighting and a footprint exists.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import vision_geometry as vg  # noqa: E402
from printer_ops.vision import WARNING  # noqa: E402
from vision_detach import DetachParams, DetachWatch  # noqa: E402

DATA = Path(__file__).parent / "data" / "vision"


class _Scene:
    """The real frame plus the masks the live loop builds for it (bed Z 10.45: the pause lift;
    the part is 5.32 mm tall)."""

    def __init__(self) -> None:
        meta = json.loads((DATA / "u1_part_detached_layer17.json").read_text())
        img = cv2.imread(str(DATA / "u1_part_detached_layer17.jpg"))
        assert img is not None
        cam = vg.bundled_calibration("Snapmaker U1")
        assert cam is not None
        objs = [[(float(x), float(y)) for x, y in o] for o in meta["objects"]]
        top = [float(meta["part_height"])] * len(objs)
        shape = img.shape[:2]
        self.z = float(meta["bed_z"])
        self.detached = img
        self.wall = vg.wall_mask(cam, self.z, shape, objs, top)
        self.body = vg.silhouette_mask(cam, self.z, shape, objs, top, pad_mm=0)
        self.bed = vg.bed_mask(cam, self.z, shape, margin_mm=7.5)
        self.attached = img.copy()
        self.attached[self.wall > 0] = (235, 235, 235)  # synthetic: wall touches the plate

    def feed(self, watch: DetachWatch, frames: list[np.ndarray]) -> list[list[str]]:
        return [
            [f"{x.kind}:{x.severity}" for x in watch.update(img, self.z, self.wall, self.body, self.bed)]
            for img in frames
        ]


@pytest.fixture(scope="module")
def scene() -> _Scene:
    return _Scene()


def test_the_real_lifted_part_shows_bare_plate_in_the_wall_band(scene: _Scene) -> None:
    gap = DetachWatch().measure(scene.detached, scene.z, scene.wall, scene.body, scene.bed)
    assert gap is not None
    assert gap >= 0.4, gap  # measured 0.52 on the full-size frame


def test_the_attached_twin_has_no_gap(scene: _Scene) -> None:
    gap = DetachWatch().measure(scene.attached, scene.z, scene.wall, scene.body, scene.bed)
    assert gap is not None
    assert gap < 0.05, gap


def test_a_detached_part_is_a_warning_after_three_checks_and_critical_after_twelve(
    scene: _Scene,
) -> None:
    out = scene.feed(DetachWatch(), [scene.attached] * 4 + [scene.detached] * 14)
    assert out[:4] == [[]] * 4
    # the median of the last three needs two detached frames; then `persist` checks in a row
    assert out[4:7] == [[], [], []]
    assert out[7] == ["vision_part_detached:warning"]
    assert out[-1] == ["vision_part_detached:critical"]


def test_an_attached_part_stays_quiet(scene: _Scene) -> None:
    assert scene.feed(DetachWatch(), [scene.attached] * 20) == [[]] * 20


def test_a_gap_that_was_always_there_is_not_a_rise(scene: _Scene) -> None:
    # A concave part: its convex-hull footprint shows plate from the first frame. Only the
    # absolute rule can fire, and it is off here, so a stable gap is silent.
    watch = DetachWatch(DetachParams(gap_abs=1.0))
    assert scene.feed(watch, [scene.detached] * 20) == [[]] * 20


def test_the_absolute_rule_catches_a_part_that_lifted_before_any_baseline(scene: _Scene) -> None:
    out = scene.feed(DetachWatch(), [scene.detached] * 5)
    assert out[2] == ["vision_part_detached:warning"]


def test_the_finding_names_the_evidence(scene: _Scene) -> None:
    watch = DetachWatch()
    for img in [scene.detached] * 3:
        found = watch.update(img, scene.z, scene.wall, scene.body, scene.bed)
    (f,) = found
    assert f.kind == "vision_part_detached"
    assert f.severity == WARNING
    assert f.data["gap"] >= 0.4
    assert f.data["z"] == 10.45
    assert "detached" in f.text


def test_it_abstains_when_the_plate_cannot_be_told_from_the_part(scene: _Scene) -> None:
    grey = cv2.cvtColor(cv2.cvtColor(scene.detached, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    watch = DetachWatch()
    assert watch.measure(grey, scene.z, scene.wall, scene.body, scene.bed) is None
    assert scene.feed(watch, [grey] * 10) == [[]] * 10


def test_it_abstains_on_the_first_layers(scene: _Scene) -> None:
    assert DetachWatch().measure(scene.detached, 0.6, scene.wall, scene.body, scene.bed) is None


def test_it_abstains_without_plate_pixels_outside_the_part(scene: _Scene) -> None:
    nothing = np.zeros_like(scene.bed)
    assert DetachWatch().measure(scene.detached, scene.z, scene.wall, scene.body, nothing) is None


def test_a_toolhead_in_front_of_the_wall_is_not_plate(scene: _Scene) -> None:
    head = np.full_like(scene.wall, 255)  # everything masked: nothing left to judge
    assert DetachWatch().measure(scene.detached, scene.z, scene.wall, scene.body, scene.bed, head) is None


def test_reset_forgets_the_baseline(scene: _Scene) -> None:
    watch = DetachWatch(DetachParams(gap_abs=1.0))
    scene.feed(watch, [scene.attached] * 4)
    assert watch.base is not None and watch.base < 0.05
    watch.reset()
    assert watch.base is None and watch.streak == 0
    # fresh print: the rule has no baseline until it has seen a frame
    assert scene.feed(watch, [scene.detached] * 5) == [[]] * 5


def test_params_are_tunable() -> None:
    assert replace(DetachParams(), gap_abs=0.5).gap_abs == 0.5


def test_detach_setting_accepts_true_false_or_a_mapping() -> None:
    from errors import UsageError
    from vision_detect import detach_params

    assert detach_params(True) == DetachParams()
    assert detach_params(False) is None
    assert detach_params({"gap_abs": 0.4}) == DetachParams(gap_abs=0.4)
    with pytest.raises(UsageError):
        detach_params({"gap_absolute": 0.4})
    with pytest.raises(UsageError):
        detach_params("yes")


def test_the_print_geometry_runs_the_check_and_forgets_it_with_the_print(scene: _Scene) -> None:
    from printer_ops.vision import PrintContext
    from vision_detect import PrintGeometry

    cam = vg.bundled_calibration("Snapmaker U1")
    objs = tuple(tuple((float(x), float(y)) for x, y in o)
                 for o in json.loads((DATA / "u1_part_detached_layer17.json").read_text())["objects"])
    geo = PrintGeometry(cam, scene=False, growth=False, detach=DetachParams())
    ctx = PrintContext("part.gcode", 5.3, 30.0, 30.0, objs)
    geo.update(scene.detached, ctx, 0.0)
    assert geo.detach is not None
    assert geo.detach.last_gap is not None  # measured: the masks reached the detector
    geo.reset_print()
    assert geo.detach.last_gap is None
    assert PrintGeometry(cam, scene=False, growth=False).detach is None


def test_the_toolhead_and_roi_assumptions_are_per_printer_settings(scene: _Scene) -> None:
    from errors import UsageError
    from printer_ops.vision import PrintContext
    from vision_detect import GeometryParams, PrintGeometry, geometry_params

    assert geometry_params(None) == GeometryParams()
    assert geometry_params({"half_mm": 20.0}).half_mm == 20.0
    with pytest.raises(UsageError):
        geometry_params({"half": 20.0})
    cam = vg.bundled_calibration("Snapmaker U1")
    ctx = PrintContext("part.gcode", 5.3, 120.0, 120.0, ((( 20.0, 20.0), (60.0, 20.0), (60.0, 60.0)),))
    shape = scene.detached.shape[:2]
    small = PrintGeometry(cam, params=GeometryParams(half_mm=10.0)).masks(ctx, shape)["head"]
    big = PrintGeometry(cam, params=GeometryParams(half_mm=60.0)).masks(ctx, shape)["head"]
    assert int((big > 0).sum()) > int((small > 0).sum()) > 0


# ---- the uncalibrated fallback (FixedBedDetach), synthetic only ------------------------------

PLATE = (40, 110, 190)  # BGR: an orange-ish PEI plate
PART = (225, 225, 225)  # a white part
ROI = [(0.05, 0.3), (0.95, 0.3), (0.95, 0.95), (0.05, 0.95)]


def _bed(part_rows: tuple[int, int] = (200, 300), head: tuple[int, int] | None = None) -> np.ndarray:
    img = np.full((360, 640, 3), PLATE, np.uint8)
    img[part_rows[0] : part_rows[1], 200:420] = PART
    if head is not None:  # the toolhead, a dark block that is never twice in the same place
        img[120:260, head[0] : head[1]] = (50, 50, 50)
    return img




def _run_fixed(frames: list[np.ndarray], step: float = 20.0) -> list[list[tuple[str, str]]]:
    from vision_detach import FixedBedDetach

    watch = FixedBedDetach(ROI)
    return [[(f.kind, f.severity) for f in watch.update(img, i * step)] for i, img in enumerate(frames)]


def _learn_frames() -> list[np.ndarray]:
    return [_bed(head=(40 + 90 * i, 120 + 90 * i)) for i in range(6)]  # the head walks across the bed


def test_the_fixed_bed_fallback_learns_the_part_and_ignores_the_moving_head() -> None:
    out = _run_fixed(_learn_frames() + [_bed(head=(500, 600))] * 20)
    assert all(o == [] for o in out)


def test_the_fixed_bed_fallback_flags_a_part_that_left_its_place_as_low_confidence() -> None:
    lifted = _bed(part_rows=(150, 215))  # the part shifted up: most of its old place is plate
    out = _run_fixed(_learn_frames() + [_bed()] * 5 + [lifted] * 25)
    kinds = [o for o in out if o]
    assert kinds, "a part that vanished from its place was not reported"
    assert set(kinds[0]) == {("vision_part_detached", "warning")}
    assert all(sev == "warning" for batch in kinds for _, sev in batch)  # never critical: low confidence


def test_the_fixed_bed_fallback_abstains_on_a_grey_plate() -> None:
    grey = np.full((360, 640, 3), 120, np.uint8)
    grey[200:300, 200:420] = PART
    assert _run_fixed([grey] * 40) == [[]] * 40


def test_the_fixed_bed_setting_needs_a_plate_polygon_and_is_off_by_default() -> None:
    from errors import UsageError
    from vision_detect import fixed_bed_detach

    assert fixed_bed_detach(None, ROI) is None and fixed_bed_detach(False, ROI) is None
    assert fixed_bed_detach(True, [[0.1, 0.3], [0.9, 0.3], [0.9, 0.9]]) is not None  # the bed's roi
    with pytest.raises(UsageError):
        fixed_bed_detach(True, None)
    with pytest.raises(UsageError):
        fixed_bed_detach({"roi": [[2, 2], [3, 3], [4, 4]]}, None)
    with pytest.raises(UsageError):
        fixed_bed_detach({"nope": 1}, ROI)
