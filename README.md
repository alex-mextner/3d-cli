# `3d` — a scriptable CLI for AI-assisted, reference-photo-driven parametric 3D modeling

`3d` operationalizes the lego-loco research pipeline: camera-locked OpenSCAD renders,
silhouette scoring, a forced-monotonic LLM match loop, and manifold / printability /
collision gates — all from one discoverable command-line dispatcher.

The methodology (report §7 pixel-perfect workflow, §8 toolchain): author a part as
parametric OpenSCAD, render it with a camera locked to an orthographic, frame-matched
view of a reference photo, score the match with an ImageMagick silhouette IoU, let a
vision LLM propose **ranked numeric-delta** parameter edits against an overlay image,
and **accept an edit only when the score strictly improves and the manifold /
printability gates pass** — logging every attempt to a changelog so the model never
re-tries a failed move. That forced-monotonic, image-in-the-loop inverse-procedural-
modeling pipeline is what turns "an LLM fiddling with numbers" into a reproducible,
pixel-matched, printable part.

## Install

```bash
# the repo lives wherever you cloned it; symlink bin/3d onto PATH:
ln -sf "$PWD/bin/3d" ~/.files/bin/3d     # or any dir on your PATH
3d help
```

Requirements:
- **OpenSCAD** (`brew install --cask openscad`) — found on PATH or common Homebrew paths.
- **ImageMagick** (`brew install imagemagick`) — for the overlay / score / silhouette commands.
- **Python**: the python subcommands run via `lib/pyrun`, which prefers a repo `.venv`,
  then `uv run --with <deps>` (no global installs), then system `python3`. With `uv`
  on PATH nothing needs pre-installing. For a fast offline path:
  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ```

Every command either works or fails with a clear "install X" message — no broken commands.

## Commands

Run `3d <command> --help` for full options. Examples below assume `examples/cube.scad`.

### Render & view

| Command | What |
|---|---|
| `3d render <file.scad>` | CGAL (`--render`) PNG, locked **6-param vector** camera, optional `--ortho`. |
| `3d preview <file.scad>` | Fast throwntogether preview (no CGAL). |
| `3d multi <file.scad> <outdir>` | 6 angles: iso/front/back/left/right/top. |
| `3d section <file.scad> -o out.png` | Colored cross-section (6-param vector camera; never 7-param gimbal). |

```bash
3d render examples/cube.scad -o cube.png
3d render examples/cube.scad --ortho --cam 130,-600,52,130,0,52 --size 1600x700
3d preview examples/cube.scad -o look.png
3d multi examples/cube.scad previews/ --render
3d section examples/cube.scad -o sec.png --module 'hollow_box(20,20,16,2);' --plane YZ
```

The match loop wants a **6-param vector camera** `ex,ey,ez,cx,cy,cz` (eye → center) plus
`--ortho`. The 7-param gimbal form (`...,dist`) with `dist=0` renders an empty frame —
`render`/`silhouette`/`score` reject a non-6 `--cam` value.

### Geometry & export

| Command | What |
|---|---|
| `3d export <file.scad>` | STL/3MF with manifold/self-intersect validation. **Nonzero exit on bad geometry.** |
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

| Command | What |
|---|---|
| `3d check <file.scad>` | manifold (`--render` + grep ERROR/WARNING) + quick printability. PASS/FAIL. |
| `3d mesh <file.stl\|.scad>` | watertight / manifold / self-intersection / volume (trimesh + open3d/manifold3d; falls back to openscad warnings). |
| `3d printability <file.scad>` | wall / min-feature / overhang / orientation (FDM, PLA/PETG). |
| `3d acceptance <assembly.scad>` | master gate: manifold + consistency + printability (+ collision/silhouette if configured). |
| `3d collision <config.json>` | generic collision/penetration engine (static / `--frame` / `--viz`). |

```bash
3d check examples/cube.scad
3d mesh cube.stl
3d printability examples/cube.scad
3d acceptance examples/cube.scad --ref ref.jpg --collision verify/collision.json
3d collision verify/collision.json            # static gate
3d collision verify/collision.json --frame    # per-frame timeline gate
```

`acceptance` runs on the single assembly you pass (add parts with `--part`); collision and
silhouette run **only when configured** (`--collision cfg.json`, `--ref img`). It prints
`>>> ACCEPTANCE: PASS/FAIL` and exits accordingly.

The collision engine is project-agnostic: a JSON config supplies the placement `.scad`,
part list, phases, intended-contact whitelist, and EPS/touch thresholds — all paths
resolved relative to the config file's directory.

### Reference-match pipeline

| Command | What |
|---|---|
| `3d silhouette <file.scad>` | camera-locked render → binary silhouette mask. |
| `3d overlay <render.png> <reference.png>` | difference / 50% ghost / canny edge-overlay diagnostics. |
| `3d score <render.png\|file.scad> <reference>` | silhouette AE + IoU (machine-parseable `KEY=VALUE` lines). |
| `3d match <assembly.scad> <reference>` | forced-monotonic acceptance loop (render→score→critic→apply→accept/revert + changelog). |
| `3d fit-camera <model.scad> <reference>` | fit an OpenSCAD camera to a reference photo by maximizing silhouette IoU; **saves the viewpoint** + a fit render + an overlay. |
| `3d preprocess <reference.jpg>` | subject mask + proportional depth (SAM2/Depth-Anything if installable, else OpenCV). |

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

`match` is the report's **forced-monotonic** loop: the critic (codex, optional) proposes
ONE numeric param delta; the IoU/AE metric + manifold gate dispose. A change is kept iff
the score strictly improves AND the model stays a clean manifold; else it is reverted.
Every step is logged to `<work>/changelog.md`, which is fed back to the critic so it never
re-proposes a reverted edit (the anti-FlipFlop defense, report §3). Tunable parameters are
**derived from the constants file** (numeric scalars) — restrict with `--params a,b,c`, or
point at a separate `--constants FILE`. `--dry-run` skips the LLM and synthesises
deterministic edits to smoke-test the machinery.

### OpenSCAD libraries

```bash
3d libs install bosl2        # clone BOSL2 into libs/
3d libs install all          # BOSL2 + NopSCADlib
3d libs list
export $(3d libs path)       # so 'include <BOSL2/std.scad>' resolves
```

## Layout

```
bin/3d              dispatcher (resolves REPO_ROOT through the symlink)
lib/common.sh       shared bash helpers (binary location, symlink-safe REPO_ROOT)
lib/pyrun           python runner: .venv -> uv -> system python3
lib/cmd_*.sh        one file per subcommand
lib/*.py            migrated python tools (mesh/collision/printability/preprocess/match)
libs/               OpenSCAD libraries cloned on demand (gitignored)
examples/cube.scad  trivial test part
requirements.txt    python deps for the full offline path
```

## Provenance

The render/section/export/validate/params helpers, the collision engine, the mesh /
printability / acceptance gates, the silhouette score/overlay, the match loop, and the
reference pre-processor were migrated and generalized from the `garage-band / lego-loco`
project's verification rig (copied, not moved; project-specific paths and hardcoded
cameras / part names removed). The methodology is documented in that project's research
report (§7 pixel-perfect workflow, §8 toolchain).
