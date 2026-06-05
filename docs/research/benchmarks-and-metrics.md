# Benchmarks & metrics for image→OpenSCAD/CAD likeness

A literature/tooling survey of how to **evaluate** how well a generated OpenSCAD/CAD
model matches its source — image, text, or reference mesh. Written to back `3d ai bench`
and `3d compare` (ROADMAP §13.4) with metrics that are **reproducible and not gameable**.

**Why this doc exists.** A Pantheon `image→OpenSCAD` benchmark run exposed that
**silhouette-IoU is a degenerate judge**: when the camera is a free variable, the
optimiser games the camera (shrink the model to a sliver, drift it off-frame) to inflate
overlap, and an *unsegmented* reference photo pollutes the target mask with background.
The metric goes up while likeness goes down. ROADMAP §13.4 currently calls silhouette-IoU
the *primary* render metric **and** the camera-fit objective — this survey argues both must
change (see §5 "Proposed §13.4 additions"). The fix is a *pipeline with ordering*, not a
better single number.

Scope:
- §1 Benchmarks — current image/text→CAD and image→3D evals, what each scores, what is reusable.
- §2 Render-vs-reference metrics — the **no-ground-truth-mesh** case (our Pantheon case).
- §3 Geometry metrics — the **ground-truth-mesh-available** case, with exact definitions.
- §4 Recommendation — concrete metric set + protocol for `3d ai bench` / `3d compare`.
- §5 Proposed ROADMAP §13.4 additions (text only — ROADMAP is NOT edited by this doc).

All cited benchmarks/metrics were verified to exist on the web (URLs inline + §6).
Cross-reference: this extends [`sources.md`](sources.md) §21–22 and ROADMAP §13.4.

---

## 1. Benchmarks

The landscape splits by **input modality** (image vs text vs point-cloud) and **output**
(OpenSCAD vs CadQuery vs sketch-extrude token sequence vs raw mesh). Almost everything
academic targets **CadQuery / DeepCAD sketch-extrude sequences**, not OpenSCAD CSG — that
gap is exactly what `3d ai bench` fills. The reusable parts are the *protocols and metric
batteries*, not the datasets.

### 1.1 OpenSCAD-specific

**ModelRift OpenSCAD-LLM benchmark** — the only OpenSCAD-specific public benchmark.
- Task: two reference photos (front facade + aerial) of the Pantheon → "build a `.scad`
  implementation", iterate via the OpenSCAD CLI render-preview loop.
- Scores: a **subjective 0–5 scale** on two axes (Time, Quality), no written rubric, no
  geometric metric. The blog itself admits "even the best result is not close to a perfect
  Pantheon model." Six agents tested (Cursor/Composer, Codex, Opus, Sonnet, Antigravity/Gemini,
  ModelRift/Gemini Flash).
- Reusable: the **task format** (image→`.scad`, CLI-render iteration) is exactly our target.
  The **scoring is not** — non-reproducible, single human, no rubric. `3d ai bench` should
  keep the task and replace the score with the automatic battery in §2–§4.
- https://modelrift.com/blog/openscad-llm-benchmark

### 1.2 Text→CAD (script/sequence output)

**BlenderLLM + CADBench** — the methodology template for a script-generating CAD benchmark.
- Task: natural-language instruction → Blender `bpy` Python script; the model self-improves
  iteratively.
- **CADBench** scores across **3 major + 8 minor dimensions**: Object Attributes (size,
  color, material), Spatial Understanding (relationships/structure), User-Instruction
  Understanding (adherence). **700 instructions** (CADBench-Sim 500 synthetic +
  CADBench-Wild 200 real-world), spanning 16 categories / 8 instruction types / 5 complexity
  levels.
- **Judge: GPT-4o, MLLM-as-a-judge**, two complementary modes — *image-based* (judge reads
  4-angle rendered screenshots) and *script-based* (judge reads the `bpy` source for
  attributes hard to see). Final score = mean over all criteria, reported per dimension +
  overall. **Human–LLM agreement Cohen's κ = 0.791** (strong) — this is the empirical
  evidence that a *well-anchored, multi-dimensional* VLM rubric is reliable enough to ship.
- Reusable: the **dimension taxonomy and the dual image+script judging** transplant almost
  directly to image→OpenSCAD (§2.5 rubric). κ=0.791 is the bar to aim for.
- https://arxiv.org/abs/2412.14203

**Text2CAD** (NeurIPS 2024) — text→sketch-extrude sequence on the DeepCAD dataset.
- ~170K models / ~660K text annotations (beginner-to-expert prompts auto-generated with
  Mistral + LLaVA-NeXT). Metrics: **F1 per primitive type** (Line/Arc/Circle/Extrusion),
  **Chamfer Distance**, **Invalidity Ratio** (fraction of sequences that fail to build).
- Reusable: the **Invalidity Ratio = render/build-success-rate** is a must-have first gate;
  a sample that does not compile scores zero on everything else.
- https://arxiv.org/abs/2409.17106 · https://sadilkhan.github.io/text2cad-project/

**Text2CAD-Bench** (May 2026) — newer, CadQuery-output, and notable because its protocol
is exactly the hybrid this doc recommends.
- 600 human-curated examples in 4 difficulty levels (L1–L2 sketch-extrude → L3 sweeps/lofts/
  shells → L4 real-world). Dual-style prompts (geometric vs sequence-of-ops).
- **L1–L3: automatic** — Chamfer (bidirectional), Invalidity Rate, volumetric IoU.
  **L4: VLM-judge** (GLM-4.6V) scoring feature completeness + design quality on 0–10 across
  five dimensions, validated against engineering-background human annotators.
- Reusable: confirms the **"automatic geometry where a target mesh exists, VLM-judge where it
  doesn't"** split is the field consensus, not a local hack.
- https://arxiv.org/html/2605.18430

### 1.3 Image→CAD (the closest analogues to our task)

**CAD-Coder** (2025) — open-source VLM, image → **CadQuery** code.
- Trained on **GenCAD-Code** (163K image+code pairs). Metrics: **valid-syntax rate**
  (executes without error), **3D solid similarity** via **IoU** and **Chamfer distance**
  (point clouds sampled from generated vs target solid). Reports 100% valid syntax + best 3D
  similarity vs GPT-4.5 / Qwen2.5-VL-72B baselines; shows some transfer to real-world photos.
- Reusable: the **valid-syntax-rate → IoU → Chamfer** ladder is the minimal automatic
  battery for any image→code CAD task; directly portable (swap CadQuery for OpenSCAD).
- https://arxiv.org/pdf/2505.14646 · https://github.com/anniedoris/CAD-Coder

**GenCAD / GenCAD-3D** (2024 / 2025) — image-conditioned CAD sequence generation (contrastive
+ latent diffusion). Generative-quality metrics **COV** (coverage) and **MMD** (minimum
matching distance) vs DeepCAD l-GAN / SkexGen. These are *distribution-level* metrics for a
generator, not per-sample likeness — **not** directly useful for `3d ai bench` (which scores
one model against one reference), but worth knowing as the "is the generator's output
*diverse and plausible*" axis.
- https://arxiv.org/abs/2409.16294 · https://arxiv.org/abs/2509.15246

**Img2CAD** (SIGGRAPH Asia 2025) — single image → editable parametric CAD via VLM-assisted
conditional factorisation (structure prediction + continuous attributes). Benchmark of
1,026 chairs / 3,243 tables / 305 cabinets. Metrics: **Chamfer distance** + **segmentation
accuracy / mIoU**. Notable finding: GPT-4o alone underperforms the factorised pipeline —
direct VLM→CAD is insufficient for both structure and continuous params.
- https://arxiv.org/html/2408.01437v2 · https://github.com/qq456cvb/Img2CAD

### 1.4 Point-cloud→CAD (adjacent — reuse the methodology, not the task)

These take a **point cloud**, not an image, so the *task* is not ours — but they are the
state of the art on the **geometry-metric protocol** (Chamfer/IoU against a target solid)
and on "CAD-as-code that an LLM can re-edit", so they belong in the geometry-metric lineage.
- **CAD-Recode** (ICCV 2025) — point cloud → CadQuery code; 10× lower Chamfer + >20% higher
  IoU than prior art on **DeepCAD** and **Fusion360** benchmarks; also evaluated on real-world
  **CC3D**. https://cad-recode.github.io/ · https://arxiv.org/pdf/2412.14042
- **cadrille** (2025) — multi-modal CAD reconstruction (incl. point cloud) with online RL.
  https://arxiv.org/html/2505.22914

### 1.5 Image→3D **mesh** (no CAD, but the geometry-metric battery is the field standard)

Single-image→mesh generators (TripoSR, InstantMesh, Unique3D, MeshFormer, TRELLIS, Hunyuan3D)
are evaluated against **scanned ground-truth meshes** on standard sets — **Google Scanned
Objects (GSO, ~1030 objects)** and **OmniObject3D** — with a fixed battery: **Chamfer
Distance, F-score, volumetric IoU, Normal Consistency** (geometry) + **PSNR, LPIPS, CLIP-Score**
(rendered-view appearance). This is the canonical "you HAVE a ground-truth mesh" protocol and
is the template for §3. Note the newer **Sharp Normal Error (SNE)** metric, introduced because
Chamfer/F-score under-weight fine salient detail.
- https://arxiv.org/html/2405.20343v3 (Unique3D) · https://arxiv.org/html/2408.10198v1 (MeshFormer)

**Summary table — what is reusable for `3d ai bench`:**

| Benchmark | Input→Output | Scoring | Reusable for `3d` |
|---|---|---|---|
| ModelRift | image→OpenSCAD | subjective 0–5, no rubric | **task format** (keep), score (replace) |
| CADBench (BlenderLLM) | text→bpy | GPT-4o MLLM-judge, 3+8 dims, κ=0.791 | **judge rubric + dual image/script judging** |
| Text2CAD | text→seq | F1/Chamfer/Invalidity | **Invalidity = build-success gate** |
| Text2CAD-Bench | text→CadQuery | auto geom (L1–3) + VLM-judge (L4) | **the hybrid split itself** |
| CAD-Coder | image→CadQuery | valid-syntax + IoU + Chamfer | **valid-syntax→IoU→Chamfer ladder** |
| Img2CAD | image→CAD | Chamfer + mIoU | per-sample geometry metrics |
| GenCAD(-3D) | image→seq | COV/MMD (distribution) | generator-diversity axis only |
| image→mesh (GSO) | image→mesh | Chamfer/F-score/IoU/NC + PSNR/LPIPS/CLIP | **the full GT-mesh battery** |

---

## 2. Render-vs-reference metrics (NO ground-truth mesh — the Pantheon case)

When the only reference is a **photo**, every metric below compares the *rendered* `.scad`
against that photo. **Pixel-level metrics are meaningless without camera + scale alignment.**
The hard ordering is therefore a pipeline, not a menu:

```
1. SEGMENT the reference photo   → clean binary subject mask (SAM 2)
2. FIT the camera against the MASK, on landmarks/reprojection — NOT by maximising IoU
3. REJECT degenerate cameras, re-fit
4. FREEZE the pose
5. RENDER the .scad at the frozen pose
6. Compute pixel metrics {SSIM/DSSIM, LPIPS, CLIP-sim} on aligned render-vs-mask/photo
7. VLM-judge render-vs-photo on the rubric (§2.5)
```

Steps 1–4 are the precondition. Skip them and steps 5–7 produce numbers, not measurements.

### 2.1 Silhouette IoU — why it is degenerate here, and the fix

`IoU = |S ∩ R| / |S ∪ R|`, S = rendered silhouette mask, R = reference mask; range 0..1,
1 best. It is the natural shape-overlap metric and a fine *reported* channel. It is a
**terrible optimisation target / camera-fit objective**, for two reasons our Pantheon run hit:

1. **Fit-camera games the camera.** If IoU is maximised over camera params, the optimiser
   finds cheap wins that have nothing to do with likeness: collapse the model to a thin sliver
   that fits *inside* the reference everywhere (high precision, the union shrinks), or drift the
   projection so the model lands on the densest part of the reference mask. IoU rises;
   resemblance falls. A drifting pose also makes the §13.1 monotonic-acceptance loop
   meaningless ("score improves for no reason").
2. **Raw unsegmented reference pollutes the mask.** If R is the whole photo (sky, ground,
   neighbouring buildings) thresholded, the "target" is mostly background; matching it rewards
   filling the frame, not matching the subject.

**The fix (do all of these):**
- **Segment first.** Run **SAM 2** (or SAM) on the photo to get a clean subject-only binary
  mask before anything touches it. R = subject mask, never the raw photo.
  https://arxiv.org/abs/2408.00714
- **Do NOT fit the camera by maximising IoU.** Fit the 3–4 pose DoF (ortho-scale, in-plane
  translation, small roll for a side elevation) by **minimising reprojection error on marked
  landmarks** (corners, axis endpoints) against the mask. Landmarks can't be gamed by a sliver;
  IoU can. Coarse grid → local hill-climb, then **freeze the pose** for the entire shape match.
  `3d fit-camera` already produces a locked pose; add the reprojection mode and the freeze rule.
- **Reject degenerate cameras** before accepting a fit. Concretely reject when:
  - **scale collapse / explosion** — rendered silhouette area is outside `[20%, 95%]` of the
    reference-mask bounding area (tune; the point is to forbid slivers and frame-fillers);
  - **off-frame translation** — silhouette centroid outside the reference-mask bbox, or >X% of
    the silhouette clipped by the frame;
  - **extreme foreshortening** — silhouette aspect ratio diverges >K× from the reference's.
  On rejection, re-fit from a different seed; never report metrics on a rejected camera.
- **Then** report IoU **against the segmented mask at the frozen pose** as ONE channel among
  many — descriptive, not the objective, not "primary".

This directly contradicts ROADMAP §13.4's "silhouette IoU (primary)" + "fit by maximising
silhouette IoU"; §5 proposes the reconciling edit.

### 2.2 SSIM / DSSIM

**SSIM** (Wang et al. 2004), per local window:
`SSIM(x,y) = [(2μ_xμ_y + C₁)(2σ_xy + C₂)] / [(μ_x² + μ_y² + C₁)(σ_x² + σ_y² + C₂)]`,
with `C₁=(0.01·L)²`, `C₂=(0.03·L)²`, L = dynamic range (255 for 8-bit). Range −1..1, **1 best**;
report the mean over windows (MSSIM). **DSSIM = (1−SSIM)/2**, range 0..1, **0 best** — opposite
sense. This sign flip is the silent footgun: guard the sense explicitly in the store.
- Use: structural (luminance×contrast×structure) agreement of aligned renders. Only meaningful
  after pose-freeze (§2 step 4–5). Library: `scikit-image` `structural_similarity`, or
  ImageMagick `compare -metric DSSIM`.
- https://www.cns.nyu.edu/pub/eero/wang03-reprint.pdf ·
  https://en.wikipedia.org/wiki/Structural_similarity_index_measure

### 2.3 LPIPS

**LPIPS** (Zhang et al. CVPR 2018) — weighted MSE over channel-normalised deep features of a
pretrained net (AlexNet/VGG): `d(x,y) = Σ_l (1/H_lW_l) Σ_{h,w} ‖ w_l ⊙ (φ̂_l(x) − φ̂_l(y)) ‖²`.
Range ≥0, **0 best**. Correlates with human perceptual judgement far better than PSNR/SSIM.
- Use: perceptual "does the render look like the photo" beyond pixel structure — captures
  texture/material/shading mismatch SSIM misses. Again, only after alignment.
- `pip install lpips`. https://arxiv.org/abs/1801.03924

### 2.4 CLIP-similarity (image–image)

**CLIPScore** (Hessel et al. EMNLP 2021): `CLIPScore = max(100·cos(E_I, E_C), 0)`. The
**image–image** variant uses CLIP image embeddings of render and photo: cosine similarity of
the two embeddings. Range effectively 0..100, higher better.
- Use: **semantic** "is it the right object" — robust to pose/scale/lighting, so it's the one
  channel that still says something useful even *before* perfect alignment. Blind to fine
  geometric accuracy (a vaguely dome-topped cylinder still reads "Pantheon-ish").
- https://arxiv.org/abs/2104.08718

### 2.5 VLM-judge (multimodal model scores render vs photo on a rubric)

The CADBench result (κ=0.791) shows a well-anchored multi-dimensional VLM rubric is reliable
enough to ship — provided you control for the known failure modes (FlipFlop instability,
position bias, scoring drift). This is the channel that captures "looks like the Pantheon" the
way pixel metrics cannot.

**Rubric** (adapt CADBench's 3 dims to image→OpenSCAD; each scored **0–4** with anchors so the
score is reproducible, not vibes):

| Dimension | 0 | 2 | 4 |
|---|---|---|---|
| **Silhouette / proportion fidelity** | wrong overall shape | right family, proportions off | silhouette + key proportions match |
| **Feature completeness** | major features missing (no portico/dome) | some present, some missing | all salient features present |
| **Structural / spatial correctness** | parts misplaced/wrong count | mostly right, minor errors | correct arrangement, counts, relations |
| **Detail fidelity** | flat/blocky vs reference | coarse detail | fine detail (columns, coffers) present |

Report per-dimension + mean. The caller supplies the feature taxonomy (no hardcoded
"portico/dome" in core — ROADMAP §15 leakage rule); the table above is an *example* fill.

**Reproducibility protocol** (this is what makes a VLM-judge a metric, not a mood):
- **Fixed prompt**, exact rubric text + anchors in the system prompt; render and photo passed
  side by side, **both at the frozen pose / same crop**.
- **Temperature**: there is a real tension. **Temp 0** is the determinism default; but
  empirically self-consistency/accuracy of LLM judges peaks near **temp 0.1**, not 0
  (https://arxiv.org/html/2603.28304v1). Recommendation: run **temp 0 as the canonical logged
  score** for determinism, AND run **N=5 samples at temp 0.1** to *measure* stability. If the N
  samples disagree by more than 1 point, flag the instance as low-confidence — this is how you
  *detect* the **FlipFlop effect** (LLMs flip ~46% of answers when challenged,
  https://arxiv.org/abs/2311.08596) rather than pretending it's absent.
- **Multiple judges / self-consistency**: use ≥2 distinct judge models (e.g. one Anthropic, one
  other-vendor) and aggregate; treat large cross-judge disagreement as low-confidence. Sampling
  multiple outputs and taking the mode/mean is the standard self-consistency stabiliser.
- **Position bias**: if doing pairwise (model-A render vs model-B render), **swap order and
  average** — judges favour the first-presented option (position bias is well documented,
  https://arxiv.org/abs/2406.07791). MLLM judges additionally show egocentric/length/visual
  biases (https://arxiv.org/pdf/2402.04788), so prefer **pointwise rubric scoring** over
  pairwise for the logged metric, and reserve pairwise for `3d compare` A/B with order-swap.
- **Persist** the full judge transcript + per-dimension scores + which judge + temp + sample
  count, so a score is auditable and re-runnable.

**Sense summary for the store** (the §13.4 footgun guard):

| Metric | Range | Best | Needs alignment? |
|---|---|---|---|
| Silhouette IoU (vs mask, post-reject) | 0..1 | 1 | yes (pose-freeze) |
| SSIM | −1..1 | 1 | yes |
| DSSIM | 0..1 | **0** | yes |
| LPIPS | ≥0 | **0** | yes |
| PSNR | dB | high | yes |
| CLIP-sim | 0..100 | 100 | partial (semantic) |
| VLM-judge | 0..4 / 0..10 | high | yes (same crop) |

---

## 3. Geometry metrics (ground-truth mesh IS available)

If a target mesh exists — and for Pantheon **we can fetch a public mesh and ICP-align it**
(see §3.7) — geometry metrics beat every 2D proxy because they are pose/scale-invariant after
alignment and measure actual 3D shape. **Exact definitions** below; sample both meshes to point
sets `A` (generated), `B` (target) of equal density first.

**Universal preconditions (the gotchas):**
- **Sampling density**: sample N points uniformly *by surface area* (e.g. N=100k) on both
  meshes. Too few points → noisy Chamfer/F-score; uneven sampling biases toward large faces.
- **Units**: normalise to a common unit (mm) before any threshold. F-score@τ and Hausdorff are
  meaningless if one mesh is in mm and the other in arbitrary OpenSCAD units.
- **Alignment**: rigid-align with **ICP** (after a coarse PCA/centroid+scale prealign) before
  measuring; a global pose offset otherwise dominates every distance. Record whether you allow
  scale in the alignment (similarity vs rigid ICP).
- **Watertightness**: volumetric IoU requires both meshes watertight/manifold; check with
  `trimesh.is_watertight` / Open3D `is_watertight` and repair or skip.

### 3.1 Chamfer Distance (CD) — record the convention

L2 (squared) symmetric Chamfer:
`CD₂(A,B) = (1/|A|) Σ_{a∈A} min_{b∈B} ‖a−b‖² + (1/|B|) Σ_{b∈B} min_{a∈A} ‖a−b‖²`.
L1 variant uses `‖a−b‖` (not squared). **Record k (L1/L2), mean vs sum, and bidirectional vs
one-directional** in the store — these differ across papers and silently change the number.
Lower is better. **Caveat (Tatarchenko, §3.2): Chamfer alone misleads** — a few far outliers
dominate the squared sum, and a blurry "average" shape can score well. Report it, don't rank
on it. Library: `scipy.spatial.cKDTree` for nearest-neighbour, or `trimesh` /
`open3d.compute_point_cloud_distance`.

### 3.2 F-score@τ — **PRIMARY** (Tatarchenko et al. CVPR 2019)

Precision = fraction of generated points within τ of the target;
Recall = fraction of target points within τ of a generated point;
`F-score@τ = 2·P·R / (P+R)`. Range 0..1, **1 best**. **τ ≈ 1% of the target bbox diagonal**
(report τ explicitly). Tatarchenko shows F-score@τ is the surface metric least fooled by the
failure modes that mislead IoU and Chamfer (it is robust to outliers and to the
mean-shape trick), so it is the **primary geometry metric**.
- https://arxiv.org/abs/1905.03678. Compute with `cKDTree` two-way radius queries; pure numpy
  for the precision/recall counts.

### 3.3 Hausdorff distance — directed vs symmetric

Directed: `h(A,B) = max_{a∈A} min_{b∈B} ‖a−b‖`. Symmetric: `H(A,B) = max(h(A,B), h(B,A))`.
Worst-case surface deviation; lower better. Extremely outlier-sensitive (one stray vertex sets
it) — useful as a *worst-case* companion, never alone. **Record directed vs symmetric.**
Library: `pymeshlab` `get_hausdorff_distance`, or `scipy.spatial.distance.directed_hausdorff`.

### 3.4 Normal Consistency (NC)

For each sampled point in A, find its nearest neighbour in B and compare surface normals:
`NC = (1/|A|) Σ_{a∈A} |⟨ n(a), n(nn_B(a)) ⟩|` (often averaged with the reverse direction).
Range 0..1, **1 best** (absolute value makes it orientation-agnostic — record that choice).
Captures local orientation/surface-detail agreement that point distances miss. Library:
`trimesh` / `open3d` (estimate or carry normals, then KD-tree match). Consider **SNE (Sharp
Normal Error)** if fine salient detail (column flutes, coffers) matters and NC washes it out.

### 3.5 Volumetric IoU

`IoU_vol = Vol(A ∩ B) / Vol(A ∪ B)`. Range 0..1, **1 best**. Requires watertight meshes;
compute by voxelising both on a common grid (`trimesh.voxelized` / `open3d` voxel grid) or by
boolean intersection/union volumes (`trimesh` boolean via the `manifold` backend) — record the
method and grid resolution (voxel IoU depends on resolution).

### 3.6 Library mapping (matches §13.4)

| Metric | Primary lib | Note |
|---|---|---|
| Chamfer (L1/L2) | `scipy.spatial.cKDTree`, `open3d`/`trimesh` | KD-tree NN, numpy reduce |
| **F-score@τ** | `cKDTree` radius query + numpy | τ = 1% bbox diag |
| Hausdorff | `pymeshlab.get_hausdorff_distance`, `scipy.directed_hausdorff` | symmetric = max of both |
| Normal consistency | `trimesh`/`open3d` normals + `cKDTree` | abs dot, mean |
| Volumetric IoU | `trimesh` boolean (`manifold`) or `open3d` voxel | needs watertight |
| sampling/repair | `trimesh.sample.sample_surface`, `pymeshlab` | uniform-by-area, N≈100k |

### 3.7 Unlocking geometry metrics for Pantheon

The Pantheon is a famous monument with public 3D scans/models (museum/photogrammetry
repositories, Sketchfab, cultural-heritage archives). **Recommendation: fetch one reference
Pantheon mesh, clean + ICP-align it, and treat it as ground truth.** That converts our
hardest case from the fragile 2D-only regime (§2) into the robust geometry regime (§3) —
F-score@τ on a real target is worth far more than any silhouette game. Caveats: a tourist-grade
scan has its own error and arbitrary units/scale (similarity-ICP, not rigid); document the
reference mesh's provenance and resolution in the store so a score is interpretable.

---

## 4. Recommendation for `3d ai bench` / `3d compare`

### 4.1 Decision rule (which regime)

```
target mesh available (fetched or supplied)?
  YES → §3 geometry battery is PRIMARY; §2 render metrics are secondary/sanity
  NO  → §2 pipeline (segment → fit/freeze → render → metrics → VLM-judge)
```

This mirrors the field consensus (Text2CAD-Bench L1–3 vs L4; image→mesh GSO vs in-the-wild).

### 4.2 Metric set + which is primary

**Gate 0 (always, first):** **build/render-success rate** — does the `.scad` compile and render?
A failure scores **zero everywhere else** (Text2CAD Invalidity Ratio / CAD-Coder valid-syntax).
No partial credit for code that doesn't run.

**No-mesh regime, in priority order:**
1. **VLM-judge mean** (rubric §2.5) — **primary** likeness signal; the one channel that tracks
   "looks like the subject". Logged at temp 0 + stability flag from N=5 @ 0.1, ≥2 judges.
2. **CLIP-sim** — semantic sanity, robust to imperfect alignment.
3. **LPIPS** — perceptual, post-freeze.
4. **DSSIM** — structural, post-freeze.
5. **Silhouette IoU (vs segmented mask, post-reject, frozen pose)** — **demoted to a reported
   channel, NOT the objective and NOT primary.** Camera fit uses landmark reprojection (§2.1).

**Mesh regime, in priority order:**
1. **F-score@τ (τ=1% bbox diag)** — **primary** (Tatarchenko).
2. **Normal consistency** — surface-detail agreement.
3. **Chamfer (record L1/L2)** — reported, not ranked-on.
4. **Volumetric IoU** — reported (watertight-gated).
5. **Hausdorff (symmetric)** — worst-case companion.

### 4.3 Aggregation

- **Do NOT collapse to one number by default.** Report the **vector** of channels + the gate.
  A single composite hides exactly the degeneracy we are guarding against (a high IoU masking a
  low VLM-judge). If a scalar is needed for ranking, use the **primary** metric of the active
  regime (VLM-judge mean, or F-score@τ) and show the rest alongside.
- **Normalise senses before any aggregation** (the DSSIM/LPIPS "0-best" footgun) — store each
  metric with its sense flag so a future weighted score can't silently invert.
- **Confidence**: attach the VLM-judge stability flag and cross-judge spread; a high score with
  high disagreement is not the same as a high score with consensus.

### 4.4 Longitudinal logging (§13.4)

Every `3d ai bench` / `do` / `review` / `loop` / tool run appends a timestamped JSONL record to
`~/.local/share/3d-cli/metrics/*.jsonl` (+ per-project `metrics/`) with: timestamp, backend,
model, tool, inputs (ref image/mesh hashes), **the frozen camera pose**, every metric value +
its **sense + convention** (Chamfer k, F-score τ, Hausdorff directed/symmetric, NC abs-dot,
voxel res, ICP rigid/similarity), VLM-judge transcript + per-dim scores + temp + judge IDs +
stability flag, render-success bool, tokens, cost, wall-time. `3d ai bench --compare` and
`3d metrics` show deltas vs history; `3d web` plots trend lines. **The convention fields are
load-bearing**: a longitudinal store is worthless if "Chamfer" silently means L1 one week and
L2 the next.

### 4.5 `3d compare` (A/B two models/runs)

Pointwise rubric scoring on each side (not pairwise) for the logged metric, then a side-by-side
delta table across all channels. If a pairwise VLM preference is shown, **swap order and
average** to cancel position bias; flag instances where the preference flips on swap.

---

## 5. Proposed ROADMAP §13.4 additions (text only — ROADMAP NOT edited here)

These reconcile §13.4 with the Pantheon finding. Apply when §13.4 is next revised:

1. **Demote silhouette IoU.** §13.4 currently lists silhouette IoU as the render-vs-reference
   **"(primary)"** metric and §13.4/§7 fits the camera **"by maximising silhouette IoU"**. The
   Pantheon run shows this is degenerate (camera-gaming + unsegmented-mask pollution). Change to:
   - render-vs-reference **primary = VLM-judge mean** (rubric), with CLIP-sim/LPIPS/DSSIM as
     perceptual/semantic/structural channels and **silhouette IoU as one *reported* channel,
     computed against a *segmented* mask at the *frozen* pose, after degenerate-camera rejection**.
   - **camera-fit objective = landmark reprojection error, NOT IoU.** Add an explicit
     "degenerate camera rejection" rule (scale-collapse / off-frame / foreshortening bounds) to
     `3d fit-camera` before pose-freeze.
2. **Mandate segmentation as a pre-pass.** The render-vs-reference battery must run against a
   **SAM 2 subject mask**, never the raw photo. Cross-link §13.2 P3.1 (SAM 2 reference silhouette).
3. **Add the VLM-judge as a first-class metric** with the reproducibility protocol (fixed
   prompt + anchored rubric, temp-0 canonical + N@0.1 stability flag, ≥2 judges, order-swap for
   any pairwise use). Reference CADBench's κ=0.791 as the reliability bar; cite the temperature
   (https://arxiv.org/html/2603.28304v1), FlipFlop (https://arxiv.org/abs/2311.08596), and
   position-bias (https://arxiv.org/abs/2406.07791) findings as the reasons for the protocol.
4. **Add the fetch-a-reference-mesh recommendation.** For famous-subject benchmarks (Pantheon),
   fetch a public mesh + similarity-ICP-align to unlock the §3 geometry battery (F-score@τ
   primary). Convert hard cases out of the 2D-only regime wherever a mesh can be obtained.
5. **Aggregation = vector, not scalar, by default**; normalise senses before any weighting;
   store every convention field. State explicitly that **pixel metrics are downstream of
   pose-freeze** and meaningless without it.
6. **Refresh the benchmark citations** beyond ModelRift + CADBench: add CAD-Coder
   (image→CadQuery, valid-syntax→IoU→Chamfer ladder), Text2CAD / Text2CAD-Bench
   (Invalidity-Ratio gate + the auto-geometry/VLM-judge split), Img2CAD, CAD-Recode/cadrille
   (point-cloud, geometry-metric methodology), and the GSO image→mesh battery (Chamfer/F-score/
   IoU/NC + PSNR/LPIPS/CLIP) as the canonical GT-mesh protocol.

---

## 6. Sources (all verified to resolve)

**Benchmarks**
- ModelRift OpenSCAD-LLM benchmark — https://modelrift.com/blog/openscad-llm-benchmark
- BlenderLLM + CADBench — https://arxiv.org/abs/2412.14203
- Text2CAD (NeurIPS 2024) — https://arxiv.org/abs/2409.17106 · https://sadilkhan.github.io/text2cad-project/
- Text2CAD-Bench (2026) — https://arxiv.org/html/2605.18430
- CAD-Coder — https://arxiv.org/pdf/2505.14646 · https://github.com/anniedoris/CAD-Coder
- GenCAD — https://arxiv.org/abs/2409.16294 ; GenCAD-3D — https://arxiv.org/abs/2509.15246
- Img2CAD — https://arxiv.org/html/2408.01437v2 · https://github.com/qq456cvb/Img2CAD
- CAD-Recode (ICCV 2025) — https://cad-recode.github.io/ · https://arxiv.org/pdf/2412.14042
- cadrille — https://arxiv.org/html/2505.22914
- Unique3D — https://arxiv.org/html/2405.20343v3 ; MeshFormer — https://arxiv.org/html/2408.10198v1

**Render-vs-reference metrics**
- SSIM (Wang 2004) — https://www.cns.nyu.edu/pub/eero/wang03-reprint.pdf ·
  https://en.wikipedia.org/wiki/Structural_similarity_index_measure
- LPIPS (Zhang 2018) — https://arxiv.org/abs/1801.03924
- CLIPScore (Hessel 2021) — https://arxiv.org/abs/2104.08718
- SAM 2 — https://arxiv.org/abs/2408.00714

**VLM/LLM-judge reproducibility**
- Temperature in LLM-as-a-Judge (peaks ~0.1) — https://arxiv.org/html/2603.28304v1
- FlipFlop effect — https://arxiv.org/abs/2311.08596
- Position bias in LLM-as-a-Judge — https://arxiv.org/abs/2406.07791
- MLLM-as-a-Judge (vision benchmark, biases) — https://arxiv.org/pdf/2402.04788

**Geometry metrics**
- Tatarchenko et al. (F-score@τ primary) CVPR 2019 — https://arxiv.org/abs/1905.03678
- trimesh — https://github.com/mikedh/trimesh
- Open3D — https://www.open3d.org/
- PyMeshLab — https://github.com/cnr-isti-vclab/PyMeshLab
- scipy.spatial (cKDTree, directed_hausdorff) — https://docs.scipy.org/doc/scipy/reference/spatial.html
