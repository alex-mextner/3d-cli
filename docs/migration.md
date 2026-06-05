# Migration audit — source tools → `3d` subcommands

This maps every tool in the original `garage-band` workspace to its generalized
`3d` CLI subcommand (or records why it was intentionally dropped). The CLI tools
are **project-agnostic**: no hardcoded project paths, configs/inputs passed as
arguments. Verified with `grep -rniE 'garage-band|/Users/ultra|ejector|co2|inflator' lib/`
— the only hits are explanatory comments/examples, never behavior.

## `.claude/skills/openscad/tools/` (bash)

| Source tool | `3d` subcommand | Notes |
|---|---|---|
| `preview.sh` | `3d preview` | fast throwntogether render; also `3d render` for the CGAL (`--render`) path. |
| `multi-preview.sh` | `3d multi` | 6 angles (iso/front/back/left/right/top), `--render` for CGAL. |
| `section.sh` | `3d section` | true cross-section, 6-param **vector** camera + `--render` (never 7-param gimbal). |
| `section-color.sh` (+ `SECTION-COLOR.md`) | `3d section --color` | merged into `section`: `--color` = per-part coloured assembly section (color outside `difference()`, auto `-D cut=true`). Not a separate command. |
| `export-stl.sh` | `3d export` | STL/3MF **with** manifold/self-intersect/mesh validation; nonzero exit on bad geometry (stricter than the source, which only grepped warnings). |
| `validate.sh` | `3d validate` | fast syntax check (echo export, no render). |
| `extract-params.sh` | `3d params` | Customizer-style parameter extraction; `--json`. Backed by generic `lib/extract_params.py`. |
| `version-scad.sh` | **dropped — git versioning** | the source bumped `model_NNN.scad` indices in filenames. The CLI (and project policy) version through **git** instead — one file, history in commits/tags/branches. Index-in-filename versioning is deliberately not carried over. |
| `common.sh` | `lib/common.sh` | shared binary-location / symlink-safe `REPO_ROOT`; extended here with OS detection + the dependency table that `doctor`/`setup` share. |

## `tools/collision/` (python)

| Source tool | `3d` subcommand | Notes |
|---|---|---|
| `collision_check.py` | `3d collision <cfg>` | static gate: every part pair at every phase. |
| `frame_check.py` | `3d collision <cfg> --frame` | per-frame gate over the config's timeline. |
| `collision_viz.py` | `3d collision <cfg> --viz` | renders each phase with overlaps highlighted red. |
| `config.py` | `lib/config.py` | the JSON config loader (paths resolved relative to the config file). Not a user command; backs all three modes. |

All three collision modes are exposed by the single `3d collision` command
(`--frame` / `--viz` select the mode; default is the static gate). Thresholds,
part list, phases, intended-contact whitelist and the placement `.scad` all live
in the project's JSON config — nothing project-specific is compiled in.

## Match / verify / preprocess (already migrated — verified generic)

| Source mechanism | `3d` subcommand | Generic? |
|---|---|---|
| reference-match loop (`match_loop.py`) | `3d match` | yes — tunable params derived from the constants file; `--constants`, `--params`, `--metric`, `--dry-run`. |
| camera fit (`fit_camera.py`) | `3d fit-camera` | yes — **scale-free**: distance/pan bounds derived from the model's bbox diagonal (a 20 mm cube and a 300 mm assembly both fit, no hardcoded numbers). |
| silhouette / overlay / score | `3d silhouette` / `3d overlay` / `3d score` | yes — operate on any render+reference pair. |
| reference preprocessing (`preprocess_reference.py`) | `3d preprocess` | yes — SAM2/Depth-Anything if installable, OpenCV fallback. |
| mesh QA (`mesh_check.py`) | `3d mesh` | yes — watertight/manifold/self-intersect/volume on any STL/SCAD. |
| printability (`printability_mesh.py`) | `3d printability` | yes — FDM wall/feature/overhang/orientation, PLA/PETG. |
| master gate | `3d acceptance` | yes — manifold + consistency + printability, with collision/silhouette only when configured. |

## New in this CLI (no direct source tool)

| `3d` subcommand | Purpose |
|---|---|
| `3d doctor` | detect present/missing deps with the exact per-OS install command (read-only). |
| `3d setup` | install missing deps OS-aware (brew / apt / dnf / pacman + repo `.venv`), idempotent. |
| `3d slice` | slice a model to G-code via OrcaSlicer / Bambu Studio / PrusaSlicer (per-slicer invocation; core `-g`/`--slice` flags verified, `--printer` best-effort); `--check` is a sliceability gate that discards the G-code. |
| `3d libs` | clone/manage OpenSCAD libraries (BOSL2, NopSCADlib) into `libs/`. |
| `3d fit-camera` | (see above) camera-pose fit to a reference photo by silhouette IoU. |

## Verdict

Every source tool is either exposed as a `3d` subcommand or intentionally dropped
(`version-scad` → git versioning). The collision engine exposes static + `--frame`
+ `--viz`. No gaps remain; nothing project-specific is hardcoded.
