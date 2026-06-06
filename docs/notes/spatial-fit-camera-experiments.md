# Spatial-Aware Fit-Camera Experiments

Date: 2026-06-06

## Goal

Make `fit-camera` and reference matching harder to fool. Area IoU alone is not enough:
it can reward a camera that puts the model inside a large mask, even when the visible
alignment is wrong. A proof must show the original reference, the reference mask, the fitted
render, a shared-frame overlay, and boundary-distance metrics.

This note is an experiment plan and an execution manifest. It intentionally does not change
`lib/fit_camera.py`; use it to run independent experiments before changing the optimizer.

## Existing implementation

- `3d preprocess` runs `lib/preprocess_reference.py` and writes `mask.png` plus `depth.png`.
- The light local path is `--force-fallback`: OpenCV GrabCut mask and pseudo-depth.
- The optional spatial-aware paths are import-guarded:
  - `rembg` salient-object mask, first-run download about 170 MB.
  - SAM 2 box-prompted mask from a local checkpoint, expected GPU memory about 4-8 GB.
  - Depth Anything V2 through `transformers`, expected GPU memory about 2-6 GB.
- `lib/refmatch.py` already compares against a segmented mask, rejects obvious silhouette
  fill degeneracy, and builds `render | diff | reference` collages.
- `docs/research/benchmarks-and-metrics.md` warns that free-camera silhouette IoU is
  degenerate and says the pipeline order must be segmentation, camera fit, degeneracy
  rejection, pose freeze, then scoring.

## Inputs

Local assets that are useful now:

| Case | Model | Reference(s) | Why it matters |
|---|---|---|---|
| `pantheon-gflash` | `/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad` | `references/front.jpg`, `references/oblique.jpg` | Known negative control. Existing `fit_*.json` IoU near 0.68-0.71 can look good numerically while the visible match is poor. |
| `pantheon-gpro` | `/Users/ultra/xp/3d-tests/gpro-3dcli/pantheon.scad` | `references/front.jpg`, `references/oblique.jpg` | Same subject, different generated model; useful for ranking metrics across candidate outputs. |
| `lego-loco` | `/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad` | `references/ref_emerald_night_side.jpg`, `references/ref_orient_express_side.jpg` | Side-elevation object with real depth ambiguity; should benefit from mask + depth + part prompts. |
| `cell-sensor-adapter` | `/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/assembly.scad` | project photos/previews, if present | Mechanical part; useful to separate CAD fit from landmark-free architecture photos. |

Do not commit external reference images until licensing is explicit. The harness treats them
as local optional assets and skips missing paths.

## Metrics

Report all of these for every run; do not send a Telegram proof unless the image panels and
the metrics agree.

| Metric | Sense | Purpose |
|---|---:|---|
| `area_iou` | higher better | Descriptive overlap only. Never the sole proof and not enough for camera acceptance. |
| `edge_f1@r` | higher better | Boundary hit-rate: Canny or binary-mask contour pixels are matched if they are within radius `r` pixels of the other contour. Use `r=2,4,8` at the final render size. |
| `edge_chamfer_px` | lower better | Symmetric mean nearest-boundary distance in pixels; catches global misregistration even when area IoU looks acceptable. |
| `hausdorff_p95_px` | lower better | 95th percentile boundary distance; catches one side of the model drifting away. |
| `bbox_iou` | higher better | Alignment of subject extents; cheap degeneracy guard. |
| `coverage_ratio` | near 1 | Render-mask area divided by reference-mask area; catches scale collapse/explosion. |
| `centroid_delta_px` | lower better | Translation sanity check. |
| `touches_border` | false | Rejects cropped projections unless the reference also touches the same border. |
| `depth_order_score` | higher better | For spatial-aware runs: correlation between rendered feature depth ordering and reference depth channel, measured only inside the subject mask. |
| `vlm_score` | higher better | Human-like rubric score on original reference vs render; use only after image artifacts are inspectable. |

Boundary metrics should be computed on contours, not filled areas. Area IoU remains useful as
a secondary channel, but an accepted camera should have small contour distance and a sane
coverage ratio.

## Experiment Matrix

### E0: synthetic locked-reference sanity

Purpose: prove the tooling can recover an already-rendered reference without relying on a
real photo.

Inputs:
- Asymmetric synthetic `.scad`.
- Reference render from a known camera.
- Original reference render and derived binary mask, both shown in the artifact panel.

Commands:

```bash
python3 bin/3d render work/synthetic.scad --cam "$KNOWN_CAMERA" --out work/ref.png
python3 bin/3d preprocess work/ref.png -o work/ref-pre --force-fallback
python3 bin/3d fit-camera work/synthetic.scad work/ref-pre/mask.png --out work/camera.json --rand 300 --refine 120 --seed 7
```

Expected artifacts:
- `ref.png`
- `ref-pre/mask.png`
- `camera.json`
- fitted render
- shared-frame panel: `reference | mask | fitted render | edge overlay`

Acceptance:
- `edge_f1@2 >= 0.98`
- `edge_chamfer_px <= 0.5`
- `coverage_ratio` in `[0.95, 1.05]`
- exact or near-exact camera parameters for the known synthetic reference

### E1: real-reference fallback mask

Purpose: establish the local baseline that requires no heavy models.

Commands:

```bash
python3 bin/3d preprocess "$REF" -o work/e1 --force-fallback
python3 bin/3d fit-camera "$MODEL" work/e1/mask.png --out work/e1/camera.json --rand 250 --refine 100 --seed 11
python3 bin/3d compare "$MODEL" "$REF" --out work/e1/compare --rand 250 --refine 100
```

Expected artifacts:
- original `$REF`
- `work/e1/mask.png`
- `work/e1/depth.png`
- `work/e1/camera.json`
- fitted render and overlay from `fit-camera`
- compare collage

Acceptance:
- This is not expected to pass on Pantheon. It should expose failure clearly.
- A run is unacceptable if area IoU is high but `edge_chamfer_px`, `coverage_ratio`, or visual
  overlay are poor.

### E2: rembg mask tier

Purpose: test a cheap model-backed segmentation tier before SAM 2.

Commands:

```bash
uv run --python 3.12 --with opencv-python-headless,numpy,pillow --with 'rembg[cpu]' \
  lib/preprocess_reference.py "$REF" --out-dir work/e2
python3 bin/3d fit-camera "$MODEL" work/e2/mask.png --out work/e2/camera.json --rand 250 --refine 100 --seed 11
```

Resource limits:
- CPU path only unless ONNX runtime selects acceleration.
- First run may download about 170 MB.
- Cap to one image at a time.

Compare against E1:
- mask coverage and bbox should be closer to the visible subject.
- boundary metrics should improve; if only area IoU improves, treat it as suspect.

### E3: SAM 2 mask tier

Purpose: evaluate the intended high-quality subject mask.

Commands:

```bash
uv run --python 3.12 --with opencv-python-headless,numpy,pillow --with torch \
  lib/preprocess_reference.py "$REF" --out-dir work/e3 \
  --sam2-checkpoint "$SAM2_CHECKPOINT" \
  --sam2-config configs/sam2.1/sam2.1_hiera_s.yaml
python3 bin/3d fit-camera "$MODEL" work/e3/mask.png --out work/e3/camera.json --rand 300 --refine 140 --seed 11
```

Resource limits:
- One worker, one image at a time.
- Prefer the smallest SAM2.1 checkpoint first.
- Abort if memory pressure rises; mask generation can be cached and reused.

Expected improvement:
- Cleaner silhouette on cluttered real photos.
- Lower `edge_chamfer_px` and fewer background-induced false positives.

### E4: depth-aware diagnostics

Purpose: add spatial information without using generated meshes as deliverables.

Commands:

```bash
python3 bin/3d preprocess "$REF" -o work/e4 --force-fallback
# Heavy path, when resources allow:
uv run --python 3.12 --with opencv-python-headless,numpy,pillow --with 'transformers>=4.45' --with torch \
  lib/preprocess_reference.py "$REF" --out-dir work/e4-depth
```

Expected artifacts:
- `depth.png` from fallback and, if available, Depth Anything.
- depth-colored subject panel next to reference and fitted render.

Metrics:
- `depth_order_score` over named or automatically binned regions.
- VLM critic prompt includes original reference, mask, depth, fitted render, and edge overlay.

This should not directly accept/reject camera pose yet; it is a critic channel and a
proportion sanity check.

### E5: multi-view pair

Purpose: prevent a single view from accepting a shape that only projects correctly from one
angle.

Inputs:
- Pantheon `front.jpg` plus `oblique.jpg`.
- Any project with side/front references.

Commands:

```bash
python3 bin/3d preprocess refs/front.jpg -o work/e5/front --force-fallback
python3 bin/3d preprocess refs/oblique.jpg -o work/e5/oblique --force-fallback
python3 bin/3d fit-camera "$MODEL" work/e5/front/mask.png --out work/e5/front/camera.json --rand 250 --refine 100 --seed 21
python3 bin/3d fit-camera "$MODEL" work/e5/oblique/mask.png --out work/e5/oblique/camera.json --rand 250 --refine 100 --seed 22
```

Expected artifact:
- two-view report with per-view edge metrics, then aggregate `min(edge_f1@4)` and
  `mean(edge_chamfer_px)`.

Acceptance:
- No aggregate pass if either view fails contour metrics, even if the other view has high
  area IoU.

### E6: generated spatial proxy as critic-only input

Purpose: try spatial-aware models without allowing them to become the deliverable.

Candidates:
- TRELLIS / Hunyuan3D / TripoSR mesh from the reference.
- Wonder3D normal map.

Use:
- Render proxy silhouettes/normals from the same candidate cameras.
- Feed proxy silhouette/normal as an auxiliary critic channel.
- Do not score OpenSCAD directly against the generated mesh as ground truth unless the
  experiment is explicitly labelled proxy-only.

Resource limits:
- Run manually, not in CI.
- One model at a time.
- Cache outputs per reference.
- Stop if swap grows or GPU memory pressure is visible.

## Review Protocol

Every proof candidate must be inspected by a reviewer with the images, not just the JSON.

Required panel order:

```text
original reference | reference mask | fitted render | shared-frame edge overlay
```

Required reviewer questions:

1. Does the mask isolate the intended subject?
2. Are the render and reference in the same frame, with no hidden crop/scale mismatch?
3. Do red/cyan boundaries coincide, not just filled areas?
4. Are the metrics consistent with the visible result?
5. Would this proof still look convincing if area IoU were hidden?

## Local first batch

Run these first because they are safe on the current machine:

```bash
python3 tools/spatial_fit_experiment.py --check-assets
python3 tools/spatial_fit_experiment.py --emit shell --case pantheon-gflash-front --out /tmp/3d-spatial/pantheon-gflash-front
python3 tools/spatial_fit_experiment.py --emit shell --case lego-loco-emerald-side --out /tmp/3d-spatial/lego-loco-emerald-side
```

Then execute the emitted shell commands selectively. Do not run heavy model tiers until the
fallback mask panel is visually reviewed.

### Baseline run on this machine

Started E1 for `pantheon-gflash-front` with the local fallback path:

```bash
python3 bin/3d preprocess /Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg \
  -o /tmp/3d-spatial/pantheon-gflash-front/preprocess \
  --force-fallback
```

Observed output:

- Runtime: 75.1s on CPU.
- Mask tier: `grabcut(cv2-fallback)`.
- Depth tier: `pseudo(gradient-fallback)`.
- Subject coverage: 38.7% of frame.
- Bbox: `(76, 416, 1128, 706)`.
- Centroid: `(641.75, 813.33)`.

Visual inspection:

- The mask captures the main facade, so it is a usable fallback baseline.
- The mask also includes side-background structures and bottom occluders, so it is not clean
  enough to prove `fit-camera` reliability on a real reference.
- The pseudo-depth image is useful as a critic channel, but it is heuristic; do not treat it
  as metric depth.

Conclusion: this is a negative/baseline experiment. It should be used to test whether
boundary metrics and reviewer inspection reject a weak real-photo setup, not as a success
proof.
