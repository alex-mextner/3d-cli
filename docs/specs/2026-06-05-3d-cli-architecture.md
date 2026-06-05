# `3d` CLI — modular, extensible, project-agnostic architecture

Status: DRAFT (2026-06-05) · Owner: design · Supersedes the implicit "operationalize the
lego-loco pipeline" framing.

## Why this spec

`3d` started by operationalizing one project's (lego-loco) reference-photo match pipeline.
That framing leaked into the README intro, several core module docstrings
(`preprocess_reference.py` is written around "the locomotive"), and a hardcoded critic
feature taxonomy (`funnel/boiler/smokebox`). `3d` is meant to be a **Swiss-army knife for
all 3D FDM work** — engineering-first today, artistic projects later — not a loco tool.

This spec defines the **go-to architecture**: a project-agnostic core, self-registering
capability plugins, a project layer that carries all subject-specific knowledge, and
optional named pipelines. The loco pixel-match becomes ONE pipeline among many, never the
tool's identity.

The architecture is the contract. Concrete feature list stays in `ROADMAP.md`; this doc
does not duplicate it.

---

## 1. Principles (non-negotiable)

1. **No subject knowledge in core.** Core tools take subject / reference / feature-list /
   camera / plane as **parameters**. ZERO default filenames, cameras, part lists, or
   feature taxonomies baked into core code. A core tool masks "the subject", not "the loco";
   a critic's feature list is supplied by the caller, not fixed to loco parts.
2. **Examples are allowed, but marked.** Illustrative strings in docstrings (`config.py`'s
   "ejector", `frame_check.py`'s "cartridge") are fine **only** when clearly framed as
   "e.g." examples — never as the tool's domain.
3. **Everything is a plugin.** Commands, gates, AI tools, slicers, render backends,
   importers, metrics — all self-register through one registry pattern (see §3). Adding a
   capability = drop in a module; never edit a central dispatcher or shared list.
4. **Projects are data, not code.** A project supplies a `3d.yaml`, parts, references, and
   its own checks/configs. The CLI operates on ANY project directory by finding the nearest
   `3d.yaml` from cwd. Nothing project-specific is compiled in.
5. **Engineering now, art later — by configuration, not by fork.** What gates run, what
   metrics matter, what materials/printers apply are project-declared. An art piece runs
   manifold+printability and skips strength/collision; an engineering part runs the full
   set. Same binary.
6. **Backward compatibility with the ecosystem.** The semantic object model (§4) must
   attach to `.scad`/`.stl`/`.3mf` WITHOUT breaking OpenSCAD, slicers, or mesh viewers.

---

## 2. The four layers

```
┌─────────────────────────────────────────────────────────────┐
│ 4. PIPELINES (optional, named, composable)                   │
│    reference-photo match · design-from-image · print-prep …  │
│    each a plugin; selectable; none is "the tool"             │
├─────────────────────────────────────────────────────────────┤
│ 3. PROJECT LAYER (pure data)                                 │
│    3d.yaml + parts/*.scad + references/ + project checks      │
│    object model (§4) · anchors · sections · loads · gates set │
├─────────────────────────────────────────────────────────────┤
│ 2. CAPABILITY PLUGINS (self-registering)                     │
│    gates · ai-tools · slicers · render-backends · importers   │
│    · metrics — each implements a typed capability interface  │
├─────────────────────────────────────────────────────────────┤
│ 1. CORE (project-agnostic)                                   │
│    dispatcher + registry · errors · config/discovery ·        │
│    render/section/export primitives · materials/printers ·    │
│    metrics store · AI adapters · geometry/object-model io     │
└─────────────────────────────────────────────────────────────┘
```

Dependency rule: arrows point DOWN only. Core knows nothing about plugins; plugins know
nothing about projects; projects know nothing about pipelines. A pipeline composes
capabilities over a project.

---

## 3. Extension mechanism (registry)

All capabilities self-register through the **command-registry pattern established by the
foundation wave** (bash→python dispatcher). Concrete signatures are finalized once that
wave reports its contract; this spec fixes the SHAPE, not the exact API:

- **Commands** — `lib/commands/<name>.py`, self-registering; dispatcher builds help/routing
  from the registry. No central edits to add a command.
- **Capability registries** mirror the same pattern, one per kind:
  - `gates` — `name, applies_to(project) -> bool, run(part|project) -> GateResult`.
  - `ai_tools` — `name, rag_manifest, operators {do, review, loop}` (ROADMAP §13).
  - `render_backends` — `openscad` (default), `blender` (photoreal, on-demand).
  - `slicers` — `orca > bambu > prusa`, each a backend.
  - `importers` — `.scad`, `.stl`, `.3mf`, `.step` (object-model io, §4).
  - `metrics` — emit + persist (ROADMAP §13.4).
- A new project-specific check registers as a `gate` from the project dir (loaded from the
  project layer), so projects extend core without touching it.

Rule for feature agents: a new feature ships `--help` text + a `docs/commands/<name>`
fragment. It MUST NOT touch the README intro or Requirements section (§6).

---

## 4. The 3D-model object model + file format

The semantic layer over raw geometry: names, anchors, sections, parts, colors/materials,
features, loads. One object model is authored once and drives sections, colored renders,
camera framing, `pack`, strength, kinematics, AI RAG — "say where to cut / what to frame /
how to print" once, reuse everywhere.

### 4.0 Mental model: a DOM + stylesheet, without HTML/CSS

The geometry is a **tree** (assembly → parts → features) — like a DOM. The object model
layers an **HTML/CSS-like addressing & styling system over it, but with no HTML and no CSS**:

- **id** — unique identifier per node (a part / feature / anchor), like `#boiler`.
- **class** — shared categories (these ARE the `tags`: `structural`, `cosmetic`,
  `removable`, …), like `.structural`.
- **selectors** — target a set of nodes by id/class/tag: `#valve`, `.cosmetic`,
  `.structural.removable`. ONE addressing mechanism reused by every command.
- **stylesheet (rules)** — `selector → properties`, where "properties" are: color,
  material, orientation, supports, infill, gate set, loads, section membership, camera-frame
  membership. Authored once as rules instead of repeated per part.
- **cascade + specificity** — like CSS: a class rule sets a default, an id rule overrides,
  later overrides earlier, with a pin (`!`-style) for must-win. A per-part inline value is
  the highest-specificity override.

This unifies the whole tool: selectors address nodes for `render --frame .cosmetic`,
`section through:#valve`, `check --only .structural`, `pack` per-class supports, `ai`
tool scoping. No per-part repetition; engineering-vs-art is just different rule sets.

### 4.1 Association — backward-compatible, never breaks other tools

| Carrier | How the object model attaches | Why it's safe |
|---|---|---|
| `.scad` | `// @anchor`, `// @section`, `// @part`, `// @color` comments | OpenSCAD ignores comments → file still renders everywhere |
| `.3mf` | native metadata + per-object color/material | 3MF is designed to carry this; preferred rich mesh format |
| `.stl` | **sidecar** `<model>.3d.yaml` next to the file | STL has no metadata slot; embedding would corrupt it for other readers → sidecar only |
| project | `3d.yaml` ties parts ↔ files ↔ object model | single source for multi-part assemblies |

Principle: **enrich via comments (scad) / native metadata (3mf) / sidecar (stl)** — the
geometry file always remains valid input to OpenSCAD, slicers, and mesh viewers.

### 4.2 What the object model declares

- **parts**: name, source file+module, color, material (by name → materials registry),
  tags (`structural|shell|cosmetic|functional|artistic|…`).
- **anchors**: `name, pos=[x,y,z], dir=[..], area, note` — semantic points/features.
- **sections** (see §5): named cut definitions (preset or plane+offset), referenceable by
  name.
- **loads**: at anchors, for strength.
- **gates**: which gates this project/part runs (drives "all applicable gates by default").

### 4.3 Object-model io (core)

`importers` read each carrier into one in-memory object model; an exporter writes back
(scad comments / 3mf metadata / stl sidecar). Round-trips must be lossless within a
carrier's capability and never emit a file that breaks the carrier's other consumers.

---

## 5. Sections — colored-only, anchored, multi, auto-framed

Replaces the confusing "true cross-section" / "--color per-part assembly mode" wording.

1. **Always colored.** Every section preserves each part's color ON the cut face. The plain
   monochrome section is removed — it is never wanted. There is no `--color` flag because
   color is not optional.
2. **High-level specification (primary UX):**
   - presets: `mid-x | mid-y | mid-z` (cut through the model centroid on an axis).
   - `through:<anchor>` (plane through a named anchor/feature).
   - named sections from the object model (`--section <name>`).
   - low-level still available but secondary: `--plane YZ|XZ|XY [--at <coord> | --offset d] [--keep pos|neg]`.
3. **Multiple sections at once.** Accept several `--section` specs (or a list) → render each,
   and optionally a combined multi-cut view. Useful for showing several internals together.
4. **Auto-camera for sections.** The camera auto-positions to show the cut well: an
   optimization whose objective **maximizes the projected area of the cut face inside the
   frame** and **minimizes occlusion** of the cut by the remaining solid (shares machinery
   with `fit-camera`, different objective). Manual `--cam` overrides.

---

## 6. README & framing ownership (the de-coupling, enforced)

- ONE docs/reframe owner edits the **README intro + Requirements + framing**. Feature work
  never touches these.
- README intro reframed: `3d` = scriptable, AI-assisted CLI for **any** 3D FDM project
  (engineering now, art later); the reference-photo match is presented as one example
  pipeline, with a link, not the headline.
- **Requirements section** = a plain LIST: every dependency, a one-line purpose, an
  `(optional)` marker where applicable, and a single statement that the CLI auto-installs
  what it can (`3d doctor` to inspect). Delete the manual venv/pip walkthrough.
- Core code/comments swept for subject leakage per §1 (priority: `preprocess_reference.py`,
  `critic-prompts.md` feature taxonomy).

---

## 7. Migration / sequencing

1. Foundation wave (in progress): bash→python dispatcher + command registry + errors +
   tests. Establishes the registry the rest depends on.
2. **First after foundation**: the de-coupling/reframe pass (§6) — runs before feature
   agents because the foundation wave is rewriting the README with the OLD loco framing
   (known-throwaway output).
3. Object-model io + sections (§4–§5), materials/printers, then the parallel feature swarm
   (ROADMAP §3–§13), each adding capabilities via the registry.
4. Integration wave: merge web + demo + feature branches; end-to-end tests; the showcase
   demo (ROADMAP §14).

## 8. Verification this architecture holds

- `grep` for subject leakage in core returns only marked examples (CI check).
- Adding a trivial command / gate requires a new file ONLY (no shared-file diff).
- `3d check` on two projects with different declared gate sets runs different gates.
- An object model authored in `.scad` comments survives a bare `openscad` render; the same
  model as an `.stl`+sidecar slices unchanged in OrcaSlicer.

---

## 9. Operation DAG + editable history (roll-forward over a changed past op)

Inspiration: `vector-engine` ([github.com/hyperide/hyper-saas](https://github.com/hyperide/hyper-saas), `packages/vector-engine`).
Mirror its **compute-graph** model; fix the gap it has (linear history).

- **The pipeline is a DAG of operations, not a script.** Every step (`load scad` → `select #hole`
  → `grow 2` → `section mid-x` → `render`) is a NODE with typed inputs/outputs. `3d om`'s chained
  expression is one path through this DAG; a project's build is the whole DAG. (ffmpeg's filter
  graph is the same idea — a DAG — but with better UX, see §11.)
- **Edit a past op → roll forward automatically.** Change any node's params and the executor
  **topologically recomputes only the downstream-dependent nodes**; everything else is served from
  a per-node cache keyed on `(op-type, params, input-fingerprints)` (mirror
  `vector-engine/src/graph/executor.ts`: dirty-set + fingerprint cache). This is exactly the
  "roll changes forward over a modified earlier operation" the user wants — it falls out of the
  compute-DAG, no manual replay.
- **History is itself a DAG, not a linear tape.** `vector-engine` only has a linear undo/redo tape
  (`graph/history.ts`) — you cannot edit a middle op and keep later ops. We add the missing piece:
  history nodes form a DAG; editing a past op re-derives descendants; branches are allowed
  (try-a-variant without losing the other). Undo/redo navigate the DAG.
- **Persistence**: base-state snapshot + append-only operation log + pointer, like
  `vector-engine/src/persistence/{operation-log,serialize}.ts` (diff-based ops, compactable).
  Serialize to the project (`3d.yaml`/sidecar). Replay reconstructs state deterministically.
- **Operation registry**: each op type = `{type, inputs, outputs, params, execute(inputs,params)}`
  self-registered (same registry pattern as §3). Adding an op = a new module. Operations ARE the
  capability plugins for the DAG (a `3d om` verb, a gate, a render = op node types).

## 10. Headless `lib` core + thin frontends (cli / web / gui)

Mirror the `vector-engine` (headless core, zero UI deps) ← `vector-cli` / `vector-wasm` split.

- **`lib` is the headless core**: object model + selectors/stylesheet (§4), the operation-DAG
  executor + registries (§9), gates, renderers, materials/printers, metrics, AI adapters. It is a
  normal importable library with a typed public API — NO shell, NO printing, NO argv. Everything
  the tool can do is callable as a function on the core.
- **Frontends are thin and interchangeable over that one core**:
  - **`cli`** — the `3d` dispatcher (argv → core calls → stdout); the registry maps commands to
    core ops. The CLI must not contain logic the core lacks.
  - **`web`** — the dashboard (already built, `lib/web/`): HTTP/SSE → the same core calls.
  - **`gui`** (potential, future) — a desktop app over the identical core API.
- Consequence for the foundation wave: the python dispatcher is a **frontend over an importable
  core package**, not a bag of scripts. If commands embed logic, a wave-B "core extraction" task
  lifts it into `lib`. Test the core directly (no subprocess) + smoke-test each frontend.

## 11. Two-layer command surface: technical ⊕ friendly, combinable

Inspiration: **ffmpeg's power** (a complete, composable filter graph) **without ffmpeg's UX**.

- **Layer 1 — technical / complete.** Full, explicit access to every op, param, selector, plane,
  camera vector, filter-graph edge. Nothing is hidden; power users and the DAG serialization speak
  this. (ffmpeg-grade expressiveness.)
- **Layer 2 — user/AI-friendly.** Presets, named views, anchors, selectors, intent verbs
  (`mid-x`, `through:#valve`, `--frame .cosmetic`, `bind camera to #hole`). Readable, guessable,
  the default surface for humans and the AI tools (§13).
- **The two layers COMBINE — this is the requirement, not either/or.** A friendly binding plus a
  technical tweak in one breath: *attach the camera to a part fragment, then nudge it by an
  explicit offset* —
  `3d render --frame #hole-1 --cam-offset [0,-5,12] --cam-roll 8`
  (high-level anchor binding ⊕ low-level numeric offset). Same for sections (`through:#valve`
  ⊕ `--offset 2`), transforms, loads. Layer 2 resolves to Layer 1 under the hood, so anything
  expressible high-level is inspectable/overridable low-level (`--explain` prints the resolved
  Layer-1 form).
