# `3d` — a scriptable, AI-assisted CLI + web toolkit for 3D FDM projects

`3d` is a command-line + web toolkit for the whole **[FDM](GLOSSARY.md#fdm) (filament 3D-printing)** lifecycle:
parametric modeling ([OpenSCAD](GLOSSARY.md#openscad)-first) → render & view → mesh / printability / collision
verification → AI-assisted design, animation, simulation, matching → slicing & print prep.
It is **engineering-first** today (functional parts, fits, gates) and grows toward art later.
Everything is one discoverable dispatcher: `3d <command>`, scriptable, composable, with
structured, actionable errors (what failed, why, and the exact fix).

It is **general-purpose** across 3D FDM work. One of the pipelines it ships is a
**reference-photo match loop** (camera-locked render → [silhouette](GLOSSARY.md#silhouette) score → LLM numeric-delta
edits → [manifold](GLOSSARY.md#manifold)/printability gates → accept-only-if-it-improves) — see
[Reference-match pipeline](#reference-match-pipeline) — one example workflow among many.

## What you can do with 3d

`3d` is a Swiss-army knife for the whole 3D FDM lifecycle — not a single-purpose tool. Major use cases:

- **Reference-photo match** — tune a parametric model to match a photo (one pipeline among many)
- **Design from scratch with AI** — text-to-3d, dimensions-and-sketch-to-3d, parametric skeleton generation
- **Parts & fixtures** — design brackets, mounts, connectors, enclosures with parametric constraints
- **Animation & motion** — `3d animate`, kinematics, motion verification
- **Simulation & analysis** — FEA, strength, thermal, collision detection
- **Format conversion & AR** — export to USDZ/GLB/STEP, view in AR
- **Slicing & print monitoring** — slice to G-code, monitor prints, failure recovery
- **Batch & automated workflows** — multi-angle renders, batch exports, CI gates

The reference-photo match pipeline is [documented below](#reference-match-pipeline) as one example workflow.

## Install

> **Honest status:** the package is **not on PyPI yet**. The supported, working path today
> is running it from a clone (`./bin/3d`). Standard `pipx`/`uv tool`/`pip` install is the
> TARGET once packaging lands (see [ROADMAP §29](ROADMAP.md)) — the `lib/` layout is being
> restructured into an importable `threed` package with a `3d` console-script entry point.

**Current working path — run from a clone:**

```bash
git clone https://github.com/alex-mextner/3d-cli
cd 3d-cli
./bin/3d help                 # or symlink bin/3d onto your PATH:  ln -s "$PWD/bin/3d" ~/.local/bin/3d
```

**Target path (after packaging, §29):**

```bash
pipx install 3d-cli           # or:  uv tool install 3d-cli   /   pip install 3d-cli
3d help
```

Python deps resolve per call: `3d` prefers a repo `.venv`, then `uv run --with <deps>` (no
global installs), then system `python3`. With `uv` on PATH nothing needs pre-installing. For
a fast offline path:

```bash
uv sync --all-extras          # creates .venv from the lockfile
```

## Requirements

`3d` **auto-installs what it can** on first run (OpenSCAD libraries clone into `libs/`; Python
deps resolve per call via `uv`/`.venv`), and every command either works or fails with a clear
"install X" message naming the exact per-OS command. Run **`3d doctor`** to inspect what is
present or missing.

**External tools** — system programs you install yourself (the CLI prints the exact per-OS line
when one is missing; `brew`/`apt`/`winget`):

| Tool | Purpose | Tier |
|---|---|---|
| [OpenSCAD](https://openscad.org) | the modeling engine — render, export, section, params, validate | **required** |
| [ImageMagick](https://imagemagick.org) | silhouette / overlay / score image diffs | required for the match pipeline |
| `python3` + [`uv`](https://docs.astral.sh/uv) | runtime for the python subcommands; `uv` resolves their deps per call (no global installs) | **required** (`uv` recommended) |
| a [slicer](GLOSSARY.md#slicer) — [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) / [Bambu Studio](https://bambulab.com/en/download/studio) / [PrusaSlicer](https://www.prusa3d.com/page/prusaslicer_424/) | G-code export & sliceability gate (`3d slice`) | optional |
| [ffmpeg](GLOSSARY.md#ffmpeg) | animation / report video export (`3d animate`, `3d report`) | optional |
| [Blender](GLOSSARY.md#blender) | photoreal render (`3d render --photo`) — installed on demand, not bootstrapped | optional |

**Python packages** — resolved automatically by `uv`/`.venv`; you normally never install these by
hand. Only the heavyweight ones worth knowing about:

- **core (auto):** the mesh stack [`trimesh`](GLOSSARY.md#trimesh) + [`manifold3d`](GLOSSARY.md#manifold3d) (watertight / manifold / volume) and `pyyaml` (the `3d.yaml` project model).
- **optional extras:** `opencv` + `pillow` (`preprocess`), `pyvista` (`collision --viz`), `fastapi`/`uvicorn` (`web`). The full pinned set lives in `pyproject.toml` (`preprocess`/`viz`/`web`/`dev` extras) + `uv.lock`.

A missing optional dependency degrades only the command that needs it — never the whole CLI.
`3d doctor` prints PASS/MISSING per item with the exact per-OS install line for anything absent.

```bash
3d doctor          # read-only: report present/missing + the exact install command per OS
```

The CLI bootstraps OpenSCAD libraries ([BOSL2](GLOSSARY.md#bosl2), [NopSCADlib](GLOSSARY.md#nopscadlib)) into `libs/` on the first `3d`
invocation (once, quiet, non-fatal offline) and auto-exports `OPENSCADPATH`, so
`include <BOSL2/std.scad>` just resolves with no manual step.

## Commands

Run `3d <command> --help` for full options. Examples below assume `examples/cube.scad`.

> **Coming in this branch:** `3d init` (project scaffolder), `3d projects` (project registry),
> `3d materials` / `3d printers` (shared material/printer vocabularies, §2a) are landing in
> the same development wave and are **not** documented as existing yet. The table below is the
> verified surface that exists today.

### Render & view  (unified under `render`)

`render` is the one view/section command. `multi`/`section` remain as thin aliases.

| Command | What |
|---|---|
| `3d render <file.scad> [--view NAME]` | Single [CGAL](GLOSSARY.md#cgal) view. Camera computed from the model **bounding box** + the named direction. Default view: `iso`. |
| `3d render <file.scad> --multi [outdir] [--render]` | Render all standard angles (front/back/left/right/top/iso) concurrently. |
| `3d render <file.scad> --section -o out.png [--plane …] [--color]` | True cross-section: generic STL-cut (any geometry) or `--color` per-part assembly mode. |
| `3d render <file.scad> --cam ex,..,cz` | Manual 6-param **vector** camera (wins over `--view`). |
| `3d preview <file.scad>` | Fast throwntogether preview (no CGAL). |
| `3d multi …` / `3d section …` | back-compat aliases for `render --multi` / `render --section`. |

`--view` names: `front back left right top bottom iso 3-4 front-left front-right
rear-left rear-right`. `3-4` is the canonical three-quarter hero angle (azimuth 45°,
elevation 30°). With the trimesh mesh stack present the camera is placed exactly from the
model's bounding-box centroid + diagonal; without it, `render` orbits along the view
direction with `--autocenter --viewall` (so view selection always works, mesh stack or not).

```bash
3d render examples/cube.scad --view left -o left.png
3d render examples/cube.scad --view 3-4 --ortho
3d render examples/cube.scad --multi previews/ --render
3d render examples/cube.scad --section --plane YZ -o sec.png      # generic cut (any geometry)
3d render assembly.scad --section --color --plane YZ -o sec.png   # per-part coloured assembly
3d render examples/cube.scad --cam 130,-600,52,130,0,52 --ortho --size 1600x700
```

The match loop wants a **6-param [vector camera](GLOSSARY.md#vector-camera)** `ex,ey,ez,cx,cy,cz` (eye → center) plus
`--ortho`. The 7-param gimbal form (`...,dist`) with `dist=0` renders an empty frame —
`render`/`silhouette`/`score` reject a non-6 `--cam` value.

The generic `--section` exports the model to [STL](GLOSSARY.md#stl) once, then `difference(import(stl),
halfspace)` with the colour **outside** the cut so the cut face takes the part colour — it
cuts **arbitrary** geometry with no cut-contract needed. `--color` is the richer per-part
assembly mode (the assembly must honour `-D cut=true` and colour each part outside its own
`difference`). All section cameras are 6-param **vector** cameras, never a 7-param gimbal.

### Geometry & export

| Command | What |
|---|---|
| `3d export <file.scad>` | STL/[3MF](GLOSSARY.md#3mf) with manifold/self-intersect validation. **Nonzero exit on bad geometry.** |
| `3d validate <file.scad>` | Fast syntax check (no render). |
| `3d params <file.scad> [--json]` | Extract Customizer-style parameters. |

```bash
3d export examples/cube.scad -o cube.stl          # PASS, exit 0
3d export examples/cube.scad -o cube.3mf -D 'width=80'
3d validate examples/cube.scad
3d params examples/cube.scad --json
```

`export` validates the produced mesh with the trimesh/manifold3d stack (watertight +
manifold) when available — so a non-manifold part exits 1 even when OpenSCAD's modern
backend emits no text warning. Without the mesh stack it degrades to log-grep and tells
you to run `3d mesh` for the full check.

### QA & gates

`check` is the one verification command — the **master acceptance gate**. With no
selection flags it runs ALL applicable gates; selectors run a subset; `--skip` excludes.

| Command | What |
|---|---|
| `3d check <file.scad> [parts…]` | All applicable gates: manifold + consistency + printability (+ collision/silhouette when data is supplied). Prints a per-gate breakdown + `>>> CHECK: PASS/FAIL`. |
| `3d check … --mesh \| --manifold \| --consistency \| --printability` | run only the named **core** gate(s). |
| `3d check … --skip GATE` | exclude a gate (`manifold\|consistency\|printability\|collision\|silhouette`). |
| `3d check … --collision cfg.json` / `--ref img` | supply data; the collision/silhouette gate then runs (never narrows the core set). |
| `3d acceptance <assembly.scad>` | back-compat alias for `check` (all gates). |
| `3d mesh <file.stl\|.scad>` | watertight / manifold / self-intersection / volume (trimesh + open3d/manifold3d; falls back to openscad warnings). |
| `3d printability <file.scad>` | wall / min-feature / overhang / orientation (FDM, PLA/PETG). |
| `3d collision <config.json>` | generic collision/penetration engine (static / `--frame` / `--viz`). |

```bash
3d check examples/cube.scad                          # all applicable gates
3d check examples/cube.scad --mesh                   # only the manifold gate
3d check asm.scad --skip printability
3d check asm.scad --collision verify/collision.json --ref ref.jpg
3d mesh cube.stl
3d collision verify/collision.json --frame           # per-frame timeline gate
```

`--collision` and `--ref` supply **data**, not a selector: they make the collision /
silhouette gate applicable but never narrow the core gate set — so a supplied config can
**never** silently skip a HARD gate (no false PASS). For a genuine subset, use `--skip`
or name the core gates explicitly.

The collision engine is project-agnostic: a JSON config supplies the placement `.scad`,
part list, phases, intended-contact whitelist, and EPS/touch thresholds — all paths
resolved relative to the config file's directory.

### Reference-match pipeline

Match a parametric model to a reference photo by viewpoint and silhouette, for when you have a
photo of a real object and want a printable part with the same proportions and pose.

> You photograph a bracket, write a rough parametric `bracket.scad`, then `3d [fit-camera](GLOSSARY.md#fit-camera)` locks
> the camera to the photo and `3d match` nudges the parameters until the rendered silhouette
> matches the photo — keeping only edits that raise the silhouette [IoU](GLOSSARY.md#iou) and stay manifold.

| Command | What |
|---|---|
| `3d silhouette <file.scad>` | camera-locked render → binary silhouette mask. |
| `3d overlay <render.png> <reference.png>` | difference / 50% ghost / canny edge-overlay diagnostics. |
| `3d score <render.png\|file.scad> <reference>` | silhouette [AE](GLOSSARY.md#ae) + IoU (machine-parseable `KEY=VALUE` lines). |
| `3d match <assembly.scad> <reference>` | forced-monotonic acceptance loop (render→score→critic→apply→accept/revert + changelog). |
| `3d fit-camera <model.scad> <reference>` | fit an OpenSCAD camera to a reference photo by maximizing silhouette IoU; **saves the viewpoint** + a fit render + an overlay. |
| `3d preprocess <reference.jpg>` | subject mask + proportional depth ([SAM2](GLOSSARY.md#sam2)/[Depth-Anything](GLOSSARY.md#depth-anything) if installable, else [OpenCV](GLOSSARY.md#opencv). |

```bash
3d silhouette examples/cube.scad -o mask.png --ortho --cam 130,-600,52,130,0,52
3d overlay render.png ref.jpg -o work/
3d score model.scad ref.jpg                       # renders, then scores
3d score mask_a.png mask_b.png --masks            # compare two ready masks
3d match model.scad ref.jpg --rounds 8 --dry-run  # exercise the loop without the LLM
3d fit-camera model.scad ref.jpg --out camera.json --draw-axes
3d preprocess ref.jpg -o work/ --force-fallback   # OpenCV grabCut + pseudo-depth
```

`fit-camera` searches the camera **pose** (azimuth, elevation, distance, pan-x, pan-z
orbiting the look-at) to maximize silhouette IoU between the CGAL render and the
reference, then writes `camera.json` with the fitted 6-param vector `camera_arg`, the
per-param values, the IoU, plus `<out>_fit.png` (full-res fit) and `<out>_overlay.png`
(render-cyan over reference-red ghost). The optimizer is random-search → coordinate-descent
with a deterministic seed. Crucially it is **scale-free**: it exports a temporary STL,
reads the model's bounding-box centroid + diagonal, and derives the distance/pan bounds and
refine steps from that diagonal — so a 20 mm cube and a 300 mm assembly both fit without
hardcoded numbers. `--center` overrides the auto look-at; `--draw-axes` overlays each
silhouette's PCA principal axis + bounding-box contour so axis/contour alignment is visible.
Different builds never reach IoU = 1 (the shapes differ) — the point is best alignment of
the bounding silhouette so viewpoint, scale and gross proportions match. Use the result:

```bash
openscad --render --camera="$(jq -r .camera_arg camera.json)" -o view.png model.scad
```

`score` prints `AE=`, `AE_NORM=`, `IoU=`, `CLOSENESS=`, `FRAME=`, `OVERLAY=` — one per
line, machine-parseable. An empty render mask scores IoU=0 (never rewards a blank frame).

`match` is the **[forced-monotonic loop](GLOSSARY.md#forced-monotonic-loop)**: the critic (codex, optional) proposes ONE numeric
param delta; the IoU/AE metric + manifold gate dispose. A change is kept iff the score
strictly improves AND the model stays a clean manifold; else it is reverted. Every step is
logged to `<work>/changelog.md`, which is fed back to the critic so it never re-proposes a
reverted edit (the anti-FlipFlop defense). Tunable parameters are **derived from the
constants file** (numeric scalars) — restrict with `--params a,b,c`, or point at a separate
`--constants FILE`. `--dry-run` skips the LLM and synthesises deterministic edits to
smoke-test the machinery.

### Slicing

| Command | What |
|---|---|
| `3d slice <stl\|3mf\|file.scad>` | slice to G-code via the installed slicer; **`--check` = sliceability gate** (nonzero exit on failure). |

```bash
3d slice part.stl -o part.gcode
3d slice part.scad --check                          # .scad → STL → slice, gate only
3d slice part.3mf --profile "machine.json,process.json" --printer "Bambu Lab A1"
```

Slicer auto-detection preference: **OrcaSlicer → Bambu Studio → PrusaSlicer**. Found on PATH
and on macOS app bundles (`/Applications/OrcaSlicer.app/...`, `BambuStudio.app`,
`PrusaSlicer.app`); force a specific one with `SLICER=/path/to/binary`. The three share
heritage but the CLIs **diverged**, so each gets its own invocation: PrusaSlicer is `-g
--output out.gcode`, OrcaSlicer/Bambu are `--slice 0 --outputdir <dir>` (the produced G-code
is relocated to your `-o` path). Those core flags are the verified part of the contract;
`--printer` is **best-effort** (no single agreed printer-preset flag exists across the three
— it routes through the profile-load mechanism, so prefer `--profile` for control).
`--check` slices as a pass/fail oracle and discards the G-code. A `.scad` input is exported
to STL first via `3d export`. If no slicer is installed, `3d slice` fails with the exact
per-OS install command (e.g. `brew install --cask orcaslicer`) — never broken.

### Environment (deps) & tests

| Command | What |
|---|---|
| `3d doctor` | report present/missing deps + the exact install command per OS (read-only). |
| `3d test [pytest-args]` | run the test gate: pytest (unit + CLI smoke harness) then mypy. |

```bash
3d doctor                 # PASS/MISSING table (read-only)
3d test                   # pytest + mypy — both must pass
3d test -k registry       # forward args to pytest
```

OpenSCAD libraries auto-install on the first run, python deps resolve via uv/`.venv`
per-call, and `3d doctor` prints the exact install command for anything still missing.

### Web dashboard

| Command | What |
|---|---|
| `3d web [--root DIR] [--port N] [--open]` | local FastAPI + SSE + three.js dashboard for your projects and for watching AI agents work live (optional **web** tier). |

```bash
3d web --root ~/models --open            # scan that root, open the dashboard
3d web --port 9000                       # override the default 8733
```

See [docs/commands/web.md](docs/commands/web.md) for the full feature list. The web tier
(`fastapi`/`uvicorn`/`markdown`) is optional — the core geometry/render/check pipeline does
not need it; a missing dep is a warning in `3d doctor`, not a failure.

### OpenSCAD libraries

BOSL2 + NopSCADlib **auto-install on the first `3d` invocation** (cloned into `libs/`, once,
gated by `~/.config/3d-cli/.bootstrapped`, non-fatal if offline), and `OPENSCADPATH` is
auto-exported by the CLI — so `include <BOSL2/std.scad>` just resolves, no manual step.

```bash
3d libs list                 # show installed libraries
3d libs path                 # print OPENSCADPATH (for your own non-3d shells)
# re-install: rm ~/.config/3d-cli/.bootstrapped && 3d help
```

## Configuration & state

`3d` keeps all its state under one config dir and one data dir (ROADMAP §23):

- **`~/.config/3d-cli/`** — `web.json`, the first-run bootstrap marker (`.bootstrapped`),
  the projects registry, and registry overrides. (Honors `$XDG_CONFIG_HOME`.)
- **`~/.local/share/3d-cli/`** — generated state, including the longitudinal metrics store.
  (Honors `$XDG_DATA_HOME`.)

## Layout

```
bin/3d              thin Python dispatcher (resolves REPO_ROOT through the symlink)
lib/cli/dispatch.py routing + registry build + structured-error rendering
lib/cli/registry.py the command registry (Command + discover()) — the plugin extension point
lib/cli/env.py      tool discovery, OS/install table, OPENSCADPATH export, first-run bootstrap
lib/cli/paths.py    the single source of truth for config/data dirs (~/.config/3d-cli, …)
lib/cli/pyrun.py    run a lib/*.py tool with its deps (.venv -> uv -> system python3)
lib/cli/imaging.py  ImageMagick orchestration + the pure score (IoU/AE) math
lib/project.py      the 3d.yaml project model + loader (the project spine, §5/§15)
lib/errors.py       structured CLI error types (WHAT/WHY/remediation/accepted/install)
lib/commands/*.py   one self-registering module per subcommand (drop a file = add a command)
lib/*.py            heavy python tools (render/mesh/collision/printability/preprocess/match/fit)
lib/web/            the web dashboard app (FastAPI + SSE + three.js SPA)
tests/              pytest unit tests + the CLI smoke harness (run via `3d test`)
docs/commands/      per-command documentation fragments
docs/critic-prompts.md  the vision-critic prompt patterns
libs/               OpenSCAD libraries cloned on demand (gitignored)
examples/cube.scad  trivial test part
pyproject.toml      python deps (uv project: core + optional extras preprocess/viz/web/dev)
uv.lock             locked dependency set (uv sync)
```

Adding a command is a one-file change — see `lib/cli/registry.py` (and `AGENTS.md`) for the
command-authoring contract. `bin/3d` and the shared files need no edits.
