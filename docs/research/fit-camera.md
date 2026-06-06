# Fit-Camera Research Notes

This note tracks what has already been tried for `3d fit-camera`, why some
metrics are no longer accepted as proof, and what experiments remain planned.

## Current conclusion

`fit-camera` must be judged primarily by boundary alignment, not by filled-mask
area overlap and not by global SSIM.

The current research target is not a classic topology hash. A hash can retrieve
"similar-looking" candidates, but it does not normally provide a local direction
for improving camera pose. What `fit-camera` needs is a pose-aware objective:
an error surface that decreases when azimuth, elevation, distance, zoom, or
target translation moves toward the correct pose, at least inside a local basin.
Global monotonicity is not expected because single images are ambiguous and many
models have symmetric or near-symmetric silhouettes.

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

### Pose-sensitive objective / hash hypotheses

Status: active research.

User hypothesis: first recover enough spatial understanding from the reference,
then align the model with an error/hash that only improves when the camera moves
in the correct direction.

Closest known algorithm families:

- CAD/render pose estimation by render-and-compare over a view bank.
- Distance-transform templates and Chamfer matching for object detection and
  pose estimation.
- Differentiable silhouette rendering and silhouette-consistency pose losses.
- 6-DoF CAD-model pose estimation from a single RGB image by comparing rendered
  views to the observed object.
- Robust 3D registration after depth/point-cloud recovery: FPFH/SHOT/spin-image
  descriptors, RANSAC/ICP, TEASER++.
- 3D foundation models for geometry priors: DUSt3R, MASt3R, VGGT-style models
  that infer depth, point maps, correspondences, and sometimes camera
  parameters from one or more images.
- Topological signatures such as Reeb graphs or persistent homology. These are
  useful for shape identity but are probably too invariant to be the primary
  pose objective.

Working position:

There is probably no single global pose-sensitive hash for this task. The
practical design should combine a coarse retrieval descriptor with a local
pose-aware energy:

1. build a broad render bank over azimuth, elevation, distance, field of view,
   and target translation,
2. compute cheap descriptors from silhouette boundary, distance-transform
   samples, depth, and normals,
3. retrieve top-K camera basins,
4. refine top-K with symmetric boundary Chamfer / signed distance fields,
5. optionally add depth or pointmap priors from a spatial model,
6. prove local directional behavior with finite-difference perturbations around
   synthetic hidden-camera cases.

Experiment H1: boundary distance-transform energy.

- Hypothesis: a reference boundary distance field gives a smoother local error
  surface than area IoU or binary edge F1.
- Build: extract reference boundary, compute distance transform, render model
  boundary for a candidate camera, score candidate boundary pixels by distance
  to the reference and symmetrically score reference pixels against the render.
- Direction test: from a wrong camera near the hidden synthetic truth, perturb
  azimuth/elevation/distance/target both toward and away from truth. The error
  should drop more often in the toward direction.
- Expected failure: if the wrong pose has a similar silhouette, the field can
  prefer the wrong basin.
- Decision rule: keep as local refinement if directional accuracy is high inside
  the correct basin; do not use it as the only global retrieval method.

Experiment H2: multi-scale Chamfer field.

- Hypothesis: coarse blurred/dilated fields avoid zero-overlap cliffs and make
  early search less brittle; fine fields recover crisp boundaries after the
  correct basin is found.
- Build: score the same candidate at several edge-map scales or distance-field
  clipping radii, using coarse scales first and fine scales for top-K.
- Proof: evolution video should show broad pose correction first, then smaller
  boundary shifts.
- Expected failure: coarse scales may over-reward filled blobs and move toward
  a back-facing silhouette.
- Decision rule: keep only if a final fine-scale boundary gate can reject the
  bad basin.

Experiment H3: view-bank descriptor retrieval.

- Hypothesis: a large render bank can choose the correct pose basin before local
  optimization, making false back-side convergence less likely.
- Build: render silhouettes/depth/normal previews for many camera candidates;
  store compact descriptors such as boundary histograms, radial contour
  signatures, distance-transform samples, Hu/Zernike-like moments, and optional
  depth/normal summaries.
- Proof: synthetic hidden-camera references should retrieve a top-K set
  containing the true basin without being given the hidden pose.
- Expected failure: symmetric objects and frontal/back silhouettes can collide.
- Decision rule: use for coarse search only; require boundary/depth refinement
  before acceptance.

Experiment H4: finite-difference pose gradients.

- Hypothesis: even without differentiable rendering, finite differences over
  OpenSCAD renders can estimate useful local gradients for azimuth, elevation,
  distance, fov, and target translation.
- Build: around the best candidate, render small positive/negative perturbations
  per parameter and estimate directional derivatives of the boundary-field
  energy.
- Proof: synthetic hidden-camera diagnostic plots must mark whether the
  negative gradient points toward the hidden pose.
- Expected failure: expensive renders and discontinuous visibility at silhouette
  events.
- Decision rule: useful for proof diagnostics and slow `--proof` refinement, not
  default fast mode until render cost is bounded.

Experiment H5: spatial/depth prior.

- Hypothesis: a monocular depth or pointmap prior can break front/back silhouette
  ambiguity by adding approximate 3D ordering.
- Build: optional tier that runs available spatial models or preprocessors to
  produce depth/pointmap/normal cues, then compares them with depth/normal
  renders of candidate cameras.
- Proof: cases where silhouette alone selects the back should be rejected or
  re-ranked when depth/normal mismatch is considered.
- Expected failure: real photos, statues, and architecture can have poor
  monocular depth, missing scale, or clutter.
- Decision rule: optional diagnostic/refinement tier with graceful skip; never
  silently required for core `fit-camera`.

Experiment H6: 2D-to-3D correspondence and registration.

- Hypothesis: if image features can be tied to model features, robust
  registration can estimate pose more directly than silhouette search.
- Build: detect reference contours/corners/keypoints, render model feature
  candidates from many views, match topological/visual feature graphs, then
  solve pose with PnP/RANSAC or 3D registration if depth exists.
- Proof: asymmetric synthetic models should recover pose with fewer candidates
  than brute-force render search.
- Expected failure: OpenSCAD renders and real photos may not share texture or
  stable keypoints; pure silhouettes have weak correspondences.
- Decision rule: research-only until repeatable feature correspondences exist.

Experiment H7: topology signatures.

- Hypothesis: topology descriptors can reject the wrong object or grossly wrong
  segmentation before pose fitting.
- Build: compute contour topology or skeleton summaries from the reference mask
  and from rendered model views; use them as a filter before expensive scoring.
- Proof: reject masks with extra background components, holes, or missing object
  parts that would otherwise produce a misleading area IoU.
- Expected failure: topology is often pose-invariant by design and therefore
  does not tell which way to rotate the camera.
- Decision rule: useful as a validity/crop/mask diagnostic, not a primary
  pose-improving hash.

Research sources and search anchors:

- "Distance transform templates for object detection and pose estimation" is a
  direct ancestor for boundary distance-field matching.
- "Analytical Derivatives for Differentiable Renderer: 3D Pose Estimation by
  Silhouette Consistency" and related differentiable rendering pose-estimation
  papers explain why silhouette losses can provide pose gradients but also why
  visibility discontinuities are difficult.
- DRWR-style smooth silhouette losses show how distance fields can be used when
  binary masks have non-informative gradients.
- 6-DoF pose estimation from a single RGB image and CAD model retrieval papers
  validate the render-bank plus feature-similarity framing.
- TEASER++/ICP/FPFH/SHOT-style registration is relevant only after there is a
  reference depth/point cloud or reliable 2D-to-3D correspondences.
- DUSt3R, MASt3R, and VGGT are the current spatial-prior candidates for
  producing approximate depth, pointmaps, or camera estimates from image data.

Implementation notes for `3d-cli`:

- Start with pure render-and-compare because it fits the existing OpenSCAD
  pipeline and is testable with hidden-camera synthetic references.
- Add `--search broad` or `--proof-search broad` rather than changing the fast
  default path silently.
- Store proof diagnostics as JSON plus PNG/MP4 artifacts:
  `reference`, `reference_mask`, `candidate_grid`, `best_fit`, `boundary_overlay`,
  `error_vs_iteration`, and `finite_difference_direction`.
- A synthetic proof is accepted only if the hidden camera is not passed into the
  fitting command. The hidden pose is used after fitting only for evaluation.
- A real-photo proof is accepted only if visual review and boundary metrics agree.
  If the search locks onto the back side or a wrong crop, the result is
  `fail`/`diagnostic`, not `ok`.

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
