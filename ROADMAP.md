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
([github.com/alex-mextner/3d-cli](https://github.com/alex-mextner/3d-cli)), installed as a standard Python package exposing the `3d`
console-script (pipx / uv tool / pip — see §29), not a manual symlink.

## 0a. Design influences & philosophy (the meta-thinking)
The whole tool is shaped by a few deliberate analogies — keep them visible, they explain WHY
the surface looks the way it does:
- **jq** → composable, pipeable filters over a structured document. `3d om` (§18) is jq for the
  3D object model; everything streams through stdin/stdout so shell pipes compose.
- **ffmpeg** → a complete, expressive filter-GRAPH (a DAG) with total low-level power — but its UX
  is notoriously hostile. We keep the power (Layer 1, §21) and add a friendly layer on top (Layer 2)
  that resolves INTO it. ffmpeg's filtergraph is also why the pipeline is a DAG (§19).
- **vecli / vector-engine** ([github.com/hyperide/hyper-saas](https://github.com/hyperide/hyper-saas), `packages/vector-engine`) → a
  **headless compute-graph core** (`lib`) with thin frontends (`cli`/`web`/`gui`) over it (§20), and
  an **operation DAG** where editing a past node rolls forward to dependents (§19). We adopt the
  compute-DAG + headless-core split and fix its linear-history gap with a real history DAG.
- **CSS + HTML DOM** → the object model is a tree (DOM) addressed by **id + class selectors** with a
  **cascading stylesheet** of rules (§5) — but with no HTML/CSS, just the addressing/cascade idea.
- **Houdini / Blender geometry nodes** → non-destructive, re-computable node graphs; the same model
  behind §19's roll-forward.
These are not features; they are the lens. New surface should be justifiable by one of them.

### Why these tools (not others) — the deliberate choices
Tool selection is itself part of the philosophy; each pick has a reason, and a reason is the bar
for swapping it. The throughline: **prefer code/text artifacts over GUIs and binary blobs**, because
code is what AI agents, version control, and shell pipes all operate on natively.
- **OpenSCAD as the modeling base — because a model is CODE, not a GUI document.** Code is trivially
  **stored** (a `.scad` is text in git), **diffed/compared** (review a change line-by-line, bisect a
  regression), **edited by AI** (the LLM writes/patches the source directly — no clicking through a
  GUI), and lets you perform **arbitrarily complex parametric operations** and transform the model
  any way you like — none of which a GUI-first CAD (Fusion/SolidWorks/Blender-modeling) gives you.
  Every other tool here is chosen to keep that text-first, AI-and-git-native property. (Its weakness
  — a thin stock language — is why §33 extends it rather than abandoning it.)
- **Blender only for the photoreal LEAF (render/animate shaders), never as the modeling source** —
  it's a GUI binary-document tool; we touch it headless (`bpy`) at the very end for Cycles/EEVEE
  output, so the source of truth stays code. Installed on demand (§3), not in the base.
- **BOSL2 / NopSCADlib** — mature, open OpenSCAD libraries: they extend the code-base with fillets/
  threads/gears/hardware *as more OpenSCAD code*, not a separate binary kernel. Same text-first
  property; reuse beats re-deriving (§33).
- **trimesh / manifold3d / open3d** — scriptable Python mesh stack for the gates (watertight/
  manifold/self-intersection): a library you call in-process, not a GUI to drive. Degrades cleanly
  when a wheel is missing (§ FALLBACK modes) instead of hard-failing.
- **ImageMagick + OpenCV** — composable, headless image ops (silhouette/IoU/overlay, PCA/contours/
  moments §7) that pipe in the shell like every other stage; no GUI image editor in the loop.
- **ffmpeg** — the code-first video/animation backend (§6/§22): a filter-GRAPH you script, matching
  the op-DAG ethos (§19/§21), not a timeline GUI.
- **uv / pipx for distribution** (§29), **FastAPI+SSE for web** (§9) — standard, scriptable, no
  bespoke installer or heavyweight framework; the web is a *thin frontend over the same core* (§20),
  never a place logic hides.

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
  - **Implementation (from research):** export 3MF → [Blender](GLOSSARY.md#blender) `bpy` headless,
    pull materials/colors/finish from the materials registry (§2a), add HDRI + soft shadows. **Why:**
    Blender stays a render LEAF, never the source of truth (§0a). **Example:**
    `3d render boiler.scad --photo --out hero.png`.
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
  - **Implementation (from research):** the colored cut is a per-part `color()` applied OUTSIDE the
    `difference()` so the cut face keeps each part's color; presets resolve to a plane via the object
    model (§5). The auto-camera reuses the [fit-camera](GLOSSARY.md#fit-camera) optimizer with a
    cut-face-area / occlusion objective instead of silhouette-[IoU](GLOSSARY.md#iou). **Why:** a
    monochrome cut hides which part you're looking at; auto-framing removes the manual `--cam` fiddle.
    **Example:** `3d render asm.scad --section through:#valve` (plane through a named feature, camera
    auto-oriented to the cut).
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
- 📋 **Implementation (from research):** map `material + printer` (by name, from the registries §2a)
  → [slicer](GLOSSARY.md#slicer) machine/process/filament profiles; always dry-run as a gate;
  `--list-profiles` discovers installed-slicer presets + project ones; profile errors are
  self-explaining (what each file is, where to export it). Default to **Bambu A1 + PLA/PETG**.
  **Why:** the registry is the single vocabulary, so a user names a material, not a json path.
  **Example:** `3d slice cab.3mf --printer bambu-a1 --dry-run`.

## 5. 3MF builder + project config (`3d.yaml`)
- 📋 **`3d pack <3d.yaml>`** — emit a print-ready **3MF**: per-part orientation solved
  for **min support + max strength**, copy layout, colors/materials, optional
  **splitting into parts** for glue / printed-connector joints, per-object slicer settings.
  - **Orientation objective (from research, P4.2):** the per-part solver prefers orienting each
    part so its **principal tensile load lies in the XY (flat) plane** and the weakest across-layer
    direction carries the least load — consuming `3d strength`'s anisotropy model (§6). **Why:** XZ
    (on-edge) is consistently the weakest direction in both PETG and PLA (PMC9230522), so orientation
    is a strength *output*, not an afterthought; minimizing max across-layer tensile stress is the
    measurable goal. **Example:** `3d pack train.3d.yaml --orient strength` (lay each part down to
    keep its load in-plane). Use PCA of the mesh (§7) for the candidate flat/strong lay-downs.
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
  - **Implementation (from research):** parse `// @id/@class/@anchor/@section/@view/@color`
    comments from `.scad` via regex over source (OpenSCAD ignores comments → still renders); read
    `.3mf` metadata; for `.stl` use the sidecar `<f>.3d.yaml`. Build an in-memory tree of nodes
    (id, classes=tags, params, bbox). The selector engine is a tiny CSS-like matcher (`#id`,
    `.class`, `.a.b`, descendant); the stylesheet is ordered `selector → props` rules resolved per
    node with CSS specificity (class < id < inline) into an effective style map. **Why:** reusing
    the CSS/DOM mental model (§0a) means one addressing mechanism everywhere instead of per-command
    ad-hoc part lists.
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
  - **Anisotropic knockdown (from research, P4.1):** multiply the allowable stress for any component
    **normal to the layer plane** by a citable, material-specific knockdown — **PETG ~0.7–0.75×**,
    **PLA ~0.45×** of in-plane — and make the allowable a function of slicing layer height; cite the
    sources in the gate notes. **Why:** orientation is the single biggest lever on
    [FDM strength](GLOSSARY.md#fdm-anisotropy), and the magnitude is a real measured number — PLA
    across-layer can be under half the in-plane strength. **Sources:** Ahn et al. 2002
    (https://www.emerald.com/insight/content/doi/10.1108/13552540210441166/full/html); PMC9230522
    (https://pmc.ncbi.nlm.nih.gov/articles/PMC9230522/ — PET-G XY 19.27 / XZ 14.30 MPa, +34.8%; PLA
    XY 21.70 / XZ 9.55 MPa, +127%). **Example:** `3d strength bracket.scad` (reports predicted-vs-
    allowable stress with SF per zone, anisotropy ratio applied). Pulls factors from the
    [materials registry](GLOSSARY.md#fdm-anisotropy) (§2a); feeds the `3d pack` orientation solver (§5).
- 📋 **`3d fea`** (optional) — CalculiX (via FreeCAD FEM) / Elmer for nontrivial cases; consumes the
  same anisotropic material card (per-direction allowables) as `3d strength`.
- 📋 **`3d kinematics <3d.yaml>`** — model + verify motion (per-frame, axes/guides, reach/sweep).
- 📋 **`3d animate <3d.yaml>`** — generate animation + **per-frame verification**
  (collisions, sync with the motion model). Requires **ffmpeg** (check/install).
  - **Two render backends, same animation spec:** a **fast OpenSCAD preview** (throwntogether,
    no CGAL — for scrubbing/iterating, the default) and a **photoreal Blender (Cycles/EEVEE)
    shader render** (materials/HDRI/soft shadows from the materials registry, §2a) for the final
    clip. **Why:** you iterate on motion + parameters at preview speed, then render the keeper once
    with shaders — never pay Blender cost while still tuning. **Example:**
    `3d animate robot.3d.yaml --backend openscad --out preview.mp4` (fast),
    `3d animate robot.3d.yaml --backend blender --out hero.mp4` (photoreal).
  - **Drives the §9 web animation panel** — the same core produces the frames the web scrubs and
    records; the CLI is for batch/headless, the web for interactive exploration.

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
  - **Implementation (from research):** compute the principal axes via [OpenCV](GLOSSARY.md#opencv)
    PCA of the silhouette, plus image moments + contours, the bbox and centroid; match the model's
    axes to the reference's to seed the pose. Shared with the `axis` AI tool's RAG pre-flight (§13.2).
- 📋 **Optional `opencode` integration** (`--opencode`) for iterative axis tuning / checks.
  opencode runs out-of-box with free models (no key needed) — use as an optional assist.
- 📋 **Aspirational fit backends (from research, P5 — reach-for-when-needed):**
  - **Differentiable-rendering gradient fit (P5.1)** — backprop a silhouette/soft-IoU loss into
    parameters via [Mitsuba 3](GLOSSARY.md#mitsuba) (https://www.mitsuba-renderer.org/) or
    [nvdiffrast](GLOSSARY.md#nvdiffrast) (https://github.com/NVlabs/nvdiffrast), only after
    reimplementing the geometry differentiably. **Why:** faster convergence for *hundreds* of
    parameters — but unnecessary for the few-dozen-param case where the gradient-free
    forced-monotonic loop converges in a coffee-break, and it abandons OpenSCAD as the generator. Low
    priority. **Example:** `3d match part.scad ref.jpg --backend mitsuba`.
  - **Multi-view photogrammetry reference (P5.2)** — if the real subject is photographed from many
    angles, run SfM+MVS via [COLMAP](GLOSSARY.md#colmap) (https://colmap.github.io/) / Meshroom
    (https://alicevision.org/) for a metric ground-truth shell, then add a second-view IoU term to
    the match loss. **Why:** a single found photo under-constrains 3D; multiple real photos pin it —
    situational, only when such photos exist. **Example:**
    `3d fit-camera part.scad refs/*.jpg --multi-view`.

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
- 📋 **Interactive animation studio** — the web's headline creative loop, all over the same
  `lib` core (§20) and the §6 `3d animate` backends:
  - **Scrub model parameters live** (the §9 Figma-like scrubbers: drag; **Shift = fine, Alt =
    coarse**) and watch the **animation re-render in place** — change a constant, see the motion
    update without leaving the panel.
  - **Scrub time** along a timeline (scrub bar + play/pause/loop, per-frame stepping) to inspect
    any moment of the motion; the per-frame verification overlays (§6) ride along.
  - **Two view modes:** **fast OpenSCAD preview** for real-time scrubbing/iterating and a
    **photoreal Blender shader render** for the polished look — toggle without changing the spec.
  - **Record to video** — capture the current parameter/time exploration straight to an MP4
    (ffmpeg, §10), in either view mode, for sharing. **Why:** the value of code-based modeling is
    that *everything is a parameter* — the studio makes exploring that parameter+time space direct
    and visual, then bottles the good run as a clip.
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
- 📋 **`APPLY-RESEARCH.md` is a POST-survey deliverable, authored AFTER this §12 literature survey
  lands** — not a now-doc, and it **does not exist yet** (the old per-area draft was folded into the
  feature sections below and deleted). Once the survey completes, **create** `APPLY-RESEARCH.md` as
  the paper-by-paper application summary: for each surveyed paper/algorithm, how it gets applied
  (which `3d` command, library, metric). Until then ROADMAP is canonical and holds the content (see
  §17 for the per-section map).

## 13. `3d ai <tool>` — AI-assisted tool group (operators + RAG + loop + benchmarks)
A unified AI layer over the analytical commands. Pattern: **`3d ai <tool> <operator> [args]`**
(e.g. `3d ai axis do|review|loop`). Backend-agnostic via the SAME adapters as `3d web`
(claude / codex / opencode — opencode runs out-of-box with free models). `--backend` selects;
default claude.

### 13.1 The shared rule — ALWAYS run the deterministic tools FIRST, then the AI
Every operator (`do`/`review`/`loop`) follows the same two-step shape; this is the core idea,
not a per-operator detail:
1. **Auto-run the relevant deterministic `3d` tools first.** Before the model is ever called, the
   tool runs the set of `3d` commands that are *always* needed to ground a task of this kind — for
   the **part/project type at hand** (decided from `3d.yaml` tags + object model, §5, e.g. a
   `.structural` engineering part pulls `check`+`strength`+`printability`; a `match` task pulls
   `fit-camera`+`score`+multi-view renders). Their **numbers + rendered PNGs + full context** become
   the prompt. This is the RAG: the model reasons over measured ground truth, never guesses.
2. **Then call the AI** with that evidence in context.

The per-tool/per-type list of "what to auto-run first" is the tool's **RAG pre-flight set** (see
§13.2 — earlier called a "manifest"; it is just that declared list of pre-run `3d` commands).

The three operators differ only in what they do *with* the AI after step 1:
- 📋 **`do`** — call the AI **once** to perform the task and **apply** the result (mutating: writes
  the SCAD edit / `camera.json` / etc.). Then the deterministic gates re-run to confirm the change.
- 📋 **`review`** — call the AI **read-only**, **never mutates**. The model returns a DETAILED
  critique grounded in the step-1 evidence — concrete numbers, specific edits in mm, and a list of
  **recommended `3d` commands** to run next. The "detailed flavour": full context, figures, no edit.
- 📋 **`loop`** — **repeat `do` → re-run the deterministic tools → `review` in a cycle** until the
  acceptance criteria converge. The stop condition is a **NUMERIC benchmark threshold** (the tool's
  §13.4 metric: IoU ≥ τ, Chamfer ≤ ε, all gates PASS), not vibes — or convergence / a round-cap.
  Each iteration re-grounds on freshly measured tools (step 1) so the model always sees the current
  state.
  - **Forced-monotonic acceptance (from research, P0.2):** the loop applies ONE critic-proposed edit,
    re-scores, and **accepts only on strict metric improvement AND all hard gates PASS**; otherwise it
    reverts + resamples (cap ~10/round); an invalid render scores worst (zero-reward anchor); a
    **changelog** of attempts is fed back so a failed move is never retried. A coarse-to-fine phase
    controller freezes parameter subsets. **Why:** this single rule turns "an LLM fiddling with
    numbers" into a convergent optimiser — the FlipFlop effect (~46% flips, ~17% accuracy drop when a
    self-judging model is challenged) means unconstrained self-judged loops oscillate without bound.
    This is the [forced-monotonic loop](GLOSSARY.md#forced-monotonic-loop). **Sources:** ReLook
    (https://arxiv.org/abs/2510.11498); FlipFlop (https://arxiv.org/abs/2311.08596). **Metric:**
    monotone non-decreasing IoU trajectory, rounds-to-converge, reject-rate, final IoU ≥ target.
  - Optionally driven by **quorex** ([github.com/alex-mextner/quorex](https://github.com/alex-mextner/quorex),
    [ralphex](https://github.com/umputun/ralphex)-based; the `quorex` binary on `PATH`: fresh agent session per task,
    5-agent→codex→2-agent review pipeline, worktree isolation, web dashboard, notifications) for the
    heavyweight autonomous variant; `3d ai <tool> loop` emits a plan whose *validation commands* are
    this tool's metric targets and drives the cycle until met.

### 13.2 RAG pre-flight set — the deterministic `3d` commands each tool auto-runs before the AI
Each tool **declares** its pre-flight set: the deterministic `3d` runs whose outputs (numbers +
rendered PNGs) are embedded in the prompt, plus a "recommended tools" block (relevant `3d`
subcommands with one-line usage). The set is filtered by the part/project type (§13.1 step 1):
- `axis` → [OpenCV](GLOSSARY.md#opencv) PCA principal axes, contours, image moments, bbox, centroid + annotated overlay.
- `match`/`fit-camera` → silhouette **IoU**, overlay-diff (AE / blend / canny), current
  `camera.json` + before/after PNGs.
  - **Reference silhouette pre-pass (from research, P3.1):** one prompt-click on the reference photo
    → a clean binary subject mask via [SAM2](GLOSSARY.md#sam2) (+ optional per-feature sub-masks),
    normalised to the render frame; falls back to [Depth-Anything](GLOSSARY.md#depth-anything) +
    GrabCut when SAM2 is unavailable. **Why:** a clean reference silhouette is the foundation of the
    whole metric; hand-thresholding a busy photo is fragile, and per-feature masks let the critic
    target per-feature IoU. **Source:** SAM 2 (https://arxiv.org/abs/2408.00714).
- `critique` (model↔reference) → multi-view renders + the reference + current score metrics.
  - **Depth + normal critic channels (from research, P3.2):** run
    [Depth-Anything V2](GLOSSARY.md#depth-anything) (or [Marigold](GLOSSARY.md#marigold) for sharper
    edges) for a relative depth map, and [Wonder3D](GLOSSARY.md#wonder3d) once for a side-view normal
    map; hand depth + normal + silhouette to the critic and **discard any generated mesh**. **Why:** a
    single side silhouette under-determines depth; depth/normal priors give the critic a structured 3D
    cue (a front-to-back mass-order consistency check) without ever making an unprintable generated
    mesh the deliverable. **Sources:** Depth-Anything-V2
    (https://github.com/DepthAnything/Depth-Anything-V2), Marigold (https://arxiv.org/abs/2312.02145),
    Wonder3D (https://arxiv.org/abs/2310.15008).
- `strength` → computed stress vs allowable per load-case + SF.
- `printability` → overhang / wall / clearance report.

Backends share the same log-adapter interface as `3d web` (Claude / Codex / [ollama](GLOSSARY.md#ollama)
/ opencode — opencode free out-of-box).

### 13.3 Initial tool set (each gets `do/review/loop`)
- 📋 `axis`, `match` (camera/silhouette), `critique` (model↔reference), `strength`, `printability`,
  `design` (generate/modify SCAD from a reference). Adding a tool = declare its RAG pre-flight set
  (§13.2) + benchmark/metric (§13.4); the three operators come for free.
- 📋 **`design` — skeleton bootstrap (from research, P2.2; highest-value NET-NEW vein).**
  `design do` writes the initial parametric `.scad` skeleton from a reference (LLM-authored today; a
  CSGNet/ShapeAssembly-style parser later), which `design loop` then tunes via the forced-monotonic
  loop (§13.1). Borrow program-synthesis principles now without training a network: CSGNet's
  **render-reward** acceptance and ShapeAssembly's **structure + free-variables** factorisation.
  **Why:** the match loop tunes a generator that must first exist; synthesis bootstraps it, and the
  cheap high-value part (factorisation + reward shape) is adoptable immediately. **Sources:** CSGNet
  (https://arxiv.org/abs/1712.08290), ShapeAssembly (https://arxiv.org/abs/2009.08026), DeepCAD
  (https://arxiv.org/abs/2105.09492). **Metric:** render-success of the skeleton; initial IoU before
  tuning; rounds-to-target after. **Example:** `3d ai design do boiler.scad --ref photo.jpg`.
- 📋 **`design` — attachment-graph authoring convention (from research, P2.1).** Standardise authored
  models as a [BOSL2](https://github.com/BelfrySCAD/BOSL2) **attachment graph** of parameterised
  proxies (e.g. boiler/smokebox/cab/domes/funnel), with landmarks expressed as *fractions of a parent
  dimension* (`funnel_frac`) over a shared `constants.scad`. **Why:** ShapeAssembly's result shows
  attachment graphs yield more plausible, edit-stable shapes than absolute transforms, and they keep
  the match-loop parameter space low-dimensional, decoupled, and unambiguous for monotonic acceptance
  — a parent-dimension change must not break child placement (verifiable by re-render IoU delta).
  **Source:** ShapeAssembly (https://arxiv.org/abs/2009.08026). This is the authoring style §33's
  OpenSCAD-extension layer should make a one-liner. **Example:** `3d ai design review cab.scad`
  (flags absolute transforms that should be parent-relative).

### 13.4 Benchmarks (`3d ai`) + metrics (all tools) — always computed, always saved
- 📋 **Standard, commonly-accepted benchmarks** (not bespoke-only):
  - **geometry**: [Chamfer](GLOSSARY.md#chamfer) distance (L1/L2), [**F-score@τ**](GLOSSARY.md#f-score),
    [Hausdorff](GLOSSARY.md#hausdorff), [normal consistency](GLOSSARY.md#normal-consistency),
    volumetric [**IoU**](GLOSSARY.md#iou).
    - **Conventions + libraries (from research, P1.1):** **F-score@τ is the PRIMARY** geometry metric
      (τ ~1% of bbox diagonal); Chamfer/IoU alone mislead (Tatarchenko CVPR 2019,
      https://arxiv.org/abs/1905.03678). Each run **records its convention in the store**: Chamfer
      `k` (L1/L2, mean, bidirectional), F-score `τ`, Hausdorff directed-vs-symmetric. Library mapping:
      `open3d`/`trimesh` for distances + vol-IoU, `scipy` KD-tree for nearest-neighbor queries
      (Chamfer/F-score), `pymeshlab` `get_hausdorff_distance`, numpy for F-score@τ +
      normal-consistency. **Why:** a longitudinal store is worthless if the convention silently
      drifts between runs.
  - **render-vs-reference**: silhouette [**IoU**](GLOSSARY.md#iou) (primary), [**LPIPS**](GLOSSARY.md#lpips),
    [**SSIM**](GLOSSARY.md#ssim), [**PSNR**](GLOSSARY.md#psnr), [**CLIP-similarity**](GLOSSARY.md#clip-sim).
    - **Senses + footgun (from research, P1.2):** record each metric's sense — IoU (0..1, 1 best),
      LPIPS (≥0, 0 best), SSIM (−1..1, 1 best) vs **DSSIM (0 best — the silent-sign footgun, guard
      it)**, PSNR (dB), CLIPScore (0..100). Silhouette IoU is the optimisation target but is blind to
      "does it look like the subject"; LPIPS + CLIP-sim add perceptual/semantic channels IoU misses.
      Libraries: `pip install lpips` (Zhang CVPR 2018, https://arxiv.org/abs/1801.03924), CLIPScore
      (Hessel 2021, https://arxiv.org/abs/2104.08718), ImageMagick for IoU/AE/SSIM, SSIM def Wang 2004
      (https://www.cns.nyu.edu/pub/eero/wang03-reprint.pdf). The lowest-effort, highest-leverage
      primitive is a deterministic `render → binary mask → {IoU, AE} + overlay` (red=ref, cyan=render)
      for the critic — `IoU = |S∩R|/|S∪R|`, AE = mismatched-pixel count with `-fuzz`, same `WxH!` +
      crop on both (P0.1, ImageMagick `compare`).
  - **camera/pose**: reprojection error, rotation/translation error.
    - **Pose freeze (from research, P0.3):** fit `(ortho-scale, in-plane translation, small roll)` —
      3–4 DoF for a side elevation — by maximising silhouette IoU (or minimising reprojection error
      on marked landmarks), coarse grid + local hill-climb, then **hold the pose fixed** through the
      shape match. **Why:** a drifting pose makes the monotonic-acceptance score meaningless ("never
      improves for no reason" — the top failure signature); pose fit is the precondition for trusting
      the metric. `3d fit-camera` (✅) already produces the locked pose; add the reprojection-error
      mode and document the freeze rule.
  - **OpenSCAD-generation suite (from research, P1.3)**: adopt the public *image→OpenSCAD,
    iterate-via-CLI-render* task format (ref: [ModelRift OpenSCAD-LLM benchmark](https://modelrift.com/blog/openscad-llm-benchmark))
    BUT replace its purely **subjective 0–5
    score** with the automated metrics above (render-success rate + IoU + Chamfer against a target
    mesh + LPIPS), so results are reproducible. Keep a subjective score as one column, not the only
    one. **Why:** ModelRift has the right task but a non-reproducible metric; CADBench-style
    benchmarks have automated metrics but target CadQuery/Blender, not OpenSCAD — this fills the exact
    gap. Use **BlenderLLM/CADBench** (https://arxiv.org/abs/2412.14203) as the methodology template.
    Persist every run to the longitudinal store; `3d ai bench --compare` shows deltas-vs-history.
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
  writing **HTML/CSS/JS**; CLI install). Ref: [hyperframes.heygen.com](https://hyperframes.heygen.com/). The demo is itself a
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

## 17. Research-driven backlog (priority tiers + critical path)
- ✅ **The 14 actionable items are now folded into the feature sections above; ROADMAP is canonical.**
  Each item's full `{what, why, paper/tool, integration point, expected metric}` rationale lives in
  the underlying research report ([`docs/research/report.md`](docs/research/report.md)); the
  priority/sequencing metadata it carried lives here because it exists nowhere else. Highest-value
  NET-NEW vein: **program synthesis for CAD** (CSGNet / ShapeAssembly / DeepCAD) → `3d ai design`
  (§13.3).
- 📋 **Priority tiers** (leverage-per-effort + dependency order; P0 unblocks the rest):
  - **P0 — measurement foundation:** P0.1 silhouette IoU+AE+overlay primitive → §13.4; P0.2
    forced-monotonic loop + changelog → §13.1; P0.3 camera-pose fit + freeze → §13.4 / §7.
  - **P1 — standard metric battery:** P1.1 geometry metrics, pinned conventions → §13.4; P1.2 image
    metrics, correct senses → §13.4; P1.3 OpenSCAD-LLM bench, auto-scored → §13.4.
  - **P2 — authoring & generation:** P2.1 attachment-graph authoring (BOSL2) → §13.3 / §33; P2.2
    `.scad` skeleton bootstrap → §13.3.
  - **P3 — perception scaffolding:** P3.1 SAM 2 reference silhouette → §13.2 (`match` pre-pass); P3.2
    depth (Depth-Anything/Marigold) + Wonder3D normals → §13.2 (`critique`).
  - **P4 — structural gates:** P4.1 anisotropic strength knockdown → §6; P4.2 strength-driven
    orientation → §5 (`pack`).
  - **P5 — situational/aspirational:** P5.1 differentiable-render fit (Mitsuba/nvdiffrast) → §7; P5.2
    multi-view photogrammetry (COLMAP) → §7.
- 📋 **Critical path:** P0.1 → P0.3 → P0.2 unblocks the entire AI loop; P1.1/P1.2 unblock `3d ai
  bench` (P1.3) and the longitudinal metrics store. P2–P4 are parallel improvements; P5 is
  reach-for-when-needed.

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
([github.com/hyperide/hyper-saas](https://github.com/hyperide/hyper-saas), `packages/vector-engine`), fixing its linear-history gap.
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
- 📋 **Implementation (from research):** port the [vector-engine](GLOSSARY.md#vector-engine) executor
  pattern — nodes `{type, inputs, outputs, params, execute}`, topological order, per-node cache keyed
  on `(type, params, hash(inputs))`, dirty-set invalidation → recompute only descendants. History is
  a DAG of edits (not a linear tape) with branch + base-snapshot + op-log persistence. **Why:**
  pure-Python and unit-testable without subprocess, and it fixes vector-engine's linear-history gap.

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
- 📋 **Implementation (from research):** refactor commands so logic lives in importable `lib`
  functions; the registry commands are thin argv→core adapters. The same core serves cli / web /
  (future gui). **Why:** one tested code path, no logic hiding in a frontend.

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
- 📋 **Implementation (from research):** `3d report` stitches the op-DAG run record (§19) + the web
  SSE timeline (§9) + the before/debug/after images with [ffmpeg](GLOSSARY.md#ffmpeg); `3d demo`
  builds the polished §14 promo via [HyperFrames](GLOSSARY.md#hyperframes). **Why:** reuse one
  recorded run for both the factual per-run report and the curated demo, no re-capture.

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
  §2's `.bootstrapped`). RECONCILE the code: the foundation + web waves currently use `~/.config/3d-cli/`
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
Inspiration: **oxc** ([github.com/oxc-project/oxc](https://github.com/oxc-project/oxc)) — a fast linter + formatter with a clean,
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
- 📋 **Implementation (from research):** [oxc](GLOSSARY.md#oxc)-style rule registry — each rule a
  self-registering plugin with `(id, level, selector, autofix)` — driven by the `3d.yaml` `lint:`
  section, with `3d lint` reporting and a `3d fmt` formatter. **Start with a small
  geometry/printability/naming rule set and grow the catalog** rather than designing it all up front.
  **Why:** mirrors how oxc layers a clean rule-config structure, so each rule ships independently
  without editing a central list (§15).

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
  pointing to the vendored `docs/research/{report.md,report.pdf,sources.md}`.
- ✅ **The prioritized P0–P5 backlog was folded into the feature sections (map in §17) — now
  canonical — and its standalone file deleted.** The full per-item rationale lives in the research
  report ([`docs/research/report.md`](docs/research/report.md)).
- 📋 **`APPLY-RESEARCH.md` is a POST-§12-survey deliverable that does not exist yet.** Its old
  per-area application ideas/algorithms/libraries were distributed into the matching feature sections
  (e.g. strength → §6, axis/PCA → §7, ai design/critique → §13, metric formulas → §13.4) and the file
  was deleted. It is to be **created** *after* the §12 literature survey completes, summarizing how
  each newly surveyed paper/algorithm gets applied. Until then ROADMAP holds the content.
- ✅ `GLOSSARY.md` — domain terms (incl. SAM2, CGAL, …) with links; linked across the repo.
- 📋 Extend `RESEARCH.md` / `GLOSSARY.md` as new papers/tools/terms are surveyed, and create
  `APPLY-RESEARCH.md` once §12 lands (§12).

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

## 33. Extend OpenSCAD's capabilities (plugins / utilities / math / primitives)
- 📋 **Research extending OpenSCAD itself.** OpenSCAD's stock language is thin (limited math, few
  primitives, no native plugin/extension API in mainline). **Investigate, in order:**
  1. **Native plugins / extension points** if/when supported — survey current state: experimental
     features, function-literals, the customizer, and the python-powered forks
     ([PythonSCAD](https://pythonscad.org/) / [SolidPython](https://github.com/jeff-dh/SolidPython))
     as a real extensibility path. Prefer a supported native mechanism if one exists.
  2. **Else introduce a plugin system first** — a `3d`-side extension layer: a registry of reusable
     OpenSCAD modules/functions auto-made-available (a preprocessor/codegen that injects helpers), or
     generate OpenSCAD from the higher-level object model / op-DAG (§5/§19) where the extensibility
     actually lives. Make extensions discoverable + versioned like the other registries (§3).
- 📋 **Add convenient utilities / math / richer primitives.** Leverage
  [BOSL2](https://github.com/BelfrySCAD/BOSL2) / [NopSCADlib](https://github.com/nophead/NopSCADlib)
  where they cover it; fill the gaps where they don't:
  - **d3-style math helpers** (à la [d3.js](https://github.com/d3/d3)): scales (`linear/log/pow`),
    interpolation + easing, curve/spline generators, color scales, data-driven layout.
  - vector/matrix math; richer primitives: fillets/chamfers, gears, threads, sweeps, lofts,
    text-on-path.
  - **Why:** real parts constantly need fillets, sweeps, gears, splines and parametric math that you
    otherwise hand-roll every project; a curated, discoverable layer makes each a one-liner.
  - **Example:** a std helper set auto-available in any `3d` project so
    `chamfer_cube([20,20,5], r=2)` or `lerp(a, b, ease_in_out(t))` just works — instead of copy-pasting
    a 30-line module — via either a native plugin or `include <3d/std.scad>`.

## 34. Import / export of popular 3D formats
The whole pipeline lives or dies on talking to other tools: a slicer wants a mesh, a CAD engineer
wants a B-rep, a teammate wants to *tap-and-rotate* the result on their phone, the §9 viewer wants
glTF. `3d export` already ships (✅, §3 line 134 — mesh-validated, nonzero on bad geometry); this
section is the planned **format expansion** of that command (📋 `--usdz`/`--glb`/`--step`/… selectors
or `-o file.<ext>` autodetect) **plus a new `3d import`** that brings external geometry *into* a model
(feeding §12 reconstruction). Every format below states WHERE it sits in the pipeline, HOW it's
produced/consumed, and WHY it earns a slot. Detection is extension-driven; `3d export --list-formats`
enumerates what's available.

- 📋 **STL** — *the* slicing/mesh-export lingua franca.
  - **Where/How:** terminal target of the mesh path — `3d export part.scad -o part.stl` (binary by
    default), then `3d slice part.stl` (§4). **Import:** `3d import part.stl` to measure/repair/
    reconstruct (§12).
  - **Why:** universally accepted by every slicer/printer; but it is *geometry only* — no color, no
    units, no part names, no metadata. So `3d` writes a **sidecar `3d.yaml`** (§5) next to the `.stl`
    carrying material/printer/orientation/part identity, since the format itself can't.
  - **Implementation (from research):** [trimesh](GLOSSARY.md#trimesh) for read/write + manifold
    repair; STL is the lossy LEAF, the object model (§5) stays the source of truth.
  - **Example:** `3d export bracket.scad -o bracket.stl` (auto-emits `bracket.3d.yaml` sidecar).

- 📋 **3MF** — the preferred *rich* print format.
  - **Where/How:** output of `3d pack` (§5) and the default for `3d export --3mf`; consumed directly
    by Orca/Bambu/Prusa (§4). **Import:** `3d import asm.3mf` recovers per-part color/material.
  - **Why:** unlike STL it natively carries **per-part color, material, and metadata** in one file,
    so the sidecar isn't needed and the slicer sees the real assembly — this is the format we steer
    users toward for actual printing.
  - **Implementation (from research):** the §5 builder writes [3MF](GLOSSARY.md#3mf) with the
    materials registry (§2a) driving colors/materials; round-trips back through `3d import`.
  - **Example:** `3d export cabinet.scad --3mf -o cabinet.3mf` then `3d slice cabinet.3mf`.

- 📋 **OBJ / PLY** — mesh interchange & debug.
  - **Where/How:** `3d export model.scad -o model.obj` (OBJ keeps UVs/material refs) or `.ply`
    (compact, optional per-vertex color — handy for scan/point-cloud and colored-diff dumps).
    **Import:** `3d import scan.ply` to feed reconstruction (§12).
  - **Why:** OBJ is the human-debuggable, viewer-friendly mesh that almost every DCC tool opens; PLY
    is the go-to for carrying per-vertex color (e.g. a visualized surface-deviation/Hausdorff map)
    that STL can't express.
  - **Implementation (from research):** [trimesh](GLOSSARY.md#trimesh) for both.
  - **Example:** `3d export bad.scad -o bad.ply` to dump a per-vertex error map for inspection.

- 📋 **STEP / BREP** — true CAD B-rep exchange (engineering handoff).
  - **Where/How:** `3d export part.scad --step -o part.step`; the bridge runs through
    [build123d](https://github.com/gumyr/build123d) / OpenCASCADE so the result is a real
    **boundary-representation solid** (analytic faces, exact edges), not a triangle soup. **Import:**
    `3d import part.step` to bring a vendor/colleague CAD body into a model.
  - **Why:** STL/3MF lose the parametric, watertight CAD intent that a machinist or FEA tool needs;
    STEP is the only entry on this list that preserves true curved surfaces and tolerances — it's the
    format you hand an engineer or a CNC shop. (OpenSCAD has no native B-rep, so this is the
    research-flagged path to real CAD interop.)
  - **Implementation (from research):** OpenCASCADE via build123d for the B-rep conversion; falls
    back to a meshed approximation with a loud warning if a true B-rep isn't recoverable from the
    OpenSCAD CSG.
  - **Example:** `3d export gear.scad --step -o gear.step` (hand off to a CNC shop).

- 📋 **glTF / GLB** — web / three.js viewer transport (§9).
  - **Where/How:** `3d export model.scad --glb -o model.glb`; the §9 dashboard's three.js viewer
    loads GLB directly (single self-contained binary with mesh + materials + colors).
  - **Why:** glTF is "the JPEG of 3D" for the web — compact, materials/PBR baked in, instant to load
    in a browser; it's the natural bridge from the CLI to the §9 interactive viewer and any external
    web embed, where STL/STEP would be wrong or heavy.
  - **Implementation (from research):** [trimesh](GLOSSARY.md#trimesh) (or `pygltflib`) export with
    colors/materials from the registry (§2a); GLB (binary) preferred over `.gltf`+buffers for sharing.
  - **Example:** `3d export robot.scad --glb -o robot.glb` then open it in `3d web`.

- 📋 **SVG** — 2D profiles **in and out**.
  - **Where/How (in):** `3d import profile.svg` → a closed 2D path you can `linear_extrude`/
    `rotate_extrude` into a solid — *draw a shape in Inkscape/Figma, extrude it in `3d`*. **(out):**
    `3d export part.scad --svg --section mid-z -o part.svg` emits a vector **section outline** or a
    flat **silhouette** (§3 sections / §8 silhouette) — perfect for laser-cutting, drawings, or a
    dimensioned 2D sheet.
  - **Why:** bridges the 2D and 3D worlds at both ends — designers think in 2D vector profiles, and
    fabrication (laser/CNC/drawings) wants clean vector outlines, not rasters or meshes.
  - **Implementation (from research):** OpenSCAD's own `import("…svg")` / `projection()` for the
    extrude-in and section-out paths; `svgpathtools`/shapely to normalize paths on import.
  - **Example:** `3d import logo.svg` → extrude to a keychain; `3d export box.scad --svg --silhouette`.

- 📋 **USDZ** — Apple **AR Quick Look**: tap a file on iPhone/iPad/Mac to view & rotate the model in
  3D / AR, no app install. *The* format for handing a finished result to a human.
  - **Where/How:** `3d export result.scad --usdz -o result.usdz`; AirDrop/iMessage it, the recipient
    taps and the model opens full-screen, orbits with a finger, and can be **placed in their room in
    AR**. Also the basis for an embeddable `<model-viewer>` on the web.
  - **Why:** every other format on this list needs a slicer, a CAD seat, or a dev environment to look
    at. USDZ needs *a phone you already own and one tap* — it's the lowest-friction way to show a
    non-technical person (client, teammate, the person you're printing for) what the thing actually
    looks like at real scale. Sharing the *result*, not the *toolchain*.
  - **Implementation (from research):** build the [USD](https://openusd.org/) stage with **`pxr`**
    (the OpenUSD Python libs), then package to `.usdz`. Three correctness musts so it renders right in
    Quick Look: **Y-up** axis (USD/Quick Look convention, vs `3d`'s Z-up — apply the rotation on
    export), **`metersPerUnit`** set so a part modeled in mm shows at true real-world scale in AR
    (`metersPerUnit = 0.001`), and a **`UsdPreviewSurface`** material (with the registry's color/
    metalness/roughness, §2a) so it isn't flat untextured grey. (Backlog task #4 is this helper.)
  - **Example:** `3d export boiler.scad --usdz -o boiler.usdz` → AirDrop to your phone → tap → rotate
    it in your hand. (`--up y` and `--units mm` are the defaults for USDZ; override only if needed.)

- 📋 **Import side, summarized** — `3d import <file>` accepts **STL / 3MF / OBJ / PLY / STEP / SVG**
  and lands them as model geometry: meshes go straight in (measure/repair/compare), STEP comes in as a
  B-rep solid, SVG comes in as an extrudable 2D profile. Imported meshes are exactly the input to
  **reconstruct-to-mesh (§12)** — e.g. a downloaded `.stl` or a scanned `.ply` → cleaned, re-fit, and
  folded back into the object model so the rest of the pipeline (sections, checks, pack, slice) works
  on it. **Why:** `3d` shouldn't only emit — most real work starts from *someone else's file*.
  **Example:** `3d import thing.stl && 3d check thing` (repair + run all gates on a downloaded mesh).

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
- ✅ Research vendored: `docs/research/{report.md,report.pdf,sources.md}` + `RESEARCH.md` /
   `GLOSSARY.md`. (The prioritized backlog + the APPLY-RESEARCH draft were folded into the ROADMAP
   feature sections and their files deleted — see §17/§27.)
- ✅ All temp doc branches merged + deleted; no open branches, no worktrees.
- ⚠️ **First real code task next session — config dir**: code uses `~/.config/3d-cli/` (foundation + web,
    incl. the web agent's choice); rename to `~/.config/3d-cli/` per §23 (one constant in
   `lib/cli/env.py`, `lib/web/webconfig.py`, `lib/commands/{web,libs,doctor}.py`) so docs+code agree.
- NOTE: nothing is in-flight; this session ended cleanly. Start from the build order above (1→4).
