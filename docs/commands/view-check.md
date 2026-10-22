# `3d view-check` — does the fit explain the other views?

Takes a `fit.json` from [fit-silhouette](fit-silhouette.md), renders it with the **independent** OpenCV rasteriser (not the soft torch one that
optimised it) into extra views, and compares each rendering with that view's mask.

**Why it exists.** A silhouette fit always reproduces the photos it was trained on; that proves nothing about 3-D. Flat plates and boxes fit
every training view at IoU ~0.89 and still predict an unseen view at IoU 0.2–0.5 (RESULT §4c). The held-out view IoU is the only honest confidence
signal, and a low one tells the user *which photo to take next*.

## Usage

```
3d view-check <fit.json> [--view MASK:CAM.json ...] [--flag-below 0.75] [--overlay out.png] [--json]
```

`MASK:CAM.json` is the same as in `fit-silhouette` (`CAM.json` = `{"K","R","t"}` world→camera mm, or `{"K","pose":"identity"}`); the world is the
frame of the fit's cameras. `--overlay` writes one review image per view stacked into one PNG (mask contour green, rendered fit red).

| Exit | Meaning |
|---|---|
| 0 | `ok` (every extra view ≥ threshold) or `unverified` (single-photo mode) |
| 3 | `flagged`: a view is below `--flag-below`, or the model failed a sanity check |
| 2 | bad input (missing/malformed fit or view) |

```bash
3d fit-silhouette --view top.png:cam.json --planar --known-depth 1184 -o fit/
3d view-check fit/fit.json --view side.png:cam_side.json --overlay check.png --json
```

## Output (`--json`)

```json
{"mode": "heldout", "views": [{"name": "side.png", "iou": 0.62, "flag": true, "azimuth_deg": 83.0, "elevation_deg": 12.0, "distance_mm": 650.0}],
 "min_iou": 0.62, "flag_below": 0.75, "status": "flagged",
 "needs_views": [{"azimuth_deg": 0.0, "elevation_deg": 90.0, "direction_object": [0, 0, 1], "reason": "...", "priority": 1}],
 "sanity": {"ok": true, "checks": [{"name": "thickness_prior", "ok": true, "value": 0.5, "detail": "..."}],
            "canonical": [{"name": "top", "azimuth_deg": 0, "elevation_deg": 90, "area_mm2": 6900.0, "extent_mm": [30.1, 240.9]}]}}
```

### Angles (`needs_views`, `views[*]`) — OBJECT frame

Azimuth is measured in the length(X)–width(Y) plane of the fitted object from +X toward +Y; elevation from that plane toward +Z, the thickness
axis. Elevation 90 = looking straight down the thickness axis (a top-down photo of a flat object), azimuth 90 / elevation 0 = side-on along the
width axis (shows the thickness profile), azimuth 0 / elevation 0 = end-on. `direction_object` is the unit vector from the object centre to the
camera in that frame. Suggestions (at most 3, `priority` 1 first), only when the status is not `ok`:

1. **top-down** for flat / box templates that no view has seen from above (|elevation| < 60) — thickness is unobserved;
2. the **canonical direction farthest from every view so far** (26-neighbourhood of the object frame; *d* and −*d* count as the same view);
3. the **opposite side of the best-fitting view** — lowest priority and honestly labelled: the orthographic silhouette from the opposite side is a mirror image and adds nothing, it only helps with perspective and self-occlusion, and "flip the object over" is the easiest photo to take.

## Single-photo mode (no `--view`) — what it can and cannot prove

With one photo there is **no independent evidence about the hidden 3-D shape**; `view-check` says so: `mode` is `single_photo` and `status` is
`unverified`, never `ok`. It then *sanity-checks the fitted model alone*, rendering it from four canonical synthetic cameras (top, side, end,
three-quarter; weak perspective) and testing:

| Check | Fails when |
|---|---|
| `canonical_views_nondegenerate` | a canonical silhouette is < 0.3 % of the largest (a sliver, NaN, collapsed profile) |
| `thickness_prior` | thickness/width is outside the template's prior band (÷3.5…×3.5 of lathe 1.0, flat 0.22, box 0.5; lathe must be exactly round). Skipped when the user set the thickness |
| `fill_ratio` | top-view area / (length × max width) is outside 0.2…1.1 — length, width and profile disagree |

In single-photo mode a failed check turns the status into `flagged` (exit 3). What this **cannot** show: that the thickness, the roll about the axis, a concavity on
the hidden side or the overall depth ordering match the real object — those need a second view. `needs_views` always lists which one.
With extra views the same sanity block is reported too, but only the per-view IoU decides `ok` / `flagged`.

Python: `from threed_vision.view_check import check, render_overlays; check(fit, views, flag_below=0.75)` (`views=[]` = single-photo mode).
