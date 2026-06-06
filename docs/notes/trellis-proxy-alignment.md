# TRELLIS Proxy Alignment Notes

## Hypothesis

A generated 3D proxy from a reference image can provide a coarse spatial prior for
`fit-camera`. Instead of searching every camera pose from a 2D silhouette alone, we:

1. generate or import a proxy mesh from the reference image;
2. align proxy mesh to the CAD/model mesh in 3D;
3. keep the best transform, error, and ambiguity diagnostics;
4. derive camera priors and run contour-based `fit-camera` refinement from those priors.

## Current Implementation

`3d proxy-align` implements the local geometry core:

- load CAD and proxy meshes via `trimesh`;
- normalize each mesh by bounding-box center and diagonal;
- sample surfaces deterministically;
- scan coarse yaw/pitch/roll candidates;
- refine each candidate with nearest-neighbor rigid + uniform-scale ICP;
- score with bidirectional Chamfer mean, p95, Hausdorff max, radial histogram distance,
  and topology penalties for component/Euler/watertight mismatch;
- compare CAD and aligned proxy in shared orthographic projection frames (XY/XZ/YZ) with
  boundary edge F1@3px, contour Chamfer, coverage drift, and centroid drift;
- write a `quality_gate` that can reject a generated proxy before it becomes a
  `fit-camera` prior;
- emit both normalized and original-space row-vector affine transforms that can be
  applied directly to proxy mesh vertices;
- write `result.json` and `alignment_proof.png`.

This is enough to test the alignment algorithm on saved TRELLIS outputs without depending
on Hugging Face queues.

## Proxy Rejection Rule

Image-to-3D output must be treated as untrusted. A proxy mesh can be wrong even when it is
watertight and visually plausible in isolation. The current rule is:

1. align proxy to CAD in normalized 3D;
2. project both meshes into the same XY/XZ/YZ frames;
3. compare projection contours, not filled areas;
4. mark the proxy `reject` if normalized 3D Chamfer p95 is too high, any projection has
   low edge F1, projection contour Chamfer is too large, or projection coverage drifts too
   far from the CAD silhouette.

This is intentionally stricter than a provider smoke test. The earlier ZeroGPU TRELLIS
sample proved that the Space can return GLB/USDZ artifacts, but its generated mesh was
not similar enough to the source image to trust as geometry. Such outputs should fail the
proxy quality gate and remain only provider diagnostics.

## ZeroGPU / TRELLIS Provider Plan

Current public documentation says ZeroGPU is Gradio-only and quota/queue managed. It is
suitable as an optional provider, not as a mandatory test dependency.

### Confirmed ZeroGPU smoke

`trellis-community/TRELLIS` was called through `gradio_client` without `HF_TOKEN` and
returned a preview MP4 plus GLB mesh in about 25 seconds for a small synthetic reference.
The GLB converted to STL and then through the existing `3d usdz` path:

- GLB: `/tmp/3d-trellis-provider/output_1.glb`
- preview MP4: `/tmp/3d-trellis-provider/output_0.mp4`
- USDZ: `/tmp/3d-trellis-provider/trellis_output.usdz`
- mesh stats: 5663 vertices, 8454 faces, not watertight

This proves ZeroGPU can be a useful optional provider even before auth is configured.
Unauthenticated calls still have low quota/priority, so `3d auth hf` exists for a more
stable workflow and for gated models.

Provider contract:

```text
reference image -> provider -> proxy mesh path + provider metadata
```

Required metadata:

- provider name (`trellis-community/TRELLIS`, local TRELLIS, manual);
- model/version/Space revision when known;
- source image hash;
- output mesh hash;
- runtime, queue/quota status, and any failure text.

## Planned Experiments

- Use the Hugging Face TRELLIS Space API through `gradio_client` when credentials/quota are
  available.
- Store a manually downloaded TRELLIS GLB in a local artifact directory and replay the
  alignment step offline.
- Compare CAD vs proxy with multi-view contour descriptors, not only 3D Chamfer.
- Convert the best proxy->CAD transform into initial camera candidates for
  contour-objective `fit-camera`.
- Add a proof video that rotates CAD/proxy overlays and annotates the error trend.
- Add an optional provider loop that renders the generated proxy from candidate views
  against the original reference and rejects wrong orientation before calling
  `fit-camera`.

## Apple Silicon Local Candidates

Current checked candidates:

- `vggt-mps`: installs cleanly on Apple Silicon, `torch.backends.mps` works, and the
  project MPS tests pass. It downloaded the public 5GB `facebook/VGGT-1B` weights without
  `HF_TOKEN`. However, the current package has two blockers: downloader stores
  `models/model.pt` while config expects `models/vggt_model.pt`, and reconstruction falls
  back to simulated depth because the upstream `vggt` module is not installed. It is a
  promising spatial-awareness backend after packaging fixes, not yet a verified mesh
  provider.
- Official TripoSR: public and lightweight enough to test, but the official install hit
  `xatlas` build failure on this Mac/CMake stack, and the official `run.py` chooses CPU
  when CUDA is unavailable rather than using MPS.
- Stable Fast 3D: best documented local Mac mesh candidate because official docs mention
  experimental MPS support and GLB output, but weights are gated, so it likely needs
  `3d auth hf login`.
- Mac-specific TRELLIS.2 ports: potentially high-quality GLB output on MPS, but heavier
  and gated; keep as a separate provider, not the default local dependency.

## Failure Modes To Track

- proxy topology differs from printable CAD because image-to-3D invents hidden backsides;
- symmetric objects have multiple equivalent transforms;
- PCA/ICP can converge to a locally plausible but wrong side;
- Chamfer can improve while semantically important contours get worse;
- a generated mesh may face the wrong way or invent a backside; require render/contour
  verification before using it as a camera prior;
- ZeroGPU queues and daily quotas can make provider calls unavailable.
