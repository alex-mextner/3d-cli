# Apply-research — implementation ideas for the `3d` tools

How to turn the research ([`RESEARCH.md`](RESEARCH.md), [`docs/research/`](docs/research/)) and the
design ([`ROADMAP.md`](ROADMAP.md), [`docs/specs/`](docs/specs/)) into actual `3d` tools. Concrete
approach / library / algorithm per area. Terms: [`GLOSSARY.md`](GLOSSARY.md). This is the
"how I'd build it" companion to the ROADMAP's "what".

## Core (build first — everything depends on it)
- **Object model (§5)** — parse `// @id/@class/@anchor/@section/@view/@color` comments from `.scad`
  (regex over source, OpenSCAD ignores them); read `.3mf` metadata; `.stl` → sidecar `<f>.3d.yaml`.
  In-memory tree of nodes (id, classes=tags, params, bbox). Selector engine: a tiny CSS-like matcher
  (`#id`, `.class`, `.a.b`, descendant). Stylesheet = ordered rules `selector → props` with CSS-style
  specificity (class < id < inline) — resolve per node into an effective style map.
- **Operation DAG (§19)** — port the `vector-engine` executor pattern: nodes
  `{type, inputs, outputs, params, execute}`; topological order; per-node cache keyed on
  `(type, params, hash(inputs))`; dirty-set invalidation → recompute only descendants. History as a
  DAG of edits (not a linear tape) with branch + base-snapshot + op-log persistence. Pure-Python,
  unit-testable without subprocess.
- **Headless lib core (§20)** — refactor commands so logic lives in importable `lib` functions; the
  registry commands are thin argv→core adapters. Same core serves cli/web/(future gui).

## Pixel-match pipeline (the original report cheatsheet)
- **preprocess (subject mask)** — [SAM2](GLOSSARY.md#sam2) when available (prompt = click/box or
  auto), else [Depth-Anything](GLOSSARY.md#depth-anything) + GrabCut fallback (already present).
  De-loco-ify: take the subject/ref as parameters, no fixed filenames.
- **fit-camera (§7)** — silhouette-[IoU](GLOSSARY.md#iou) optimization over azimuth/elevation/
  distance/pan: random search → coordinate descent (already async). Axes/contours via
  [OpenCV](GLOSSARY.md#opencv) PCA + moments + contours; optional `opencode` assist for tuning.
  Freeze pose to `camera.json`, then per-feature work begins.
- **match (§13/§18)** — [forced-monotonic loop](GLOSSARY.md#forced-monotonic-loop): accept an edit
  only if score strictly improves and gates pass; changelog prevents retrying failed moves. Feature
  selectors (object model) scope per-feature IoU instead of a hardcoded part list.
- **metrics (§13.4)** — implement with pinned conventions: [Chamfer](GLOSSARY.md#chamfer)/
  [F-score@τ](GLOSSARY.md#f-score)/[Hausdorff](GLOSSARY.md#hausdorff)/normal-consistency via
  trimesh + scipy KD-tree; [SSIM](GLOSSARY.md#ssim)/[LPIPS](GLOSSARY.md#lpips)/[PSNR](GLOSSARY.md#psnr)/
  [CLIP-sim](GLOSSARY.md#clip-sim) via the respective libs. Persist every run to the metrics store.

## AI layer (§13)
- **adapters** — share the `3d web` log-adapter interface (claude/codex/opencode). Backends: claude/
  codex/[ollama](GLOSSARY.md#ollama)/opencode (opencode free out-of-box).
- **operators** — `do` (apply + re-gate), `review` (RAG: auto-run the tool's manifest, inject
  numbers+images, return numbered critique + recommended `3d` commands), `loop`
  ([quorex](GLOSSARY.md#quorex), validation = the tool's metric target).
- **design** (richest net-new) — program-synthesis-style: have the model emit/edit OpenSCAD program
  fragments (CSGNet/DeepCAD framing), render via CLI, score, loop. Bootstrap a `.scad` skeleton.
- **bench** — adopt the ModelRift image→OpenSCAD task format but auto-score (render-success + IoU +
  Chamfer vs target). Persist to compare models over time.

## Print & physics
- **pack (§5)** — 3MF builder: orientation search minimizing support volume / maximizing strength
  (per-part, using material anisotropy), copy layout, per-object color/material, optional split with
  printed connectors. Drive from `3d.yaml`.
- **strength (§6)** — beam/wall/hoop stress vs allowable from the [materials registry](GLOSSARY.md#fdm-anisotropy);
  apply the anisotropy knockdown by print orientation; SF per load-case at anchors.
- **slice (§4)** — always dry-run check; map material+printer → slicer profiles; `--list-profiles`;
  self-explaining profile errors. Default Bambu A1 + PLA/PETG.
- **photoreal (§3)** — export 3MF → [Blender](GLOSSARY.md#blender) `bpy` headless (Cycles/EEVEE) with
  materials/colors from the registry; HDRI + soft shadows. On-demand install only.

## Surface & ergonomics
- **sections (§3)** — always colored cut (per-part `color()` outside `difference()`), presets
  (`mid-x/y/z`, `through:#anchor`, named), multi-section, auto-camera maximizing cut-face area /
  minimizing occlusion (reuse fit-camera machinery, different objective).
- **two-level commands (§24)** — primitives under domains (`3d check mesh`, `3d match score`);
  umbrellas (`3d analyze`) auto-run all applicable from the object model.
- **lint (§25)** — [oxc](GLOSSARY.md#oxc)-style: rule registry (id/level/selector/autofix), `3d.yaml`
  `lint:` section, `3d lint` report + `3d fmt` formatter. Start with a small geometry/printability/
  naming rule set, grow the catalog.
- **report/demo (§22/§14)** — `3d report` stitches the op-DAG run record + web SSE timeline +
  before/debug/after images + metrics with ffmpeg; `3d demo` builds the polished promo via
  [HyperFrames](GLOSSARY.md#hyperframes).

## Cross-cutting
- **errors** — every tool raises structured `lib/errors.py` (what/why/remediation/accepted-values/
  install-command). **config dir** = `~/.config/3d-cli/` (reconcile from `~/.config/3d/`, §23).
- **tests** — TDD; test the core directly; smoke each command. **docs** — README explains terms at
  first mention + links [GLOSSARY](GLOSSARY.md); life-like pipe examples + before/debug/after shots.
