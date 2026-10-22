# `3d fit-silhouette` — lathe / flat loft / box from calibrated mask views

Fits a parametric tool model (a straight axis with 8 stations of width, a superellipse cross-section) and its 3-D pose to one or more
silhouettes whose cameras are known, and writes `fit.json` (+ review overlays, + a watertight mesh in mm). The package behind it is
`threed_vision` (`packages/threed-vision`); `3d fit-silhouette` is a thin launcher, the same code is what the hsw-editor vision service imports.

**Why it exists.** A photo of a tool on a table (or a few of them) should become a size and a printable proxy model without any AI model:
closed-form geometry gives the answer in milliseconds for most objects; the differentiable soft-silhouette refiner and CMA-ES only run when
the closed form does not already explain the views. Confidence is explicit: one photo can reproduce itself but proves nothing about 3-D
([view-check](view-check.md) says which photo to take next).

## Usage

```
3d fit-silhouette --view MASK:CAM.json [--view ...] [--heldout MASK:CAM.json] [options] -o DIR [--json]
```

`MASK` is a PNG (non-zero = object) or `instances.json#ID` (from `3d segment`). `CAM.json` is the camera of that view:
`{"K": [[fx,0,cx],[0,fy,cy],[0,0,1]], "R": [[..]], "t": [..]}` — world→camera, millimetres, OpenCV convention (x right, y down, z forward;
`X_cam = R·X_world + t`) — or `{"K": ..., "pose": "identity"}` for a camera at the world origin (the usual top-down photo: the object lies
at `+z = plane depth`). `K` is in the pixels of the full-resolution mask.

| Option | Default | What |
|---|---|---|
| `--view MASK:CAM.json` | required, repeatable | training view |
| `--heldout MASK:CAM.json` | none | scored, **never fitted**: makes `confidence.heldout_iou` |
| `--template auto\|lathe\|flat\|box` | `auto` | by closed-form kind: box → box (p=8), cylinder_capsule / lathe_profile / ring_lathe → lathe (p=2), loft_flat → flat (p=4) |
| `--planar` | off | the object lies on a plane parallel to the image (top-down photo): in-plane rotation + shift only |
| `--known-depth MM` | none | camera z-depth of the object; **required for one view** (it defines the size), e.g. from `calibrate.scale_from_reference(...)["plane_depth_mm"]` |
| `--fixed-length-mm` `--fixed-width-mm` `--fixed-thickness-mm` | none | user overrides = **hard constraints**, exact in the result (not penalties) |
| `--iou-accept` | `0.9` | per-view hard-raster IoU at which the closed-form model is returned with no optimiser (`0` = closed form only; a held-out view below 0.75 still triggers the refiner) |
| `--min-confidence` | `0.8` | train IoU below this → `status: low`, exit 3 (`min_iou` in Python) |
| `--max-tries` | `3` | soft-fit restarts from the next-best pose candidate |
| `--stations` | `8` | profile stations (`K` in Python; `fit-profile` calls the same number `--k`) |
| `--device auto\|cuda\|cpu` | `auto` | refiner device: GPU when torch sees one with ≥ 700 MB free, otherwise (or if the GPU run fails) CPU; `cuda` is strict |
| `--no-cma` | off | never run the CMA-ES second attempt |
| `--seed` | `0` | seed of every random path (CMA-ES, torch) |
| `--emit glb\|stl\|obj\|scad` | none | also write `model.<ext>` (needs `-o`) |
| `--mesh-frame object\|world` | `object` | `object`: centred at the origin, X = length; `world`: the fit pose baked in |
| `-o, --out DIR` | none | `fit.json`, `overlay-N.png` (mask green, fit red), the mesh |
| `--json` | off | print exactly one JSON object (the `fit.json` content) |

```bash
# one top-down photo; the caliper (239.4 mm) fixed the scale => plane depth 1184 mm
3d fit-silhouette --view masks/046.png:cam_top.json --planar --known-depth 1184 --emit glb -o fit/046 --json | jq '.dims_mm, .confidence'
# two oblique photos fitted, a third one held out as the check
3d fit-silhouette --view a.png:cam_a.json --view b.png:cam_b.json --heldout c.png:cam_c.json -o fit/tool
# the user typed the real length: it wins exactly
3d fit-silhouette --view a.png:cam_a.json --planar --known-depth 1184 --fixed-length-mm 238 -o fit/tool
```

Exit codes: `0` ok / `single_view`, `3` low confidence (`fit.json` is still written and printed), `2` bad input (empty/1-pixel mask, bad `K/R/t`,
single view without `--known-depth`, cameras with no parallax, …), `127` missing dependency (torch / cma / trimesh are only needed when the
refiner, the CMA second attempt or a `glb/stl/obj` export actually runs).

## `fit.json` — the stable schema

```json
{"kind": "box|cylinder_capsule|lathe_profile|loft_flat|ring_lathe",
 "template": {"name": "lathe|flat|box", "p": 2.0, "stations": 8, "ring": 12},
 "params": {"L_mm": 188.6, "c_mm": [K floats], "w_mm": [K floats], "aspect": 1.0},
 "dims_mm": {"length_mm": 188.6, "max_width_mm": 22.0, "thickness_mm": 22.0},
 "pose": {"R0": [[3x3]], "t_mm": [x, y, z], "axis": [x, y, z]},
 "fixed_dims": null,
 "views": [{"name": "mask.png", "mask": "mask.png", "iou": 0.93, "azimuth_deg": 0.0, "elevation_deg": 90.0, "distance_mm": 1184.0}],
 "heldout_views": [{"name": "c.png", "iou": 0.88}],
 "confidence": {"train_min_iou": 0.93, "heldout_iou": null, "n_views": 1, "status": "ok|single_view|low"},
 "method": "closed_form|soft|cma", "tries": 0, "device": null,
 "timing_s": {"init": 0.03, "candidates": 0.02, "refine": 0.0, "cma": 0.0, "total": 0.06},
 "attempts": [{"method": "closed_form", "train_min_iou": 0.93, "train_mean_iou": 0.93, "heldout_iou": null, "time_s": 0.05}],
 "closed_form": {"kind": "lathe_profile", "views": [{"kind": "lathe_profile", "iou_recon": 0.98}], "iou_accept": 0.9},
 "mesh": "model.glb"}
```

### How the service maps `params` to lathe / loft / box

The object-local frame: **X along the length (centred at 0), Y across the width, Z through the thickness.** Station `k` of `K` sits at
`x_k = (k/(K-1) − ½)·L_mm`. `w_mm[k]` is the **half**-width, `c_mm[k]` the lateral offset of the centre line (≈ 0 for straight tools),
`aspect = half-thickness / half-width`. All lengths mm. `pose.R0` (3×3, object→world, columns = the object X/Y/Z axes in the world of the view
cameras) and `pose.t_mm` (the object centre in that world); `world_point = R0 · local + t_mm`.

| `template.name` | Cross-section at station k | Build it as |
|---|---|---|
| `lathe` (p=2, `aspect` = 1) | circle, radius `w_mm[k]` | revolve the profile `(x_k, w_mm[k])` about the X axis; `c_mm` ≈ 0 |
| `flat` (p=4, `aspect` ≈ 0.22 prior) | rounded rectangle, half-width `w_mm[k]`, half-thickness `aspect·w_mm[k]` | loft / extrude the outline `(x_k, c_mm[k] ± w_mm[k])` by `thickness_mm` (`dims_mm.thickness_mm` = `2·aspect·max w`) |
| `box` (p=8, `aspect` ≈ 0.5 prior) | near-rectangle | a cuboid `length_mm × max_width_mm × thickness_mm` (the profile is ~constant); or loft with the same stations |

`dims_mm.max_width_mm = 2·max(w_mm)` (the widest cross-section; the mesh's lateral bounding box can exceed it by the spread of `c_mm`);
`thickness_mm = 2·aspect·max(w_mm)`. When the thickness is not observable (a single top-down view) `aspect` is the **template prior**
(lathe 1.0, flat 0.22, box 0.5) unless the user set `--fixed-thickness-mm`. User overrides (`fixed_dims`) are satisfied exactly in `dims_mm`
and in the mesh length/thickness.

### Confidence

`train_min_iou` = the minimum over the training views of the IoU between the mask and the fit rendered by the **independent** hard cv2
rasteriser (not the soft one that was optimised). `heldout_iou` = the minimum over `--heldout` views (null without any).

| status | meaning |
|---|---|
| `low` | a training view below `--min-confidence`, or a held-out view below 0.75 → ask for another view / fall back to a bank mesh |
| `single_view` | one view reproduced; the 3-D shape (thickness, roll, hidden side) is **not verified** — run [view-check](view-check.md) |
| `ok` | ≥ 2 views reproduced and any held-out view predicted. **Caveat:** without `heldout_iou` this still does not prove 3-D: flat plates and boxes fit all their training views (IoU ≈ 0.89) and predict an unseen view at 0.2–0.5 in 3–6 of 8 poses (measured) |

## Which optimiser ran (`method`, `attempts`)

1. **`closed_form`** (≈ 0.05–0.3 s): per-view profile aligned to its centre line → template by `kind` → pose candidates (axis flip, lateral
   sign, roll, thickness prior) ranked by the hard rasteriser; the best candidate **is** the parametric model when every view's IoU ≥ `--iou-accept`.
2. **`soft`**: Adam on a differentiable soft silhouette (torch). The silhouette is the max-union of the per-station convex hulls of the
   projected rings, scored with a half-plane signed distance (one matmul): the boundary sits exactly at p = 0.5, the smoothness penalty uses a
   *detached* scale, softplus is inverted stably, a NaN/inf loss restores the last finite parameters.
3. **`cma`**: CMA-ES on the hard rasteriser (CPU, no torch, ≈ 3 s) — only when the result is still `low`; it is better than Adam on flat/boxy
   shapes and worse on lathe-like ones, so the better of the two attempts wins (held-out IoU when there is one, else mean training IoU).

Same inputs + same `--seed` ⇒ the same result on CPU (tested, bitwise). On CUDA repeated runs were bitwise identical in 3 runs on an RTX 4060 and are asserted equal to 1e-4; PyTorch does not promise bitwise determinism across devices/versions.

## Calling it from Python (hsw-editor vision service)

```python
import numpy as np
from threed_vision.errors import BadInput, MissingDependency
from threed_vision.fit import fit_auto
from threed_vision.mesh_export import emit_mesh
from threed_vision.view_check import check, render_overlays

# views: world->camera (OpenCV, mm); mask = full-resolution bool HxW; K in the pixels of that mask
top = {"mask": mask_bool, "K": K, "R": np.eye(3), "t": np.zeros(3), "name": "top"}
try:
    fit = fit_auto([top], known_depth=plane_depth_mm, planar=True,          # one photo: scale from the calibrated plane depth
                   fixed_dims={"length_mm": 238.0},                          # optional user overrides (hard constraints)
                   device="auto")                                            # GPU with CPU fallback
except BadInput as exc:        # empty / 1-pixel mask, bad K/R/t, single view without known_depth -> HTTP 422
    ...
except MissingDependency as exc:   # torch/cma absent while the closed form was not good enough -> HTTP 503 with exc.remediation
    ...

dims = fit["dims_mm"]; conf = fit["confidence"]; pose = fit["pose"]; kind = fit["kind"]    # length_mm / max_width_mm / thickness_mm
glb = emit_mesh(fit, "glb")                  # bytes; also "stl" | "obj" | "scad"; frame="world" bakes the pose in (default: object frame)

report = check(fit, [])                      # single photo: sanity only, status "unverified", needs_views = which photo to take next
report = check(fit, [other_view], flag_below=0.75)   # extra photos: per-view IoU, "flagged" below the threshold
overlays = render_overlays(fit, [other_view])        # RGB review images (mask green, fit red)
```

Everything returned is plain dicts/lists/floats (`json.dumps(fit)` works, no NaN); `fit_auto` keyword arguments: `template=None`, `K=8`,
`known_depth=None`, `planar=False`, `fixed_dims=None`, `heldout_views=None`, `iou_accept=0.9`, `min_iou=0.8`, `max_tries=3`, `device="auto"`,
`seed=0`, `cma=True`. Models are not involved: nothing is downloaded.

## Limits (measured)

* Single-view fits reproduce the photo; thickness/roll are priors (unseen-view IoU of single-view fits was 0.03–0.5 in the spike).
* Near-square boxes are 3-D ambiguous: three-view length errors of 12–19 % occurred on 3 of 4 synthetic poses (the held-out view is the only warning).
* The soft fit trades length for silhouette overlap: on the carpet masks its length differs from the closed-form extent by 4 % on average (caliper −7 %: its T-shaped jaws are not a lathe); `fit_profile` `length_mm` is the measured silhouette extent when the exact extent matters.
* A straight-axis template cannot follow open or bent tools (open pliers: IoU 0.71, tin snips 0.83 on the carpet masks).
* The real carpet photo set has **no** oblique second photo: real multi-view 3-D recovery is only verified on synthetic scenes.

## Measured timings

Reproduce (the carpet rows need the spike's `~/.cache/hsw-editor/spikes/vision/out/pipe_masks.npy`; `--skip-carpet` runs the synthetic rows only) with `PYTHONPATH=packages/threed-vision/src python packages/threed-vision/benchmarks/fit_bench.py --device cuda|cpu --out X.json`
(raw rows kept in `~/.cache/hsw-editor/proofs/fit/bench_{gpu,cpu}.json`). Seconds **per object**, end to end (closed-form start, candidate ranking,
refinement, hard-raster scoring), warm process, RTX 4060 8 GB shared with other jobs (load average 14.6 → 7.5 on the GPU run, 7.2 → 16.6 on the CPU
run, 12 cores, torch 6 threads), so these are pessimistic. "Carpet" = the 12 real SAM2 masks of the carpet photo (caliper 82, screwdrivers 70/40,
pliers 46, multimeter 20, tape measure 8, power supply 24, snips 80, drills 76/54, bit sets 4/9; 1.263 mm/px, f = 0.375·diag, plane depth 1184 mm).
"Synthetic" = 4 ground-truth shapes × 4 poses (3 cameras at 0°/55°/−50°, one held-out view).

| measurement | device | n | median s | p95 s | max s |
|---|---|---|---|---|---|
| closed form `fit_profile`, full 2000×1500 frame | CPU | 12 | 0.002 | 0.005 | 0.005 |
| closed form `fit_profile`, bbox crop | CPU | 12 | 0.001 | 0.005 | 0.005 |
| `fit_auto` single-view planar, carpet (5/12 accepted at closed form, no optimiser) | GPU | 12 | 0.70 | 4.26 | 4.26 |
| `fit_auto` single-view planar, carpet (5/12 accepted at closed form) | CPU | 12 | 0.63 | 4.66 | 4.66 |
| **single-view planar soft fit, first attempt**, carpet | GPU | 12 | **0.78** | **1.17** | 1.17 |
| **single-view planar soft fit, first attempt**, carpet | CPU | 12 | **0.84** | **2.24** | 2.24 |
| single-view planar soft fit, up to 3 tries, carpet | GPU | 12 | 0.81 | 2.23 | 2.23 |
| single-view planar soft fit, up to 3 tries, carpet | CPU | 12 | 0.67 | 1.46 | 1.46 |
| single-view planar soft fit, first attempt, synthetic | GPU | 16 | 0.76 | 1.24 | 1.24 |
| single-view planar soft fit, first attempt, synthetic | CPU | 16 | 0.58 | 2.09 | 2.09 |
| 3-view soft fit, first attempt, carpet mask + 2 views rendered from its fit* | GPU | 12 | 1.19 | 1.82 | 1.82 |
| 3-view soft fit, first attempt, carpet mask + 2 views rendered from its fit* | CPU | 12 | 1.45 | 1.78 | 1.78 |
| 3-view soft fit, first attempt, synthetic | GPU | 16 | 1.28 | 1.91 | 1.91 |
| 3-view soft fit, first attempt, synthetic | CPU | 16 | 1.35 | 4.49 | 4.49 |
| 3-view soft fit, up to 3 tries, synthetic | GPU | 16 | 1.38 | 3.51 | 3.51 |
| 3-view soft fit, up to 3 tries, synthetic | CPU | 16 | 1.55 | 13.44† | 13.44† |
| 3-view closed form only (`iou_accept=0`), synthetic | CPU | 16 | 0.21 | 0.29 | 0.29 |

\* there is only one real view per carpet object, so views 2 and 3 are rendered from the single-view fit (±50°): the timing is representative
(same mask sizes, same optimiser), the IoU is not evidence of 3-D recovery. † one pose (flat pliers, 3 retries) measured while the load average
was 16.6; the same three-try job took 3.0 s in the previous run of that pose.

**Targets, stated plainly.** Single-view planar fit < 2 s per object on the GPU: **met for the first attempt** (median 0.78 s, p95 1.17 s,
max 1.17 s); with the 3-try retry budget the worst object (the open red pliers, which never reaches IoU 0.8) took 2.23 s, i.e. **slightly over
2 s**. < 10 s on the CPU: **met** for every object (first attempt max 2.24 s; 3 tries max 1.46 s; the closed-form-first `fit_auto` worst case, the
pliers with a CMA-ES second attempt, 4.66 s). The earlier spike numbers were ≈ 1 s GPU / ≈ 5 s CPU single-view and 13–17 s CPU for 3 views; the
3-view CPU median is now 1.35–1.45 s. The speed-up came from the soft rasteriser: the union of K−1 per-station convex hulls instead of ~100
triangles (≈ 2.5× fewer edges) evaluated in a half-plane form that is one matmul instead of ten elementwise (N, faces, 3, 2) passes — one
3-view CPU fit went 9.6 s → 0.94 s on the same loaded machine; it also fits better (the triangle union left a dip at every shared edge). The
GPU is **not** faster than the CPU for these small crops (launch-bound; GPU 0.78 s vs CPU 0.84 s median on carpet, both with 6 CPU threads busy
elsewhere); `device="auto"` still prefers the GPU, which scales better with K and the number of views. Peak torch allocation of the fit is 37 MB (measured; the CUDA context itself adds a few hundred MB), far under the 3 GB budget.

Accuracy on the same runs (so the speed is not bought with quality): carpet single-view planar soft fit IoU ≥ 0.89 on 8/12 objects
(0.71 open pliers, 0.81 snips, 0.83 caliper, 0.89 drill); synthetic 3-view lathe length error 0.1–2.0 % (unseen-view IoU 0.88–0.92), elongated
box 0.2–2.5 %, flat pliers 0.1–0.9 % (unseen-view IoU 0.67–0.89; 3 of 4 poses below the 0.8 bar and therefore `low`), near-square box 12–19 % on 3 of 4 poses (unseen-view IoU 0.80–0.98: the bad length is not visible in the held-out IoU).
