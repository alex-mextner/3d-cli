# Fit-Camera Research Notes

This note tracks what has already been tried for `3d fit-camera`, why some
metrics are no longer accepted as proof, and what experiments remain planned.

## Current conclusion

`fit-camera` must be judged primarily by boundary alignment, not by filled-mask
area overlap and not by global SSIM.

The accepted proof format is a shared-frame visual panel:

1. original reference image or source render,
2. reference mask,
3. boundary overlay in the same frame,
4. fitted render.

The accepted numeric proof should report contour metrics:

- boundary F1 / precision / recall at a small pixel tolerance,
- symmetric contour Chamfer distance in pixels,
- 95th-percentile contour miss / Hausdorff-like distance,
- crop/frame/bbox/fill diagnostics.

Area IoU and SSIM may still be reported as secondary diagnostics, but they are
not enough to accept a camera fit.

## Why SSIM looked bad

SSIM is a poor primary metric for this specific task because `fit-camera`
compares silhouettes, not natural images.

Failure modes observed:

- A real reference photo and an OpenSCAD render have different lighting,
  antialiasing, background, and texture. SSIM sees those image-statistics
  differences even when the silhouette boundary is close.
- Global SSIM on binary masks is dominated by foreground/background balance.
  A small crop, scale mismatch, or a large clean background can change SSIM
  more than a boundary move a human would care about.
- A full-frame or contaminated segmentation mask can produce misleading global
  image statistics. In that case SSIM is not "camera quality"; it is mostly
  "mask quality".
- SSIM does not say where the camera is wrong. Boundary overlays and contour
  distances tell whether the model edge is left/right/up/down of the reference.

This does not mean SSIM is useless. It can stay as a smoke diagnostic and a
regression signal for synthetic render-derived references. It should not decide
`status=ok` by itself, and it should never be shown as the main proof for a real
photo.

## Why area IoU is not enough

Filled-mask IoU answers "how much filled area overlaps". For camera fitting, the
more important question is "are the object boundaries in the same place?"

Area IoU can be misleading when:

- a smaller render sits inside a larger reference silhouette,
- a crop/zoom error overlaps much of the same area but misses the outline,
- a broad blob masks missing columns, holes, or thin features,
- the reference mask includes background clutter,
- the model is symmetric enough that several poses share similar filled area.

Therefore area IoU is now a secondary diagnostic. It can help rank candidates,
but an accepted result must also pass boundary metrics and visual review.

## Tried approaches

### Latest visual-review evidence

Goodall visual review on 2026-06-06 confirmed the current direction:

- Synthetic shared-frame proof looked valid: original reference, mask, boundary
  overlay, and fitted render were all in one frame; contours visually matched.
  Reported metrics were `fit_status=ok`, `edge_f1=1.0`,
  `edge_distance_score=1.0`, and `area_iou=0.9696`.
- Real Pantheon proof panel was no longer broken as a collage, but the fit was
  visually bad: the cyan render contour did not match the red reference contour.
  The diagnostics correctly reported `warning`: `edge_f1=0.3416`,
  `edge_recall=0.3287`, `edge_iou=0.1885`, while `area_iou=0.7307`.

This is the key policy proof: area IoU can remain as a secondary diagnostic, but
it must not override weak boundary metrics or failed visual review.

### Area IoU on filled masks

Status: demoted.

This was the original simple objective. It is fast and useful for a coarse
baseline, but it accepted visually wrong fits and made bad proof screenshots look
better than they were.

Current use:

- keep as a weak term in the optimizer only if combined with boundary terms,
- report as `area_iou` / `iou`,
- never use it alone as acceptance proof.

### Global SSIM on masks

Status: demoted.

SSIM helped as a rough synthetic regression signal but failed as an explanation
for real-photo fits. It is too sensitive to mask/background statistics and too
weak at localizing camera error.

Current use:

- report as `ssim`,
- use only as secondary context,
- do not accept or reject real camera fits from SSIM alone.

### Boundary F1 with tolerance

Status: keep.

This measures how much of the rendered contour lands near the reference contour
and how much of the reference contour is covered by the render. It is much closer
to visual fit quality than filled-mask IoU.

Known limitation:

- a tolerance band hides small offsets, so it must be paired with real distance
  metrics such as Chamfer and a visual overlay.

### Bounded dilation-distance score

Status: keep as optimizer-friendly score, not as geometric proof.

Multi-radius dilation scores are stable for optimization because they do not
explode on noisy masks. They are useful inside the search objective.

Known limitation:

- the resulting pixel estimate is bounded and approximate. It should not be
  called a true Chamfer distance.

### Symmetric contour Chamfer distance

Status: planned/current primary reporting metric.

Chamfer measures nearest-neighbor distance between render contour pixels and
reference contour pixels in both directions. This directly answers the user's
concern: closeness of contours matters more than filled-area overlap.

Acceptance intent:

- low mean Chamfer,
- low 95th-percentile miss,
- high boundary recall,
- no crop/fill/border warnings,
- visual proof panel inspected by a reviewer.

### Shared-frame proof panel

Status: keep.

The proof must include the original reference image, not only the reference
mask. A mask-only proof can hide a bad segmentation or a mismatch between the
photo and mask.

Required panel columns:

1. reference image,
2. reference mask,
3. boundary overlay,
4. fitted render.

### Synthetic render-derived reference

Status: keep as controlled proof, not sufficient alone.

Synthetic proof is valuable because the expected camera is representable and
repeatable. It can prove the optimizer, camera parameterization, JSON schema, and
artifact writing path.

Limitations:

- it does not prove real-photo segmentation,
- it can accidentally overfit to OpenSCAD render style,
- it must not cheat by injecting the known camera as the only viable sample.

Current acceptance bar:

- source reference render is shown in the panel,
- reference mask is shown separately,
- boundary overlay is mostly white,
- edge F1 is high,
- Chamfer / p95 miss are subpixel-to-near-pixel,
- no warnings.

### Real Pantheon photo/reference

Status: negative control so far, not a success proof.

The real Pantheon attempt exposed exactly why proof must be strict: bad mask
coverage, crop/scale/frame risks, and a visually broken collage should not be
shown as success.

Current use:

- keep as a hard negative/regression corpus,
- require diagnostics to catch full-frame masks, crop risks, bbox mismatch, and
  weak boundary metrics,
- do not report it as a successful fit until the source reference, mask, overlay,
  and fitted render all visually make sense.

### Reference backplate workflow

Status: planned.

The user suggested the 3D-editor workflow: put the reference image behind the
model and build/adjust the model over it. For `3d`, this becomes a backplate
diagnostic mode:

- lock the camera,
- render the model over the reference image,
- show boundary and alpha overlays in the same frame,
- let iterative model changes compare against the fixed camera.

This is a workflow and visualization layer, not a metric by itself. It should
make failures easier to inspect.

## Spatial-aware model approaches to test

These are not production yet. They should be tested with resource limits and
visual review, not enabled blindly.

### E0. Deterministic synthetic baseline

Goal: prove camera parameterization and metric math.

Inputs:

- asymmetric OpenSCAD model,
- render-derived reference image,
- derived reference mask.

Metrics:

- boundary F1,
- Chamfer,
- p95 contour miss,
- area IoU,
- SSIM as secondary.

### E1. OpenCV contour/mask baseline on real references

Goal: establish the cheap local baseline before heavier models.

Methods:

- thresholding,
- morphology,
- contour filtering,
- edge maps,
- fill/crop/border diagnostics.

Expected result:

- useful for clean references,
- likely insufficient for cluttered real photos.

### E2. rembg / background-removal segmentation

Goal: improve mask quality for real photos.

Risks:

- first-run model download,
- foreground holes,
- wrong object selected,
- masks that look good globally but have bad engineering edges.

Acceptance:

- must improve boundary metrics and visual panel, not just make a prettier mask.

### E3. SAM2-style segmentation

Goal: test stronger segmentation when a prompt/box/point can identify the object.

Risks:

- GPU/VRAM requirements,
- prompt sensitivity,
- licensing/model availability,
- impressive masks that still cut off thin geometry.

Acceptance:

- compare against OpenCV and rembg on the same references,
- report resource usage,
- require visual review.

### E4. Depth Anything / monocular depth priors

Goal: add spatial awareness beyond 2D silhouette.

Possible uses:

- reject camera poses with impossible depth ordering,
- detect front/back or mirrored pose ambiguity,
- weight features that should be in front,
- identify cases where silhouette matches but 3D layout is wrong.

Risks:

- relative depth is not metric depth,
- CAD render and real photo domains differ,
- depth maps can be plausible but geometrically wrong.

Acceptance:

- only use as an auxiliary prior until repeated experiments show it rejects
  failures that contour metrics miss.

### E5. Multi-view fit

Goal: fit one model against several references at once.

Methods:

- per-view camera candidates,
- shared model/scale constraints,
- multi-view boundary score,
- optional COLMAP/Meshroom only when multiple real photos exist.

Risks:

- heavier runtime,
- photo metadata and feature matching may fail on smooth/textureless CAD parts,
- more complicated proof panels.

### E6. Human-review backplate loop

Goal: make the tool useful even before full automation is solved.

Workflow:

- place reference as backplate,
- render model with locked camera,
- show contour/alpha/difference overlays,
- iterate model geometry,
- keep every accepted step auditable.

This is likely the best near-term product UX because it mirrors how humans align
models over reference images in a 3D editor.

## Acceptance policy

A `fit-camera` result may be marked `ok` only when all of these are true:

- source reference image is present in the proof panel,
- reference mask is visible and plausible,
- boundary overlay is visually inspected,
- boundary F1/recall are high,
- Chamfer and p95 contour miss are low,
- no mask fill/crop/frame/bbox warnings,
- the result is not accepted solely because area IoU or SSIM is high.

A result should be marked `warning` when:

- the reference mask is tiny or near full-frame,
- the rendered silhouette touches the frame border,
- render/reference bbox scale differs strongly,
- boundary F1/recall are weak,
- Chamfer or p95 miss is high,
- optimizer parameters hit search bounds,
- the proof panel is visually broken.

## Immediate planned work

1. Finish the boundary-first `fit-camera` implementation and e2e proof.
2. Make JSON schema include true contour distance metrics.
3. Add negative tests where area IoU is misleading but boundary metrics fail.
4. Run synthetic proof with source reference included.
5. Run real-reference experiments as negative controls until proof panels look
   correct.
6. Implement the spatial-aware experiment harness from E0-E6.
7. Send only visually inspected proof panels to Telegram as success evidence.
