# Reference Backplate Workflow

Use this workflow when you have a reference image and want to model against it like a
3D editor backplate: keep the reference fixed, render the current model from a stable
camera, compare contours and overlays, then revise one figure or feature at a time.

This is a first-stage workflow built from existing `3d` commands. It does not add a new
UI, and it does not assume the model is already correct. The goal is to make each
iteration answer one question: did the latest model edit improve the silhouette and
visible contours from the same viewpoint?

## What Already Exists

| Step | Command | What it gives you |
|---|---|---|
| Clean the reference | `3d preprocess` | `mask.png` subject silhouette and `depth.png` proportional depth map. |
| Lock a viewpoint | `3d fit-camera` | `camera.json`, a fitted render, and a red/cyan alignment overlay. |
| Render the model | `3d render` | A stable named-view or manual-camera PNG. |
| Make a model mask | `3d silhouette` | A binary render silhouette for mask-to-mask scoring. |
| Inspect visually | `3d overlay` | Difference, ghost, and edge-overlay PNGs. |
| Score numerically | `3d score` | `AE`, `AE_NORM`, `IoU`, `CLOSENESS`, `FRAME`, and `OVERLAY`. |
| Run the whole comparison | `3d compare` | Segmented mask, matched render, diff, collage, IoU, SSIM, DSSIM. |

Use `compare` for a quick end-to-end pass. Use the step-by-step flow below when you are
actively editing the model and need predictable artifacts for each iteration.

## 1. Prepare A Working Directory

Keep all generated artifacts for one reference in a single directory. The examples below
use `work/backplate/`.

```bash
mkdir -p work/backplate
```

Use a reference where the subject is large in frame, has minimal lens distortion, and is
not heavily occluded. A side or 3/4 reference is usually more useful than a dramatic
perspective view because it makes silhouette errors easier to see.

## 2. Preprocess The Reference

Preprocess the image once. This creates a clean subject mask and a proportional depth map
that can guide later modeling choices.

```bash
3d preprocess refs/bracket-side.jpg -o work/backplate/
```

If the optional model tiers are unavailable or you want a deterministic low-dependency
pass, force the fallback:

```bash
3d preprocess refs/bracket-side.jpg -o work/backplate/ --force-fallback
```

Open `work/backplate/mask.png` before trusting any score. If the mask includes the table,
wall, hand, or shadows as part of the subject, the numeric score is not meaningful yet.
Crop or replace the reference, then preprocess again.

## 3. Choose And Freeze The Camera

There are two practical starting points.

For a standard engineering view, render from a named camera. This is repeatable and works
well when the reference was taken roughly from the front, side, top, or a 3/4 view.

```bash
3d render model.scad --view left --ortho --size 1600x1200 -o work/backplate/render-left.png
```

For a photo with an unknown viewpoint, fit the camera to the reference and save it:

```bash
3d fit-camera model.scad refs/bracket-side.jpg \
  --out work/backplate/camera.json \
  --draw-axes
```

Inspect the fit artifacts next to the reference:

- `work/backplate/camera_fit.png`
- `work/backplate/camera_overlay.png`

`camera.json` contains `camera_arg`, the 6-value OpenSCAD camera vector. Reuse that value
for later `render`, `silhouette`, or `score` calls so every iteration compares geometry
changes from the same viewpoint. Do not add `--ortho` to renders that reuse a
`fit-camera` camera unless that camera was fitted through an orthographic path; changing
projection after fitting makes the score reflect camera drift instead of model geometry.

```bash
CAM="$(jq -r .camera_arg work/backplate/camera.json)"
```

## 4. Render And Overlay The Current Model

Render from the frozen camera, then produce the visual diagnostics.

```bash
3d render model.scad \
  --cam "$CAM" \
  --size 1600x1200 \
  -o work/backplate/render-001.png

3d overlay work/backplate/render-001.png refs/bracket-side.jpg -o work/backplate/overlay-001/
```

Read the overlay set in this order:

1. `edge_overlay.png` first: red reference edges and cyan render edges show contour drift.
2. `ghost.png` second: the 50% blend makes scale and placement errors obvious.
3. `overlay.png` last: the difference map highlights changed areas, but can exaggerate
   texture and lighting differences in the photo.

## 5. Score The Silhouette

For a quick render-vs-reference score:

```bash
3d score work/backplate/render-001.png refs/bracket-side.jpg -o work/backplate/score-001/
```

For a cleaner mask-to-mask score, compare the model silhouette against the preprocessed
reference mask:

```bash
3d silhouette model.scad \
  --cam "$CAM" \
  --size 1600x1200 \
  -o work/backplate/model-mask-001.png

3d score work/backplate/model-mask-001.png work/backplate/mask.png \
  --masks \
  -o work/backplate/score-001/
```

Treat `IoU` as the primary trend number, not as a proof that the model is correct.
Texture, holes, internal geometry, and occluded details need separate checks.

## 6. Iterate Figure By Figure

Change one visible figure, feature, or proportion per iteration. For example:

```bash
3d render model.scad -D 'arm_width=18' --cam "$CAM" \
  --size 1600x1200 \
  -o work/backplate/render-002.png

3d overlay work/backplate/render-002.png refs/bracket-side.jpg -o work/backplate/overlay-002/

3d silhouette model.scad -D 'arm_width=18' --cam "$CAM" \
  --size 1600x1200 \
  -o work/backplate/model-mask-002.png

3d score work/backplate/model-mask-002.png work/backplate/mask.png \
  --masks \
  -o work/backplate/score-002/
```

Record the parameter edit, the overlay directory, and the score. Keep the edit if the
edge overlay improves the intended region and the score does not regress. Revert or
adjust if the numeric score rises only because the silhouette got smaller, shifted, or
lost an important contour.

## 7. Use The End-To-End Comparison As A Checkpoint

After a few focused edits, run the integrated comparison. It segments the reference,
fits or reuses the model/render path internally, and writes a collage.

```bash
3d compare model.scad refs/bracket-side.jpg -o work/backplate/compare-003/
```

For a render that already uses the frozen camera:

```bash
3d compare work/backplate/render-003.png refs/bracket-side.jpg -o work/backplate/compare-003/
```

If `compare` warns that the result is unreliable, inspect the reported `mask.png`,
`matched_render.png`, and `collage.png` before changing model geometry. A bad mask or
camera fit can make a good edit look worse.

## Remaining Gaps

- `3d render` does not currently accept `--camera-json`; use `jq -r .camera_arg` from
  `fit-camera` output and pass it through `--cam`.
- There is no persistent backplate session file yet. The user tracks `CAM`, output
  directories, parameter edits, and score history manually.
- There is no interactive image plane inside `3d web` yet. The current workflow uses
  rendered PNGs, overlays, and collages as external artifacts.
- Multi-view backplate matching is still manual. Run the same workflow per reference
  image and keep each camera/output set in its own directory.
