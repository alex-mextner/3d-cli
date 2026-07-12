"""Unit tests for the perceptual metric battery (lib/perceptual_metrics.py).

PSNR has a pure numpy core with EXACT known answers (identical -> capped, a max-range
difference -> 0 dB, a unit offset -> ~48.13 dB). LPIPS / CLIP need heavy wheels; when
absent they must raise a structured MissingDependency (NOT a fabricated score), and the
battery must record them as unavailable while still returning the PSNR it could compute.
"""
from __future__ import annotations

import importlib.util
import math

import pytest

np = pytest.importorskip("numpy")

import perceptual_metrics as pm  # noqa: E402
from errors import MissingDependency  # noqa: E402


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def test_psnr_identical_is_capped() -> None:
    img = np.zeros((8, 8, 3))
    result = pm.psnr_arrays(img, img)
    assert result["value"] == pm.PSNR_CAP_DB
    assert result["capped"] is True
    assert result["sense"] == "higher_better"


def test_psnr_max_difference_is_zero_db() -> None:
    black = np.zeros((8, 8, 3))
    white = np.full((8, 8, 3), 255.0)
    assert pm.psnr_arrays(black, white)["value"] == pytest.approx(0.0)


def test_psnr_unit_offset_is_known_db() -> None:
    black = np.zeros((8, 8, 3))
    off_by_one = black + 1.0  # MSE = 1 -> 10*log10(255^2) ~= 48.13 dB
    assert pm.psnr_arrays(black, off_by_one)["value"] == pytest.approx(48.13, abs=0.01)


def test_psnr_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        pm.psnr_arrays(np.zeros((8, 8, 3)), np.zeros((4, 4, 3)))


def test_lpips_missing_wheel_raises_structured_error() -> None:
    if _installed("lpips"):
        pytest.skip("lpips installed; degrade path not exercised")
    with pytest.raises(MissingDependency) as exc:
        pm.lpips_distance("/tmp/does-not-matter-a.png", "/tmp/does-not-matter-b.png")
    assert exc.value.exit_code == 127
    assert "lpips" in exc.value.install


def test_clip_missing_wheel_raises_structured_error() -> None:
    if _installed("open_clip"):
        pytest.skip("open_clip installed; degrade path not exercised")
    with pytest.raises(MissingDependency) as exc:
        pm.clip_similarity("/tmp/does-not-matter-a.png", "/tmp/does-not-matter-b.png")
    assert exc.value.exit_code == 127
    assert "open_clip" in exc.value.install


def test_battery_records_unavailable_metrics_without_faking(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Force LPIPS/CLIP to look absent so the honest-degrade branch is covered
    # regardless of the host, and stub PSNR so no image I/O is needed.
    def _fake_psnr(a: str, b: str) -> dict:
        return {"value": 42.0, "sense": "higher_better", "unit": "dB", "capped": False}

    def _missing_lpips(a: str, b: str, **_: object) -> dict:
        raise MissingDependency("lpips", install="pip install lpips torch pillow",
                                degrades="x", command="metrics")

    def _missing_clip(a: str, b: str, **_: object) -> dict:
        raise MissingDependency("open_clip_torch", install="pip install open_clip_torch torch pillow",
                                degrades="x", command="metrics")

    monkeypatch.setattr(pm, "psnr_images", _fake_psnr)
    monkeypatch.setattr(pm, "lpips_distance", _missing_lpips)
    monkeypatch.setattr(pm, "clip_similarity", _missing_clip)

    report = pm.perceptual_battery("/a.png", "/b.png")
    assert report["psnr"]["available"] is True
    assert report["psnr"]["value"] == 42.0
    assert report["lpips"]["available"] is False
    assert "install" in report["lpips"]
    assert report["clip"]["available"] is False
    # senses are preserved even for unavailable channels (the store footgun guard).
    assert report["lpips"]["sense"] == "lower_better"
    assert report["clip"]["sense"] == "higher_better"


def test_battery_unknown_metric_raises() -> None:
    with pytest.raises(ValueError):
        pm.perceptual_battery("/a.png", "/b.png", metrics=("bogus",))


def test_parse_psnr_maps_inf_to_cap() -> None:
    assert pm._parse_psnr("inf") == pm.PSNR_CAP_DB
    assert pm._parse_psnr("0 (0)") == pytest.approx(0.0)
    assert math.isfinite(pm._parse_psnr("31.5 (0.9)"))
