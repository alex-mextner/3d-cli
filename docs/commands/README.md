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
| `multi` | [multi.md](multi.md) | Thin compatibility wrapper for `render --multi`. |
| `section` | [section.md](section.md) | Thin compatibility wrapper for `render --section`. |

### Geometry & Export

| Command | Doc | Role |
|---|---|---|
| `export` | [export.md](export.md) | [STL](GLOSSARY.md#stl)/[3MF](GLOSSARY.md#3mf) export with [manifold](GLOSSARY.md#manifold) validation when the mesh stack is available. |
| `import` | [import.md](import.md) | Generate OpenSCAD wrappers or conversion plans for imported model formats. |
| `validate` | [validate.md](validate.md) | Fast OpenSCAD parse check without rendering. |
| `params` | [params.md](params.md) | Extract Customizer-style parameters. |
| `om` | [om.md](om.md) | Query `.scad` object-model annotations as JSON. |
| `usdz` | [usdz.md](usdz.md) | Export `.scad`/`.stl` to colored USDZ for Apple AR Quick Look. |

### QA & Gates

| Command | Doc | Role |
|---|---|---|
| `check` | [check.md](check.md) | Master acceptance gate; runs all applicable gates by default. |
| `mesh` | [mesh.md](mesh.md) | Watertight/manifold/self-intersection/volume checks. |
| `printability` | [printability.md](printability.md) | FDM wall, min-feature, overhang, and orientation checks. |
| `collision` | [collision.md](collision.md) | Static, frame, and visualization collision checks from JSON config. |
| `lint` | [lint.md](lint.md) | Advisory repository lint rules. |
| `acceptance` | [check.md](check.md) | Alias declared by `check`. |

### Reference-Match Pipeline

For a practical figure-by-figure image backplate loop, see
[Reference Backplate Workflow](../workflows/reference-backplate.md).

| Command | Doc | Role |
|---|---|---|
| `compare` | [compare.md](compare.md) | Segmented model/reference comparison with [IoU](GLOSSARY.md#iou), [SSIM](GLOSSARY.md#ssim)/DSSIM, and artifacts. |
| `fit-camera` | [fit-camera.md](fit-camera.md) | Fit a camera pose to a reference by [silhouette](GLOSSARY.md#silhouette) IoU. |
| `silhouette` | [silhouette.md](silhouette.md) | Camera-locked render to binary silhouette mask. |
| `score` | [score.md](score.md) | Silhouette [AE](GLOSSARY.md#ae) + IoU, printed as `KEY=VALUE` lines. |
| `overlay` | [overlay.md](overlay.md) | Difference, ghost, and edge-overlay diagnostics. |
| `match` | [match.md](match.md) | [Forced-monotonic](GLOSSARY.md#forced-monotonic-loop) param edit loop. |
| `preprocess` | [preprocess.md](preprocess.md) | Reference subject mask and proportional depth preprocessing. |

### Slicing

| Command | Doc | Role |
|---|---|---|
| `slice` | [slice.md](slice.md) | Slice STL/3MF/SCAD to G-code or run the sliceability gate. |

### Libraries

| Command | Doc | Role |
|---|---|---|
| `libs` | [libs.md](libs.md) | OpenSCAD library path/list info; install is automatic on first run. |
| `printers` | [printers.md](printers.md) | Inspect the merged printer registry. |

### Environment

| Command | Doc | Role |
|---|---|---|
| `ai` | [ai.md](ai.md) | Build offline AI-assist prompt bundles without calling a backend. |
| `doctor` | [doctor.md](doctor.md) | Read-only dependency report with install commands. |
| `events` | [events.md](events.md) | Record and inspect append-only CLI/model workflow events. |
| `init` | [init.md](init.md) | Scaffold a `3d.yaml` project and agent-friendly project skeleton. |
| `projects` | [projects.md](projects.md) | Register/list/remove project directories used by `3d web`. |
| `materials` | [materials.md](materials.md) | Inspect the merged FDM material registry. |
| `metrics` | [metrics.md](metrics.md) | Inspect persisted command metrics JSONL records. |
| `hardware` | [hardware.md](hardware.md) | List or validate local machine/toolchain capabilities. |
| `inventory` | [inventory.md](inventory.md) | Maintain a local materials and parts inventory. |
| `web` | [web.md](web.md) | Local FastAPI/SSE/three.js dashboard. |
| `test` | [test.md](test.md) | Ruff, pytest, and mypy gate. |

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

`3d web` is an optional frontend over the same core. The command module lazy-imports the
web tier so missing `fastapi`/`uvicorn` affects only `3d web`, not the CLI dispatcher or
offline help.

## Test And Review Workflow

For docs/help changes, run the command help smoke tests when possible:

```bash
./bin/3d test -q
```

For code changes, keep the usual red/green loop and run the full `3d test` gate before
claiming the work is complete. Before committing, self-review the diff and run the
repository-required peer review:

```bash
timeout 1200 codex exec review --uncommitted
```

Some task slices may require additional reviewers or narrower commands; follow the slice
instructions when they are stricter than the repository baseline.
