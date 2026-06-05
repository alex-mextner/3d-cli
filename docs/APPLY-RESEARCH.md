# APPLY-RESEARCH.md — From Surveyed Papers to `3d` Commands

This document maps every surveyed paper, algorithm, and tool from the research phase to a concrete `3d` CLI command, the library that implements it, and the metric or benchmark it feeds. It is the POST-survey deliverable promised in ROADMAP §12 / §27.

**Scope:** The research report (`docs/research/report.md`), benchmarks-and-metrics doc (`docs/research/benchmarks-and-metrics.md`), and ROADMAP §17 (research-driven backlog) are the sources. Each entry below states: *what it is*, *why it matters*, and *exactly how it lands in the CLI*.

---

## P0 — Measurement foundation (unblocks everything)

### P0.1 Silhouette IoU + AE + overlay primitive
- **What:** Image-based silhouette matching between a rendered `.scad` and a reference photo. IoU = `|S ∩ R| / |S ∪ R|` on binary masks; AE = absolute (pixel) error; overlay = red/cyan ghost-blend for visual critique.
- **Why:** The primary numeric signal for the pixel-match loop. Without it, the AI critic has no external arbiter.
- **Applied to:** `3d score`, `3d match`, `3d fit-camera` (camera-fit objective).
- **Library:** ImageMagick (`compare -metric AE`, set-operation masks for IoU, `-compose Screen` for overlay). `lib/cli/imaging.py` orchestrates the pure math.
- **Metric / benchmark:** IoU ∈ [0,1] (1 = perfect), AE ≥ 0 (0 = perfect). Persisted in the longitudinal store with frame dimensions and `-fuzz` value.
- **Source:** Report §5.1, §7.3; ROADMAP §3 / §7.

### P0.2 Forced-monotonic loop + changelog
- **What:** ReLook’s strict acceptance rule: an AI-proposed edit is accepted only if the score strictly improves AND all hard gates pass; otherwise revert, log, and resample. A running changelog is fed back to the critic so failed moves are never retried.
- **Why:** Prevents the FlipFlop effect (~46% answer flips, ~17% accuracy drop when LLMs are challenged) from turning the loop into an oscillator.
- **Applied to:** `3d match` (the pixel-match loop), `3d ai <tool> loop` (general AI operator).
- **Library:** `lib/match_loop.py` (existing, via `lib/commands/match.py`). Future generalization (`3d ai <tool> loop`) may refactor into `lib/ai/loop.py`.
- **Metric / benchmark:** Monotone non-decreasing IoU trajectory; rounds-to-converge; reject-rate; final IoU ≥ target.
- **Sources:** ReLook (arXiv:2510.11498), FlipFlop (arXiv:2311.08596); Report §3.1–3.2, §7.4; ROADMAP §13.1.

### P0.3 Camera-pose fit + freeze
- **What:** Fit the 3–4 DoF orthographic pose (scale, in-plane translation, small roll) by maximizing silhouette IoU or minimizing reprojection error on landmarks, then **freeze the pose** for the entire shape match.
- **Why:** A drifting pose makes the monotonic-acceptance score meaningless ("improves for no reason"). Pose freeze is the precondition for trusting any silhouette metric.
- **Applied to:** `3d fit-camera` (existing), `3d match` (consumes its output).
- **Library:** `numpy` + `Pillow` (random search + coordinate-descent refine). `lib/fit_camera.py` (called via `lib/commands/fit_camera.py`).
- **Metric / benchmark:** Reprojection error (px), rotation/translation error, final IoU at frozen pose.
- **Sources:** Report §7.1, §24; ROADMAP §7 / §13.4.

---

## P1 — Standard metric battery (unblocks `3d ai bench`)

### P1.1 Geometry metrics (pinned conventions)
- **What:** Chamfer distance (L1/L2, bidirectional), F-score@τ (τ ~1% bbox diagonal), Hausdorff (directed / symmetric), normal consistency, volumetric IoU.
- **Why:** Automated, reproducible 3D-shape evaluation. Tatarchenko et al. (CVPR 2019) shows F-score@τ is the least gameable primary metric; Chamfer/IoU alone mislead.
- **Applied to:** `3d ai bench` (geometry regime), `3d metrics`, `3d compare`.
- **Library:** `open3d` / `trimesh` for distances + vol-IoU; `scipy.spatial.cKDTree` for nearest-neighbor queries (Chamfer/F-score); `pymeshlab` `get_hausdorff_distance`; numpy for F-score@τ + normal-consistency.
- **Metric / benchmark:** F-score@τ ∈ [0,1] (primary); Chamfer ≥ 0; Hausdorff ≥ 0; NC ∈ [0,1]. Every record stores the convention (k, τ, directed/symmetric, voxel res) so the longitudinal store doesn’t drift.
- **Sources:** Tatarchenko (arXiv:1905.03678); Report §22.1; ROADMAP §13.4.

### P1.2 Image metrics (correct senses)
- **What:** LPIPS (perceptual), SSIM / DSSIM (structural), PSNR (pixel-level), CLIP-similarity (semantic).
- **Why:** Silhouette IoU is blind to "does it look like the subject"; LPIPS + CLIP add perceptual/semantic channels.
- **Applied to:** `3d ai bench` (no-mesh regime), `3d score`, `3d compare`.
- **Library:** `lpips` (Zhang CVPR 2018), `clip-score` (Hessel 2021), ImageMagick (SSIM/DSSIM/PSNR).
- **Metric / benchmark:** IoU (0..1, 1 best), LPIPS (≥0, 0 best), SSIM (−1..1, 1 best), **DSSIM (0..1, 0 best — guarded footgun)**, PSNR (dB, higher better), CLIP-sim (0..100, higher better). Senses are stored explicitly.
- **Sources:** Report §22.2; ROADMAP §13.4; benchmarks-and-metrics §2.

### P1.3 OpenSCAD-LLM benchmark (auto-scored)
- **What:** Adopt the ModelRift image→OpenSCAD→iterate task format, but replace its purely subjective 0–5 score with the automated metrics above (render-success rate + IoU + Chamfer + LPIPS). Keep a subjective score as one column, not the only one.
- **Why:** Fills the exact gap: ModelRift has the right task but a non-reproducible metric; CADBench-style benchmarks have automated metrics but target CadQuery/Blender, not OpenSCAD.
- **Applied to:** `3d ai bench [suite]`; `3d ai bench --compare` for longitudinal deltas.
- **Library:** `lib/ai/bench.py` orchestrates the pipeline; metrics libs from P1.1/P1.2.
- **Metric / benchmark:** Build-success rate (gate 0), then the full metric vector per sample. Persisted to `~/.local/share/3d-cli/metrics/*.jsonl`.
- **Sources:** ModelRift (https://modelrift.com/blog/openscad-llm-benchmark); BlenderLLM/CADBench (arXiv:2412.14203); Report §21; ROADMAP §13.4.

---

## P2 — Authoring & generation

### P2.1 Attachment-graph authoring (BOSL2)
- **What:** Standardize authored models as a BOSL2 attachment graph of parameterized proxies (e.g. boiler / smokebox / cab / domes / funnel), with landmarks expressed as *fractions of a parent dimension* over a shared `constants.scad`.
- **Why:** ShapeAssembly shows attachment graphs yield more plausible, edit-stable shapes than absolute transforms, and they keep the match-loop parameter space low-dimensional and decoupled.
- **Applied to:** `3d ai design review` (flags absolute transforms that should be parent-relative), `3d ai design do` (writes skeleton using attachment style).
- **Library:** BOSL2 (`attach()`, `position()`, `align()`) — vendored into `libs/` during bootstrap.
- **Metric / benchmark:** Re-render IoU delta after a parent-dimension change (child placement must not break).
- **Source:** ShapeAssembly (arXiv:2009.08026); Report §19.2, §9.2; ROADMAP §13.3 / §33.

### P2.2 `.scad` skeleton bootstrap
- **What:** Generate the initial parametric `.scad` skeleton from a reference (LLM-authored today; a CSGNet/ShapeAssembly-style parser later). Borrow program-synthesis principles: CSGNet’s render-reward acceptance and ShapeAssembly’s structure + free-variables factorization.
- **Why:** The match loop tunes a generator that must first exist; synthesis bootstraps it, and the cheap high-value part (factorization + reward shape) is adoptable immediately.
- **Applied to:** `3d ai design do` (mutating: writes the skeleton), then `3d ai design loop` tunes it.
- **Library:** No trained network yet — the `do` operator uses an LLM with the RAG pre-flight set (§13.2). The *principles* (render-reward, attachment graph) are hard-coded in the prompt and orchestrator.
- **Metric / benchmark:** Render-success of the skeleton; initial IoU before tuning; rounds-to-target after.
- **Sources:** CSGNet (arXiv:1712.08290), ShapeAssembly (arXiv:2009.08026), DeepCAD (arXiv:2105.09492); Report §19; ROADMAP §13.3.

---

## P3 — Perception scaffolding

### P3.1 SAM 2 reference silhouette
- **What:** One prompt-click on the reference photo → a clean binary subject mask via SAM 2 (+ optional per-feature sub-masks), normalized to the render frame. Falls back to Depth-Anything + GrabCut when SAM 2 is unavailable.
- **Why:** A clean reference silhouette is the foundation of the whole metric; hand-thresholding a busy photo is fragile, and per-feature masks let the critic target per-feature IoU.
- **Applied to:** `3d ai match` RAG pre-flight set, `3d preprocess` (one-shot reference pre-pass).
- **Library:** `sam2` (Meta, `pip install` from HF repo + checkpoint, ~4–8 GB VRAM, <1 s/image).
- **Metric / benchmark:** Mask quality (edge coverage, no background bleed); downstream IoU at frozen pose.
- **Source:** SAM 2 (arXiv:2408.00714); Report §6.2, §13.5; ROADMAP §13.2.

### P3.2 Depth + normal critic channels
- **What:** Run Depth-Anything V2 (or Marigold for sharper edges) for a relative depth map, and Wonder3D once for a side-view normal map. Hand depth + normal + silhouette to the critic and **discard any generated mesh**.
- **Why:** A single side silhouette under-determines depth; depth/normal priors give the critic a structured 3D cue (front-to-back mass-order consistency) without ever making an unprintable generated mesh the deliverable.
- **Applied to:** `3d ai critique` RAG pre-flight set (the model↔reference critic).
- **Library:** `depth_anything_v2` (NeurIPS 2024, ~2–6 GB VRAM, ~0.1–1 s/image); `Wonder3D` / `Wonder3D++` (normal-map output, ~2–3 min); Marigold (arXiv:2312.02145) as optional fallback.
- **Metric / benchmark:** Not a direct metric — these are **critic channels** (extra images in the prompt). Their value is measured indirectly by critic edit quality and final convergence speed.
- **Sources:** Depth-Anything-V2 (https://github.com/DepthAnything/Depth-Anything-V2), Marigold (arXiv:2312.02145), Wonder3D (arXiv:2310.15008); Report §6.1, §20.3; ROADMAP §13.2.

---

## P4 — Structural gates

### P4.1 Anisotropic strength knockdown
- **What:** Multiply the allowable stress for any component **normal to the layer plane** by a citable, material-specific knockdown — PETG ~0.7–0.75×, PLA ~0.45× of in-plane — and make the allowable a function of slicing layer height.
- **Why:** Orientation is the single biggest lever on FDM strength; the magnitude is a real measured number (PLA across-layer can be under half the in-plane strength).
- **Applied to:** `3d strength` (stress vs allowable per load-case, SF per zone), `3d check` (when `.structural` tag is present).
- **Library:** Pure Python (knockdown table from the materials registry `materials.yaml`); no external ML.
- **Metric / benchmark:** Predicted stress / allowable stress ratio ≤ 1.0 per zone; SF ≥ target; anisotropy ratio applied explicitly.
- **Sources:** Ahn et al. 2002 (https://doi.org/10.1108/13552540210441166), PMC9230522 (https://pmc.ncbi.nlm.nih.gov/articles/PMC9230522/); Report §23; ROADMAP §6.

### P4.2 Strength-driven orientation
- **What:** Per-part orientation solver that prefers orienting each part so its **principal tensile load lies in the XY (flat) plane** and the weakest across-layer direction carries the least load — consuming `3d strength`’s anisotropy model.
- **Why:** XZ (on-edge) is consistently the weakest direction in both PETG and PLA, so orientation is a strength *output*, not an afterthought; minimizing max across-layer tensile stress is the measurable goal.
- **Applied to:** `3d pack` (orientation solver), `3d pack --orient strength`.
- **Library:** `trimesh` (PCA of the mesh for candidate flat/strong lay-downs), `numpy` (eigen decomposition).
- **Metric / benchmark:** Max across-layer tensile stress (minimized); strength safety factor per part.
- **Source:** PMC9230522 (orientation numbers); Report §23.2; ROADMAP §5.

---

## P5 — Situational / aspirational (reach-for-when-needed)

### P5.1 Differentiable-rendering gradient fit
- **What:** Backprop a silhouette/soft-IoU loss into parameters via Mitsuba 3 or nvdiffrast, only after reimplementing the geometry differentiably.
- **Why:** Faster convergence for *hundreds* of parameters — but unnecessary for the few-dozen-param case where the gradient-free forced-monotonic loop converges in a coffee-break, and it abandons OpenSCAD as the generator.
- **Applied to:** `3d match <model> <ref> --backend mitsuba` (optional, aspirational).
- **Library:** `mitsuba` (https://www.mitsuba-renderer.org/) or `nvdiffrast` (https://github.com/NVlabs/nvdiffrast). Both require CUDA + PyTorch/Dr.Jit.
- **Metric / benchmark:** Same IoU target, but rounds-to-converge and wall-time per step are the comparison.
- **Sources:** Mitsuba 3, nvdiffrast; Report §5.3, §13.6; ROADMAP §7.

### P5.2 Multi-view photogrammetry reference
- **What:** If the real subject is photographed from many angles, run SfM+MVS via COLMAP / Meshroom for a metric ground-truth shell, then add a second-view IoU term to the match loss.
- **Why:** A single found photo under-constrains 3D; multiple real photos pin it — situational, only when such photos exist.
- **Applied to:** `3d fit-camera <model> refs/*.jpg --multi-view` (optional).
- **Library:** `colmap` (https://colmap.github.io/) or `meshroom` (https://alicevision.org/) (external binaries, not Python wheels).
- **Metric / benchmark:** Multi-view IoU (side + front); reprojection error per view; COLMAP sparse/dense reconstruction completeness.
- **Sources:** COLMAP, Meshroom; Report §5.5; ROADMAP §7.

---

## VLM-judge methodology (cross-cutting)
- **What:** A multimodal LLM scores the rendered model against the reference photo on a fixed rubric (silhouette/proportion, feature completeness, structural correctness, detail fidelity), with reproducibility guards: temp-0 canonical + N=5 @ 0.1 stability flag, ≥2 judges, position-swap for pairwise.
- **Why:** CADBench (κ=0.791) shows a well-anchored VLM rubric is reliable enough to ship; it captures "looks like the subject" that pixel metrics miss.
- **Applied to:** `3d ai bench` (no-mesh regime, primary metric), `3d compare` (A/B side-by-side).
- **Library:** Any vision-capable backend (Claude, GPT-4o, Gemini) via the same adapters as `3d web` (§9). The rubric + protocol are in `lib/ai/judge.py`.
- **Metric / benchmark:** Per-dimension score (0–4) + mean; stability flag; cross-judge spread.
- **Sources:** CADBench (arXiv:2412.14203), FlipFlop (arXiv:2311.08596), position-bias (arXiv:2406.07791), temp-0.1 peak (arXiv:2603.28304v1); benchmarks-and-metrics §2.5.

---

## Summary table — Paper / algorithm → `3d` command → library

| Paper / algorithm | `3d` command | Library / tool | Metric / benchmark |
|---|---|---|---|
| Silhouette IoU + AE (P0.1) | `3d score`, `3d match`, `3d fit-camera` | ImageMagick | IoU, AE |
| ReLook forced-monotonic loop (P0.2) | `3d match`, `3d ai <tool> loop` | Python orchestrator | Monotone trajectory, rounds-to-converge |
| Camera pose fit + freeze (P0.3) | `3d fit-camera` | numpy, Pillow | Reprojection error, IoU at frozen pose |
| Chamfer, F-score@τ, Hausdorff, NC, vol-IoU (P1.1) | `3d ai bench`, `3d metrics`, `3d compare` | open3d, trimesh, scipy, pymeshlab | F-score@τ (primary), Chamfer, Hausdorff, NC |
| LPIPS, SSIM, PSNR, CLIP-sim (P1.2) | `3d ai bench`, `3d score`, `3d compare` | lpips, clip-score, ImageMagick | LPIPS, SSIM/DSSIM, PSNR, CLIP-sim |
| OpenSCAD-LLM benchmark (P1.3) | `3d ai bench [suite]` | `lib/ai/bench.py` + above libs | Build-success, IoU, Chamfer, LPIPS, VLM-judge |
| ShapeAssembly attachment graph (P2.1) | `3d ai design review` | BOSL2 | Parent-dimension change → IoU delta |
| CSGNet / ShapeAssembly / DeepCAD skeleton (P2.2) | `3d ai design do` | LLM (today); parser (later) | Skeleton render-success, initial IoU, rounds-to-target |
| SAM 2 reference mask (P3.1) | `3d ai match` pre-flight, `3d preprocess` | sam2 | Mask edge quality, downstream IoU |
| Depth-Anything V2 / Marigold depth (P3.2) | `3d ai critique` pre-flight | depth_anything_v2, Marigold | Critic channel (indirect) |
| Wonder3D normal maps (P3.2) | `3d ai critique` pre-flight | Wonder3D / Wonder3D++ | Critic channel (indirect) |
| Ahn 2002 / PMC9230522 anisotropy (P4.1) | `3d strength`, `3d check` | materials registry (YAML) | Stress/allowable ratio, SF |
| PCA-based orientation solver (P4.2) | `3d pack`, `3d pack --orient strength` | trimesh, numpy | Max across-layer stress, SF per part |
| Mitsuba 3 / nvdiffrast (P5.1) | `3d match --backend mitsuba` | mitsuba, nvdiffrast | IoU, rounds-to-converge, wall-time |
| COLMAP / Meshroom multi-view (P5.2) | `3d fit-camera --multi-view` | colmap, meshroom | Multi-view IoU, reprojection error |
| CADBench VLM-judge rubric | `3d ai bench`, `3d compare` | Any MLLM backend | Per-dim 0–4 + mean, stability flag |
| Tatarchenko F-score@τ convention | `3d ai bench`, `3d metrics` | scipy, open3d, trimesh | F-score@τ (τ = 1% bbox diagonal) |
| ModelRift task format | `3d ai bench` | N/A (protocol) | Auto-scored (replaces subjective 0–5) |
| Text2CAD Invalidity Ratio | `3d ai bench` | N/A (protocol) | Build-success rate (gate 0) |
| Text2CAD-Bench hybrid split | `3d ai bench` | N/A (protocol) | Auto geom (L1–3) + VLM-judge (L4) |

---

## Notes on conventions and the longitudinal store

Every metric in the table above is persisted with its **convention** (Chamfer k, F-score τ, Hausdorff directed/symmetric, SSIM vs DSSIM sense, voxel resolution) so that `3d ai bench --compare` and `3d metrics` show meaningful deltas rather than silently comparing numbers computed under different rules. The store lives at `~/.local/share/3d-cli/metrics/*.jsonl` (per-user) and `metrics/` (per-project). `3d web` surfaces trend lines from the same data.

**Why this matters:** The benchmarks-and-metrics doc (§4.4) explicitly warns that "a longitudinal store is worthless if the convention silently drifts between runs." APPLY-RESEARCH.md is the contract that prevents that drift: each row above states the convention, the library, and the command, so an implementation can be audited against it.

---

## How to extend this document

When a new paper or tool is surveyed (ROADMAP §12), add a row to the table and a subsection under the appropriate priority tier. If the tier is unclear, add a new "P6+" or "Cross-cutting" section. The rule: *no surveyed item stays unmapped* — every algorithm must answer "which command, which library, which metric."
