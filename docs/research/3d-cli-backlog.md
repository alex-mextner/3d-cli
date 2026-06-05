---
title: "3d CLI - implementable backlog of algorithms, tools & metrics"
subtitle: "Prioritized, actionable items distilled from the research report, for folding into the ROADMAP"
date: "June 2026"
geometry: margin=2cm
fontsize: 10pt
colorlinks: true
---

# How to read this

Each item is `{what, why, paper/tool, integration point, expected metric, status}`.

- **Integration point** = the `3d` subcommand it lands in (ROADMAP surface: `3d render`,
  `3d check`, `3d slice`, `3d pack`, `3d strength`, `3d fea`, `3d kinematics`,
  `3d fit-camera`, `3d ai <tool> do|review|loop`, `3d ai bench`, `3d metrics`, `3d web`).
- **Status** flags how much already exists, checked against the ROADMAP's own `✅/🔨/📋`:
  - **PRESENT** - shipped (✅) or in active progress (🔨); item = formalise / harden / cite.
  - **PARTIAL** - named/planned in the ROADMAP (📋) but underspecified; item = the concrete
    algorithm + formula + library + metric to make it real.
  - **NET-NEW** - not in the ROADMAP; a genuinely new capability.
- **Expected metric** = the standard, reproducible number that proves the item works, drawn
  from the precise definitions in report Section 22 (record the convention: Chamfer `k`,
  F-score `tau`, Hausdorff directed/symmetric).

Priority tiers reflect leverage-per-effort and dependency order (P0 unblocks the rest).

Section references below point into `report.md`.

---

# P0 - the measurement foundation (everything depends on these)

## P0.1 Lock the match metric: silhouette IoU + AE + overlay (`3d match`, `3d metrics`)
- **What**: a deterministic `render -> binary mask -> {IoU, AE} + overlay/edge-overlay`
  primitive. ImageMagick recipes; IoU = `|S AND R| / |S OR R|`; AE = mismatched-pixel count
  with `-fuzz`. Emit the overlay (red=ref, cyan=render) for the critic.
- **Why**: without a camera-locked render and a numeric metric, every AI/match step is
  guesswork; this is the single highest-leverage, lowest-effort piece (report 8.2 item 1).
- **Tool/paper**: ImageMagick `compare`; report Sections 5.1, 7.3, 9.4.
- **Integration point**: `3d match` (the `match` ai-tool's analytical core) + `3d metrics`.
- **Expected metric**: silhouette IoU (0..1, target agreed per-part), AE (px); registration
  sanity = same `WxH!` + same crop.
- **Status**: PARTIAL - `3d fit-camera` (✅) produces the locked pose; the IoU/AE/overlay
  scoring primitive itself is not yet a standalone command.

## P0.2 Forced-monotonic acceptance loop with changelog (`3d ai <tool> loop`)
- **What**: orchestrator that applies ONE critic-proposed edit, re-scores, and **accepts only
  on strict metric improvement AND all hard gates PASS**; else reverts + resamples (cap
  ~10/round); invalid render = worst score (zero-reward anchor); feeds a changelog back to the
  critic. Coarse-to-fine phase controller freezes parameter subsets.
- **Why**: this single rule is what turns "an LLM fiddling with numbers" into a convergent
  optimiser; the FlipFlop effect (~46% flips, ~17% accuracy drop when challenged) means
  self-judged loops oscillate without bound (report 3.1-3.2, 7.4, 17.2).
- **Tool/paper**: ReLook (https://arxiv.org/abs/2510.11498); FlipFlop
  (https://arxiv.org/abs/2311.08596).
- **Integration point**: `3d ai <tool> loop` (the universal `loop` operator, ROADMAP §13.1).
- **Expected metric**: monotone non-decreasing IoU trajectory; rounds-to-converge;
  reject-rate; final IoU >= target.
- **Status**: PARTIAL - ROADMAP §13.1 names `loop` as a universal operator; the
  forced-monotonic+changelog+zero-reward mechanics are the concrete spec to implement.

## P0.3 Camera-pose fit by silhouette-IoU / reprojection, then freeze (`3d fit-camera`)
- **What**: fit `(ortho-scale, in-plane translation, small roll)` - 3-4 DoF for a side
  elevation - by maximising silhouette IoU (or minimising reprojection error on marked
  landmarks); coarse grid + local hill-climb; then **hold the pose fixed** through the shape
  match.
- **Why**: a drifting pose makes the monotonic-acceptance score meaningless ("never improves
  for no reason" - the top failure signature). Pose fit is the *precondition* for trusting
  the metric (report 24, 17.4).
- **Tool/paper**: analysis-by-synthesis pose estimation; report Section 24.
- **Integration point**: `3d fit-camera` (already ✅) - add the reprojection-error mode and
  the freeze-pose discipline as the documented contract.
- **Expected metric**: silhouette IoU at fitted pose; reprojection error (px); rotation/
  translation error if ground-truth pose known (ROADMAP §13.4 camera/pose metrics).
- **Status**: PRESENT (✅) - formalise the objective + reprojection variant + freeze rule.

---

# P1 - the standard metric battery (unblocks `3d ai bench` and regression tracking)

## P1.1 Geometry metrics with pinned conventions (`3d metrics`, `3d ai bench`)
- **What**: implement **F-score@tau** (primary), **Chamfer (L1/L2, mean, bidirectional)**,
  **Hausdorff (symmetric)**, **normal consistency**, **volumetric IoU** - each recording its
  convention (tau, `k`, directed/symmetric) in the store.
- **Why**: ROADMAP §13.4 lists these by name but a benchmark store is worthless if the
  convention drifts between runs; Tatarchenko et al. show IoU/Chamfer alone mislead, F-score
  is the robust primary (report 22.1).
- **Tool/paper**: Tatarchenko CVPR 2019 (https://arxiv.org/abs/1905.03678); `open3d`/`trimesh`
  for distances/IoU, `pymeshlab` `get_hausdorff_distance`, numpy for F-score@tau & NC.
- **Integration point**: `3d metrics` (per-run) + `3d ai bench` (suite).
- **Expected metric**: F-score@tau (0..1, tau ~1% bbox-diag), Chamfer (mm, state k),
  Hausdorff (mm, symmetric), NC (0..1), vol-IoU (0..1).
- **Status**: PARTIAL - metric names are in §13.4; precise formulas, library mapping, and
  convention-recording are net-new specification (report 22.1).

## P1.2 Image metrics with correct senses (`3d metrics`, `3d ai bench`)
- **What**: **silhouette IoU** (primary), **LPIPS** (perceptual), **SSIM/DSSIM** (mind the
  sense), **PSNR** (cheap baseline), **CLIP-sim image-image** (semantic).
- **Why**: silhouette IoU is the optimisation target but is blind to "does it look like a
  loco"; LPIPS and CLIP-sim add perceptual/semantic channels that catch failures IoU misses
  (report 22.2).
- **Tool/paper**: SSIM (Wang 2004, https://www.cns.nyu.edu/pub/eero/wang03-reprint.pdf),
  LPIPS (Zhang CVPR 2018, https://arxiv.org/abs/1801.03924, `pip install lpips`), CLIPScore
  (Hessel 2021, https://arxiv.org/abs/2104.08718); ImageMagick for IoU/AE/SSIM.
- **Integration point**: `3d metrics` + `3d ai bench`.
- **Expected metric**: IoU (0..1, 1 best), LPIPS (0+, 0 best), SSIM (-1..1, 1 best) /
  DSSIM (0 best), PSNR (dB), CLIPScore (0..100).
- **Status**: PARTIAL - names in §13.4; the exact formulas + sense table + the DSSIM-vs-SSIM
  footgun guard are the net-new spec (report 22.2, 9.4).

## P1.3 OpenSCAD-LLM benchmark suite: ModelRift task, automated scoring (`3d ai bench`)
- **What**: adopt ModelRift's **image -> .scad -> iterate-via-CLI-render** task format, but
  replace its subjective 0-5 with **render-success rate + silhouette IoU + Chamfer vs target
  mesh + LPIPS** (keep a subjective column as one signal, not the only one). Persist every run
  to the longitudinal store.
- **Why**: ModelRift has the right task but a non-reproducible metric; CADBench-style
  benchmarks have automated metrics but target CadQuery/Blender, not OpenSCAD - this fills the
  exact gap (report 21.1-21.3, ROADMAP §13.4).
- **Tool/paper**: ModelRift (https://modelrift.com/blog/openscad-llm-benchmark); BlenderLLM/
  CADBench (https://arxiv.org/abs/2412.14203) as the methodology template.
- **Integration point**: `3d ai bench [suite]`, `3d ai bench --compare`.
- **Expected metric**: render-success rate (%), silhouette IoU, Chamfer (mm), LPIPS;
  deltas-vs-history.
- **Status**: PARTIAL - ROADMAP §13.4 already calls for exactly this; the literature grounding
  and the metric-swap design are supplied here.

---

# P2 - authoring & generation upgrades

## P2.1 Attachment-graph authoring convention (BOSL2) (`3d ai design`, `3d render`)
- **What**: standardise authoring as a BOSL2 **attachment graph** of parameterised proxies
  (boiler/smokebox/cab/domes/funnel), landmarks expressed as fractions of a parent dimension
  (`funnel_frac`), over a shared `constants.scad`.
- **Why**: ShapeAssembly's academic result confirms attachment graphs yield more plausible,
  edit-stable shapes than absolute transforms; it keeps the match-loop parameter space
  low-dim, decoupled, and unambiguous for monotonic acceptance (report 9.2, 19.2, 15.3).
- **Tool/paper**: ShapeAssembly (https://arxiv.org/abs/2009.08026); BOSL2
  (https://github.com/BelfrySCAD/BOSL2).
- **Integration point**: `3d ai design` (RAG manifest + house style) + `3d render` examples.
- **Expected metric**: edit-stability (a parent-dimension change doesn't break child placement
  - verifiable by re-render IoU delta); parameter count kept low.
- **Status**: NET-NEW (as an enforced convention; BOSL2 itself is a known dependency).

## P2.2 `3d ai design` skeleton bootstrap (program synthesis principles) (`3d ai design`)
- **What**: an operator that writes the initial parametric `.scad` skeleton from a reference
  (LLM-authored today; CSGNet/ShapeAssembly-style parser later), which the P0.2 loop then
  tunes. Borrow CSGNet's **render-reward** acceptance and ShapeAssembly's **structure+free-vars**
  factorisation now; defer training a synthesis network.
- **Why**: the match loop tunes a generator that must first exist; synthesis bootstraps it.
  The cheap, high-value part (factorisation + reward shape) is adoptable immediately (report
  19.1-19.4).
- **Tool/paper**: CSGNet (https://arxiv.org/abs/1712.08290), ShapeAssembly
  (https://arxiv.org/abs/2009.08026), DeepCAD (https://arxiv.org/abs/2105.09492).
- **Integration point**: `3d ai design do` (writes skeleton) feeding `3d ai design loop`.
- **Expected metric**: render-success of the generated skeleton; initial IoU before tuning;
  rounds-to-target after.
- **Status**: NET-NEW (the design tool is named in §13.3; skeleton-from-reference is new).

---

# P3 - perception scaffolding (one-shot reference pre-processing)

## P3.1 SAM 2 reference silhouette (`3d match` pre-pass)
- **What**: one prompt-click on the reference photo -> clean binary loco mask (+ optional
  per-feature sub-masks for funnel/boiler/cab), normalised to the render frame.
- **Why**: a clean reference silhouette is the foundation of the whole metric; hand-
  thresholding a busy photo is fragile. Highest-value single AI model for the pipeline
  (report 6.2, 9.6).
- **Tool/paper**: SAM 2 (https://arxiv.org/abs/2408.00714).
- **Integration point**: `3d match` pre-pass (produces `mask_ref`); per-feature masks feed
  per-feature IoU (report 15.2).
- **Expected metric**: per-feature IoU vector (sharpens critic targeting); mask quality is the
  input, not scored itself.
- **Status**: NET-NEW.

## P3.2 Monocular depth + Wonder3D normal-map as critic channels (`3d ai critique`)
- **What**: run **Depth-Anything-V2** (or **Marigold** for sharper edges) for a relative depth
  map, and **Wonder3D** once for a side-view **normal map**; hand both to the critic as extra
  channels (the depth axis the side silhouette can't show). Keep only depth+normal+silhouette;
  discard any generated mesh.
- **Why**: a single side silhouette under-determines depth; depth/normal priors give the
  critic a structured 3D cue without making any unprintable mesh the deliverable (report 15.4,
  20.3, 6.1).
- **Tool/paper**: Depth-Anything-V2 (https://github.com/DepthAnything/Depth-Anything-V2),
  Marigold (https://arxiv.org/abs/2312.02145), Wonder3D (https://arxiv.org/abs/2310.15008).
- **Integration point**: `3d ai critique` (extra input channels).
- **Expected metric**: qualitative critic-channel value; the proportional depth ordering as a
  consistency check (front-to-back mass order), not a scored number.
- **Status**: PARTIAL - depth/segmentation models are surveyed; Marigold and the Wonder3D
  normal-map-as-prior use are net-new; mesh-as-scaffold was already noted.

---

# P4 - structural & printability gates (citable, not hand-waved)

## P4.1 Anisotropic strength knockdown with peer-reviewed factors (`3d strength`)
- **What**: in the strength gate, multiply the allowable stress for any component **normal to
  the layer plane** by a material-specific knockdown: **PETG ~0.7-0.75x**, **PLA ~0.45x** of
  in-plane; make the allowable a function of slicing layer height; cite the sources in the
  gate notes.
- **Why**: orientation is the biggest lever on FDM strength, and the anisotropy magnitude is a
  real, citable number - PLA across-layer can be <half the in-plane strength (report 23.1-23.3).
- **Tool/paper**: Ahn et al. 2002 — https://www.emerald.com/insight/content/doi/10.1108/13552540210441166/full/html ;
  PMC9230522 — https://pmc.ncbi.nlm.nih.gov/articles/PMC9230522/
  (PET-G XY 19.27 / XZ 14.30 MPa, +34.8%; PLA XY 21.70 / XZ 9.55 MPa, +127%).
- **Integration point**: `3d strength` (knockdown factor) + `3d fea` (anisotropic material card).
- **Expected metric**: predicted-vs-allowable stress with SF per zone; anisotropy ratio applied;
  (not IoU - this is a strength gate).
- **Status**: PARTIAL - `3d strength` is planned (📋); the citable knockdown factors + layer-
  height coupling are the net-new grounding.

## P4.2 Orientation solver objective drives `3d pack` (`3d pack`, `3d strength`)
- **What**: the per-part orientation solver should prefer orienting each part so its principal
  tensile load lies in the XY (flat) plane and the weakest across-layer direction carries the
  least load.
- **Why**: XZ (on-edge) is consistently weakest in both PETG and PLA; orientation is a design
  output, not an afterthought (report 23.3).
- **Tool/paper**: PMC9230522; Ahn 2002.
- **Integration point**: `3d pack` orientation solver (ROADMAP §5) consuming `3d strength`'s
  anisotropy model.
- **Expected metric**: max across-layer tensile stress minimised; SF improvement vs naive
  orientation.
- **Status**: PARTIAL - `3d pack` orientation solving is planned (📋); the strength-driven
  objective is the net-new tie-in.

---

# P5 - situational / aspirational (only if a need appears)

## P5.1 Differentiable-rendering gradient fit (Mitsuba 3 / nvdiffrast) (`3d match`)
- **What**: backprop a silhouette/soft-IoU loss into parameters via a differentiable renderer
  - only after reimplementing the geometry differentiably.
- **Why**: faster convergence for *hundreds* of parameters; unnecessary for the few-dozen-param
  loco, where the gradient-free loop converges in a coffee-break (report 5.3, 9.8, 13.6).
- **Tool/paper**: Mitsuba 3 (https://www.mitsuba-renderer.org/), nvdiffrast
  (https://github.com/NVlabs/nvdiffrast).
- **Integration point**: `3d match` (alternative gradient-based backend).
- **Expected metric**: silhouette IoU; convergence steps vs gradient-free.
- **Status**: NET-NEW, low priority (last resort; abandons OpenSCAD as the differentiable
  generator).

## P5.2 Multi-view photogrammetry reference (COLMAP) (`3d fit-camera`, `3d match`)
- **What**: if the real loco is photographed from many angles, run SfM+MVS for a metric
  ground-truth shell; add a second-view IoU term to the match loss.
- **Why**: a single found photo under-constrains 3D; multiple real photos pin it. Only applies
  if such photos are captured (report 5.5, 15.4).
- **Tool/paper**: COLMAP (https://colmap.github.io/), Meshroom (https://alicevision.org/).
- **Integration point**: `3d fit-camera` (multi-view pose) + `3d match` (multi-view IoU).
- **Expected metric**: multi-view silhouette IoU; Chamfer vs the photogrammetric reference.
- **Status**: NET-NEW, situational.

---

# Top items at a glance

| # | Item | Subcommand | Metric | Status |
|---|------|-----------|--------|--------|
| P0.1 | Silhouette IoU+AE+overlay primitive | `3d match` | IoU, AE | PARTIAL |
| P0.2 | Forced-monotonic loop + changelog | `3d ai <t> loop` | monotone IoU, rounds | PARTIAL |
| P0.3 | Camera-pose fit + freeze | `3d fit-camera` | IoU, reprojection err | PRESENT |
| P1.1 | Geometry metrics (F-score@tau primary) | `3d metrics` | F-score, Chamfer, NC | PARTIAL |
| P1.2 | Image metrics (LPIPS, CLIP-sim, SSIM) | `3d metrics` | LPIPS, CLIP, SSIM | PARTIAL |
| P1.3 | OpenSCAD-LLM bench (auto-scored) | `3d ai bench` | render-rate, IoU, Chamfer | PARTIAL |
| P2.1 | Attachment-graph authoring | `3d ai design` | edit-stability | NET-NEW |
| P2.2 | `.scad` skeleton bootstrap | `3d ai design` | init IoU, rounds | NET-NEW |
| P3.1 | SAM 2 reference silhouette | `3d match` pre-pass | per-feature IoU | NET-NEW |
| P3.2 | Depth/Marigold + Wonder3D normals | `3d ai critique` | depth-order check | PARTIAL |
| P4.1 | Anisotropic strength knockdown | `3d strength` | stress vs allow, SF | PARTIAL |
| P4.2 | Strength-driven orientation | `3d pack` | min across-layer stress | PARTIAL |
| P5.1 | Differentiable-render fit | `3d match` | IoU, steps | NET-NEW (low) |
| P5.2 | Multi-view photogrammetry | `3d fit-camera` | multi-view IoU | NET-NEW (sit.) |

**The critical path**: P0.1 -> P0.3 -> P0.2 unblocks the entire AI loop; P1.1/P1.2 unblock
`3d ai bench` (P1.3) and the longitudinal metrics store. P2-P4 are parallel improvements;
P5 is reach-for-when-needed.
