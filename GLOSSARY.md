# Glossary

One-line definitions + a good link for every domain term used across `3d`. Reference it from
anywhere: `[SAM2](GLOSSARY.md#sam2)`. Keep it growing as new terms appear; explain each term at its
first mention in the README, then link here.

## Tools & formats

- <a id="openscad"></a>**OpenSCAD** — script-based parametric solid CAD; the primary modeling
  language `3d` drives. https://openscad.org/
- <a id="cgal"></a>**CGAL** — Computational Geometry Algorithms Library; OpenSCAD's exact-geometry
  backend for `--render`/F6 (real booleans, manifold solids) vs the fast preview (F5). https://www.cgal.org/
- <a id="bosl2"></a>**BOSL2** — "Belfry OpenSCAD Library v2", a large helper library (shapes,
  threads, transforms) for OpenSCAD. https://github.com/BelfrySCAD/BOSL2
- <a id="nopscadlib"></a>**NopSCADlib** — OpenSCAD library of mechanical parts (vitamins),
  assemblies, BOM. https://github.com/nophead/NopSCADlib
- <a id="stl"></a>**STL** — triangle-mesh format; geometry only, no color/metadata. Lossy target
  for slicing. https://en.wikipedia.org/wiki/STL_(file_format)
- <a id="3mf"></a>**3MF** — modern print format that natively carries color, materials, and
  metadata (preferred rich mesh format). https://3mf.io/
- <a id="slicer"></a>**Slicer** — turns a mesh into printer G-code. `3d` autodetects
  **OrcaSlicer** > **Bambu Studio** > **PrusaSlicer**. https://github.com/SoftFever/OrcaSlicer
- <a id="fdm"></a>**FDM** — Fused Deposition Modeling; layer-by-layer filament 3D printing.
  https://en.wikipedia.org/wiki/Fused_filament_fabrication
- <a id="fdm-anisotropy"></a>**FDM anisotropy** — printed parts are weaker across layer lines than
  in-plane; strength must apply a knockdown factor by print orientation (PETG ~0.7×, PLA ~0.45×
  cross-layer). https://doi.org/10.1108/13552540210441166 (Ahn et al. 2002)
- <a id="blender"></a>**Blender / bpy** — open-source 3D suite; `bpy` is its Python API, used here
  for photoreal (Cycles/EEVEE) rendering. https://docs.blender.org/api/current/
- <a id="trimesh"></a>**trimesh** — Python mesh library (load/repair/measure). https://trimesh.org/
- <a id="manifold3d"></a>**manifold3d** — robust mesh boolean/manifoldness library.
  https://github.com/elalish/manifold
- <a id="opencv"></a>**OpenCV** — computer-vision library (contours, PCA, moments) used for axis/
  silhouette analysis. https://opencv.org/
- <a id="oxc"></a>**oxc** — fast JS/TS linter+formatter; the inspiration for `3d`'s layered
  linter/formatter config structure (§25). https://github.com/oxc-project/oxc
- <a id="jq"></a>**jq** — command-line JSON processor; the inspiration for `3d om` (pipeable filters
  over the object model). https://jqlang.github.io/jq/
- <a id="ffmpeg"></a>**ffmpeg** — media transcoder with a DAG filter-graph; inspiration for the
  op-DAG + the power-without-bad-UX layering. https://ffmpeg.org/
- <a id="vector-engine"></a>**vector-engine** — the user's headless compute-graph engine
  (`packages/vector-engine`); the model for `3d`'s lib-core ← cli/web/gui split and op-DAG.
  https://github.com/hyperide/hyper-saas
- <a id="quorex"></a>**quorex** — the user's ralphex-based autonomous Claude-Code runner (fresh
  session per task + review pipeline); the engine behind `3d ai <tool> loop`.
  https://github.com/alex-mextner/quorex
- <a id="hyperframes"></a>**HyperFrames** — HeyGen tool where agents compose video via HTML/CSS/JS;
  used to build the §14 showcase demo. https://hyperframes.heygen.com/

## AI models

- <a id="sam2"></a>**SAM2** — "Segment Anything Model 2" (Meta); promptable image/video
  segmentation, used to extract a clean subject mask from a reference photo.
  https://github.com/facebookresearch/sam2
- <a id="depth-anything"></a>**Depth-Anything (V2)** — monocular depth estimation foundation model.
  https://github.com/DepthAnything/Depth-Anything-V2
- <a id="marigold"></a>**Marigold** — diffusion-based monocular depth/normal estimation.
  https://github.com/prs-eth/Marigold
- <a id="trellis"></a>**TRELLIS** — image/text-to-3D structured latent generation (Microsoft).
  https://github.com/microsoft/TRELLIS
- <a id="hunyuan3d"></a>**Hunyuan3D** — Tencent image-to-3D generation. https://github.com/Tencent/Hunyuan3D-2
- <a id="instantmesh"></a>**InstantMesh** — feed-forward sparse-view → mesh.
  https://github.com/TencentARC/InstantMesh
- <a id="lrm"></a>**LRM** — Large Reconstruction Model; feed-forward single-image → triplane NeRF.
  https://yiconghong.me/LRM/
- <a id="wonder3d"></a>**Wonder3D** — cross-domain multi-view normal-map diffusion for single-image
  3D; its normal maps are useful as a critic channel. https://github.com/xxlong0/Wonder3D
- <a id="nvdiffrast"></a>**nvdiffrast** — differentiable rasterizer (NVIDIA) for inverse rendering.
  https://github.com/NVlabs/nvdiffrast
- <a id="mitsuba"></a>**Mitsuba 3** — differentiable/physically-based renderer.
  https://www.mitsuba-renderer.org/
- <a id="colmap"></a>**COLMAP** — structure-from-motion / multi-view stereo. https://colmap.github.io/
- <a id="ollama"></a>**ollama** — run LLMs locally; optional local-AI backend for `3d ai`.
  https://ollama.com/

## Metrics

- <a id="iou"></a>**IoU** — Intersection-over-Union; overlap of two regions (silhouettes) or volumes.
  1.0 = identical. https://en.wikipedia.org/wiki/Jaccard_index
- <a id="chamfer"></a>**Chamfer distance** — mean nearest-neighbor distance between two point sets/
  meshes; lower = closer. https://en.wikipedia.org/wiki/Chamfer_(geometry) (CD as used in 3D recon)
- <a id="f-score"></a>**F-score@τ** — harmonic mean of precision/recall of points within threshold τ;
  standard 3D-reconstruction accuracy metric (Tatarchenko et al. 2019). https://arxiv.org/abs/1905.03678
- <a id="hausdorff"></a>**Hausdorff distance** — worst-case (max) surface deviation between two
  shapes. https://en.wikipedia.org/wiki/Hausdorff_distance
- <a id="normal-consistency"></a>**Normal consistency** — agreement of surface normals between
  meshes; complements point-distance metrics.
- <a id="lpips"></a>**LPIPS** — Learned Perceptual Image Patch Similarity; perceptual image distance
  (Zhang et al. 2018). https://arxiv.org/abs/1801.03924
- <a id="ssim"></a>**SSIM** — Structural Similarity Index for images.
  https://en.wikipedia.org/wiki/Structural_similarity_index_measure
- <a id="psnr"></a>**PSNR** — Peak Signal-to-Noise Ratio; pixel-level image fidelity.
  https://en.wikipedia.org/wiki/Peak_signal-to-noise_ratio
- <a id="clip-sim"></a>**CLIP-similarity** — cosine similarity of CLIP image/text embeddings;
  semantic match score. https://github.com/openai/CLIP

## Concepts in `3d`

- <a id="manifold"></a>**Manifold** — a watertight, closed solid (every edge shared by exactly two
  faces); required for valid boolean ops and printing.
- <a id="csg"></a>**CSG** — Constructive Solid Geometry; building shapes via union/difference/
  intersection. https://en.wikipedia.org/wiki/Constructive_solid_geometry
- <a id="op-dag"></a>**Operation DAG** — the pipeline modeled as a directed acyclic graph of operation
  nodes; editing a past node rolls forward to dependents via topological recompute (§19).
- <a id="object-model"></a>**Object model** — the semantic layer over geometry (a DOM-like tree with
  id/class selectors + a CSS-like stylesheet of rules); see architecture spec §4 and ROADMAP §5.
- <a id="fit-camera"></a>**fit-camera** — silhouette-IoU camera-pose fitting: search azimuth/
  elevation/distance/pan until a render's silhouette best matches a reference photo, then freeze.
- <a id="silhouette"></a>**Silhouette** — the binary subject mask (white=subject) of a render or
  reference; the basis of IoU matching.
- <a id="forced-monotonic-loop"></a>**Forced-monotonic loop** — an LLM edit loop that ACCEPTS a
  parameter change only if the score strictly improves (and gates pass), logging every attempt so a
  failed move is never retried; turns "an LLM fiddling with numbers" into reproducible convergence.
- <a id="rag"></a>**RAG** — Retrieval-Augmented Generation; here: auto-run deterministic tools and
  inject their numbers+images into the AI prompt before it acts (§13).
