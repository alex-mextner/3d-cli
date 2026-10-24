"""3d measure-ref — measure feature shapes on a reference and on a render, gate the delta.

WHAT: for each feature in a JSON spec (an eye opening with its iris and pupil, a nostril, a pad ...) it measures
  corner-to-corner length, thickness, aspect, tilt (corner line and principal axis), extents, area, part sizes and
  eyelid coverage on the reference image and on the render, prints both, the delta and a PASS/FAIL per metric.

WHY: matching a figurine's eyes to a reference by eye is slow and unrepeatable; "tilt 19 deg vs 12, aspect 2.4 vs 1.8"
  says what to change, and the gate says when it is close enough. Extracted from the Chekhov cat's eye_measure.py.

Thin launcher: the algorithm is lib/ref_measure.py (numpy + scipy + Pillow), run through cli.pyrun.
"""

from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command

USAGE = """3d measure-ref <reference.png> <render.png> --spec FEATURES.json [options]
  Measure the same feature shapes on a reference image and on a render, print both with the delta, gate it.

Per feature (lengths in reference pixels):
  corner_len thick ar_axis   corner-to-corner length, thickness across the corner line, their ratio
  tilt pca_tilt pca_ar       tilt in degrees of the corner line / principal axis (outer corner higher = +, needs
                             inner_side), sqrt of the principal-axis eigenvalue ratio
  W H area                   x extent, y extent, filled area
  <part>_W <part>_H          extents of each named part (iris, pupil ...)
  cover_top cover_bot        share of the iris radius that the upper / lower lid covers, from the pupil centre
                             (only when the feature has parts named iris and pupil)

Spec (JSON): {"tolerance": {...}, "features": {NAME: {"inner_side": "left"|"right", "box": [x0,y0,x1,y1],
  "reference": SOURCE, "render": SOURCE, "parts": {"iris": {"reference": SOURCE, "render": SOURCE}}, ...}}}
  SOURCE is one of {"polygon": [[x,y],...]} | {"ellipse": [cx,cy,rx,ry,deg]} | {"mask": "m.png"} |
  {"colour": "#rrggbb", "tol": 30, "close": 6} | {"alpha": true}; "close" joins notches up to 2*N px wide (a pupil
  touching the lid colour in a flat render); optional per feature: "render_box", "corners", "tolerance".
  An empty reference shape is an input error (exit 2); an empty render shape is a FAIL (the feature is missing).

Options:
  --spec FILE            feature spec (required)
  --tol KEY=VAL          override a tolerance, repeatable: tilt=2 (absolute), ar_axis=10% (relative), iris_W=off.
                         Defaults: tilt 3, pca_tilt 4, ar_axis 15%, pca_ar 15%, iris_W 15%; other metrics are only reported
  --cand-scale S         multiply render lengths by S (areas by S*S) when the render's pixels are not the reference's
  --overlay FILE.png     reference | render crops per feature with the measured hull (red) and corners (yellow)
  -o, --out FILE.json    write the JSON report
  --json                 print the JSON report instead of the table

Exit: 0 PASS, 1 a metric is outside its tolerance or a feature is missing in the render, 2 usage / input error.

Examples:
  3d measure-ref tile.png render.png --spec eyes.json
  3d measure-ref tile.png render.png --spec eyes.json --tol tilt=2 --tol ar_axis=10%
  3d measure-ref tile.png render_2x.png --spec eyes.json --cand-scale 0.5 --overlay eyes_overlay.png
  3d measure-ref tile.png render.png --spec eyes.json --json -o eyes.json | jq '.features[].metrics.tilt.delta'"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return exec_tool("numpy,scipy,pillow", "ref_measure.py", argv)


COMMAND = Command(
    name="measure-ref",
    group="REFERENCE-MATCH PIPELINE",
    summary="measure feature shapes (tilt, aspect, iris, lids) on a reference and a render, gate the delta",
    usage=USAGE,
    run=run,
)
