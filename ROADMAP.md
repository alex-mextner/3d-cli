# 3d CLI — ROADMAP

The single source of all requirements for the `3d` toolkit (CLI + web). Captured
from the design conversation. Keep this updated as items land.

Status legend: ✅ done · 🔨 in progress · 📋 planned

---

## 0. Vision
`3d` is a rich, cross-platform (macOS + Linux) command-line + web toolkit covering the WHOLE
FDM lifecycle — idea/spec → AI-assisted, reference-photo-driven **parametric** modeling
(OpenSCAD-first) → verification, pixel-perfect matching, physics/kinematics → **material
procurement/inventory** → print prep → **printing, live monitoring & failure recovery** (Klipper/
Moonraker, OctoPrint, Bambu, Prusa) — plus live observation of AI agents doing the work. Its own repo
(`github.com/alex-mextner/3d-cli`), installed as a standard Python package exposing the `3d`
console-script (pipx / uv tool / pip — see §29), not a manual symlink.

## 0a. Design influences & philosophy (the meta-thinking)
The whole tool is shaped by a few deliberate analogies — keep them visible, they explain WHY
the surface looks the way it does:
- **jq** → composable, pipeable filters over a structured document. `3d om` (§18) is jq for the
  3D object model; everything streams through stdin/stdout so shell pipes compose.
- **ffmpeg** → a complete, expressive filter-GRAPH (a DAG) with total low-level power — but its UX
  is notoriously hostile. We keep the power (Layer 1, §21) and add a friendly layer on top (Layer 2)
  that resolves INTO it. ffmpeg's filtergraph is also why the pipeline is a DAG (§19).
- **vecli / vector-engine** (`github.com/hyperide/hyper-saas`, `packages/vector-engine`) → a
  **headless compute-graph core** (`lib`) with thin frontends (`cli`/`web`/`gui`) over it (§20), and
  an **operation DAG** where editing a past node rolls forward to dependents (§19). We adopt the
  compute-DAG + headless-core split and fix its linear-history gap with a real history DAG.
- **CSS + HTML DOM** → the object model is a tree (DOM) addressed by **id + class selectors** with a
  **cascading stylesheet** of rules (§5) — but with no HTML/CSS, just the addressing/cascade idea.
- **Houdini / Blender geometry nodes** → non-destructive, re-computable node graphs; the same model
  behind §19's roll-forward.
These are not features; they are the lens. New surface should be justifiable by one of them.

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
- ✅ **Dependencies fully specified** — `pyproject.toml` (uv project: core + optional extras
  `preprocess`/`viz`/`web`/`dev`) + `uv.lock` + `3d doctor` + first-run auto-bootstrap. No
  `requirements.txt` (pip-era); `uv sync --all-extras` for the offline `.venv`. No dependency
  left implicit.
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
- 📋 **Compute axes/contours by math (OpenCV/ImageMagick: PCA, image moments, contours).**
  **Why:** a model and a reference photo almost never start aligned — matching their **principal
  axes** (PCA of the silhouette) + bbox + centroid gives `fit-camera` a strong INITIAL pose, so the
  IoU search converges in a handful of steps instead of random-searching from scratch. **Other uses
  for the same primitives:** print **auto-orient** (§5 `pack` — PCA of the mesh picks the flat/strong
  lay-down), **section auto-camera** (§3 — orient the cut to the part's principal axes), **symmetry
  detection** (validate/mirror a part), and **feature localization** for debug overlays (§8 — moments
  + contours locate a hole/boss to label). **Example:**
  `3d fit-camera part.scad ref.jpg --draw-axes` overlays the model's red PCA axes against the
  reference's, so an axis/orientation mismatch is visible *before* any search runs.
- 📋 **Optional `opencode` integration** (`--opencode`) for iterative axis tuning / checks.
  opencode runs out-of-box with free models (no key needed) — use as an optional assist.

## 8. Visual debug modes
- 📋 Rich `--debug` across render/fit-camera/score/strength/kinematics: draw intermediate
  results with **overlaid axes (PCA/bbox), contours, feature/anchor labels, masks,
  render↔reference overlays**. Emit **before / intermediate-debug / after** images.

## 9. `3d web` — interactive dashboard (✅ integrated into the registry CLI)
- Local FastAPI + uvicorn + **SSE** app. Config in `~/.config/3d-cli/` (port, host) — the canonical
  config dir (§23).
- 📋 **No single `project_root`.** `3d web` lists the **registered projects** from
  `~/.config/3d-cli/projects.{toml,json}` (populated by `3d init`, §28). Manage with
  `3d projects list|add <path>|remove <path>`. "Browse all projects" = the registered set, not one
  root. (Removes the old single-root model.)
- **Watch agents work live** — structured SSE logs + visualizations, via extensible
  **adapters**: Claude (dynamic read of JSONL transcripts), Codex, opencode. Auto-associate
  agents↔projects by mentioned dirs/files; cache tracked session ids; detect inactive
  sessions and find new ones.
- 3D **model viewer** (three.js): orbit, toggle **analytical layers**, **compare**.
- **Constants editor** with Figma-like **scrubbers** (drag; **Shift = fine, Alt = coarse**),
  live dynamic re-render.
- Run **animations**; change **colors/materials**; view project **spec**; browse **all projects**.
- 📋 **Print monitoring & control** (§31) — auto-discovers printers, shows **live print status**
  (temps/progress/layer/ETA), the **camera stream**, and the printer's own web UI; **monitor and
  control** prints (start/pause/resume/cancel) — all driven through the same CLI/core so the web is
  just a frontend over `3d print`.

## 10. AI model running (ollama) + hardware compatibility
- 📋 `3d` can use **ollama** for local AI; install required models **on user request**.
- 📋 **Hardware compatibility check** — describe min specs; check the user's OS/RAM/disk/
  CPU/GPU; **use GPU where possible**. Target a **MacBook M4 Pro** class; warn/skip models
  that won't fit. `3d doctor` reports hardware + model feasibility.
- 📋 **ffmpeg** — check/install (at minimum) for animation export.

## 11. Docs
- 📋 **EVERY feature and EVERY option carries motivation + an example.** Non-negotiable authoring
  rule across `--help`, the README, and this ROADMAP: each command/flag states **why it exists**
  (the problem it solves / when to reach for it) and shows a **concrete example invocation** — never
  a bare noun-phrase capability list. A flag documented as just "draw axes" is incomplete; "draw the
  model's PCA axes vs the reference's so an orientation mismatch is visible before fitting —
  `… --draw-axes`" is the bar. Review rejects features/options whose help has no why + example.
  (Backfill the existing terse ROADMAP bullets to this standard as they're implemented.)
- 📋 **README** with life-like examples and invocations — especially **pipes and series
  of calls** with varied args (active-use workflows), and embedded **screenshots**:
  **before / intermediate-debug / after** (generated by the tool, committed to `docs/img/`).
  Explain each domain term at first mention with a link to [`GLOSSARY.md`](GLOSSARY.md) (§26).
- ✅ `docs/migration.md` (source-tool → `3d` subcommand map). 📋 `docs/critic-prompts.md`.

## 12. Research & extension (ongoing)
- 📋 Re-read the research report (`docs/research/report.md`) and
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
- 📋 **`loop`** — autonomous iteration via **quorex** (`github.com/alex-mextner/quorex`,
  ralphex-based; invoked as the `quorex` binary on `PATH`: fresh agent session per task,
  5-agent→codex→2-agent review pipeline, worktree isolation, web dashboard, notifications).
  `3d ai <tool> loop` **emits a plan** whose *validation
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

## 14. Showcase demo video (FINAL deliverable)
- 📋 At the **end** of all the work, produce an **impressive showcase demo of the `3d` CLI** — with
  **music, kinetic text/captions, scene transitions, pacing**. Not a raw screen grab: a polished,
  edited promo.
- 📋 **Built code-first via HeyGen HyperFrames** (open-source, Apache 2.0 — AI agents compose video by
  writing **HTML/CSS/JS**; CLI install). Ref: https://hyperframes.heygen.com/ . The demo is itself a
  small program (HTML/CSS/JS scenes) rendered to a video file — fits this repo's "everything-as-code"
  ethos. Install HyperFrames **on demand** (only when building the demo), not in the bootstrap.
- 📋 **Content**: real `3d` CLI in action — capture actual runs (render `--multi`/`--section`,
  `check` gates, `fit-camera` IoU climbing, `match` loop, `3d ai review` RAG output, `pack`→slice,
  photoreal Blender vs OpenSCAD side-by-side, `3d web` dashboard, benchmark trend lines). Use the
  **before / intermediate-debug / after** images from §8/§11 as scenes. Captions state what each
  command does + the numbers (IoU, SF, metrics).
- 📋 Music bed (royalty-free), title/section kinetic typography, smooth cuts. Render to a lossless
  H.264 file. Deliver via `tg --file`. A `3d demo` command (or `docs/demo/` build script) reproduces it.

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
- 📋 Source of truth: `docs/research/3d-cli-backlog.md` (14 prioritized
  items P0–P5, each with integration point + expected metric). Fold the actionable ones into the
  sections above. Highest-value NET-NEW vein: **program synthesis for CAD** (CSGNet / ShapeAssembly
  / DeepCAD) → `3d ai design`. Also: pin exact metric formulas + library conventions in `3d metrics`
  (§13.4); peer-reviewed FDM anisotropy knockdowns (PETG ~0.7×, PLA ~0.45× cross-layer) in
  `3d strength`; normal-map critic channels (Marigold/Wonder3D) for `3d ai critique`.

## 18. `3d om` — object-model query & transform language (jq for 3D)
- 📋 **`3d om '<expr>'`** — a **jq-like** filter/transform engine over the object model (§5). Reads a
  model (`.scad`/`.stl`/`.3mf` + its object model) from a file arg or **stdin**, applies a chained
  expression, and emits a **model document to stdout** that downstream `3d` commands consume. Pipes
  compose in the shell; jq is the explicit analogy (identity, selection, transformation, composition).
- 📋 **Selectors + operations, chainable** (CSS selectors from §5):
  - select / scope: `.select("#hole-1")`, `.select(".cosmetic")`, `.parent()`, `.children()`.
  - visibility: `.isolate()` (keep only selected), `.exclude()` / `.hide(sel)` (render-with-exclusion).
  - transforms: `.scale(...)`, `.translate(...)`, `.rotate(...)`, `.grow(mm)` / resize a feature
    (e.g. enlarge a hole).
  - **boolean ops**: `.union(sel)`, `.difference(sel)`, `.intersect(sel)`.
  - style: `.color(...)`, `.material(...)`, `.tag(...)`, `.id(...)`.
  - intent: `.section(<preset|plane>)`, `.frame(<sel>, angle)` → produce section/camera intent
    consumed by `render`.
- 📋 **Streaming interchange format** — a defined object-model document (geometry reference +
  selectors/styles/intent) that flows between `3d` commands over stdin/stdout, so `3d om` output
  pipes into `3d render`/`check`/`pack`/`ai`. Round-trips without re-parsing geometry each stage.
- 📋 **Common render-scoping also exposed directly on `render`** for the simple case
  (`render --isolate <sel>`, `--exclude <sel>`, `--frame <sel>`) — `3d om` is the composable engine
  behind them.
- 📋 **Examples (ship in README, with pipes):**
  - enlarge a hole then photoreal-render it:
    `3d om part.scad '.select("#hole-1").grow(2).isolate()' | 3d render --realistic`
    (stdin form: `cat part.scad | 3d om '.select("#hole-1").grow(2).isolate()' | 3d render --realistic`).
  - render everything except cosmetic accents: `3d om asm.scad '.exclude(".cosmetic")' | 3d render --multi`.
  - boolean preview of a pocket: `3d om body.scad '.difference("#pocket")' | 3d render --section mid-x`.
  - isolate the structural set and check it: `3d om asm.scad '.select(".structural").isolate()' | 3d check`.

## 19. Operation DAG + editable history (roll-forward over a changed past op)
Design: `docs/specs/2026-06-05-3d-cli-architecture.md` §9. Modeled on `vector-engine`
(`github.com/hyperide/hyper-saas`, `packages/vector-engine`), fixing its linear-history gap.
- 📋 **Pipeline = a DAG of operation nodes** (`load → select → grow → section → render`), each a
  typed `{type, inputs, outputs, params, execute}` self-registered op. `3d om` chains are paths
  through it; a project build is the whole DAG. (ffmpeg's filter graph is a DAG too — §21.)
- 📋 **Edit a past op → automatic roll-forward.** Topological recompute of ONLY downstream-dependent
  nodes; the rest served from a per-node cache keyed on `(op-type, params, input-fingerprints)`
  (mirror `vector-engine/src/graph/executor.ts` dirty-set + fingerprint cache). No manual replay.
- 📋 **History is a DAG, not a linear tape** (vector-engine has only a linear undo/redo tape) —
  editing a middle op re-derives descendants; **branches** allowed (variant without losing the
  other); undo/redo navigate the DAG.
- 📋 **Persistence** = base snapshot + append-only op log + pointer (mirror
  `vector-engine/src/persistence/{operation-log,serialize}.ts`); compactable; replays
  deterministically into `3d.yaml`/sidecar.

## 20. Headless `lib` core + thin frontends (cli / web / gui)
Design: spec §10. Mirror `vector-engine` (headless, zero-UI) ← `vector-cli`/`vector-wasm`.
- 📋 **`lib` = headless core** (object model + selectors, op-DAG executor + registries, gates,
  renderers, materials/printers, metrics, AI adapters) with a typed public API — NO argv/printing/
  shell. Everything the tool does is a function call on the core.
- 📋 **Thin interchangeable frontends over the one core**: `cli` (the `3d` dispatcher), `web`
  (the dashboard, already built), and a **potential `gui` app** later. A frontend holds no logic
  the core lacks.
- 📋 **Foundation consequence**: the python dispatcher must be a frontend over an **importable core
  package**, not a bag of scripts. Wave-B "core extraction" task lifts any command-embedded logic
  into `lib`; the core is unit-tested directly (no subprocess), frontends smoke-tested.

## 21. Two-layer command surface: technical ⊕ friendly, combinable
Design: spec §11. Inspiration: **ffmpeg's power without ffmpeg's UX**.
- 📋 **Layer 1 — technical/complete**: explicit access to every op/param/selector/plane/camera
  vector/filter-graph edge; nothing hidden; the DAG serialization speaks this.
- 📋 **Layer 2 — user/AI-friendly**: presets, named views, anchors, selectors, intent verbs
  (`mid-x`, `through:#valve`, `--frame .cosmetic`, `bind camera to #hole`) — the default surface
  for humans and the AI tools.
- 📋 **The layers COMBINE (the requirement, not either/or).** Friendly binding ⊕ technical tweak in
  one call, e.g. attach the camera to a fragment then nudge by an explicit offset:
  `3d render --frame #hole-1 --cam-offset [0,-5,12] --cam-roll 8`; sections `through:#valve --offset 2`.
  Layer 2 resolves INTO Layer 1; `--explain` prints the resolved low-level form so anything
  high-level is inspectable/overridable.

## 22. Video report (auto-generated, per run — distinct from the §14 promo)
- 📋 **`3d report [--video]`** — an AUTO-generated, factual **video report of a build / verification /
  match / ai-loop run**: a captioned timeline of the operations performed, the renders/sections/
  overlays produced, and the metrics/benchmarks (IoU climbing, gate PASS/FAIL, SF, Chamfer). Not the
  polished promo of §14 — this is the "here's what the tool/agent just did, with the numbers" artifact
  for sharing progress (the train project's per-iteration render dumps, generalized & automated).
- 📋 Built from the **same op-DAG run record (§19)** + the **§9 web SSE timeline** + the
  **before/intermediate-debug/after** images (§8/§11). Stitched with **ffmpeg**; optional kinetic
  captions via HyperFrames (§14) when `--video`. Also emit a Markdown/HTML report (no ffmpeg needed)
  as the default; `--video` adds the rendered clip.
- 📋 Deliverable via `tg --file`. Reuses the metrics store (§13.4) so the report's numbers are the
  persisted ones, not recomputed ad hoc.

## 23. Engineering rules, AGENTS.md & `docs/rules/` (ported from the draft workspace)
- 📋 Ship a **comprehensive `AGENTS.md` (+ `CLAUDE.md` symlink)** and a **`docs/rules/`** set, ported
  and Python-adapted from `hyper-canvas-draft` (the user's "write a good Claude file" ask). Portable
  rules to carry over (generic, not stack-specific):
  - **Commit discipline**: atomic commits; pre-commit 3-stage review (dead-code scan → self-review →
    `codex exec review --uncommitted`); never `--no-verify`; push regularly; separate `style:` commits
    for formatter-only churn.
  - **TDD**: failing test first → confirm it fails for the RIGHT reason → minimal code → green →
    refactor; tests exercise PRODUCTION code (no copy-pasted logic, no mock-only); never delete a
    failing test — investigate; changing a test to match code is a red flag (regression vs setup bug).
  - **Zero-warnings**: lint/mypy warnings are errors; no blanket ignores.
  - **Naming**: describe WHAT not HOW; no temporal names (`new`/`legacy`/`improved`); no pattern
    suffixes (`Registry` not `RegistryManager`).
  - **File headers** (Python docstring): purpose, accessed-via, assumptions/invariants, past bugs,
    architecture link. **Comment hygiene**: evergreen, English-only, never silently drop comments.
  - **Shared utilities**: single source of truth (path validation, error shapes, parsing) — never
    inline-reimplement; `SYNC:` comments when duplication is unavoidable.
  - **Dead code**: investigate before delete (`git log -S`, read adding commit, check callsites) →
    DELETE / FIX-RECONNECT / SALVAGE / ESCALATE; never delete on a bare grep miss.
  - **Systematic debugging**: reproduce → compare working vs broken → one hypothesis, smallest change
    → fix root cause, never stack symptom-fixes; timeouts are a smell, fix the cause.
  - **Decision escalation**: verify-it-yourself first (advisor() + code review), escalate only
    product/architectural calls not derivable from code, with Context/Problem/Options/Recommendation.
  - **Pre-commit hooks** (lefthook or equivalent): lint + format + typecheck + conflict-marker check,
    parallel; adapted to Python tools (ruff/black + mypy).
- 📋 **Canonical config dir = `~/.config/3d-cli/`** (the user's stated path for `web.json` + ROADMAP
  §2's `.bootstrapped`). RECONCILE the code: the foundation + web waves currently use `~/.config/3d/`
  in `lib/cli/env.py`, `lib/web/webconfig.py`, `lib/commands/{web,libs,doctor}.py` — rename to
  `~/.config/3d-cli/` (one constant, used everywhere) so docs and code agree.

## 24. Command structure: two levels + umbrella commands
The two-layer idea (§21) applies to the **command tree itself**, not just argument ergonomics.
- 📋 **Low-level tools are SECOND level, not at the root.** `silhouette`, `overlay`, `score`,
  `mesh`, `manifold`, `printability`, `fit-camera`, `preprocess`, etc. are primitives — group them
  under their domain (e.g. `3d match silhouette`, `3d match overlay`, `3d match score`,
  `3d check mesh`, `3d check manifold`). The root stays small and intent-level; primitives live one
  level down. (Thin top-level aliases may remain for the most common, but the canonical home is the
  second level.)
- 📋 **Umbrella commands auto-run everything applicable.** A high-level verb runs ALL relevant
  primitives for the target with one call — e.g. `3d check` = every applicable gate (already),
  `3d analyze` = full analysis with all available tools (mesh + printability + strength + silhouette
  + metrics, whatever the project/object-model declares), `3d match` = the whole pixel-match
  pipeline. The umbrella decides what's applicable from the `3d.yaml`/object model (§5), not a fixed
  list. Two levels: the umbrella for "just do the right thing", the primitives for surgical control.

## 25. Linter system (oxc-inspired) + `3d.yaml` `lint:` section
Inspiration: **oxc** (`github.com/oxc-project/oxc`) — a fast linter + formatter with a clean,
layered rule-config structure. Build an analogous multi-level lint system for 3D/FDM models.
- 📋 **`3d lint`** — runs a configurable set of model checks (geometry, printability, naming,
  object-model hygiene, convention conformance, style/format of the `.scad`/`3d.yaml`). Distinct
  from `check` (the correctness/acceptance gates): `lint` is advisory/style/best-practice with
  levels (`error|warn|off`), like a code linter. A `3d fmt` formatter counterpart (canonicalize
  `.scad`/`3d.yaml`) is in scope, oxc-style.
- 📋 **`3d.yaml` `lint:` section** — declare which checks run and at which level, per project / per
  selector (§5): e.g. `lint: { wall-min: {level: error, mm: 1.2}, unused-anchor: warn,
  overhang-45: {level: warn, select: ".structural"}, naming-id-kebab: error }`. Rules are a
  **registry** (each rule a self-registering plugin, §3) with id, level, selector scope, autofix?.
- 📋 **Detailed multi-level rule design** — rule categories (geometry / printability / object-model /
  style / naming / project-convention), severity levels, per-selector scoping, autofixable vs manual,
  baseline/suppression, and an aggregate `3d lint` report. Work out the rule catalog in detail
  (this is a meaty sub-design — give it its own spec when built).

## 26. Glossary + first-use term explanations
- 📋 **`GLOSSARY.md`** — a single glossary of every domain term (SAM2, Depth-Anything, OpenSCAD,
  BOSL2/NopSCADlib, manifold, IoU, Chamfer, F-score@τ, LPIPS, SSIM, op-DAG, CSG, 3MF, FDM
  anisotropy, etc.) with a one-line definition + a good external link each. Linked from EVERYWHERE
  in the repo (README, ROADMAP, specs) — `[SAM2](GLOSSARY.md#sam2)`.
- 📋 **README explains each term at first mention** with a good link, then defers to the glossary.
  No unexplained acronyms. (Initial `GLOSSARY.md` shipped this session; keep it growing as terms
  appear.)

## 27. Research capture
- ✅ `RESEARCH.md` (this repo) — consolidated index of the literature survey + benchmarks + metrics,
  pointing to the vendored `docs/research/{report.md,report.pdf,sources.md,3d-cli-backlog.md}`.
- ✅ `APPLY-RESEARCH.md` — concrete implementation ideas/plan for turning research into `3d` tools
  (per-area approach/library/algorithm), referenced by the feature sections.
- ✅ `GLOSSARY.md` — domain terms (incl. SAM2, CGAL, …) with links; linked across the repo.
- 📋 Extend all three as new papers/tools/terms are surveyed (§12).

## 28. `3d init` — project scaffolder + project registry
- 📋 **`3d init [path]`** — fully sets up a new `3d` project in one command:
  - **git** — `git init` if not already a repo; a sensible `.gitignore` (libs/, .venv, previews/
    scratch, etc.).
  - **`3d.yaml`** (§5) — project config (name, units, printer, default material, bed) from
    answers/flags.
  - **directory skeleton** — `parts/`, `references/`, `previews/`, `docs/`, `verify/` as applicable.
  - **MCP** — write `.mcp.json` wiring the `openscad` MCP server (and any others).
  - **skills** — install/link the `openscad` (and related) skills into `.claude/skills/`.
  - **git hooks** — pre-commit (lint/format/typecheck + the relevant `3d` gates) per `docs/rules/`.
  - **agents docs** — generate `AGENTS.md` and a `CLAUDE.md` **symlink** → `AGENTS.md`.
  - **register the project** in `~/.config/3d-cli/projects.{toml,json}` so `3d web` (§9) lists it.
- 📋 **Three input modes, one implementation:**
  - **interactive** (TTY) — prompt one question at a time (printer, material, dimensions, which
    pieces to scaffold).
  - **no-TTY / non-interactive** — `--no-input`/`--yes`: everything from flags + defaults (CI, agents).
  - **combined** — flags pre-fill some answers; prompt only for the rest (skip when `--no-input`).
  - Flags mirror every prompt: `--name --printer --material --units --bed --git/--no-git
    --mcp/--no-mcp --skills/--no-skills --hooks/--no-hooks`, etc. **Idempotent** — re-running on an
    existing project tops up missing pieces without clobbering.
- 📋 **`3d projects list|add <path>|remove <path>`** — manage the registry that `3d init` writes and
  `3d web` reads (replaces the single web root, §9).

## 29. Distribution & packaging — standard Python, NOT a manual symlink
The go-to install is the standard Python mechanism, not `ln -s bin/3d`.
- 📋 **Ship as a proper installable package** with a **`3d` console-script entry point**
  (`[project.scripts] 3d = "threed.cli.dispatch:main"`). Install via **`pipx install 3d-cli`** /
  **`uv tool install 3d-cli`** / `pip install 3d-cli` — pip/pipx put `3d` on `PATH` the standard way.
  Dev: `uv pip install -e .` / `pip install -e .` (editable).
- 📋 **Requires a layout restructure** (FOUNDATION task): `lib/` currently sits on `sys.path` with
  top-level modules `cli`/`commands`/`web` (not pip-shippable — would pollute the global namespace).
  Move to a real package `threed/` (`threed/cli`, `threed/commands`, `threed/web`, …) with
  `__init__.py`; `bin/3d` becomes a thin entry (or is dropped for the console script); add
  `[build-system]` (hatchling) and flip `[tool.uv] package = true`. Update `cli.pyrun` script
  resolution (it locates `lib/*.py` tools) to package resources (`importlib.resources`).
- 📋 Publish to PyPI (or a private index); `3d --version` reads package metadata. Keep `examples/`,
  `docs/`, OpenSCAD libs handling working after the move. Verify the test gate stays green.

## 30. Structured logging with levels
- 📋 **One structured-logging path** for the whole tool: events carry **levels**
  (`debug|info|warn|error`) + structured fields (command, op/op-DAG node, target, metric, duration),
  emitted as human text by default and **JSON** on request. Global controls: `-v/-vv` (raise),
  `-q` (quiet), `--log-level`, `--log-format text|json`.
- 📋 **Shared by all frontends/sinks** — the same structured event stream feeds the terminal, the
  **web SSE** log view (§9), the **op-DAG run record** (§19), and the **report** (§22). The web
  timeline and `3d report` are just renderings of this stream; levels gate verbosity per sink.
- 📋 Integrates with the rest: `lib/errors.py` (§1) raises the error-level events; metrics (§13.4)
  can be derived from the stream; no ad-hoc `print()` for diagnostics — route through the logger.

## 31. `3d print` — drive, monitor & recover real prints (printer integrations)
`3d` covers the WHOLE lifecycle: idea → spec → **procurement/materials (§32)** → model → verify →
pack/slice → **print → monitor → recover** → report. `3d print` is the execution end.
- 📋 **Printer integrations** — pluggable backends (registry, §3): **Klipper/Moonraker** (primary;
  Mainsail/Fluidd ecosystem), **OctoPrint** (REST), **Bambu** (LAN/MQTT), **PrusaLink/PrusaConnect**.
  Each backend implements a common interface (discover, status, upload, start/pause/resume/cancel,
  temps, camera).
- 📋 **Auto-discovery** — find printers on the LAN (mDNS/zeroconf, Moonraker/OctoPrint/Bambu probes);
  cache them in `~/.config/3d-cli/` (alongside the printers registry, §2a). `3d print --printer <name>`
  or auto-pick the single discovered one.
- 📋 **Send & run a job** — `3d print <model|3mf|gcode>`: pack/slice if needed (§4/§5), upload,
  start; `3d print status|pause|resume|cancel`. Material check against the printers/materials
  registry + inventory (§32) before starting.
- 📋 **Monitor** — live status (temps, progress, current layer, ETA, speed), **camera stream**, and a
  structured-log feed (§30). Surfaced in the terminal and in `3d web`.
- 📋 **Recover / continue on failure** — detect failures (thermal runaway, power loss, filament
  runout, spaghetti via the camera + a detector), pause safely, and **resume from a layer**
  (Klipper power-loss/`SDCARD_RESET_FILE`-style recovery). Never silently abandon a multi-hour print.
- 📋 **Multi-printer / queue** — a job queue across discovered printers; assign by capability
  (bed size, material) from the registry.

## 32. Material procurement & inventory management
Closes the "from idea to print" loop on the materials side; ties into the materials registry (§2a).
- 📋 **Inventory** — track filament spools on hand (material, color, brand, remaining grams, location),
  decrement by the slicer's estimated usage per job. `3d materials inventory list|add|use`.
- 📋 **Procurement** — when stock is low or a project needs a material not on hand, surface what to
  buy: required spec (material/diameter/color/amount) + **concrete sourcing links** (per the project
  rules: real shop/part-number/URL + price, no placeholders). Reorder list per project/BOM.
- 📋 **Drives planning** — `3d pack`/`3d print` check inventory before committing; a project's
  required-material total (from `3d.yaml` copies × per-part grams) feeds the procurement list.

---

## Execution plan & handoff (for the NEXT session)

This session was originally the **lego-loco train** project; it grew the `3d` CLI as a side effect.
The CLI work now has its own repo and this ROADMAP as the single source. Pick up from here.

**Agreed build order (user-approved: "2+3, max reasonable autonomy"):**
1. **README/docs de-coupling FIRST** (§16) — runs before feature work; the foundation wave rewrote
   the README with the OLD lego-loco framing (throwaway). Reframe intro + Requirements (plain list +
   auto-install) + sweep core for subject leakage (§15). Also land §23 (AGENTS.md + docs/rules).
2. **Core wave (B1, mostly serial — it is the dependency for everything else):** headless `lib` core
   (§20), the **object model + selectors/stylesheet** (§5), the **operation-DAG executor + history**
   (§19), capability registries. Reconcile the config dir (§23).
3. **Leaf wave (B2, parallel worktrees over the stable core):** materials/printers (§2a),
   `3d.yaml`+`pack` (§5), `strength`/`kinematics`/`animate` (§6), `3d om` (§18), sections + camera
   presets/auto-frame (§3), photoreal Blender (§3), `3d ai` (§13), `slice` changes (§4),
   ollama+hardware-check (§10), debug-viz+axis-math (§7/§8), `3d report` (§22).
4. **Integration + demo (final):** merge everything, end-to-end tests, README screenshots (§11),
   then the §14 showcase demo + §22 video report.

**State at handoff (2026-06-05) — all consolidated on `main`, tree clean, pushed:**
- ✅ Foundation: python registry CLI + `lib/errors.py`, **72 tests**, mypy clean.
- ✅ Web dashboard integrated into the registry (`lib/commands/web.py` + `lib/web/`); `3d web` boots.
- ✅ ROADMAP §0–§27 + `docs/specs/2026-06-05-3d-cli-architecture.md` + `docs/rules/` (dev/testing/
   code-style/decision-requests).
- ✅ Research vendored: `docs/research/{report.md,report.pdf,sources.md,3d-cli-backlog.md}` +
   `RESEARCH.md` / `APPLY-RESEARCH.md` / `GLOSSARY.md`.
- ✅ All temp doc branches merged + deleted; no open branches, no worktrees.
- ⚠️ **First real code task next session — config dir**: code uses `~/.config/3d/` (foundation + web,
   incl. the web agent's choice); rename to `~/.config/3d-cli/` per §23 (one constant in
   `lib/cli/env.py`, `lib/web/webconfig.py`, `lib/commands/{web,libs,doctor}.py`) so docs+code agree.
- NOTE: nothing is in-flight; this session ended cleanly. Start from the build order above (1→4).
