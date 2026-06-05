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
- 🔨 **`3d render <file.scad>`** — `--view front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right` (camera from model bbox), `--multi [outdir]` (all standard angles), `--section [--plane YZ|XZ|XY] [--color] [--keep pos|neg] [--module 'm();']` (proper 6-param vector cross-section, never 7-param gimbal), `--cam` (manual override), `--ortho`, `-D k=v`, `--debug`.
- 📋 **Photorealistic render** — `3d render --photo` (or `3d photo`) via **Blender** (Cycles/EEVEE):
  export STL/3MF → Blender headless (`bpy`) with materials/colors from the materials registry,
  proper lighting/HDRI, soft shadows. **Blender is installed ON DEMAND** (only when the user
  requests a photoreal render), NOT auto-bootstrapped. README must show **OpenSCAD render vs
  Blender photoreal** side by side so the difference is clear.
- 📋 **`3d check <file>`** — runs ALL applicable gates by DEFAULT; `--mesh --printability --collision --manifold --silhouette` select a subset; `--skip X` excludes. Per-gate breakdown + PASS/FAIL. (= the acceptance master gate.)
- ✅ `export` (mesh-validated, nonzero on bad geometry), `validate`, `params`.
- ✅ `mesh`, `printability`, `collision` (static / `--frame` / `--viz`), `acceptance`, `silhouette`, `overlay`, `score`, `match` (forced-monotonic loop + changelog, `--dry-run`), `fit-camera`, `preprocess`.
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
