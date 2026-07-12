"""Unit tests for the opt-in view-bank pose-seed grid (fit_camera.viewbank_anchor_samples).

These exercise the pure branching (bbox-None fallback, 5- vs 6-parameter search space, and
bounds clamping) that the CLI e2e only covers end-to-end.
"""
from __future__ import annotations

import os
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

# fit_camera resolves an openscad path at IMPORT time and sys.exits if it is absent, so
# point OPENSCAD at any existing binary FOR THE DURATION OF THE IMPORT only. Restoring it
# immediately keeps an openscad-less CI able to import the pure sample-grid helper WITHOUT
# the stub leaking into os.environ — where it would be inherited by the real `bin/3d
# render` subprocesses that later e2e tests spawn (a python interpreter fed OpenSCAD's
# flags fails every render). Do NOT hoist this into a module-level setdefault: that mutates
# the process env for the whole pytest run.
_prev_openscad = os.environ.get("OPENSCAD")
os.environ["OPENSCAD"] = sys.executable

from fit_camera import viewbank_anchor_samples  # noqa: E402

if _prev_openscad is None:
    os.environ.pop("OPENSCAD", None)
else:
    os.environ["OPENSCAD"] = _prev_openscad

LO5 = [-180.0, -45.0, 50.0, -100.0, -100.0]
HI5 = [180.0, 85.0, 300.0, 100.0, 100.0]
LO6 = [-180.0, -45.0, 50.0, -100.0, -100.0, -100.0]
HI6 = [180.0, 85.0, 300.0, 100.0, 100.0, 100.0]


def _mask(nonzero: bool) -> object:
    arr = np.zeros((40, 60), dtype=np.uint8)
    if nonzero:
        arr[10:30, 15:45] = 1
    return arr


def test_grid_count_matches_az_times_el() -> None:
    seeds = viewbank_anchor_samples(LO5, HI5, 100.0, _mask(True), (60, 40))
    # az from -180..150 step 30 = 12 values; elevations = 4
    assert len(seeds) == 12 * 4


def test_five_param_samples_have_five_coords_all_in_bounds() -> None:
    seeds = viewbank_anchor_samples(LO5, HI5, 100.0, _mask(True), (60, 40))
    for sample in seeds:
        assert len(sample) == 5
        for value, lo, hi in zip(sample, LO5, HI5):
            assert lo <= value <= hi


def test_six_param_samples_have_six_coords_all_in_bounds() -> None:
    seeds = viewbank_anchor_samples(LO6, HI6, 100.0, _mask(True), (60, 40))
    for sample in seeds:
        assert len(sample) == 6
        for value, lo, hi in zip(sample, LO6, HI6):
            assert lo <= value <= hi


def test_empty_mask_falls_back_to_frame_center_without_error() -> None:
    seeds = viewbank_anchor_samples(LO5, HI5, 100.0, _mask(False), (60, 40))
    assert len(seeds) == 12 * 4
    # centered fallback -> pan hints ~0, still within bounds
    for sample in seeds:
        assert LO5[3] <= sample[3] <= HI5[3]


def test_distance_is_shared_across_all_seeds() -> None:
    seeds = viewbank_anchor_samples(LO5, HI5, 100.0, _mask(True), (60, 40))
    distances = {round(sample[2], 6) for sample in seeds}
    assert len(distances) == 1  # single fixed framing distance for the grid
