# `3d calibrate-grid` - camera and wall plane from the honeycomb panel

Finds the honeycomb cell centres in a wall photo, labels them on the HSW lattice (20.4382 x 23.6 mm) and solves the
pinhole camera (focal length, rotation, translation) plus the photo-px -> wall-mm homography. Numpy + OpenCV (+ scipy's
KD-tree) only: **no GPU, no model**. (`3d calibrate` is the *printer* calibration command, hence the name.)

The algorithm lives in `packages/threed-vision` (`threed_vision.calibrate`); the command is a thin launcher
(`lib/commands/calibrate_grid.py` -> `python -m threed_vision calibrate-grid`).

## Usage

```bash
3d calibrate-grid <image> [-o DIR] [--grid-pitch COLxROW] [--f-px N | --f-ratio R] [--time-budget S] [--json]
```

| Option | Default | What |
|---|---|---|
| `-o, --out DIR` | off | Write `DIR/calibration.json` and `DIR/overlay.jpg` (green = detected cells, red = reprojected lattice) |
| `--grid-pitch COLxROW` | `20.4382x23.6` | Column x row pitch in mm (the HSW panel) |
| `--f-px N` | estimate | Known focal length in px (EXIF / earlier calibration): only R and t are solved |
| `--f-ratio R` | estimate | Known focal length / image diagonal (the HSW phone is 0.375) |
| `--time-budget S` | `8` | Wall-clock budget: the lattice search stops at the deadline, so a photo without a grid ends with `reason: time_budget` instead of searching on |
| `--json` | off | Print exactly one JSON object (the calibration) |

Exit codes: `0` grid found, `2` bad input (missing/undecodable image), `3` no grid found / poor fit (with `--json` the
JSON with `"ok": false` and a `reason` is still printed).

```bash
3d calibrate-grid wall.jpg --json
3d calibrate-grid wall.heic -o calib/ --f-px 720.8
```

## What it does

1. **Cell centres** (`find_cell_centres`): a difference-of-Gaussians band-pass tuned to the cell size (blob scale = row
   pitch in px / ~4.8), extrema with sub-pixel parabola refinement. Recessed cells are dark blobs; light cells are the
   fallback polarity. The scale is searched (`sigma` 3, 2, 4.2, 6, 8.5, 12) until a strong lattice is found, then
   re-detected at the scale the lattice implies.
2. **Panel ROI**: only extrema whose six nearest neighbours are near-equidistant at the dominant spacing (the periodic
   texture) seed the lattice, so screws, holders, clutter and the room never do.
3. **Lattice labelling** (`solve_grid_pose`): seeded region growing - a 3-point local lattice hypothesis, homography
   fit, bounded extrapolation, iterate - from up to 40 seeds; the best labelling wins. Cells are then re-collected with
   the refined model (12 % of the spacing) so the whole panel edge is used.
4. **Pose + focal**: square pixels, central principal point, one unknown focal. Coarse f from a scan over the
   homography, then Huber-weighted Levenberg-Marquardt over `f, R, t` minimising the reprojection of all inliers.
5. **Labelling convention**: the lattice has 6-fold symmetry, so the labelling with the grid `+y` closest to image-up is
   chosen; handedness is fixed (wall seen from the front).

It rejects chance lattices: median reprojection must be <= max(1.5 px, 5 % of the cell spacing), the inliers must fill
>= 40 % of the nodes inside their hull, the cells must be >= 6 px, and the *wall-junction* lattice of a honeycomb (a
triangular lattice with one node in three missing, 1/sqrt(3) of the pitch) is refused (`vertex_lattice`).

## Grid frame and cell labels

Same as hsw-editor `packages/core/src/grid.ts`: millimetres, x right, **y up**, z out of the wall towards the viewer;
cell `(col, row)` is centred at `((col + 0.5) * 20.4382, (row + 0.5 + 0.5 * (col & 1)) * 23.6)`. A photo cannot tell
where the board corner is, so the labelling origin is arbitrary: the lowest-left detected inlier cell is `(0, 0)`
(`min col = min row = 0`). Anchor the labels to the real board in the UI.

Camera: `X_cam = R @ X_grid + t_mm` (OpenCV camera, x right, y down, z forward), `K = [[f,0,w/2],[0,f,h/2],[0,0,1]]`.
`homography_grid_mm_to_px = K [r1 r2 t]` (normalised `h33 = 1`); `homography_px_to_grid_mm` is its inverse, row-major
3x3 - what hsw-editor stores as `Calibration.homography`.

## Output (`--json`, and the dict returned by `detect_grid`)

| Key | Meaning |
|---|---|
| `ok`, `reason` | `reason` is `null` when ok; otherwise `too_few_cells`, `no_lattice`, `degenerate_lattice`, `poor_fit`, `sparse_lattice`, `cells_too_small`, `vertex_lattice`, `implausible_pose`, `time_budget` (all numeric fields are then `null`) |
| `K`, `f_px`, `f_source` | intrinsics; `f_source` = `given` / `estimated` / `default` |
| `R`, `t_mm` | grid frame -> camera |
| `homography_grid_mm_to_px`, `homography_px_to_grid_mm` | 3x3, row-major |
| `reproj_median_px`, `reproj_p95_px` | reprojection of the inlier cells with the final model |
| `n_cells`, `n_inliers`, `coverage` | detected centres, labelled cells, inliers / nodes inside their hull |
| `inlier_cells_px`, `inlier_cells` | Nx2 pixel centres and the matching `[col, row]` labels |
| `mm_per_px_centre`, `plane_depth_mm` | wall scale and plane depth at the centroid of the cells |
| `tilt_deg`, `roll_deg`, `roi_xywh` | plane tilt, roll, bounding box of the cells |
| `f_rel_uncertainty` | relative uncertainty of an *estimated* focal length (see below) |
| `quality` | flags: `f_estimated`, `f_weak`, `f_unobservable_prior_used`, `few_cells`, `near_fronto_parallel` |
| `polarity`, `sigma`, `timing_s` | which detector found it, and timings |

### The focal length is weakly observable

A single wall plane only reveals the focal length through the ~0.03 %/px perspective gradient of the lattice scale.
Measured on IMG_4937 (tilt 9.4 degrees): changing the detector blob scale from 2.2 to 3.4 px moves f from 796 to 649 px
while the reprojection median stays 0.39-0.41 px, and fixing the HSW reference f = 720.82 px gives 0.404 px median. So
`f_rel_uncertainty = max(noise std, 3e-5 / g)` (`g` = log-scale gradient per px, a heuristic calibrated on those
sweeps) says ~10 % there, `quality` gets `f_weak`, and when it exceeds 15 %, or the grid spans < 20 % of the image
diagonal, the default `0.375 * diagonal` is used instead (`f_source: "default"`, `f_unobservable_prior_used`). **The
wall-plane homography and the mm/px are not affected** (they come from the lattice, not from f); only the camera
distance and the other-plane depths scale with f. If you know f, pass `f_px`.

## Python API (the calls the vision service should make)

```python
from threed_vision import io
from threed_vision.calibrate import detect_grid, px_to_grid_mm, grid_mm_to_px, local_mm_per_px

rgb = io.load_rgb("IMG_4937.jpeg")                    # upright RGB uint8 (EXIF/HEIC handled)
cal = detect_grid(rgb, time_budget_s=8.0)                                 # f estimated; detect_grid(rgb, f_px=720.8) when EXIF/calibration known
if not cal["ok"]:
    ...                                                # cal["reason"]; never raises on "no grid", never NaN
H_px_to_mm = cal["homography_px_to_grid_mm"]           # 3x3 -> hsw-editor Calibration.homography
wall_mm = px_to_grid_mm(H_px_to_mm, [[x, y], ...])    # photo px -> wall mm (grid frame, y up)
px = grid_mm_to_px(cal["homography_grid_mm_to_px"], wall_mm)
mm_per_px = local_mm_per_px(H_px_to_mm, (x, y))        # wall mm per pixel at a pixel
```

`solve_grid_pose(points_px, *, image_size, grid_pitch_mm=..., f_px=None, f_ratio=None)` is the same solver from a plain
list of cell centres (what `find_cell_centres(rgb)["points"]` returns); `tests/data/vision/IMG_4937_cell_centres.json`
is a recorded example.

## Measured (IMG_4937.jpeg, 1152 x 1536, reference `refit/photo_calibration.json`)

| | this detector | reference |
|---|---|---|
| cells used | 432 (of 1379 candidate extrema) | 283 inliers of 300 |
| reprojection median / p95 | 0.39 / 1.07 px | 0.436 / 1.037 px |
| focal | 756.6 px (+5.0 %, `f_rel_uncertainty` 0.096) | 720.82 px |
| plane tilt | 9.4 degrees | 8.7 degrees |

Projecting the same lattice cells with both cameras (nearest-cell labelling: my cell -> reference object plane -> nearest
reference lattice node): median 0.09 px, max 0.32 px. Run time 0.1-0.3 s on the full photo. Photos without a grid (measured after the budget change): the carpet HEIC (2000x1500, carpet texture) returns `ok: false, sparse_lattice` in 1.9 s, IMG_4934 in 1.0 s; the search is capped at `time_budget_s` (default 8 s; `reason: time_budget`).

Other photos: IMG_4935 (room, no panel) -> `ok: false` (`poor_fit`); IMG_4936 (the panel sticks into the top-right
corner, ~70 cells over 150 x 150 px) -> `ok: true`, median 0.34 px, f falls back to the default (footprint too small).

## Limits

- Square pixels, centred principal point, no lens distortion; a strongly distorted lens needs a prior undistort.
- Needs >= 12 cells; at least ~30 for a trustworthy pose. The focal length needs a tilted view (see above).
- A recessed (dark cell) or printed light-cell panel photographed in focus; cells smaller than ~6 px are refused.
