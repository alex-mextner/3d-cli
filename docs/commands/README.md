# Command Surface

This directory documents the registered `3d <command>` surface. The dispatcher builds
`3d help` from `lib/commands/*.py`; this index mirrors that surface so docs can stay
organized without turning `README.md` into the whole manual.

Run `3d <command> --help` for the exact accepted flags and examples. Per-command docs here
explain why the command exists, what its inputs mean, and how it fits into the larger
[OpenSCAD](GLOSSARY.md#openscad)/[FDM](GLOSSARY.md#fdm) workflow.

## Current Surface

### Render & View

| Command | Doc | Role |
|---|---|---|
| `axis` | [axis.md](axis.md) | Validate named axes, section planes, views, and OpenSCAD camera vectors. |
| `animate` | [animate.md](animate.md) | Generate deterministic render frame plans or PNG frame sequences. |
| `render` | [render.md](render.md) | [CGAL](GLOSSARY.md#cgal) render, named views, multi-render, and cross-sections. |
| `preview` | [preview.md](preview.md) | Fast throwntogether preview, no CGAL render. |
| `video` | [video.md](video.md) | Render turntable videos or encode progress PNG artifacts. |
| `multi` | [multi.md](multi.md) | Thin compatibility wrapper for `render --multi`. |
| `section` | [section.md](section.md) | Thin compatibility wrapper for `render --section`. |

### Geometry & Export

| Command | Doc | Role |
|---|---|---|
| `export` | [export.md](export.md) | Format-aware [STL](GLOSSARY.md#stl)/[3MF](GLOSSARY.md#3mf)/USDZ export, format planning, and [manifold](GLOSSARY.md#manifold) validation when the mesh stack is available. |
| `generate` | [generate.md](generate.md) | Text description + named dimensions -> verified parametric `.scad` via an AI backend (generate/validate/render/check/fix loop). |
| `fit-niche` | [fit-niche.md](fit-niche.md) | Generate a parametric insert/plug that mates into a described cavity with FDM clearances. |
| `import` | [import.md](import.md) | Generate OpenSCAD wrappers or conversion plans for imported model formats. |
| `pack` | [pack.md](pack.md) | Plan deterministic print-bed layouts from explicit part footprints. |
| `prep` | [prep.md](prep.md) | Model to print-ready plate with cached steps: repair, orientation, no-support adaptation, fit table, stability, colour regions, layers, Orca project, slice, G-code gates, proof pack. |
| `props` | [props.md](props.md) | Snap-off support ribs for a part printed standing, with the height/base stability gate. |
| `validate` | [validate.md](validate.md) | Fast OpenSCAD parse check without rendering. |
| `params` | [params.md](params.md) | Extract Customizer-style parameters. |
| `om` | [om.md](om.md) | Query `.scad` object-model annotations as JSON. |
| `opdag` | [opdag.md](opdag.md) | Describe, plan, and query model operation DAGs. |
| `usdz` | [usdz.md](usdz.md) | Export `.scad`/`.stl` to colored USDZ for Apple AR Quick Look. |

### QA & Gates

| Command | Doc | Role |
|---|---|---|
| `check` | [check.md](check.md) | Master acceptance gate; runs all applicable gates by default. |
| `mesh` | [mesh.md](mesh.md) | Watertight/manifold/self-intersection/volume checks. |
| `halves-check` | [halves-check.md](halves-check.md) | Paired halves of a split part: cut faces, dowel holes, size and islands match in the assembly frame. |
| `fit-check` | [fit-check.md](fit-check.md) | Already-printed insert vs its socket: locked printed size, no overlap, gap per side all the way up. |
| `printability` | [printability.md](printability.md) | FDM wall, min-feature, overhang, and orientation checks. |
| `visible-faces` | [visible-faces.md](visible-faces.md) | Visible side of a decorative part prints clean: no unsupported layer piece (mesh) and no support or sagging island (G-code). |
| `collision` | [collision.md](collision.md) | Static, frame, and visualization collision checks from JSON config. |
| `kinematics` | [kinematics.md](kinematics.md) | Validate project joint specs and emit deterministic JSON. |
| `lint` | [lint.md](lint.md) | Advisory repository and OpenSCAD model lint rules. |
| `report` | [report.md](report.md) | Compose existing gate and metric artifacts into deterministic summaries. |
| `strength` | [strength.md](strength.md) | Validate structural load cases and emit deterministic dry-run reports. |
| `acceptance` | [check.md](check.md) | Alias declared by `check`. |

### Reference-Match Pipeline

For a practical figure-by-figure image backplate loop, see
[Reference Backplate Workflow](../workflows/reference-backplate.md).

| Command | Doc | Role |
|---|---|---|
| `compare` | [compare.md](compare.md) | Segmented model/reference comparison with [IoU](GLOSSARY.md#iou), [SSIM](GLOSSARY.md#ssim)/DSSIM, and artifacts. |
| `fit-camera` | [fit-camera.md](fit-camera.md) | Fit a camera pose to a reference by [silhouette](GLOSSARY.md#silhouette) IoU. |
| `proxy-align` | [proxy-align.md](proxy-align.md) | Align generated proxy meshes to CAD meshes for spatial camera priors. |
| `silhouette` | [silhouette.md](silhouette.md) | Camera-locked render to binary silhouette mask. |
| `score` | [score.md](score.md) | Silhouette [AE](GLOSSARY.md#ae) + IoU, printed as `KEY=VALUE` lines. |
| `overlay` | [overlay.md](overlay.md) | Difference, ghost, and edge-overlay diagnostics. |
| `match` | [match.md](match.md) | [Forced-monotonic](GLOSSARY.md#forced-monotonic-loop) param edit loop. |
| `preprocess` | [preprocess.md](preprocess.md) | Reference subject mask and proportional depth preprocessing. |

### Slicing

Every gate from model to printer, in order: [Print pipeline](../workflows/print-pipeline.md).

| Command | Doc | Role |
|---|---|---|
| `calibrate` | [calibrate.md](calibrate.md) | Calibrations per head × filament via 3d-api: what is needed (new material/vendor blocks, new lot warns, maintenance invalidates), record values, the `calibration_current` G-code gate; `gen` writes calibration prints locally (PA tower, flow tiles, shaper commands). |
| `gcode-thumbnails` | [gcode-thumbnails.md](gcode-thumbnails.md) | Check/add the printer-screen preview thumbnails a G-code carries (Orca CLI writes none). |
| `print` | [print.md](print.md) | Validate print fields and emit deterministic dry-run job plans. |
| `gcode-bounds` | [gcode-bounds.md](gcode-bounds.md) | Check every G-code move (arc bulges too) against axis travel and the printable area; offline. |
| `gcode-resume` | [gcode-resume.md](gcode-resume.md) | Resume a stopped multi-head print above the last intact layer (no Z homing); offline. |
| `gcode-overhangs` | [gcode-overhangs.md](gcode-overhangs.md) | Find extrusion printed on nothing, per object and layer (the slicer is silent with supports off); offline. |
| `mix` | [mix.md](mix.md) | Colour mixing by layers (Snapmaker Orca mixed filaments): `check` lints a project so every mixed part shows its colour on walls, not on a flat top. |
| `printer` | [printer.md](printer.md) | Drive configured printers (Moonraker, Creality stock): list, status, upload, start `--yes`, cancel `--yes`, `watch` (read-only anomaly events to stdout/exec/omp session), `vision` (camera spaghetti detection into the same events), `subscribe` / `agent` (alerts go to an AI agent, never straight to the user, [printer-subscribe.md](printer-subscribe.md)). |
| `print-preflight` | [print-preflight.md](print-preflight.md) | Read-only pre-print checks: every G-code gate (bounds, overhangs, slicer warnings, head sequence, mix face, thumbnails, visible faces), loaded colours vs G-code, halves, printer free. |
| `printops` | [printops.md](printops.md) | PrintOps through 3d-api (Spoolman + PrintOps DB on the Odroid): health, event log, tokens, deploy, backup; spools + ledger + weighing ([printops-spool.md](printops-spool.md)). |
| `profiles` | [profiles.md](profiles.md) | Keep OrcaSlicer / Snapmaker Orca / Bambu Studio user presets in the repo `profiles/`; three-way status/diff/sync, backups, refuses while the slicer runs. |
| `settings` | [settings.md](settings.md) | Printer settings-change journal via 3d-api: import the offline `printer-history` buffers (calibration lines become `calibrate` records, idempotent by uid), history per printer. |
| `slice` | [slice.md](slice.md) | Slice STL/3MF/SCAD to G-code or run the sliceability gate; records the slicer's warnings next to the G-code. |
| `slice-check` | [slice-check.md](slice-check.md) | Headless 3MF open/plates/slice gate, plus offline G-code checks (`--check head-sequence`). |
| `slice-warnings` | [slice-warnings.md](slice-warnings.md) | Record the slicer's warnings for a G-code (`NAME.slice-warnings.json`, SHA-256 bound) and accept each with a reason. |

### Libraries

| Command | Doc | Role |
|---|---|---|
| `libs` | [libs.md](libs.md) | OpenSCAD library path/list info; install is automatic on first run. |
| `models` | [models.md](models.md) | Search Printables, MakerWorld, Thingiverse and Cults3D; show a model; download a free model's files with a `model.json` (author, license, source). Paid models are never downloaded. |
| `openscad` | [openscad.md](openscad.md) | Install OpenSCAD libraries (WriteText, BOSL2, …) into OpenSCAD's user library folder for plain `openscad`. |
| `printers` | [printers.md](printers.md) | Inspect the merged printer registry. |
| `procurement` | [procurement.md](procurement.md) | Build deterministic purchase plans from local BOM and inventory files. |

### Environment

| Command | Doc | Role |
|---|---|---|
| `ai` | [ai.md](ai.md) | Build offline AI-assist prompt bundles without calling a backend. |
| `bench` | [bench.md](bench.md) | Auto-scored generative-modeling benchmark: prompt → `.scad`, build-success gate, metric vector, `$/ok` + `--compare`. |
| `auth` | [auth.md](auth.md) | Configure optional Hugging Face credentials for ZeroGPU and gated weights. |
| `doctor` | [doctor.md](doctor.md) | Read-only dependency report with install commands. |
| `heavy` | [heavy.md](heavy.md) | Run one command alone under the machine heavy lock with a memory cap (re-entrant, bounded wait). |
| `remote` | [remote.md](remote.md) | Push files to an ssh heavy box, run under its heavy lock, pull results. |
| `events` | [events.md](events.md) | Record and inspect append-only CLI/model workflow events. |
| `ha` | [ha.md](ha.md) | Deploy `integrations/homeassistant/` to the live Home Assistant config (submodule + symlinks, check_config, reloads, rollback); `ha vision` generates the per-printer camera-vision files from the fleet. |
| `init` | [init.md](init.md) | Scaffold a `3d.yaml` project and agent-friendly project skeleton. |
| `projects` | [projects.md](projects.md) | Register/list/remove project directories used by `3d web`. |
| `workspaces` | [workspaces.md](workspaces.md) | Create/list/read web dashboard workspace metadata. |
| `materials` | [materials.md](materials.md) | Inspect the merged FDM material registry. |
| `metrics` | [metrics.md](metrics.md) | Inspect persisted metrics; compute geometry (mesh) + perceptual (image) batteries. |
| `hardware` | [hardware.md](hardware.md) | List or validate local machine/toolchain capabilities. |
| `inventory` | [inventory.md](inventory.md) | Maintain a local materials and parts inventory. |
| `ollama` | [ollama.md](ollama.md) | Validate local Ollama settings and print a dry-run generate request. |
| `web` | [web.md](web.md) | Local FastAPI/SSE/three.js dashboard. |
| `worktree` | [worktree.md](worktree.md) | Create agent git worktrees with a bootstrapped dev `.venv`. |

## Adding Or Changing A Command

A command is one stdlib-only module under `lib/commands/<name>.py` with a module-level
`COMMAND = Command(...)`. Discovery imports every command on every `3d` invocation, so
command modules must keep heavy imports out of module top level. Use `cli.pyrun.run_tool`
or lazy imports inside `run()` for dependencies such as [`trimesh`](GLOSSARY.md#trimesh), `cv2`, `pyvista`,
`fastapi`, or external OpenSCAD/ImageMagick/slicer workflows.

Adding a command should normally change:

- `lib/commands/<name>.py`
- `docs/commands/<name>.md`
- focused tests for parsing, routing, smoke help, or the core behavior behind the command

It should not require edits to `bin/3d`, `lib/cli/dispatch.py`, or a central command list.
Aliases can be declared on the target command, as `check` does for `acceptance`, or exposed
as tiny forwarding modules, as `multi` and `section` do for `render`.

## Structured Errors

Commands should raise the structured errors in `lib/errors.py` instead of printing ad-hoc
messages or calling `sys.exit()`:

| Error | Exit | Use |
|---|---:|---|
| `MissingDependency` | 127 | Required tool/library is absent; include install command and what capability degrades. |
| `UsageError` | 2 | Wrong invocation shape, missing positional, or mode conflict. |
| `InvalidArgument` | 2 | Flag value is outside the accepted set; list accepted values. |
| `InputNotFound` | 2 | Required input file path does not exist. |
| `GateFailure` | 1 | A verification gate produced a FAIL verdict. |

The dispatcher catches `ThreeDError`, renders the actionable message, and returns the
error's exit code without a bare traceback.

## Project, Config, And Web Notes

`3d init` writes the project spine (`3d.yaml` plus standard directories and optional
agent assets). `3d projects` maintains the cross-project registry used by `3d web`.
`3d materials` and `3d printers` expose the names that projects reference in `3d.yaml`.

Global state uses one config directory and one data directory:

- `~/.config/3d-cli/` or `$XDG_CONFIG_HOME/3d-cli/`: bootstrap marker, `web.json`,
  projects registry, and user registry overrides.
- `~/.local/share/3d-cli/` or `$XDG_DATA_HOME/3d-cli/`: generated state such as metrics.
- `~/.local/state/3d-cli/` or `$XDG_STATE_HOME/3d-cli/`: runtime state such as the
  `3d printer watch` heartbeat files.

`3d web` is an optional frontend over the same core. The command module lazy-imports the
web tier so missing `fastapi`/`uvicorn` affects only `3d web`, not the CLI dispatcher or
offline help.

## Test And Review Workflow

For docs/help changes, run the command help smoke tests when possible:

```bash
dev run test -- -q
```

For code changes, keep the usual red/green loop and run the full `dev run test` gate before
claiming the work is complete. Before committing, self-review the diff and run the
repository-required peer review:

```bash
review -m codex -m gemini -m oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo
```

For staged changes, add `--staged` to that command. If both staged and unstaged changes
exist, review both diffs separately before committing. See `AGENTS.md` for fallback
reviewer commands and the minimum independent-reviewer bar.

Some task slices may require additional reviewers or narrower commands; follow the slice
instructions when they are stricter than the repository baseline.
