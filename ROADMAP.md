# 3d CLI — ROADMAP

The single source of all requirements for the `3d` toolkit (CLI + web). Captured
from the design conversation. Keep this updated as items land.

Status legend: ✅ done · 🔨 in progress · 📋 planned

---

## 0. Vision
`3d` is a rich, cross-platform (macOS + Linux) command-line + web toolkit for
AI-assisted, reference-photo-driven **parametric** 3D modeling (OpenSCAD-first),
verification, pixel-perfect matching, print preparation, physics/kinematics, and
live observation of AI agents doing the work. Its own repo
(`github.com/alex-mextner/3d-cli`), `3d` symlinked into `~/.files/bin`.

## 1. Engineering policy (applies to ALL work)
- 📋 **Python everywhere** — replace all `sh`/`bash` with Python; `bin/3d` is a thin
  Python dispatcher. Type hints everywhere, **mypy-clean**. **async** where it
  genuinely helps (parallel OpenSCAD renders in multi / fit-camera / match,
  subprocess via `asyncio`, async SSE).
- 📋 **Tests** — pytest (unit: bbox→camera, axis-math, score/IoU, strength formulas,
  `3d.yaml` loader, log adapters) + CLI smoke harness (`--help` + runs on `examples/`)
  + mypy in the test gate. A `3d test` / CI-ready run.
- ✅ **Commit discipline** — ATOMIC commits; **run `codex exec review --uncommitted`
  before EVERY commit**, read findings, fix real issues, then commit; **push to
  origin regularly**.
- 📋 **Dependencies fully specified** — `requirements.txt` + `3d doctor` + first-run
  auto-bootstrap. No dependency left implicit.
- 📋 **Parallel build** — independent work runs in parallel subagents in **separate
  git worktrees** (distinct file ownership → clean merge).
- 📋 **Error UX — verbose, actionable errors.** Every error states (1) WHAT failed and
  WHY (the actual cause, not just a stack trace), (2) CONCRETE step-by-step remediation
  (the exact command to run / file to edit), (3) the **accepted options/values** when
  input was invalid (e.g. "got `--plane=ZZ`; accepted: YZ, XZ, XY"), and (4) the precise
  install command + which tier degrades if a dependency is missing. Never a bare
  "command failed". A shared `lib/errors.py` with structured, helpful error types.

## 2. First-run auto-bootstrap (NO manual setup)
- 🔨 On any `3d` invocation, if not bootstrapped (`~/.config/3d-cli/.bootstrapped`):
  auto-clone/configure OpenSCAD libraries (BOSL2, NopSCADlib) + set `OPENSCADPATH`,
  once, quietly, idempotent, non-fatal offline.
- 📋 **Remove `3d setup` and `3d libs install`** — both become automatic. Keep only
  `3d libs path` (info). `3d doctor` stays (read-only health/compat report).

## 2a. Materials & printers — shared, cross-cutting vocabularies
- 📋 **Single canonical registries** `materials.yaml` + `printers.yaml` (built-in defaults
  + user/project overrides), referenced BY NAME everywhere. Materials and printers are
  cross-cutting concepts used at EVERY stage:
  - **`3d.yaml`** parts reference a material + the project a printer by name.
  - **strength / FEA** pull material properties (density, E-modulus, tensile/yield,
    **FDM layer-adhesion anisotropy factors**, max temp) from the material entry.
  - **slicing** maps material+printer → slicer filament/process/machine profiles.
  - **rendering / visualization** pull the material's color + finish (matte/gloss/metal)
    for both OpenSCAD colors and the photoreal (Blender) shader.
  - **simulations** (thermal/kinematics) use material + printer constraints.
  - `3d materials list|show <name>`, `3d printers list|show <name>`. One vocabulary, no
    duplicated per-stage definitions.

## 3. Core command surface (option-driven, consolidated)
- 🔨 **`3d render <file.scad>`** — `--view front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right` (camera from model bbox), `--multi [outdir]` (all standard angles), `--section` (see "Sections" below), `--cam` (manual override, last resort), `--ortho`, `-D k=v`, `--debug`.
- 📋 **Camera: whole-model-in-frame is the DEFAULT, not a flag.** `--autocenter`/`--viewall`
  behavior is always on — every render centers and fits the whole model unless told otherwise.
  There is no `--autocenter`/`--viewall` flag. **Cropping/zoom happens ONLY when the camera is
  told to focus** on specific regions/anchors/parts.
- 📋 **Many convenient modes & PRESETS, not raw numbers.** The primary UX for camera/render/
  section is high-level intent; raw `--cam` coordinates are the rare escape hatch. Provide a rich
  preset library (named views, framings, section presets) and let the **object model (§5)** name
  reusable ones per project.
- 📋 **Object-model-driven camera framing** (§5): say WHAT must be in frame and FROM WHICH
  angle — `--frame <anchor|part|tag>[,..]` (fit exactly those, zoomed/cropped to them) and
  `--view`/named angles — instead of computing eye/center coordinates by hand. The camera solves
  the pose to satisfy the intent.
- 📋 **Photorealistic render** — `3d render --photo` (or `3d photo`) via **Blender** (Cycles/EEVEE):
  export STL/3MF → Blender headless (`bpy`) with materials/colors from the materials registry,
  proper lighting/HDRI, soft shadows. **Blender is installed ON DEMAND** (only when the user
  requests a photoreal render), NOT auto-bootstrapped. README must show **OpenSCAD render vs
  Blender photoreal** side by side so the difference is clear.
- 📋 **`3d check <file>`** — runs ALL applicable gates by DEFAULT; `--mesh --printability --collision --manifold --silhouette` select a subset; `--skip X` excludes. Per-gate breakdown + PASS/FAIL. (= the acceptance master gate.)
- ✅ `export` (mesh-validated, nonzero on bad geometry), `validate`, `params`.
- ✅ `mesh`, `printability`, `collision` (static / `--frame` / `--viz`), `acceptance`, `silhouette`, `overlay`, `score`, `match` (forced-monotonic loop + changelog, `--dry-run`), `fit-camera`, `preprocess`.
- 📋 **Sections — colored-only, anchored, multi, auto-framed** (replaces the confusing
  "true cross-section" / "--color per-part assembly mode" wording):
  - **Always colored.** Every section preserves each part's color ON the cut face. The plain
    monochrome section is REMOVED — never wanted. No `--color` flag (color is not optional).
  - **High-level spec (primary):** presets `mid-x|mid-y|mid-z` (through the centroid on an
    axis), `through:<anchor>` (plane through a named feature), and named sections from the
    object model (`--section <name>`). Low-level secondary: `--plane YZ|XZ|XY [--at <coord> |
    --offset d] [--keep pos|neg]`. All cameras 6-param vector, never 7-param gimbal.
  - **Multiple sections at once** — accept several `--section` specs → render each + optionally
    a combined multi-cut view.
  - **Auto-camera for sections** — pose solved to **maximize the projected area of the cut face
    in frame** and **minimize occlusion** of the cut by the remaining solid (shares machinery
    with `fit-camera`, different objective). `--cam` overrides.
- 📋 **Thin aliases** `multi`/`section`/`mesh`/`printability`/`collision`/`acceptance` → `render --multi`/`render --section`/`check --…`.

## 4. Slicing
- 🔨 `3d slice <stl|3mf|scad> [-o] [--printer] [--profile]` — Orca > Bambu Studio > Prusa autodetect.
- 📋 **Always runs the sliceability check** as a gate. Rename `--check` → **`--dry-run`** (slice to temp, verify only, keep no g-code).
- 📋 **Profiles must be self-explaining.** A slicer needs config files — typically a
  **machine** profile (printer geometry/firmware), a **process** profile (layer height,
  speeds, supports, infill), and a **filament/material** profile. `--profile` /
  `--printer` help + errors must explain: WHAT each file is, WHERE to get it (export from
  the OrcaSlicer/Bambu Studio GUI → "export config", or the slicer's bundled presets), and
  WHY. Provide `3d slice --list-profiles` (discover installed-slicer presets + any in the
  project), ship/auto-pick a **sensible default for the Bambu A1 + PLA/PETG**, and if a
  profile is missing/invalid, the error names the accepted forms and the exact export steps.
  Prefer letting the `3d.yaml` (material/printer/supports/infill) drive profile selection so
  the user rarely hand-passes raw json paths.

## 5. 3MF builder + project config (`3d.yaml`)
- 📋 **`3d pack <3d.yaml>`** — emit a print-ready **3MF**: per-part orientation solved
  for **min support + max strength**, copy layout, colors/materials, optional
  **splitting into parts** for glue / printed-connector joints, per-object slicer settings.
- 📋 **`3d.yaml`** — project+part config consumed by BOTH the AI and the tools:
  - `project`: name, units, copies, printer, default material, bed.
  - standard **tags** (combinable, not a single type): `structural | shell | cosmetic | functional | flexible | engineering | artistic | press-fit | removable | bought`.
  - per `parts.<name>`: file, module, tags, material, color, copies, `orientation` (auto|flat-bottom|[rx,ry,rz]), `supports` (minimize|none|tree), `infill`, `split: {allowed, joint: printed-connector|dovetail|pin|glue}`, `anchors: [...]`, `loads: [{at:<anchor>, type, N, dir, min_sf}]`.
- 📋 **Anchors** answer "where + which characteristics": named anchors declared in the
  `.scad` via `// @anchor <name> pos=[x,y,z] dir=[..] area=<feat> note="…"` comments
  (recommended over a sidecar `.anchors.yaml`); `loads` in `3d.yaml` reference them.
- 📋 **Object model = a DOM + stylesheet, without HTML/CSS** (design: `docs/specs/2026-06-05-3d-cli-architecture.md` §4):
  geometry is a tree (assembly → parts → features); the object model adds an HTML/CSS-like
  layer over it but with no HTML/CSS:
  - **id** (unique, `#boiler`) + **class** (= `tags`, `.structural`) per node.
  - **selectors** — one addressing mechanism reused EVERYWHERE: `#valve`, `.cosmetic`,
    `.structural.removable`. Used by `render --frame .cosmetic`, `section through:#valve`,
    `check --only .structural`, `pack` per-class supports, `ai` tool scoping.
  - **stylesheet (rules)** `selector → properties` (color, material, orientation, supports,
    infill, gate set, loads, section & camera-frame membership) — authored once, not repeated
    per part. **Cascade + specificity** like CSS (class default < id override < inline; pinnable).
  - engineering-vs-art is just a different rule set; no fork.
- 📋 **Object-model file format — backward-compatible, never breaks other tools:**
  - `.scad` → `// @anchor`/`// @section`/`// @part`/`// @view`/`// @class`/`// @color`
    comments (OpenSCAD ignores comments → file still renders everywhere).
  - `.3mf` → native metadata + per-object color/material (preferred rich mesh format).
  - `.stl` → **sidecar** `<model>.3d.yaml` next to it (STL has no metadata slot; embedding
    would corrupt it → sidecar only).
  - `3d.yaml` ties parts ↔ files ↔ object model. One model drives sections, camera framing,
    colors/materials, `pack`, strength, kinematics, AI RAG.
- 📋 **Named camera views/framings in the object model** — declare WHAT must be in frame and
  FROM WHICH angle (`// @view <name> frame=#valve,.cosmetic angle=front-left`); reuse by name
  (`3d render --view <name>`) instead of raw coordinates. Convenient modes & presets first,
  numbers last.

## 6. Physics / math tools
- 📋 **`3d strength <part|3d.yaml>`** — strength-of-materials (beam/wall/hoop stress vs
  allowable, FDM anisotropy by print orientation, SF per load-case at anchors).
- 📋 **`3d fea`** (optional) — CalculiX (via FreeCAD FEM) / Elmer for nontrivial cases.
- 📋 **`3d kinematics <3d.yaml>`** — model + verify motion (per-frame, axes/guides, reach/sweep).
- 📋 **`3d animate <3d.yaml>`** — generate animation + **per-frame verification**
  (collisions, sync with the motion model). Requires **ffmpeg** (check/install).

## 7. Camera fit, axes, opencode
- ✅ **`3d fit-camera <model> <ref>`** — silhouette-IoU camera pose fitting (bbox-derived
  bounds), saves `camera.json`, writes fit render + overlay; `--draw-axes`.
- 📋 Compute axes/contours by **math + OpenCV/ImageMagick** (PCA, moments, contours) by default.
- 📋 **Optional `opencode` integration** (`--opencode`) for iterative axis tuning / checks.
  opencode runs out-of-box with free models (no key needed) — use as an optional assist.

## 8. Visual debug modes
- 📋 Rich `--debug` across render/fit-camera/score/strength/kinematics: draw intermediate
  results with **overlaid axes (PCA/bbox), contours, feature/anchor labels, masks,
  render↔reference overlays**. Emit **before / intermediate-debug / after** images.

## 9. `3d web` — interactive dashboard (🔨 in progress, worktree)
- Local FastAPI + uvicorn + **SSE** app. Config `~/.config/3d-cli/web.json` (project_root,
  port, host). Default project_root e.g. the garage-band repo.
- **Watch agents work live** — structured SSE logs + visualizations, via extensible
  **adapters**: Claude (dynamic read of JSONL transcripts), Codex, opencode. Auto-associate
  agents↔projects by mentioned dirs/files; cache tracked session ids; detect inactive
  sessions and find new ones.
- 3D **model viewer** (three.js): orbit, toggle **analytical layers**, **compare**.
- **Constants editor** with Figma-like **scrubbers** (drag; **Shift = fine, Alt = coarse**),
  live dynamic re-render.
- Run **animations**; change **colors/materials**; view project **spec**; browse **all projects**.

## 10. AI model running (ollama) + hardware compatibility
- 📋 `3d` can use **ollama** for local AI; install required models **on user request**.
- 📋 **Hardware compatibility check** — describe min specs; check the user's OS/RAM/disk/
  CPU/GPU; **use GPU where possible**. Target a **MacBook M4 Pro** class; warn/skip models
  that won't fit. `3d doctor` reports hardware + model feasibility.
- 📋 **ffmpeg** — check/install (at minimum) for animation export.

## 11. Docs
- 📋 **README** with life-like examples and invocations — especially **pipes and series
  of calls** with varied args (active-use workflows), and embedded **screenshots**:
  **before / intermediate-debug / after** (generated by the tool, committed to `docs/img/`).
- ✅ `docs/migration.md` (source-tool → `3d` subcommand map). 📋 `docs/critic-prompts.md`.

## 12. Research & extension (ongoing)
- 📋 Re-read the research report (`garage-band/projects/lego-loco/research/report.md`) and
  put into work **everything still not implemented**.
- 📋 Survey **more scientific papers** on related topics (silhouette/inverse-procedural/
  differentiable rendering, single-image-to-3D, depth/segmentation, FDM strength), **extend
  the report**, and **implement** the interesting algorithms; **use and improve** the tools
  it mentions (BOSL2, NopSCADlib, trimesh/manifold3d, SAM2, Depth-Anything, TRELLIS/
  Hunyuan3D, Mitsuba/nvdiffrast, COLMAP, etc.).

## 13. `3d ai <tool>` — AI-assisted tool group (operators + RAG + loop + benchmarks)
A unified AI layer over the analytical commands. Pattern: **`3d ai <tool> <operator> [args]`**
(e.g. `3d ai axis do|review|loop`). Backend-agnostic via the SAME adapters as `3d web`
(claude / codex / opencode — opencode runs out-of-box with free models). `--backend` selects;
default claude.

### 13.1 Operators — universal, available for EVERY ai tool
- 📋 **`do`** — run the AI ONCE to perform the task and **apply** the result (mutating: writes the
  SCAD edit / `camera.json` / etc.). One shot, then the deterministic gates re-run to confirm.
- 📋 **`review`** — read-only **RAG-style** advisory, **never mutates**. Before the model is called,
  a curated set of deterministic `3d` tools is **auto-run** and their **numbers + images + full
  context** are injected into the prompt immediately (the RAG: ground truth in context, no
  guessing). The model returns a DETAILED critique — concrete numbers, specific edits in mm, and a
  list of **recommended `3d` commands** to run next. This is the "detailed flavour" the user
  asked for: full context, details, figures, recommended tools.
- 📋 **`loop`** — autonomous iteration via **quorex** (`/Users/ultra/xp/quorex/quorex`,
  ralphex-based: fresh agent session per task, 5-agent→codex→2-agent review pipeline, worktree
  isolation, web dashboard, notifications). `3d ai <tool> loop` **emits a plan** whose *validation
  commands* are this tool's benchmark/metric targets, then drives quorex until the target is met /
  converged / round-cap. The loop's stop condition is a NUMERIC benchmark threshold, not vibes.

### 13.2 RAG pre-flight — what `review`/`do` auto-run before the model
Each tool declares a **manifest** of deterministic pre-runs; their outputs (numbers + rendered
PNGs) are embedded in the prompt, plus a "recommended tools" block (relevant `3d` subcommands with
one-line usage):
- `axis` → OpenCV PCA principal axes, contours, image moments, bbox, centroid + annotated overlay.
- `match`/`fit-camera` → silhouette **IoU**, overlay-diff (AE / blend / canny), current
  `camera.json` + before/after PNGs.
- `critique` (model↔reference) → multi-view renders + the reference + current score metrics.
- `strength` → computed stress vs allowable per load-case + SF.
- `printability` → overhang / wall / clearance report.

### 13.3 Initial tool set (each gets `do/review/loop`)
- 📋 `axis`, `match` (camera/silhouette), `critique` (model↔reference), `strength`, `printability`,
  `design` (generate/modify SCAD from a reference). Adding a tool = declare its RAG manifest +
  benchmark/metric; the three operators come for free.

### 13.4 Benchmarks (`3d ai`) + metrics (all tools) — always computed, always saved
- 📋 **Standard, commonly-accepted benchmarks** (not bespoke-only):
  - **geometry**: Chamfer distance (L1/L2), **F-score@τ**, Hausdorff, normal consistency, volumetric **IoU**.
  - **render-vs-reference**: silhouette **IoU**, **LPIPS**, **SSIM**, **PSNR**, **CLIP-similarity**.
  - **camera/pose**: reprojection error, rotation/translation error.
  - **OpenSCAD-generation suite**: adopt the public *image→OpenSCAD, iterate-via-CLI-render*
    task format (ref: ModelRift OpenSCAD-LLM benchmark —
    https://modelrift.com/blog/openscad-llm-benchmark) BUT replace its purely **subjective 0–5
    score** with the automated metrics above (render-success rate + IoU + Chamfer against a target
    mesh), so results are reproducible. Keep a subjective score as one column, not the only one.
  - `3d ai bench [suite]` runs the suite; `3d ai bench --compare` shows deltas vs history.
- 📋 **Per-tool metrics** for the non-AI tools too (render time, mesh stats, gate pass/fail, score
  deltas, IoU) — emitted on every run.
- 📋 **Always-on, persisted longitudinal store.** EVERY `do/review/loop` and every tool run appends
  a timestamped record (backend, model, tool, inputs, metric/benchmark scores, tokens, cost,
  wall-time) to a metrics store (`~/.local/share/3d-cli/metrics/*.jsonl` + per-project `metrics/`).
  Purpose: regression tracking + **data for subsequent improvement** (prompt tuning, model A/B,
  fine-tuning). `3d metrics` / `3d ai bench --compare` view history + deltas. `3d web` surfaces the
  benchmark/metric trend lines live.

## 15. Project-agnostic, modular architecture (the go-to design)
Full design: **`docs/specs/2026-06-05-3d-cli-architecture.md`**. `3d` is a Swiss-army knife for
**all 3D FDM work — engineering-first today, artistic later** — NOT the lego-loco tool it started
as. The four layers: (1) project-agnostic **core**, (2) self-registering **capability plugins**
(gates/ai-tools/slicers/render-backends/importers/metrics), (3) **project layer** (pure data:
`3d.yaml` + parts + references + project checks; CLI finds nearest `3d.yaml` from cwd), (4) optional
named **pipelines** (the reference-photo match is ONE pipeline, not the identity).
- 📋 **No subject knowledge in core (enforce in code, not just docs).** Core tools take subject /
  reference / feature-list / camera / plane as PARAMETERS — zero default filenames, cameras, part
  lists, or feature taxonomies. Sweep core for leakage; priority offenders found:
  `lib/preprocess_reference.py` (written around "the locomotive" → must mask "the subject"),
  `docs/critic-prompts.md` (hardcodes `funnel/boiler/smokebox` as THE feature taxonomy → caller
  supplies it). Marked "e.g." examples (`config.py` "ejector", `frame_check.py` "cartridge") are OK.
  Add a CI/grep check that core contains only marked examples.
- 📋 **Gate set is project-determined, not a fixed list.** `3d check` with no flags runs the gates
  THIS project declares (via `3d.yaml`/tags/stylesheet), so an art piece runs manifold+printability
  and skips strength/collision while an engineering part runs the full set — same binary.
- 📋 **Extension = drop in a self-registering module** (command/gate/ai-tool/backend/importer/metric)
  following the foundation wave's registry contract; never edit a central dispatcher or shared list.

## 16. README & docs de-coupling (user MINIMUM ask — keeps getting missed)
- 📋 **ONE docs/reframe owner** edits the README intro + Requirements + framing; feature work NEVER
  touches these (a feature ships `--help` + a `docs/commands/<name>` fragment only). Runs FIRST
  after the foundation wave (the foundation rewrites the README with the OLD loco framing — that
  output is known-throwaway).
- 📋 **README intro reframed:** `3d` = scriptable, AI-assisted CLI for ANY 3D FDM project
  (engineering now, art later); the reference-photo match is one example pipeline (linked), not the
  headline. Drop "operationalizes the lego-loco research pipeline."
- 📋 **Requirements section = a plain LIST, no manual instructions.** Every dependency, a one-line
  purpose, an `(optional)` marker where applicable, and a single line that the CLI **auto-installs
  what it can** (`3d doctor` to inspect). DELETE the manual venv/pip walkthrough and the `3d setup`
  block. Must list ALL deps (it is still incomplete).

## 17. Research-driven backlog (from §12 survey)
- 📋 Source of truth: `garage-band/projects/lego-loco/research/3d-cli-backlog.md` (14 prioritized
  items P0–P5, each with integration point + expected metric). Fold the actionable ones into the
  sections above. Highest-value NET-NEW vein: **program synthesis for CAD** (CSGNet / ShapeAssembly
  / DeepCAD) → `3d ai design`. Also: pin exact metric formulas + library conventions in `3d metrics`
  (§13.4); peer-reviewed FDM anisotropy knockdowns (PETG ~0.7×, PLA ~0.45× cross-layer) in
  `3d strength`; normal-map critic channels (Marigold/Wonder3D) for `3d ai critique`.
