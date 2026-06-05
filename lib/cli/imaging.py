"""imaging.py — ImageMagick orchestration + the pure score math for the match pipeline.

The image diffing (silhouette mask build, overlay diagnostics, score IoU/AE) runs
ImageMagick as a subprocess — we never import a python imaging stack here, keeping this
module import-light. The NUMERIC core of scoring (IoU / AE-norm / closeness) is a pure
function (`score_metrics`) so it is unit-testable without ImageMagick.
"""
from __future__ import annotations

import subprocess

from cli.env import find_magick, magick_compare
from errors import GateFailure

# OpenSCAD default render background (srgb 255,255,229).
BG = "#ffffe5"


def run_magick(args: list[str], *, what: str) -> str:
    """Run `magick <args>`; return stdout. Raise GateFailure (exit 1) on failure."""
    mgk = find_magick()
    if mgk is None:  # callers should require_magick first; defensive.
        raise GateFailure(f"{what}: ImageMagick missing", silent=True)
    r = subprocess.run([mgk, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GateFailure(f"{what} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def magick_identify(path: str, fmt: str) -> str:
    mgk = find_magick()
    assert mgk is not None
    r = subprocess.run([mgk, "identify", "-format", fmt, path], capture_output=True, text=True)
    return r.stdout.strip()


def compare_ae(a: str, b: str, *, fuzz: str | None = None) -> str:
    """Run ImageMagick `compare -metric AE` and return its raw output (the AE count)."""
    mgk = find_magick()
    assert mgk is not None
    cmp_cmd = magick_compare(mgk)
    extra = ["-fuzz", fuzz] if fuzz else []
    r = subprocess.run(
        [*cmp_cmd, "-metric", "AE", *extra, a, b, "null:"],
        capture_output=True, text=True,
    )
    # `compare` writes the metric to stderr and exits nonzero when images differ — that
    # is expected, not an error. Take the first token of whichever stream carries it.
    out = (r.stderr or r.stdout).strip()
    return out


def score_metrics(inter: float, union: float, ae: float, area: int) -> dict[str, float]:
    """Pure: silhouette IoU / normalized-AE / closeness from raw measurements.

      inter, union : mean of the multiply/lighten composites (0..1 fractions of frame).
      ae           : absolute mismatched-pixel count.
      area         : frame pixel count (W*H).

    An empty union (blank render) scores IoU=0 — never reward a blank frame.
    """
    iou = (inter / union) if union > 0 else 0.0
    ae_norm = (ae / area) if area > 0 else 1.0
    closeness = iou
    if union <= 0:
        iou = 0.0
        closeness = 0.0
    return {"IoU": iou, "AE_NORM": ae_norm, "CLOSENESS": closeness}
