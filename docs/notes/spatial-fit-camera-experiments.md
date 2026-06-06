# Spatial-Aware Fit-Camera Experiments

Date: 2026-06-06

## Goal

Make `fit-camera` and reference matching harder to fool. Area IoU alone is not enough:
it can reward a camera that puts the model inside a large mask, even when the visible
alignment is wrong. A proof must show the original reference, the reference mask, the fitted
render, a shared-frame overlay, and boundary-distance metrics.

This note is an experiment plan and an execution manifest. The first prototype now adds
optional `fit-camera` flags instead of changing the default command behavior:

- `--objective contour` uses a contour-first loss built from edge F1@4, symmetric Chamfer,
  p95 boundary distance, coverage ratio, and border-touch penalty.
- `--spatial-report DIR` writes `spatial_metrics.json`, `edge_overlay.png`, and
  `proof_panel.png`.
- `--trace FILE` writes best-candidate JSONL for candidate-evolution videos.
- `--mask-polarity light` is required for white-subject binary masks from preprocess
  tiers; the default remains `dark` for raw dark-subject photos.
- `--backplate FILE` lets `proof_panel.png` show the original photo/render when the fit
  reference is a derived binary mask.

Default `fit-camera` remains `--objective area-iou`.

The standing hypothesis/experiment log lives in
`docs/research/fit-camera.md`. Keep that log updated with hypothesis, related work/tools,
assembled 3d-cli variant, expected proof, artifacts, result, and decision for every
spatial-awareness approach.

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
| `boundary_sdf_loss_px` | lower better | Symmetric boundary sampled against signed distance fields; useful for local pose-gradient diagnostics. |
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
python3 bin/3d fit-camera work/synthetic.scad work/ref-pre/mask.png --mask-polarity light --backplate work/ref.png --out work/camera.json --rand 300 --refine 120 --seed 7
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
python3 bin/3d fit-camera "$MODEL" work/e1/mask.png --mask-polarity light --backplate "$REF" --out work/e1/camera.json --rand 250 --refine 100 --seed 11
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
python3 bin/3d fit-camera "$MODEL" work/e2/mask.png --mask-polarity light --backplate "$REF" --out work/e2/camera.json --rand 250 --refine 100 --seed 11
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
python3 bin/3d fit-camera "$MODEL" work/e3/mask.png --mask-polarity light --backplate "$REF" --out work/e3/camera.json --rand 300 --refine 140 --seed 11
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
python3 bin/3d fit-camera "$MODEL" work/e5/front/mask.png --mask-polarity light --backplate refs/front.jpg --out work/e5/front/camera.json --rand 250 --refine 100 --seed 21
python3 bin/3d fit-camera "$MODEL" work/e5/oblique/mask.png --mask-polarity light --backplate refs/oblique.jpg --out work/e5/oblique/camera.json --rand 250 --refine 100 --seed 22
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

### Prototype run on this machine

Command:

```bash
uv run --with pillow,numpy,scipy python3 tools/spatial_fit_experiment.py \
  --run-synthetic-oracle \
  --out /tmp/3d-spatial/synthetic-oracle
```

Artifacts:

- `/tmp/3d-spatial/synthetic-oracle/known_reference.png`
- `/tmp/3d-spatial/synthetic-oracle/known_mask.png`
- `/tmp/3d-spatial/synthetic-oracle/camera.json`
- `/tmp/3d-spatial/synthetic-oracle/spatial-report/proof_panel.png`
- `/tmp/3d-spatial/synthetic-oracle/demo/candidate_evolution.gif`
- `/tmp/3d-spatial/synthetic-oracle/demo/candidate_evolution.mp4`
- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.json`
- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.png`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank.json`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank_heatmap.png`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.json`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.png`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/renders`

Observed contour-objective synthetic oracle metrics:

```json
{
  "area_iou": 0.7338,
  "edge_f1@4": 0.5815,
  "edge_chamfer_px": 5.82,
  "boundary_sdf_loss_px": 5.81,
  "hausdorff_p95_px": 17.53,
  "bbox_iou": 0.9138,
  "coverage_ratio": 1.0171,
  "spatial_warning": null
}
```

Result: partial pass. The contour objective recovered a same-frame silhouette with sane
coverage and no spatial warning, but it did not exactly recover the known camera. The proof
panel and candidate-evolution video are diagnostic artifacts, not a success claim: the
video must be read as contour evolution, with white aligned boundaries, red reference-only
boundary, cyan render-only boundary, and Chamfer/p95/F1 labels in the frame. Area IoU is
shown only as secondary context.

Pose-sensitive objective diagnostics:

- Finite-difference sweeps around hidden-camera truth covered azimuth, elevation,
  distance, target-x, and target-z.
- For this asymmetric synthetic model, every tested axis had `monotonic_fraction=1.0` and
  `near_truth_all_toward_steps_improve=true`: boundary SDF loss decreased when stepping
  toward the hidden pose and increased when stepping away.
- Coarse view-bank retrieval over azimuth/elevation ranked the hidden grid point
  `(azimuth=135, elevation=20)` top-1 with `boundary_sdf_loss_px=0.112`.
- This is a local result, not a raw-pose global hash guarantee. The research log treats
  symmetry as equivalence/plateau rather than automatic failure: sphere azimuth is
  unobservable, fourfold rotations produce multiple valid minima, and a non-equivalent
  +180 degree backside on asymmetric geometry remains high loss.

Equivalence diagnostics:

- Sphere azimuth sweep: `loss_range_px=0.0`, `unobservable_dof_detected=true`.
- Fourfold model: low-loss azimuths were `[45, 135, 225, 315]`, matching declared
  modulo-90 equivalence; non-equivalent sampled azimuths had boundary SDF about `5.36 px`.
- Asymmetric model: truth boundary SDF `0.11 px`, +180 degree backside `6.46 px`,
  `backside_rejected=true`.

Real-image diagnostic run:

```bash
bin/3d fit-camera /Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad \
  /tmp/3d-spatial/pantheon-gflash-front/preprocess/mask.png \
  --out /tmp/3d-spatial/pantheon-gflash-front/contour/camera.json \
  --opt-size 240x160 \
  --final-size 480x320 \
  --rand 60 \
  --refine 25 \
  --seed 11 \
  --mask-polarity light \
  --backplate /Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg \
  --objective contour \
  --spatial-report /tmp/3d-spatial/pantheon-gflash-front/contour/spatial-report \
  --trace /tmp/3d-spatial/pantheon-gflash-front/contour/trace.jsonl
```

Observed metrics:

```json
{
  "area_iou": 0.2386,
  "edge_chamfer_px": 25.4,
  "boundary_sdf_loss_px": 24.4,
  "hausdorff_p95_px": 73.1,
  "coverage_ratio": 0.38,
  "bbox_iou": 0.27,
  "spatial_warning": "spatial mismatch risk: coverage_ratio=0.38; bbox_iou=0.27; edge_chamfer_px=25.4; hausdorff_p95_px=73.1"
}
```

Result: fail/warn as intended. The fitted render is in the same general frame but occupies
only part of the facade mask; the contour metrics catch the scale and boundary mismatch.

Approach status:

| Approach | Result |
|---|---|
| image backplate loop | Prototyped as `proof_panel.png`: reference/mask/render/edge overlay in one shared frame. `--backplate` now shows the original photo/render when fitting against a derived mask. |
| contour-first objective | Implemented as experimental `--objective contour`; reports edge F1/Chamfer/p95 and warning. It produced a same-frame synthetic fit but is not yet sufficient for exact camera recovery. |
| segmentation tiers | Current fallback mask remains the local baseline. `tools/spatial_fit_experiment.py --include-heavy` emits rembg and Depth Anything commands; SAM/Depth Anything remain manual/graceful-skip due resource/checkpoint requirements. |
| multi-view/synthetic oracle | Synthetic oracle harness implemented. Coarse view-bank retrieval recovered hidden azimuth/elevation top-1; unconstrained optimizer still landed on a plausible but different silhouette, so view-bank seeding should precede local refinement. Multi-view aggregate remains planned. |
| real image diagnostic | Implemented through `spatial_warning`; Pantheon fallback run failed loudly on scale/crop mismatch. |
| pose-sensitive hash/objective | Raw-pose global monotone hash rejected as the wrong contract; equivalence-aware objective remains plausible. Local SDF/Chamfer plus finite-difference gradients and view-bank retrieval works on the asymmetric synthetic oracle, sphere azimuth is detected as an unobservable plateau, fourfold symmetry is accepted modulo 90 degrees, and non-equivalent asymmetric backside is rejected. Depth/normal priors require a renderer with depth/normal buffers; OpenSCAD RGB is only a weak proxy. |

## Not implemented yet

These are research tracks, not current product behavior:

- Approximate 3D reconstruction from one photo: no TRELLIS/Hunyuan3D/TripoSR-style proxy
  mesh is wired into `fit-camera` yet. It remains a critic-only experiment because generated
  meshes can hallucinate geometry and must not become ground truth.
- Normal-map recovery from a photo: Wonder3D-style normal maps are planned as an auxiliary
  channel, but no normal-map scorer is implemented.
- Metric depth/normal comparison: `3d preprocess` can emit fallback pseudo-depth and heavy
  Depth Anything commands can be generated, but `fit-camera` does not yet compare candidate
  cameras against true rendered depth/normal buffers. OpenSCAD PNG output is insufficient for
  that; a Blender/OpenGL/manifold render pass is needed.
- Topological similarity: contour topology/skeleton/persistent-homology descriptors are
  documented as filters for bad masks or wrong objects, not implemented as a pose-improving
  hash. The next useful step is a coarse view-bank descriptor that combines contour shape
  context/radial signatures with SDF/Chamfer refinement.
