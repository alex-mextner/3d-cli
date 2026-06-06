# Fit-Camera Research Notes

This note tracks what has already been tried for `3d fit-camera`, why some
metrics are no longer accepted as proof, and what experiments remain planned.

## Current conclusion

`fit-camera` must be judged primarily by boundary alignment, not by filled-mask
area overlap and not by global SSIM.

As of 2026-06-06, current results do not prove a finished spatial-aware
`fit-camera`. Synthetic diagnostics and proxy-alignment tools are useful, but
the real-reference path still fails on visible alignment in the hard cases. Do
not report mask-only, contour-only, proxy-only, point-cloud, hull, or heatmap
artifacts as success. They are diagnostics unless the original reference and the
model render visibly align in the same frame.

Non-negotiable proof report package for any claimed `fit-camera` or
spatial-awareness success. This is the completion bar, not a claim that the
current CLI already emits every final schema field:

1. original reference image;
2. fitted model render in the same image frame;
3. boundary/alpha/error overlay that exposes mismatch;
4. reference mask or segmentation panel;
5. metrics JSON with boundary F1, symmetric contour Chamfer or SDF loss, p95
   miss, coverage/bbox/crop/border diagnostics;
6. explicit report label: success, warning, failure, or diagnostic-only.

If the visible reference/render/overlay disagree, the result is not a proof even
when a secondary metric looks good.

Telegram proof reports must make the human inspection path obvious. Send the
original reference and same-frame fitted render before or alongside masks, point
clouds, hulls, view-bank plots, optimizer charts, or other instrumental images.
Links may be included as supplemental pointers, but they do not replace sending
the core reference/render evidence. Every visual claim must name the artifact
path and explain the evidence in plain language. A report like "final PNGs are
visually normal" is invalid unless it says which PNGs, what visible alignment
was checked, and whether the run is success, warning, failure, or
diagnostic-only.

The current research target is not a classic topology hash. A hash can retrieve
"similar-looking" candidates, but it does not normally provide a local direction
for improving camera pose. What `fit-camera` needs is a pose-aware objective:
an error surface that decreases when azimuth, elevation, distance, zoom, or
target translation moves toward the correct pose, at least inside a local basin.
Global monotonicity may be possible for many practical objects if it is defined
over observable pose parameters and known equivalence classes. Symmetry is not
automatically a failure: a sphere has unobservable rotation, and a model with two
identical entrances can have multiple equally valid azimuths. The objective must
distinguish true equivalence/plateaus from a wrong pose that only happens to
match a weak silhouette.

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

There may not be a single scalar hash that is globally monotonic in raw camera
parameters, but a practical objective can still be monotonic over the quotient
space of visually distinguishable poses. The design should combine a coarse
retrieval descriptor with a pose-aware energy and explicit equivalence handling:

1. build a broad render bank over azimuth, elevation, distance, field of view,
   and target translation,
2. compute cheap descriptors from silhouette boundary, distance-transform
   samples, depth, and normals,
3. retrieve top-K camera basins,
4. refine top-K with symmetric boundary Chamfer / signed distance fields,
5. optionally add depth or pointmap priors from a spatial model,
6. detect symmetric or unobservable degrees of freedom when several basins are
   genuinely equivalent,
7. prove directional behavior with finite-difference perturbations around
   synthetic hidden-camera cases, comparing pose error modulo known symmetries.

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
- Expected failure: non-equivalent frontal/back silhouettes can collide when the
  descriptor is too weak. Truly symmetric views should be reported as equivalent
  solutions instead of false failures.
- Decision rule: use for coarse search only; require boundary/depth refinement
  before acceptance, and compare final pose modulo declared or detected
  symmetries.

Experiment H4: finite-difference pose gradients.

- Hypothesis: even without differentiable rendering, finite differences over
  OpenSCAD renders can estimate useful local gradients for azimuth, elevation,
  distance, fov, and target translation.
- Build: around the best candidate, render small positive/negative perturbations
  per parameter and estimate directional derivatives of the boundary-field
  energy.
- Proof: synthetic hidden-camera diagnostic plots must mark whether the
  negative gradient points toward the hidden pose or an equivalent pose.
- Expected failure: expensive renders and discontinuous visibility at silhouette
  events.
- Decision rule: useful for proof diagnostics and slow `--proof` refinement, not
  default fast mode until render cost is bounded.

Experiment H4b: pose equivalence and unobservable degrees of freedom.

- Hypothesis: some objects have several equally valid fitted poses, and the
  metric should report equivalence instead of forcing an arbitrary "front".
- Build: add synthetic fixtures with known symmetries: sphere-like object,
  rotationally repeated object, and a two-entrance yurt-style object. Evaluate
  pose recovery modulo those symmetries.
- Proof: the diagnostic should show flat/periodic error curves along
  unobservable rotations, while still decreasing with observable camera
  translation, distance, and scale corrections.
- Expected failure: if the model is only approximately symmetric, treating poses
  as exactly equivalent may hide a real mismatch.
- Decision rule: accept equivalent poses only when geometry or measured render
  descriptors demonstrate equivalence; otherwise keep the stricter directional
  pose test.

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
  fitting command. The hidden pose is used after fitting only for evaluation,
  and pose error should be measured modulo declared or detected symmetries.
- A real-photo proof is accepted only if visual review and boundary metrics agree.
  If the search locks onto the back side or a wrong crop, the result is
  `fail`/`diagnostic`, not `ok`, unless the candidate is demonstrably equivalent
  under the object's geometry.

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

A `fit-camera` result may be marked `success` only when all of these are true:

- source reference image is present in the proof panel,
- reference mask is visible and plausible,
- boundary overlay is visually inspected,
- boundary F1 and edge-hit diagnostics are high,
- Chamfer and p95 contour miss are low,
- no mask fill/crop/frame/bbox warnings,
- the result is not accepted solely because area IoU or SSIM is high.

A result should be marked `warning` when:

- the reference mask is tiny or near full-frame,
- the rendered silhouette touches the frame border,
- render/reference bbox scale differs strongly,
- boundary F1 or edge-hit diagnostics are weak,
- Chamfer or p95 miss is high,
- optimizer parameters hit search bounds,
- the proof panel is visually broken.

Here "edge-hit diagnostics" means the fraction of rendered/reference boundary pixels within
a stated tolerance when an experimental variant reports precision/recall-style hit rates.

## Spatial-awareness research log, 2026-06-06

Fixed log format:

```text
hypothesis -> related algorithms/papers/tools -> assembled 3d-cli variant ->
expected proof -> experiment/artifacts -> result -> decision
```

The standing goal is not just a higher score. A credible camera fit must show
that the render and reference share a frame, the boundary error is small, and
local camera changes have an error gradient that points back toward the hidden
or intended pose when such a pose is identifiable.

### Summary decision

Global pose-sensitive monotonicity may be possible in many practical cases when
it is defined over pose equivalence classes rather than raw poses. Exact
symmetries and unobservable degrees of freedom should become
plateaus/equivalent minima, not failures. The hard requirement is different:
non-equivalent wrong views, such as a false backside selection on asymmetric
geometry, must remain distinguishable.

The practical assembly is a staged objective:

1. Segment the visible subject and reject crop/scale failures.
2. Declare or detect pose equivalence classes and unobservable degrees of
   freedom.
3. Retrieve a coarse view-bank candidate using contour descriptors and boundary
   distance.
4. Refine locally with a boundary signed-distance-field loss, symmetric
   Chamfer, edge F1, p95 boundary distance, and coverage/border priors.
5. Add depth/normal priors when the renderer can expose them; OpenSCAD PNG-only
   renders currently provide only a weak RGB shading proxy.
6. Prove local pose sensitivity and equivalence behavior with synthetic
   hidden-camera truth before trusting the same objective on real photographs.

### H1: image backplate loop

Hypothesis: A standard camera pose plus reference backplate/plane and model
overlay makes frame mismatch obvious and prevents hidden crop/scale wins.

Related algorithms/papers/tools: CAD-on-image registration, visual hull
inspection, OpenSCAD image render, alpha/backplate compositing, edge overlay
review panels.

Assembled 3d-cli variant: `3d fit-camera --spatial-report DIR` writes
`proof_panel.png` as `backplate/reference | mask | fitted render | edge overlay`;
`--backplate FILE` supplies the original photo/render when `--ref` is a derived
mask, and `--trace FILE` feeds a candidate-evolution demo.

Expected proof: A reviewer can see whether the model occupies the same image
frame as the reference. The panel must not rely on area IoU text alone.

Experiment/artifacts:

- `/tmp/3d-spatial/synthetic-oracle/spatial-report/proof_panel.png`
- `/tmp/3d-spatial/pantheon-gflash-front/contour/spatial-report/proof_panel.png`

Result: The synthetic panel is readable and exposes residual boundary drift.
The Pantheon panel fails visibly and is accompanied by a spatial warning.

Decision: Keep. It is a required diagnostic artifact for fit-camera proofs.

### H2: contour-first objective

Hypothesis: Boundary metrics are harder to game than filled-area IoU because
they penalize shape drift even when area overlap looks acceptable.

Related algorithms/papers/tools: Hausdorff distance, symmetric Chamfer distance,
edge F1 under a tolerance radius, distance transforms, ICP-like contour
matching.

Assembled 3d-cli variant: `3d fit-camera --objective contour` combines edge
F1@4, symmetric Chamfer, boundary signed-distance-field loss, p95 boundary
distance, coverage ratio, and border-touch penalties.

Expected proof: On a synthetic oracle, boundary loss should be near zero at the
hidden camera and should increase under local perturbations. On real photos,
poor crop/scale must fail loudly instead of producing a plausible single score.

Experiment/artifacts:

- `/tmp/3d-spatial/synthetic-oracle/spatial-report/spatial_metrics.json`
- `/tmp/3d-spatial/synthetic-oracle/spatial-report/edge_overlay.png`
- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.json`
- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.png`

Result: Synthetic final fit is a partial pass: same-frame, sane coverage, no
spatial warning, but not exact camera recovery. Hidden-camera perturbation
sweeps are locally monotone for azimuth, elevation, distance, target-x, and
target-z in the tested window.

Decision: Keep as experimental objective. Do not make it an acceptance proof
without synthetic local-gradient diagnostics or multi-view constraints.

### H3: segmentation tiers

Hypothesis: Camera fitting quality is capped by the reference mask. A better
segmentation tier should reduce contour false positives and improve boundary
metrics, while missing heavy resources must degrade gracefully.

Related algorithms/papers/tools: OpenCV GrabCut/contours, `rembg` U2-Net
salient mask, SAM/SAM2 prompted segmentation, Depth Anything V2 monocular depth.

Assembled 3d-cli variant: `3d preprocess --force-fallback` is the baseline. The
experiment harness emits optional `rembg` and Depth Anything commands; SAM2
remains checkpoint-driven and manual.

Expected proof: Mask panel shows the intended subject; contour metrics improve
against the same model/camera search. Heavy tiers may skip, but must not leave
missing artifacts.

Experiment/artifacts:

- `/tmp/3d-spatial/pantheon-gflash-front/preprocess/mask.png`
- `/tmp/3d-spatial/pantheon-gflash-front/preprocess/depth.png`

Result: Pantheon fallback GrabCut produced a usable negative baseline, but
included background/occluder regions and later failed the real-image spatial
diagnostic.

Decision: Keep fallback as baseline only. Evaluate `rembg`/SAM2 separately
before claiming real-photo success.

### H4: multi-view and synthetic oracle

Hypothesis: A single silhouette can accept wrong geometry or pose; a synthetic
oracle with known camera and multi-view references exposes whether the optimizer
is actually recovering pose rather than just matching projection area.

Related algorithms/papers/tools: synthetic render oracle, multi-view
consistency, visual hull constraints, coarse view banks.

Assembled 3d-cli variant:
`tools/spatial_fit_experiment.py --run-synthetic-oracle` creates an asymmetric
SCAD model, renders a hidden-camera reference, runs contour fit, writes proof
artifacts, and evaluates view-bank retrieval.

Expected proof: The known view ranks top in a coarse view bank; local
perturbation losses decrease toward truth; candidate-evolution frames show
visible progress.

Experiment/artifacts:

- `/tmp/3d-spatial/synthetic-oracle/known_reference.png`
- `/tmp/3d-spatial/synthetic-oracle/known_mask.png`
- `/tmp/3d-spatial/synthetic-oracle/demo/candidate_evolution.gif`
- `/tmp/3d-spatial/synthetic-oracle/demo/candidate_evolution.mp4`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank.json`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank_heatmap.png`

Result: The coarse view bank ranked the hidden camera grid point top-1:
azimuth 135 deg, elevation 20 deg, boundary SDF loss 0.112 px. The optimizer
itself still landed on a different but plausible silhouette fit, so view-bank
seeding is likely needed.

Decision: Add view-bank seeding before local contour refinement in a future
command change. Keep the current harness as the proof generator.

### H5: real image diagnostic

Hypothesis: Real references must warn/fail when segmentation, perspective, crop,
or scale make a camera fit untrustworthy.

Related algorithms/papers/tools: crop detection, bbox IoU, coverage ratio,
centroid drift, Hausdorff/p95 boundary distance, visual QA panels.

Assembled 3d-cli variant: `--spatial-report` includes `spatial_warning` when
coverage, bbox, p95, Chamfer, or border conditions are suspicious.

Expected proof: Pantheon negative control should not pass just because a
silhouette overlap exists.

Experiment/artifacts:

- `/tmp/3d-spatial/pantheon-gflash-front/contour/camera.json`
- `/tmp/3d-spatial/pantheon-gflash-front/contour/spatial-report/proof_panel.png`

Result: Fail/warn as intended: `area_iou=0.2386`, `coverage_ratio=0.38`,
`bbox_iou=0.27`, `edge_chamfer_px=25.4`,
`boundary_sdf_loss_px=24.4`, `hausdorff_p95_px=73.1`.

Decision: Keep warnings as diagnostic failures for real-image proofs.

### H6: pose-sensitive hash or objective

Hypothesis: The requested descriptor would have an error that decreases only
when moving in the correct pose direction.

Related algorithms/papers/tools: shape contexts, image moments,
Fourier/Zernike descriptors, Chamfer matching, distance-transform matching,
differentiable rendering, finite-difference gradients, view-bank retrieval,
depth/normal consistency.

Assembled 3d-cli variant:

- Boundary signed-distance field and symmetric Chamfer in
  `lib/spatial_fit_metrics.py`.
- `--objective contour` consumes the SDF/Chamfer/F1/p95 mix.
- `tools/spatial_fit_experiment.py --run-synthetic-oracle` writes
  finite-difference sweeps for azimuth, elevation, distance, target-x, and
  target-z.
- The same harness writes a coarse view-bank retrieval heatmap over
  azimuth/elevation.
- Depth/normal mismatch is documented as unavailable for OpenSCAD PNG-only
  renders; the harness records `rgb_shading_mse_proxy` only as a weak proxy.

Expected proof: Around a synthetic hidden camera, stepping toward the truth
lowers boundary SDF loss and stepping away raises it. A coarse view bank should
rank the hidden view first or near-first before local refinement.

Experiment/artifacts:

- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.json`
- `/tmp/3d-spatial/synthetic-oracle/pose-sensitivity/pose_sensitivity.png`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank.json`
- `/tmp/3d-spatial/synthetic-oracle/view-bank/view_bank_heatmap.png`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.json`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.png`

Result: Local monotonicity held in the synthetic asymmetric model for all five
tested axes; each axis reported `monotonic_fraction=1.0` and
`near_truth_all_toward_steps_improve=true`. The view bank also recovered the
hidden azimuth/elevation as top-1. Equivalence diagnostics then separated three
cases: sphere azimuth was an unobservable plateau with `loss_range_px=0.0`; the
fourfold model accepted only azimuths `[45, 135, 225, 315]` as low-loss
modulo-90 equivalents; the asymmetric +180 degree backside stayed
non-equivalent with boundary SDF `6.46 px` versus truth `0.11 px`.

Where raw-pose monotonicity fails but equivalence-aware monotonicity can still
hold:

- Exact object symmetries produce valid plateaus or multiple equivalent minima.
- Unobservable degrees of freedom, such as sphere azimuth, should be reported as
  unobservable rather than rejected.
- Repeated architecture features can produce local minima where moving toward a
  different repeated column lowers the contour loss; this is acceptable only if
  the repetitions are declared or detected as equivalent.
- Cropped real images can make the correct pose look worse than a wrong scaled
  pose; this remains a diagnostic failure because it is not a pose equivalence.
- Depth/normal priors require a renderer that exposes those buffers; OpenSCAD
  PNG shading is not a reliable substitute.

Decision: Reject a raw-pose global hash as the contract. Keep an
equivalence-aware pose objective: plateaus are valid for declared/detected
symmetries, while non-equivalent wrong views must stay high-loss or receive a
diagnostic failure.

### H7: symmetry-aware equivalence classes

Hypothesis: A pose-sensitive objective can be globally useful if it is evaluated
modulo symmetry/equivalence classes. Symmetry is not failure; accepting a
non-equivalent backside on asymmetric geometry is failure.

Related algorithms/papers/tools: quotient spaces over transformation groups,
cyclic rotational symmetry detection, observability analysis, view-bank
retrieval modulo symmetry, distance-transform matching.

Assembled 3d-cli variant:

- `tools/spatial_fit_experiment.py --run-synthetic-oracle` now writes a
  `symmetry-equivalence` report.
- Sphere azimuth sweep detects an unobservable DOF plateau.
- A fourfold synthetic model checks azimuth modulo 90 degrees.
- The asymmetric oracle compares the true azimuth against +180 degree backside
  and marks it non-equivalent.

Expected proof:

- Sphere azimuth has small loss range and is labelled unobservable.
- Fourfold low-loss minima match the declared modulo-90 equivalence class.
- Asymmetric +180 degree backside has much higher boundary SDF loss than truth.

Experiment/artifacts:

- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.json`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/symmetry_equivalence.png`
- `/tmp/3d-spatial/synthetic-oracle/symmetry-equivalence/renders`

Result: Pass for the assembled synthetic diagnostics:

- Sphere azimuth loss range was `0.0 px`, and the DOF was labelled
  unobservable.
- Fourfold low-loss azimuths were `[45, 135, 225, 315]`, matching the declared
  modulo-90 equivalence class; non-equivalent sampled azimuths had boundary SDF
  about `5.36 px`.
- Asymmetric truth had boundary SDF `0.11 px`; +180 degree backside had
  `6.46 px`, so `backside_rejected=true`.

Decision: Treat equivalence classes as first-class metadata/diagnostics before
any future global pose-sensitive objective is promoted from research to command
behavior.

## Immediate planned work

1. Integrate the `roadmap/spatial-fit-experiments` branch only after rebasing it
   onto current `main` and replacing any misleading proof wording. Current
   status: useful experimental harness, not production success.
2. Integrate or supersede `roadmap/fit-camera-proof`. Current status: three
   commits ahead of old `origin/main`, behind current `main`, plus dirty
   `ROADMAP.md`; Pantheon remains diagnostic/failure, not a success proof.
3. Finish boundary-first `fit-camera` as a command feature: same-frame render,
   original reference, overlay, metrics JSON, and warning/failure status must be
   emitted by the normal CLI path.
4. Make JSON schema include true contour distance metrics and distinguish area
   IoU, SSIM, SDF loss, symmetric Chamfer, p95 miss, and diagnostic warnings.
5. Add negative tests where area IoU is misleading but boundary metrics fail.
6. Run synthetic hidden-camera proof with source reference included and without
   giving the hidden camera to the fitter.
7. Run real-reference experiments as negative controls until proof panels look
   correct. Pantheon must not be presented as success until reference/render
   overlay visibly matches.
8. Evaluate spatial-aware priors:
   - local Apple Silicon image-to-3D candidates from the provider research;
   - HF ZeroGPU/TRELLIS with auth when quota/key is needed;
   - monocular depth/normal/pointmap approaches such as Depth Anything,
     DUSt3R/MASt3R/VGGT-style models;
   - topological/descriptor filters only as validity or retrieval aids, not as
     a promised monotonic pose hash until proven.
9. For image-to-3D proxy meshes, add a render-against-original-reference gate
   before using the proxy as a `fit-camera` prior. A generated 3D mesh that does
   not resemble the original image is rejected regardless of proxy/CAD alignment.
10. Send every substantial result to Telegram with the proof package. If only
    instrumental diagnostics exist, label the report as diagnostic/failure.
