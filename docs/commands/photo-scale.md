# `3d photo-scale` - millimetres per pixel, plane depth and intrinsics for a photo

Turns one photo into a physical scale by one of four routes: a **reference object** of known length, **two points** a
known distance apart, the **honeycomb grid** on the wall, or **another photo** of the same plane whose scale is known.
Numpy + OpenCV only: no GPU, no model. Segmentation, the closed-form profile fit and the silhouette fit all need mm/px;
the plane depth (`f_px * mm_per_px`) places objects for the multi-view check.

The algorithm lives in `packages/threed-vision` (`threed_vision.calibrate`); the command is a thin launcher.

## Usage

```bash
3d photo-scale <image> (--ref INSTANCE=MM | --points X1,Y1,X2,Y2 --distance MM | --grid honeycomb
                        | --homography-to OTHER.jpg --other-mm-per-px X|--other-grid honeycomb)
               [--instances FILE] [--calibration FILE] [--at X,Y] [--f-px N | --f-ratio R] [-o FILE] [--json]
```

| Source / option | What |
|---|---|
| `--ref INSTANCE=MM` | Object of known longest dimension. `INSTANCE` is an instance id of `--instances` (the `instances.json` written by `3d segment`) or a mask PNG path; `MM` its length. Pixel length = long side of the minimum-area rectangle of the mask (+1 px) |
| `--points X1,Y1,X2,Y2` + `--distance MM` | Two pixel points a known distance apart |
| `--grid honeycomb` | Wall-plane scale from the honeycomb panel: runs the [`calibrate-grid`](calibrate-grid.md) detector, or reuses `--calibration FILE` (a `calibrate-grid` `calibration.json`, or an HSW `refit/photo_calibration.json`) |
| `--homography-to OTHER.jpg` | [`register`](register.md) this photo to a second photo of the same plane and transfer its scale: needs `--other-mm-per-px X` (uniform scale) or `--other-grid honeycomb` (OTHER shows the panel; perspective-correct) |
| `--at X,Y` | `--grid` / `--homography-to`: the pixel where the scale is wanted (default: centroid of the grid cells / image centre) |
| `--f-px N`, `--f-ratio R` | Focal length in px, or as a fraction of the diagonal (default **0.375**, the calibrated HSW phone). With `--grid` and neither, f is estimated |
| `-o, --out FILE` | Also write the JSON here |
| `--json` | Print exactly one JSON object |

Exit codes: `0` ok, `2` bad input (unknown instance, missing `--distance`/`--instances`, ...), `3` grid not found /
photos not registrable.

```bash
3d photo-scale tools.jpg --ref 82=239.4 --instances seg/instances.json --json
3d photo-scale tools.jpg --points 120,340,980,360 --distance 300 --f-px 937.5
3d photo-scale wall.jpg --grid honeycomb --json
3d photo-scale wall.jpg --grid honeycomb --calibration refit/photo_calibration.json
3d photo-scale a.jpg --homography-to b.jpg --other-grid honeycomb
3d photo-scale a.jpg --homography-to b.jpg --other-mm-per-px 2.0 --at 600,450 --f-ratio 0.4
```

## Output

```json
{"mm_per_px": 1.263, "plane_depth_mm": 1184.0, "K": [[937.5, 0, 1000], [0, 937.5, 750], [0, 0, 1]], "f_px": 937.5,
 "f_source": "default", "source": "ref|points|grid|homography", "quality": ["fronto_parallel_assumed", "f_default_ratio"],
 "length_px": 189.5, "image_size": [2000, 1500], "reg": {"matches": 1853, "inliers": 1828, "median_reproj_px": 0.045,
 "rel_rot_deg": 0.0, "translation_ratio": 0.0, "baseline_ok": false}}
```

`reg` appears for `--homography-to` only; `at_px` for `--grid` / `--homography-to`; `length_px` for `--ref`/`--points`.
`plane_depth_mm = f_px * mm_per_px` (pinhole; for the grid route it is the true camera-frame depth of the plane at `at_px`).

## Assumptions (also in `quality`)

- `--homography-to ... --other-grid`: **only `mm_per_px` is perspective-correct** (it comes from the composed homography at `--at`); `plane_depth_mm` there is still `f * mm_per_px`, i.e. assumes the plane is fronto-parallel at that pixel.
- `--grid --calibration FILE` uses the calibration's own focal length (`--f-px/--f-ratio` only feed the detector when no `--calibration` is given). The HSW `photo_calibration.json` route reports `f_source: "given"`.

- **Fronto-parallel** (`--ref`, `--points`, `--homography-to`): the measured segment / plane is parallel to the image plane.
  A segment foreshortened by a tilt of *a* degrees is under-measured by `1 - cos a`: 3.4 % at 15 degrees, 13 % at 30 degrees.
  Flag `fronto_parallel_assumed`.
- **Pinhole, centred principal point, no distortion.** Default `f = 0.375 * diagonal` (flag `f_default_ratio`); EXIF or a
  grid calibration (`f_given`) is better. The focal length only moves `plane_depth_mm`, not `mm_per_px`.
- The scale holds for things lying **in that plane** (`plane_of_grid_only`, `plane_of_overlap_only`); a tall object's
  top is nearer the camera than the plane.
- `short_reference`: the reference spans < 2 % of the image diagonal (1 px error is > 1 % of the scale at 50 px).
- `mask_touches_border`: the reference object is cut by the frame, its length is under-estimated.
- `same_viewpoint` (`--homography-to`): the second photo adds no parallax; the scale transfer is still valid.

## Python API (the calls the vision service should make)

```python
from threed_vision import io
from threed_vision.calibrate import (default_intrinsics, detect_grid, scale_from_grid, scale_from_points,
                                     scale_from_reference)

rgb = io.load_rgb("tools.jpg"); h, w = rgb.shape[:2]

# 1. reference object: a boolean HxW mask (e.g. a Segmenter instance["mask"]) + its real longest dimension
s = scale_from_reference(mask, 239.4, image_size=(w, h), f_px=None, f_ratio=0.375)
# 2. two clicked points
s = scale_from_points((120, 340), (980, 360), 300.0, image_size=(w, h), f_px=937.5)
# 3. honeycomb grid (also accepts an HSW photo_calibration.json dict)
s = scale_from_grid(detect_grid(rgb), at_px=None)
# every result: s["mm_per_px"], s["plane_depth_mm"], s["K"] (3x3), s["f_px"], s["f_source"], s["source"], s["quality"]
K = default_intrinsics(w, h, f_px=None, f_ratio=0.375)   # EXIF-less fallback K
```

`scale_from_homography(H_px_to_mm, image_size=..., at_px=None, f_px=None, f_ratio=0.375)` is the fourth route (the registered-photo transfer: pass `H_other_px_to_mm @ H_a_to_b`); `scale_from_grid(cal, at_px=None, image_size=(w, h))` rejects a calibration made on a photo of another size. `scale_from_points` / `scale_from_reference` raise `BadInput` (exit 2 in the CLI) for a zero/negative length, a non-finite
point, an empty mask or a mask whose shape differs from `image_size`.
