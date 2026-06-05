"""Tests for preprocess_reference.py — the generic subject mask + depth pre-processor.

These tests exercise the real production code (no reimplementation):
  - the pure tier functions (mask_grabcut, depth_pseudo) on a tiny synthetic image,
    asserting they return solid 0/255 uint8 masks / 8-bit depth as the rest of the
    pipeline relies on;
  - the parser/help surface, asserting no user-facing text hardcodes a specific subject
    identity (ROADMAP §15: subject knowledge removed from core).
"""
from __future__ import annotations

import numpy as np
import pytest

# preprocess_reference imports cv2 + PIL at module load; both live in the OPTIONAL
# `preprocess` extra, not in the `dev`/test tier that `3d test` resolves. Skip the whole
# module when they are absent so the mandatory test gate stays green in a clean env.
pytest.importorskip("cv2")
pytest.importorskip("PIL")

import preprocess_reference as pr  # noqa: E402  (after the optional-dep guard)


def _synthetic_subject() -> np.ndarray:
    """A 64x64 RGB frame: dark background, a bright centered rectangle as the subject."""
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[16:48, 20:44] = 220  # a bright solid block roughly centered
    return rgb


def test_mask_grabcut_returns_binary_uint8() -> None:
    mask = pr.mask_grabcut(_synthetic_subject(), iters=3)
    assert mask.dtype == np.uint8
    assert mask.shape == (64, 64)
    # grabCut yields a 0/255 mask — never intermediate values.
    assert set(np.unique(mask)).issubset({0, 255})


def test_depth_pseudo_is_8bit_and_masked() -> None:
    rgb = _synthetic_subject()
    mask = np.zeros((64, 64), dtype=np.uint8)
    mask[16:48, 20:44] = 255
    depth = pr.depth_pseudo(rgb, mask)
    assert depth.dtype == np.uint8
    assert depth.shape == (64, 64)
    # background (outside the mask) must be black; subject must carry some depth.
    assert depth[0, 0] == 0
    assert depth[mask > 0].max() > 0


def test_mask_metadata_reports_bbox_centroid_and_coverage() -> None:
    mask = np.zeros((10, 12), dtype=np.uint8)
    mask[2:6, 3:9] = 255

    meta = pr.mask_metadata(mask)

    assert meta["coverage_pct"] == pytest.approx(20.0)
    assert meta["bbox_xywh"] == (3, 2, 6, 4)
    assert meta["centroid_xy"] == pytest.approx((5.5, 3.5))


def test_mask_metadata_handles_empty_mask() -> None:
    meta = pr.mask_metadata(np.zeros((8, 9), dtype=np.uint8))

    assert meta["coverage_pct"] == pytest.approx(0.0)
    assert meta["bbox_xywh"] is None
    assert meta["centroid_xy"] is None


def test_no_hardcoded_subject_in_user_facing_text() -> None:
    """The parser's description/help must not assert a single subject as THE subject.

    A concrete example is allowed only when clearly marked (e.g. ...), so we check the
    description line (the always-shown summary), which must stay subject-agnostic.
    """
    p = pr.build_parser()
    desc = (p.description or "").lower()
    for leaked in ("locomotive", "loco", "funnel", "boiler"):
        assert leaked not in desc, f"description leaks subject identity: {leaked!r}"
