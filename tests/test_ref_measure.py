"""lib/ref_measure.py: shape measurement on synthetic ellipses with known tilt and aspect, and the delta gate."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import ref_measure as rm
from errors import GateFailure, UsageError
from PIL import Image, ImageDraw

SIZE = (160, 100)
BG = (200, 200, 200)
EYE = (0x28, 0x40, 0xA0)  # "#2840a0"


def ellipse_points(
    cx: float, cy: float, rx: float, ry: float, deg: float
) -> list[tuple[float, float]]:
    """Outline of an ellipse tilted `deg` counter-clockwise on screen (image y points down)."""
    th = math.radians(deg)
    pts = []
    for k in range(360):
        t = math.radians(k)
        a, b = rx * math.cos(t), ry * math.sin(t)
        pts.append(
            (
                cx + a * math.cos(th) - b * math.sin(th),
                cy - (a * math.sin(th) + b * math.cos(th)),
            )
        )
    return pts


def draw(
    path: Path,
    ellipses: list[tuple[float, float, float, float, float]],
    colour: tuple[int, int, int] = EYE,
    size: tuple[int, int] = SIZE,
) -> str:
    im = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(im)
    for e in ellipses:
        d.polygon(ellipse_points(*e), fill=colour)
    im.save(path)
    return str(path)


def spec_for(box: list[int] | None = None, **feature: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "inner_side": "left",
        "reference": {"colour": "#2840a0", "tol": 20},
        "render": {"colour": "#2840a0", "tol": 20},
    }
    if box is not None:
        base["box"] = box
    base.update(feature)
    return {"features": {"eye": base}, "base_dir": "."}


def measure(
    ref: str,
    cand: str,
    spec: dict[str, Any],
    tol: dict[str, tuple[str, float | None]] | None = None,
    scale: float = 1.0,
) -> dict[str, Any]:
    report, _, _ = rm.run_measure(ref, cand, spec, tol or {}, scale)
    return report


@pytest.mark.parametrize(
    ("tilt", "aspect"), [(0, 2.0), (15, 2.0), (-20, 3.0), (30, 1.6), (-8, 2.4)]
)
def test_known_tilt_and_aspect_are_measured_within_tolerance(
    tilt: float, aspect: float
) -> None:
    shape = rm.build_shape(
        {"ellipse": [60, 50, 24, 24 / aspect, tilt]}, _blank(), None, "t", "."
    )
    metrics, _, (inner, outer) = rm.measure_shape(shape, "left", None, "t")
    assert metrics["pca_tilt"] == pytest.approx(tilt, abs=0.5)
    assert metrics["tilt"] == pytest.approx(
        tilt, abs=1.0
    )  # corner line through the two tip centroids
    assert metrics["pca_ar"] == pytest.approx(aspect, rel=0.03)
    assert metrics["ar_axis"] == pytest.approx(aspect, rel=0.05)
    assert metrics["corner_len"] == pytest.approx(48.0, rel=0.02)
    assert inner[0] < outer[0]  # inner_side left: the inner corner is the left one


def test_image_masks_measure_the_same_as_the_analytic_ellipse(tmp_path: Path) -> None:
    path = draw(tmp_path / "e.png", [(80, 50, 30, 12, 17)])
    from_image = rm.build_shape(
        {"colour": "#2840a0", "tol": 20},
        rm.load_image(path),
        [20, 10, 140, 90],
        "t",
        ".",
    )
    from_ellipse = rm.build_shape(
        {"ellipse": [80, 50, 30, 12, 17]}, _blank(), None, "t", "."
    )
    a, _, _ = rm.measure_shape(from_image, "left", None, "t")
    b, _, _ = rm.measure_shape(from_ellipse, "left", None, "t")
    assert a["pca_tilt"] == pytest.approx(b["pca_tilt"], abs=0.7)
    assert a["pca_ar"] == pytest.approx(b["pca_ar"], rel=0.04)
    assert a["area"] == pytest.approx(
        b["area"], rel=0.08
    )  # PIL fills the boundary pixels it touches: ~7 % more than the exact area


def test_inner_side_decides_the_sign_of_the_tilt() -> None:
    shape = rm.build_shape({"ellipse": [60, 50, 24, 10, 20]}, _blank(), None, "t", ".")
    left, _, _ = rm.measure_shape(shape, "left", None, "t")
    right, _, _ = rm.measure_shape(shape, "right", None, "t")
    assert left["tilt"] == pytest.approx(
        20, abs=2.0
    )  # right end is higher and is the outer corner
    assert right["tilt"] == pytest.approx(
        -20, abs=2.0
    )  # now the higher end is the inner corner
    assert right["pca_tilt"] == pytest.approx(-left["pca_tilt"], abs=1e-9)


def test_matching_render_passes_the_gate(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    cand = draw(tmp_path / "cand.png", [(82, 51, 30, 12, 17)])  # shifted, same shape
    report = measure(ref, cand, spec_for([20, 10, 140, 90]))
    assert report["status"] == "PASS", report["features"]["eye"]["failures"]
    tilt = report["features"]["eye"]["metrics"]["tilt"]
    assert tilt["status"] == "PASS" and abs(tilt["delta"]) < 2.0


def test_wrong_tilt_fails_the_delta_gate(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    cand = draw(tmp_path / "cand.png", [(80, 50, 30, 12, 2)])  # 15 degrees flatter
    report = measure(ref, cand, spec_for([20, 10, 140, 90]))
    metrics = report["features"]["eye"]["metrics"]
    assert report["status"] == "FAIL"
    assert metrics["tilt"]["status"] == "FAIL"
    assert metrics["tilt"]["delta"] == pytest.approx(-15, abs=2.5)
    assert metrics["pca_tilt"]["delta"] == pytest.approx(-15, abs=1.0)
    assert metrics["ar_axis"]["status"] == "PASS"  # same aspect: only the tilt is wrong
    assert any(f.startswith("tilt:") for f in report["features"]["eye"]["failures"])


def test_wrong_aspect_fails_the_delta_gate(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 10, 12)])  # aspect 3
    cand = draw(tmp_path / "cand.png", [(80, 50, 30, 15, 12)])  # aspect 2
    report = measure(ref, cand, spec_for([20, 10, 140, 90]))
    metrics = report["features"]["eye"]["metrics"]
    assert report["status"] == "FAIL"
    assert metrics["pca_ar"]["status"] == "FAIL"
    assert metrics["tilt"]["status"] == "PASS"  # same tilt: only the aspect is wrong
    assert metrics["pca_ar"]["delta_pct"] == pytest.approx(-33, abs=4)


def test_tolerance_overrides_decide_the_verdict(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    cand = draw(tmp_path / "cand.png", [(80, 50, 30, 12, 11)])  # 6 degrees off
    spec = spec_for([20, 10, 140, 90])
    assert measure(ref, cand, spec)["status"] == "FAIL"  # default tilt tolerance 3
    assert (
        measure(ref, cand, spec, rm.parse_tolerance({"tilt": 9, "pca_tilt": 9}, "t"))[
            "status"
        ]
        == "PASS"
    )
    assert (
        measure(
            ref, cand, spec, rm.parse_tolerance({"tilt": "off", "pca_tilt": "off"}, "t")
        )["status"]
        == "PASS"
    )
    tight = measure(
        ref, cand, spec, rm.parse_tolerance({"tilt": 1, "pca_tilt": 1}, "t")
    )
    assert tight["status"] == "FAIL"


def test_render_without_the_feature_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    cand = draw(
        tmp_path / "cand.png", [(80, 50, 30, 12, 17)], colour=(250, 250, 10)
    )  # wrong colour: nothing selected
    report = measure(ref, cand, spec_for([20, 10, 140, 90]))
    assert report["status"] == "FAIL"
    assert any(
        "missing from the render" in f for f in report["features"]["eye"]["failures"]
    )


def test_empty_reference_is_an_input_error(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [], colour=EYE)
    cand = draw(tmp_path / "cand.png", [(80, 50, 30, 12, 17)])
    with pytest.raises(UsageError, match="reference shape is empty"):
        measure(ref, cand, spec_for([20, 10, 140, 90]))


def test_stray_pixels_outside_the_largest_component_do_not_move_the_corners(
    tmp_path: Path,
) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    clean = draw(tmp_path / "clean.png", [(80, 50, 30, 12, 17)])
    noisy = Image.open(clean)
    ImageDraw.Draw(noisy).rectangle(
        [22, 12, 23, 13], fill=EYE
    )  # a speck far from the eye, inside the box
    noisy.save(tmp_path / "noisy.png")
    report = measure(ref, str(tmp_path / "noisy.png"), spec_for([20, 10, 140, 90]))
    assert report["features"]["eye"]["metrics"]["corner_len"]["delta"] == pytest.approx(
        0, abs=0.5
    )


def test_cand_scale_puts_a_larger_render_into_reference_pixels(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    big = draw(tmp_path / "big.png", [(160, 100, 60, 24, 17)], size=(320, 200))
    spec = spec_for([20, 10, 140, 90], render_box=[40, 20, 280, 180])
    report = measure(ref, big, spec, scale=0.5)
    metrics = report["features"]["eye"]["metrics"]
    assert metrics["corner_len"]["delta_pct"] == pytest.approx(0, abs=3)
    assert metrics["area"]["delta_pct"] == pytest.approx(0, abs=6)
    unscaled = measure(ref, big, spec)["features"]["eye"]["metrics"]
    assert unscaled["corner_len"]["delta_pct"] == pytest.approx(100, abs=4)


def test_polygon_and_mask_sources_agree_with_the_colour_source(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    mask = Image.new("L", SIZE, 0)
    ImageDraw.Draw(mask).polygon(ellipse_points(80, 50, 30, 12, 17), fill=255)
    mask.save(tmp_path / "mask.png")
    outline = [[x - 0.5, y - 0.5] for x, y in ellipse_points(80, 50, 30, 12, 17)]
    spec = spec_for([20, 10, 140, 90])
    spec["base_dir"] = str(tmp_path)
    spec["features"]["eye"]["render"] = {"mask": "mask.png"}
    spec["features"]["eye"]["reference"] = {"polygon": outline}
    report = measure(ref, ref, spec)
    metrics = report["features"]["eye"]["metrics"]
    assert report["status"] == "PASS", report["features"]["eye"]["failures"]
    assert metrics["pca_tilt"]["render"] == pytest.approx(17, abs=1.0)
    assert metrics["pca_tilt"]["reference"] == pytest.approx(17, abs=1.0)


def test_alpha_source_reads_a_transparent_render(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    rgba = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(rgba).polygon(
        ellipse_points(80, 50, 30, 12, 17), fill=(255, 0, 0, 255)
    )
    rgba.save(tmp_path / "cand.png")
    spec = spec_for([20, 10, 140, 90], render={"alpha": True})
    report = measure(ref, str(tmp_path / "cand.png"), spec)
    assert report["status"] == "PASS", report["features"]["eye"]["failures"]


def test_iris_pupil_parts_report_sizes_and_lid_coverage(tmp_path: Path) -> None:
    # opening 40 x 20 centred (80, 50); iris disc clipped by the lids, centred 4 px above the opening's centre; pupil 6 x 12
    ref = tmp_path / "ref.png"
    cand = tmp_path / "cand.png"
    for path, iris_r in ((ref, 9), (cand, 6)):
        im = Image.new("RGB", SIZE, BG)
        lids = Image.new("L", SIZE, 0)
        ImageDraw.Draw(lids).polygon(ellipse_points(80, 50, 20, 10, 0), fill=255)
        eyeball = Image.new("RGB", SIZE, EYE)
        d = ImageDraw.Draw(eyeball)
        d.polygon(ellipse_points(80, 46, iris_r, iris_r, 0), fill=(10, 160, 10))
        d.polygon(ellipse_points(80, 46, 3, 6, 0), fill=(0, 0, 0))
        im.paste(eyeball, mask=lids)
        im.save(path)
    spec = spec_for(
        [20, 10, 140, 90],
        reference={"colour": ["#2840a0", "#0aa00a", "#000000"], "tol": 20},
        render={"colour": ["#2840a0", "#0aa00a", "#000000"], "tol": 20},
        parts={
            "iris": {
                "reference": {"colour": ["#0aa00a", "#000000"], "tol": 20},
                "render": {"colour": ["#0aa00a", "#000000"], "tol": 20},
            },
            "pupil": {
                "reference": {"colour": "#000000", "tol": 20},
                "render": {"colour": "#000000", "tol": 20},
            },
        },
    )
    report = measure(str(ref), str(cand), spec)
    metrics = report["features"]["eye"]["metrics"]
    assert metrics["iris_W"]["reference"] == pytest.approx(18, abs=1.5)
    assert metrics["iris_W"]["render"] == pytest.approx(12, abs=1.5)
    assert metrics["pupil_W"]["reference"] == pytest.approx(6, abs=1.5)
    assert metrics["pupil_H"]["reference"] == pytest.approx(12, abs=1.5)
    # pupil centre y=46, opening top y=40, iris radius 9: the upper lid covers 1 - 6/9 of the iris radius
    assert metrics["cover_top"]["reference"] == pytest.approx(1 - 6 / 9, abs=0.15)
    assert (
        metrics["iris_W"]["status"] == "FAIL"
    )  # the render's iris is a third too small
    assert report["status"] == "FAIL"


def test_spec_typos_are_errors_not_silent_no_ops(tmp_path: Path) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    spec = spec_for([20, 10, 140, 90])
    spec["features"]["eye"]["innerside"] = "left"
    with pytest.raises(UsageError, match="unknown key"):
        measure(ref, ref, spec)
    bad_source = spec_for([20, 10, 140, 90], render={"colour": "#2840a0", "colr": 1})
    with pytest.raises(UsageError, match="unknown source key"):
        measure(ref, ref, bad_source)
    two_kinds = spec_for([20, 10, 140, 90], render={"colour": "#2840a0", "alpha": True})
    with pytest.raises(UsageError, match="exactly one"):
        measure(ref, ref, two_kinds)


def test_load_spec_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"features": {"a": {}}, "tolerence": {}}))
    with pytest.raises(UsageError, match="unknown top-level"):
        rm.load_spec(str(path))


def test_main_raises_the_gate_failure_with_the_failing_feature_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = draw(tmp_path / "ref.png", [(80, 50, 30, 12, 17)])
    cand = draw(tmp_path / "cand.png", [(80, 50, 30, 12, -10)])
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"features": spec_for([20, 10, 140, 90])["features"]}))
    with pytest.raises(GateFailure, match=r"MEASURE-REF: FAIL — eye"):
        rm.main([ref, cand, "--spec", str(spec)])
    assert "tilt" in capsys.readouterr().out


def _blank() -> rm.Image:
    import numpy as np

    return rm.Image("blank", np.zeros((SIZE[1], SIZE[0], 4), dtype=np.uint8))


def test_explicit_corners_define_the_corner_line_and_its_length() -> None:
    outline = [(10, 40), (20, 31), (35, 28), (50, 30), (38, 38), (22, 43)]
    shape = rm.build_shape({"polygon": outline}, _blank(), None, "t", ".")
    corners = {"inner": [10, 40], "outer": [50, 30]}
    metrics, _, (inner, outer) = rm.measure_shape(shape, "left", corners, "t")
    assert metrics["corner_len"] == pytest.approx(math.hypot(40, 10), abs=1e-9)
    assert metrics["tilt"] == pytest.approx(math.degrees(math.atan2(10, 40)), abs=1e-9)  # outer corner is 10 px higher
    assert (inner, outer) == ((10.0, 40.0), (50.0, 30.0))
    flipped, _, _ = rm.measure_shape(shape, "right", {"inner": [50, 30], "outer": [10, 40]}, "t")
    assert flipped["tilt"] == pytest.approx(-metrics["tilt"], abs=1e-9)  # same line, the higher end is now the inner corner


def test_close_joins_a_notched_feature_that_colour_alone_leaves_open(tmp_path: Path) -> None:
    im = Image.new("RGB", SIZE, BG)
    d = ImageDraw.Draw(im)
    d.polygon(ellipse_points(80, 50, 30, 18, 0), fill=EYE)
    d.rectangle([76, 30, 83, 50], fill=BG)  # an 8 px wide notch cut in from the top, like a pupil touching the lid colour
    im.save(tmp_path / "notched.png")
    image = rm.load_image(str(tmp_path / "notched.png"))
    plain = rm.build_shape({"colour": "#2840a0", "tol": 20}, image, [20, 10, 140, 90], "t", ".")
    closed = rm.build_shape({"colour": "#2840a0", "tol": 20, "close": 6}, image, [20, 10, 140, 90], "t", ".")
    assert closed.mask.sum() > plain.mask.sum() + 100  # the notch is filled
    full = rm.build_shape({"ellipse": [80, 50, 30, 18, 0]}, _blank(), None, "t", ".")
    assert closed.mask.sum() == pytest.approx(full.mask.sum() / rm.ANALYTIC_SCALE**2, rel=0.1)
    with pytest.raises(UsageError, match="close must be"):
        rm.build_shape({"colour": "#2840a0", "close": -1}, image, None, "t", ".")
