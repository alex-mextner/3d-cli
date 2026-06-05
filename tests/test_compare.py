"""Unit tests for the reliable compare pipeline (lib/refmatch.py + lib/commands/compare.py).

Pure metric math + the <50% warning logic are tested on synthetic data with NO
external binaries. The ImageMagick / OpenSCAD-touching paths are gated behind
shutil.which so the suite passes on a machine without them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import refmatch  # noqa: E402
from cli.imaging import score_metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure metric math (no binaries).
# --------------------------------------------------------------------------- #
def test_score_metrics_perfect_overlap():
    # identical masks: intersection == union -> IoU 1.0
    m = score_metrics(inter=0.4, union=0.4, ae=0.0, area=1000)
    assert m["IoU"] == pytest.approx(1.0)


def test_score_metrics_no_overlap():
    m = score_metrics(inter=0.0, union=0.5, ae=500.0, area=1000)
    assert m["IoU"] == pytest.approx(0.0)


def test_score_metrics_blank_render_scores_zero():
    # union == 0 means a blank frame -- must never be rewarded.
    m = score_metrics(inter=0.0, union=0.0, ae=0.0, area=1000)
    assert m["IoU"] == 0.0
    assert m["CLOSENESS"] == 0.0


def test_score_metrics_partial():
    m = score_metrics(inter=0.25, union=0.5, ae=0.0, area=1000)
    assert m["IoU"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Degenerate-fit rejection (pure).
# --------------------------------------------------------------------------- #
def test_is_degenerate_collapsed():
    # silhouette fills (almost) the whole frame -> degenerate (camera collapsed).
    assert refmatch.is_degenerate(0.99) is True


def test_is_degenerate_speck():
    # silhouette is a speck -> degenerate (model pushed away).
    assert refmatch.is_degenerate(0.02) is True


def test_is_degenerate_sane():
    assert refmatch.is_degenerate(0.40) is False
    # boundary values are inclusive of the sane band.
    assert refmatch.is_degenerate(refmatch.MIN_SILHOUETTE_FRAC) is False
    assert refmatch.is_degenerate(refmatch.MAX_SILHOUETTE_FRAC) is False


def test_unreliable_threshold_constant():
    # The contract: IoU < 0.50 is unreliable.
    assert refmatch.UNRELIABLE_IOU == 0.50


# --------------------------------------------------------------------------- #
# compare metric parsing (pure).
# --------------------------------------------------------------------------- #
def test_parse_metric_lone_number():
    # A build that prints just the normalized value.
    assert refmatch._parse_metric("0.83712") == pytest.approx(0.83712)


def test_parse_metric_takes_parenthesized_normalized_value():
    # Real IM output is `RAW (NORMALIZED)` -- the [0,1] value is in parentheses,
    # NOT the leading raw quantum sum.
    assert refmatch._parse_metric("6756.61 (0.103099)") == pytest.approx(0.103099)


def test_parse_metric_identical_images():
    assert refmatch._parse_metric("0 (0)") == pytest.approx(0.0)


def test_parse_metric_empty_raises():
    with pytest.raises(refmatch.MagickError):
        refmatch._parse_metric("   ")


# --------------------------------------------------------------------------- #
# CompareResult.reliable wiring + warning logic.
# --------------------------------------------------------------------------- #
def _result(iou: float, *, fallback: bool = False) -> refmatch.CompareResult:
    return refmatch.CompareResult(
        iou=iou,
        ssim=0.5,
        dssim=0.5,
        mask_png="/tmp/x/mask.png",
        matched_render_png="/tmp/x/matched_render.png",
        diff_png="/tmp/x/diff.png",
        collage_png="/tmp/x/collage.png",
        used_fallback=fallback,
        fallback_reason="degenerate" if fallback else "",
        reliable=iou >= refmatch.UNRELIABLE_IOU,
    )


def test_reliable_flag_below_threshold():
    assert _result(0.49).reliable is False


def test_reliable_flag_at_threshold():
    assert _result(0.50).reliable is True


def test_compare_command_warns_when_unreliable(capsys, monkeypatch):
    """The command must print a WARNING (naming what to check) when IoU < 0.50,
    and not warn when reliable. We stub the pipeline + require_magick so no
    binaries are touched."""
    import commands.compare as compare_cmd

    monkeypatch.setattr(compare_cmd, "require_magick", lambda *a, **k: "magick")

    # Two synthetic input files so the existence check passes.
    import tempfile

    d = tempfile.mkdtemp()
    model = os.path.join(d, "m.scad")
    ref = os.path.join(d, "r.jpg")
    open(model, "w").close()
    open(ref, "w").close()

    # --- unreliable case ---
    monkeypatch.setattr(refmatch, "compare_pipeline",
                        lambda *a, **k: _result(0.20, fallback=True))
    rc = compare_cmd.run([model, ref])
    out = capsys.readouterr().out
    assert rc == 0
    assert "IoU=0.2000" in out
    assert "WARNING" in out
    assert "UNRELIABLE" in out
    assert "mask" in out.lower()  # names the mask as a thing to check

    # --- reliable case: no warning ---
    monkeypatch.setattr(refmatch, "compare_pipeline", lambda *a, **k: _result(0.80))
    rc = compare_cmd.run([model, ref])
    out = capsys.readouterr().out
    assert rc == 0
    assert "IoU=0.8000" in out
    assert "WARNING" not in out


def test_compare_command_prints_machine_parseable_keys(capsys, monkeypatch):
    import commands.compare as compare_cmd

    monkeypatch.setattr(compare_cmd, "require_magick", lambda *a, **k: "magick")
    monkeypatch.setattr(refmatch, "compare_pipeline", lambda *a, **k: _result(0.90))

    import tempfile

    d = tempfile.mkdtemp()
    model = os.path.join(d, "m.scad")
    ref = os.path.join(d, "r.jpg")
    open(model, "w").close()
    open(ref, "w").close()

    compare_cmd.run([model, ref])
    out = capsys.readouterr().out
    keys = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    for required in ("IoU", "SSIM", "DSSIM", "MASK", "MATCHED_RENDER", "DIFF", "COLLAGE", "FALLBACK"):
        assert required in keys, f"missing {required} in output"


def test_compare_command_missing_input_raises(monkeypatch):
    import commands.compare as compare_cmd
    from errors import InputNotFound

    with pytest.raises(InputNotFound):
        compare_cmd.run(["/nonexistent/model.scad", "/nonexistent/ref.jpg"])


def test_compare_usage_has_why_and_example():
    import commands.compare as compare_cmd

    assert "WHY" in compare_cmd.USAGE
    assert "Example" in compare_cmd.USAGE


# --------------------------------------------------------------------------- #
# ImageMagick-gated smoke: real IoU on synthetic masks (skipped without magick).
# --------------------------------------------------------------------------- #
def _magick_bin():
    return shutil.which("magick") or shutil.which("convert")


@pytest.mark.skipif(_magick_bin() is None, reason="ImageMagick not installed")
def test_silhouette_iou_identical_shapes(tmp_path):
    mgk = _magick_bin()
    # A render whose "silhouette" is a white disc on the OpenSCAD BG colour.
    # The render silhouette is a non-background (gray) disc on the OpenSCAD BG;
    # the silhouette mask = "everything that is NOT the BG colour".
    render = str(tmp_path / "render.png")
    subprocess.run(
        [mgk, "-size", "200x200", f"xc:{refmatch.BG}",
         "-fill", "gray", "-draw", "circle 100,100 100,40", render],
        check=True,
    )
    # The subject mask = the same disc, white on black.
    mask = str(tmp_path / "mask.png")
    subprocess.run(
        [mgk, "-size", "200x200", "xc:black",
         "-fill", "white", "-draw", "circle 100,100 100,40", mask],
        check=True,
    )
    iou = refmatch.silhouette_iou(render, mask, str(tmp_path))
    assert iou > 0.95  # identical shapes -> near-perfect IoU


@pytest.mark.skipif(_magick_bin() is None, reason="ImageMagick not installed")
def test_silhouette_iou_disjoint_shapes(tmp_path):
    mgk = _magick_bin()
    render = str(tmp_path / "render.png")
    subprocess.run(
        [mgk, "-size", "200x200", f"xc:{refmatch.BG}",
         "-fill", "gray", "-draw", "circle 50,50 50,20", render],
        check=True,
    )
    mask = str(tmp_path / "mask.png")
    subprocess.run(
        [mgk, "-size", "200x200", "xc:black",
         "-fill", "white", "-draw", "circle 150,150 150,180", mask],
        check=True,
    )
    iou = refmatch.silhouette_iou(render, mask, str(tmp_path))
    assert iou < 0.10  # disjoint shapes -> near-zero IoU


@pytest.mark.skipif(_magick_bin() is None, reason="ImageMagick not installed")
def test_ssim_dssim_invariants(tmp_path):
    """The invariant unit tests structurally can't check (they don't run magick):
    identical images -> SSIM~=1 / DSSIM~=0; different images -> both in [0,1] and
    SSIM strictly below 1. Guards against a regression to broken native SSIM."""
    mgk = _magick_bin()
    a = str(tmp_path / "a.png")
    subprocess.run([mgk, "-size", "120x120", f"xc:{refmatch.BG}",
                    "-fill", "gray", "-draw", "circle 60,60 60,20", a], check=True)
    try:
        ssim_same, dssim_same = refmatch.ssim_dssim(a, a, str(tmp_path))
    except refmatch.MagickError as exc:
        msg = str(exc)
        if "DSSIM" in msg or "metric" in msg:
            pytest.skip(f"ImageMagick build does not support DSSIM: {msg}")
        raise
    assert dssim_same == pytest.approx(0.0, abs=1e-6)
    assert ssim_same == pytest.approx(1.0, abs=1e-6)

    b = str(tmp_path / "b.png")
    subprocess.run([mgk, "-size", "120x120", f"xc:{refmatch.BG}",
                    "-fill", "black", "-draw", "rectangle 10,10 110,110", b], check=True)
    ssim_diff, dssim_diff = refmatch.ssim_dssim(a, b, str(tmp_path))
    assert 0.0 <= ssim_diff < 1.0
    assert 0.0 < dssim_diff <= 1.0
    assert ssim_diff == pytest.approx(1.0 - dssim_diff)


@pytest.mark.skipif(_magick_bin() is None, reason="ImageMagick not installed")
def test_build_collage_three_panels(tmp_path):
    mgk = _magick_bin()
    paths = []
    for name in ("a.png", "b.png", "c.png"):
        p = str(tmp_path / name)
        subprocess.run([mgk, "-size", "60x60", "xc:gray", p], check=True)
        paths.append(p)
    out = refmatch.build_collage(paths[0], paths[1], paths[2], str(tmp_path))
    assert os.path.isfile(out)
    # 3 tiles wide -> width clearly exceeds a single panel.
    w = refmatch._identify_int(out, "%w")
    assert w > 120


def test_parse_metric_rejects_nonnumeric_imagemagick_errors() -> None:
    with pytest.raises(refmatch.MagickError, match="no numeric metric output"):
        refmatch._parse_metric("convert-im6.q16: unrecognized metric type `DSSIM'")
