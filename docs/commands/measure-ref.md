# `3d measure-ref` — measure feature shapes on a reference and on a render

Measures the same feature (an eye opening with its iris and pupil, a nostril, a toe bean ...) on
a reference image and on a render, prints both side by side with the delta, and gates the
delta. "Tilt 19 deg vs 12, aspect 2.4 vs 1.8, iris 24 px vs 22" says what to change in the
model; a PASS says it is close enough. Extracted from a figurine project's `eye_measure.py`
(hand-picked reference outlines versus flat-colour label renders in the same camera), generalised
to any feature and any pair of images.
Issue: [#551](https://github.com/alex-mextner/3d-cli/issues/551).

## Usage

```
3d measure-ref <reference.png> <render.png> --spec FEATURES.json [options]
```

| Option | Default | What |
|---|---|---|
| `--spec FILE` | required | Feature spec (below). |
| `--tol KEY=VAL` | see below | Override a tolerance, repeatable: `tilt=2` (absolute), `ar_axis=10%` (relative to the reference), `iris_W=off`. |
| `--cand-scale S` | `1` | Multiply render lengths by `S` (areas by `S*S`) when the render's pixels are not the reference's. |
| `--overlay FILE.png` | none | Reference \| render crops per feature with the measured hull (red) and corners (yellow). |
| `-o, --out FILE.json` | none | Write the JSON report. |
| `--json` | off | Print the JSON report instead of the table. |

```bash
3d measure-ref tile.png render.png --spec eyes.json
3d measure-ref tile.png render.png --spec eyes.json --tol tilt=2 --tol ar_axis=10%
3d measure-ref tile.png render_2x.png --spec eyes.json --cand-scale 0.5 --overlay eyes_overlay.png
3d measure-ref tile.png render.png --spec eyes.json --json -o eyes.json | jq '.features[].metrics.tilt.delta'
```

Exit: `0` PASS, `1` a metric is outside its tolerance or a feature is missing in the render,
`2` usage or input error (bad spec, unreadable image, a reference shape that selects nothing).

## Metrics

Lengths are in reference pixels. `tilt` is positive when the outer corner is higher.

| Metric | Meaning | Default gate |
|---|---|---|
| `corner_len` | length of the shape along the corner line | report only |
| `thick` | extent perpendicular to the corner line | report only |
| `ar_axis` | `corner_len / thick` | 15 % |
| `tilt` | angle of the corner line, degrees (needs `inner_side`) | 3 deg |
| `pca_tilt`, `pca_ar` | angle of the principal axis, sqrt of the eigenvalue ratio | 4 deg, 15 % |
| `W`, `H`, `area` | x extent, y extent, filled area | report only |
| `<part>_W`, `<part>_H` | extents of every named part (`iris`, `pupil` ...) | `iris_W` 15 %, others report only |
| `cover_top`, `cover_bot` | share of the iris radius covered by the upper / lower lid, measured from the pupil centre (only with parts named `iris` and `pupil`) | report only |

The corner line runs through the centroids of the two end regions of the shape (the outer
15 % along its major axis), not through the farthest pair of boundary points: on a pixel mask
the farthest pair jitters by 3-7 degrees, the end-region centroid by under 1. With explicit
`corners` the corner line is the one you picked and `corner_len` its length. `pca_tilt` is the
most stable angle; use it to cross-check `tilt`.

## Spec

```json
{
  "tolerance": {"tilt": 2},
  "features": {
    "eye_R": {
      "inner_side": "left",
      "box": [100, 60, 150, 95],
      "render_box": [100, 60, 150, 95],
      "reference": {"polygon": [[104, 84], [112, 72], [126, 68], [140, 72], [146, 80], [136, 88], [120, 90]]},
      "render": {"colour": ["#c8d0a0", "#0a0a0a", "#fafafa"], "tol": 24},
      "corners": {"reference": {"inner": [104, 84], "outer": [146, 80]}},
      "parts": {
        "iris":  {"reference": {"polygon": [[108, 70], [140, 70], [140, 90], [108, 90]]},
                  "render": {"colour": ["#c8d0a0", "#0a0a0a"], "tol": 24}},
        "pupil": {"reference": {"polygon": [[120, 70], [128, 70], [128, 90], [120, 90]]},
                  "render": {"colour": "#0a0a0a", "tol": 24}}
      }
    }
  }
}
```

A source is exactly one of:

| Source | Selects |
|---|---|
| `{"polygon": [[x, y], ...]}` | the polygon, absolute image pixels (a hand-picked outline) |
| `{"ellipse": [cx, cy, rx, ry, deg]}` | an ellipse, `deg` counter-clockwise on screen (optional, default 0) |
| `{"mask": "m.png"}` | white pixels of a mask the size of the image, inside the box (path relative to the spec) |
| `{"colour": "#rrggbb" or [...], "tol": 30, "close": 6}` | pixels within RGB distance `tol` of any colour, inside the box; transparent pixels never match. `close` N joins notches up to 2N px wide (a pupil that touches the lid colour in a flat render); keep `tol` small on flat renders, anti-aliased edges between two colours pass a loose one |
| `{"alpha": true}` | pixels with alpha > 127, inside the box |

Shapes taken from an image keep their largest connected component, so a stray pixel in the box
cannot stretch the corners. An empty reference shape is an input error (exit 2); an empty
render shape is a FAIL, because the feature is missing from the render. Unknown spec keys are
errors, not silent no-ops.

Tolerances: a number is absolute (degrees for tilts, pixels for lengths), `"15%"` is relative
to the reference value, `"off"` disables the gate. Order of precedence: defaults, the spec's
`tolerance`, the feature's `tolerance`, then `--tol`.

## Proof

The overlay (`--overlay`) shows the reference crop and the render crop of every feature with
the measured hull and corners drawn on, so a reviewer can see what was measured. Read it
together with the original reference and the render in the same camera: the numbers are only
as good as the shapes they were taken from.
