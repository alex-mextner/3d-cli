# Research index

Consolidated index of the literature / benchmark / metric research behind `3d`. The full survey and
sources are vendored in [`docs/research/`](docs/research/). Terms are defined in
[`GLOSSARY.md`](GLOSSARY.md).

## Sources in this repo
- [`docs/research/report.md`](docs/research/report.md) — the full research report (~16k words; CLI
  3D tools, OpenSCAD libraries, AI skills/prompts, verification, pixel-perfect 2D→3D, plus the
  extended survey §19–§25 below). PDF: [`docs/research/report.pdf`](docs/research/report.pdf).
- [`docs/research/sources.md`](docs/research/sources.md) — ~130 sources, full visible URLs, grouped.
- [`docs/research/3d-cli-backlog.md`](docs/research/3d-cli-backlog.md) — 14 prioritized (P0–P5)
  implementation items, each {what, why, paper/tool, integration point, expected metric, status}.

## Survey summary (extended sections in the report)
- **Program synthesis for CAD** — CSGNet, ShapeAssembly, DeepCAD → most valuable net-new vein for
  `3d ai design` (generate/edit parametric programs, not meshes).
- **Single-image / few-image → 3D** — LRM, InstantMesh, Wonder3D, TRELLIS, Hunyuan3D. Key insight:
  use Wonder3D/[Marigold](GLOSSARY.md#marigold) **normal maps** as a critic channel, not the mesh.
- **Differentiable rendering** — nvdiffrast, Mitsuba 3 — for gradient-based shape/pose fitting (low
  priority; the silhouette-IoU search covers the near-term need).
- **Segmentation / depth** — [SAM2](GLOSSARY.md#sam2), [Depth-Anything V2](GLOSSARY.md#depth-anything)
  for clean reference masks + depth priors in `preprocess`.
- **FDM strength** — peer-reviewed anisotropy (Ahn 2002, PMC9230522) with citable knockdown factors
  (PETG ~0.7×, PLA ~0.45× cross-layer) → `3d strength`.
- **Evaluation metrics** — exact formulas + canonical sources + library + convention to pin for
  Chamfer / F-score@τ / Hausdorff / normal-consistency / vol-IoU and SSIM / LPIPS / PSNR / CLIPScore
  → `3d metrics` / `3d ai bench`.
- **OpenSCAD-LLM benchmark landscape** — ModelRift (subjective 0–5, the gap to fix), BlenderLLM /
  CADBench, CadQuery benchmarks → adopt the task format, replace subjective scoring with automated
  metrics.

## Design inspirations (non-paper, but research that shaped the architecture)
- [jq](GLOSSARY.md#jq) (composable pipes → `3d om`), [ffmpeg](GLOSSARY.md#ffmpeg) (DAG filtergraph +
  power-vs-UX → §19/§21), [vector-engine](GLOSSARY.md#vector-engine) (headless core + op-DAG →
  §19/§20), CSS/DOM (object model → §5), [oxc](GLOSSARY.md#oxc) (layered linter/formatter → §25),
  [HyperFrames](GLOSSARY.md#hyperframes) (code-first video → §14), [quorex](GLOSSARY.md#quorex)
  (autonomous loop → §13).

## Planned further research (see ROADMAP §12/§27)
- Deeper rule catalog for the linter (§25), oxc rule taxonomy as a template.
- Object-model / op-DAG serialization formats in comparable tools (Houdini HDA, Blender geometry
  nodes, USD) for the history-DAG design (§19).
- More OpenSCAD-LLM / code-CAD benchmarks as they appear, to keep `3d ai bench` standard-aligned.
