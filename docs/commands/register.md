# `3d register` - register two photos of one scene

SIFT features + RANSAC give the **plane-induced homography** A -> B; it is decomposed into the relative camera
rotation / translation, masks of A are warped into B and, when B's masks are given, scored per instance (repeatability
IoU). Numpy + OpenCV only: no GPU, no model. A second photo is the held-out view of the multi-view check - and a second
photo at the *same* viewpoint must not be mistaken for one (`baseline_ok`).

The algorithm lives in `packages/threed-vision` (`threed_vision.register`); the command is a thin launcher.

## Usage

```bash
3d register <a.jpg> <b.jpg> [--mask-a MASKS] [--mask-b MASKS] [--f-px-a N] [--f-px-b N]
            [--plane-depth-mm D] [--flag-below IOU] [-o DIR] [--json]
```

| Option | Default | What |
|---|---|---|
| `--mask-a MASKS` | none | Masks in A to warp into B: an `instances.json` (from `3d segment`), a directory of `*.png`, or one PNG |
| `--mask-b MASKS` | none | Masks segmented in B (same formats): each warped A mask is scored by IoU with its best-overlapping B mask |
| `--f-px-a N`, `--f-px-b N` | `0.375 * diagonal` | Focal length of each photo in px (feeds the pose decomposition) |
| `--plane-depth-mm D` | none | Distance of the plane from camera A: scales `t_over_depth` to `t_mm` |
| `--flag-below IOU` | `0.75` | Repeatability IoU below this marks a mask `unstable` |
| `-o, --out DIR` | off | Write `DIR/registration.json` and `DIR/warped/NNN.png` (A masks in B, 1-based like `masks/NNN.png`) |
| `--json` | off | Print exactly one JSON object |

Exit codes: `0` registered (also when `baseline_ok` is false), `2` bad input, `3` the photos could not be registered
(`too_few_features`, `too_few_matches`, `no_homography`, `too_few_inliers`).

```bash
3d register a.jpg b.jpg --json
3d register a.jpg b.jpg --mask-a segA/instances.json --mask-b segB/instances.json -o reg/
```

## Output

| Key | Meaning |
|---|---|
| `ok`, `reason` | registration succeeded / why not |
| `matches`, `inliers`, `median_reproj_px` | ratio-test matches, RANSAC inliers (4 px), median reprojection of the inliers |
| `H_a_to_b` | 3x3 homography in full-resolution pixels |
| `rel_R`, `rel_rot_deg` | rotation of B w.r.t. A (orthonormalised) and its angle |
| `t_over_depth`, `translation_ratio`, `t_mm` | translation in units of the plane distance from A (`t_mm` with `--plane-depth-mm`) and its norm |
| `plane_normal`, `n_solutions` | the chosen of OpenCV's decompositions (most matches in front of both cameras) |
| `baseline_ok` | **false** when rotation < 3 degrees AND translation < 5 % of the plane depth: B adds no parallax |
| `quality` | `same_viewpoint`, `few_inliers`, `no_decomposable_translation` |
| `repeatability` | per A mask: `iou` (null without `--mask-b`), `best_b_index`, `unstable`, `area_a_px`, `warped_area_px`, `inside_b_frac` |

The homography is exact for **one plane** (the wall, the carpet); tall objects are only approximate, which is why a warped
mask's IoU against the independently segmented B mask is the honest measure of segmentation stability.

### The carpet pair

Carpet HEIC (2000 x 1500) vs IMG_4934: 1853 matches, 1828 inliers, 0.045 px median, 0.5 s on the full photos;
`rel_rot_deg` ~ 0, `translation_ratio` 0, `baseline_ok: false`. The spike reported a 0.94 degree rotation for this pair;
that was the angle computed from OpenCV's *non-orthonormal* `R` (diagonal 1.0000, 1.0001, 0.9996). The two photos differ by a
pure zoom about the principal point, i.e. the same viewpoint - the conclusion (no parallax) stands, the number does not.

## Python API (the call the vision service should make)

```python
from threed_vision import io
from threed_vision.register import register

a, b = io.load_rgb("a.jpg"), io.load_rgb("b.jpg")
reg = register(a, b, masks_a=[m1, m2], K_a=None, K_b=None, masks_b=[n1, n2, n3], plane_depth_mm=None)
if not reg["ok"]:                       # reg["reason"]; no exception for "cannot register"
    ...
reg["H_a_to_b"], reg["rel_R"], reg["t_over_depth"], reg["baseline_ok"], reg["translation_ratio"]
reg["warped_masks"]                     # list of bool HxW arrays (A masks in B's frame), same order as masks_a
reg["repeatability"]                    # [{"index", "iou", "best_b_index", "unstable", ...}]
```

`masks_a` / `masks_b` are lists (or 3-D arrays) of bool masks of the respective photo's size (`BadInput` otherwise);
`K_a` / `K_b` default to `default_intrinsics` (`f = 0.375 * diagonal`). Features are computed on a copy shrunk to 2000 px
(`max_side`), the homography is returned in full-resolution pixels.
