# `lib/` Architecture Map

Status: first-stage guardrail.

This map documents the current `lib/` layout and the intended package boundaries before
any broad file moves happen. The CLI depends on import-light command discovery, so the
safe first stage is to make the structure explicit, test it, and move modules later with
compatibility wrappers.

## Current Domains

### CLI / Dispatch

- `lib/cli/dispatch.py` routes `bin/3d` requests, renders structured errors, and keeps
  bootstrap / environment setup centralized.
- `lib/cli/registry.py` discovers `lib/commands/*.py` modules and reads each `COMMAND`.
- `lib/cli/env.py` owns tool discovery, install hints, and `OPENSCADPATH` export.
- `lib/cli/pyrun.py` runs heavier bundled Python tools through the selected Python tier.
- `lib/cli/paths.py` contains shared path helpers.
- `lib/errors.py` defines structured CLI errors and exit-code mapping.

### Commands

- `lib/commands/` is the only home for command modules.
- Each command module is import-light, exposes one module-level `COMMAND`, and delegates
  heavy work to lazy imports or `cli.pyrun`.
- Thin forwarding commands such as `multi` and `section` stay here because they are part
  of the command surface, not shared geometry code.

### Geometry / Render / Mesh

These modules are the current root-level implementation tools for model processing,
rendering, slicing support, and verification:

- `animation.py`
- `lib/geometry/axis.py`
- `arrange_pack.py`
- `collision_check.py`
- `collision_viz.py`
- `fit_camera.py`
- `fit_camera_math.py`
- `frame_check.py`
- `export_formats.py`
- `import_formats.py`
- `lib/geometry/mesh_metrics.py`
- `mesh_check.py`
- `niche_fit.py`
- `orca_project_3mf.py`
- `packing.py`
- `printability_mesh.py`
- `render.py`
- `strength.py`
- `usdz.py`
- `video.py`

Target package: `lib/geometry/`, with subpackages only when a real boundary appears
(`rendering`, `mesh`, `collision`, `export`).

### Slicing / Print Workflow

- `lib/slicing/printing.py`

Target package: `lib/slicing/` for slicer orchestration, dry-run print planning, printer
job state, and future sender integrations.

### Project / Config / Registries

These modules model project state, user config, and local registries:

- `ai_tools.py`
- `config.py`
- `events.py`
- `extract_params.py`
- `hardware.py`
- `hf_auth.py`
- `lib/registries/inventory.py`
- `kinematics.py`
- `linting.py`
- `model_lint.py`
- `lib/registries/materials.py`
- `lib/registries/metrics.py`
- `object_model.py`
- `ollama.py`
- `opdag.py`
- `procurement.py`
- `lib/registries/printers.py`
- `lib/registries/projects.py`
- `project.py`
- `reporting.py`
- `workspaces.py`

Target package: `lib/project/` for project/config/object-model behavior and
`lib/registries/` for materials, printers, project indexes, and metrics when those
boundaries are ready.

### Reference / Matching / Imaging

These modules support reference preprocessing, image comparison, scoring, and match-loop
orchestration:

- `debug_overlay.py`
- `match_loop.py`
- `mask_geometry.py`
- `preprocess_reference.py`
- `proxy_align.py`
- `refmatch.py`
- `perceptual_metrics.py`
- `spatial_fit_metrics.py`
- `lib/cli/imaging.py`

Target package: `lib/reference/`. `cli.imaging` may remain under `cli/` while command help
and dispatcher tests depend on it as a lightweight helper; move it only after callers can
use a compatibility import.

### Web

- `lib/web/` contains the `3d web` FastAPI/SSE/three.js application and its optional
  agent-log adapters.
- `lib/commands/web.py` remains the import-light command entry point and lazy-loads the
  optional web tier.

### Data

- `lib/data/materials.yaml`
- `lib/data/printers.yaml`

Data files stay under `lib/data/` until a packaging pass decides whether they should move
to a dedicated resources package.

## Root `lib/*.py` Inventory

Every current root Python module is listed here so future additions are intentional:

- `ai_tools.py` — offline AI-assist prompt bundle construction.
- `animation.py` — deterministic render frame planning.
- `arrange_pack.py` — split parts into connected bodies, shelf-pack them onto bed-sized plates, write either ONE multi-plate Orca/Bambu project 3MF (default) or one print-ready 3MF per plate (the `3d arrange` backend).
- `orca_project_3mf.py` — pure-stdlib writer for a single multi-plate OrcaSlicer/Bambu project `.3mf` (OPC zip: `[Content_Types].xml`, `_rels/.rels`, `3D/3dmodel.model` with positioned build items, `Metadata/model_settings.config` with plate assignments). Format matches the real OrcaSlicer exporter `_BBS_3MF_Exporter` in `src/libslic3r/Format/bbs_3mf.cpp`; used by `arrange_pack.py`'s single mode.
- `axis.py` — compatibility wrapper re-exporting `geometry.axis`.
- `collision_check.py` — geometry / collision check implementation.
- `collision_viz.py` — geometry / collision visualization implementation.
- `config.py` — project and CLI configuration helpers.
- `debug_overlay.py` — pure planning and advisory helpers for render/reference debug overlays.
- `errors.py` — structured CLI error model.
- `events.py` — append-only CLI/model workflow event store.
- `extract_params.py` — OpenSCAD parameter extraction.
- `export_formats.py` — export format registry, selector parsing, and dry-run export planning.
- `fit_camera.py` — camera fitting implementation.
- `fit_camera_math.py` — import-light camera pose and proof-status math helpers.
- `frame_check.py` — render framing checks.
- `hardware.py` — local machine and toolchain capability reporting.
- `hf_auth.py` — optional Hugging Face token loading, validation, and secure storage.
- `import_formats.py` — import wrapper generation and conversion-plan helpers.
- `inventory.py` — compatibility wrapper re-exporting `registries.inventory`.
- `kinematics.py` — project joint spec validation and deterministic summaries.
- `linting.py` — repository lint helpers.
- `model_lint.py` — OpenSCAD object-model metadata lint rules.
- `match_loop.py` — reference match-loop orchestration.
- `mask_geometry.py` — lightweight mask coverage, bounding-box, and centroid metadata.
- `materials.py` — compatibility wrapper re-exporting `registries.materials`.
- `mesh_check.py` — mesh verification implementation.
- `lib/geometry/mesh_metrics.py` — pinned-convention 3D shape metrics (Chamfer, F-score@tau, Hausdorff, normal consistency, volumetric IoU) between two meshes; backs `3d metrics geometry`.
- `perceptual_metrics.py` — perceptual/semantic image metrics (PSNR, LPIPS, CLIP-sim) with explicit senses and graceful degradation; backs `3d metrics perceptual`.
- `metrics.py` — compatibility wrapper re-exporting `registries.metrics`.
- `object_model.py` — semantic object-model structures.
- `ollama.py` — local Ollama endpoint validation and dry-run request planning.
- `opdag.py` — operation graph validation, dependency ordering, and query helpers.
- `niche_fit.py` — cavity/niche clearance math and parametric insert `.scad` emission.
- `packing.py` — deterministic 2D print-bed layout planning helpers.
- `printing.py` — compatibility wrapper re-exporting `slicing.printing`.
- `preprocess_reference.py` — reference image preprocessing.
- `printability_mesh.py` — printability mesh checks.
- `procurement.py` — deterministic local BOM and inventory purchase-plan helpers.
- `proxy_align.py` — generated proxy mesh to CAD mesh alignment and proof artifacts.
- `printers.py` — compatibility wrapper re-exporting `registries.printers`.
- `project.py` — project model and discovery.
- `projects_registry.py` — compatibility wrapper re-exporting `registries.projects`.
- `reporting.py` — deterministic gate and metric artifact report composition.
- `workspaces.py` — web dashboard workspace metadata registry.
- `refmatch.py` — image/reference matching helpers.
- `render.py` — OpenSCAD render and section implementation.
- `spatial_fit_metrics.py` — contour-first camera-fit diagnostics and warning metrics.
- `strength.py` — deterministic structural-check dry-run report helpers.
- `usdz.py` — USDZ export helpers.
- `video.py` — video turntable and progress-frame planning / encoding helpers.

Adding a new root `lib/*.py` file should be rare. Prefer a domain package. If a root file
is needed as a compatibility shim, document it in this inventory and keep it import-light.

Root compatibility wrappers currently allowed during the staged migration:

- `axis.py` → `geometry.axis`
- `inventory.py` → `registries.inventory`
- `materials.py` → `registries.materials`
- `metrics.py` → `registries.metrics`
- `printers.py` → `registries.printers`
- `printing.py` → `slicing.printing`
- `projects_registry.py` → `registries.projects`

## Staged Migration Plan

1. Keep `bin/3d`, `cli.dispatch`, and `cli.registry` stable. Command discovery must
   continue to glob only `lib/commands/*.py`.
2. Create one target package at a time, starting with the least coupled domain. Move the
   implementation module into that package and leave a root `lib/<name>.py` compatibility
   wrapper that re-exports the public API.
3. Keep compatibility wrappers stdlib-only where possible. If a wrapper must import the
   moved implementation, the moved implementation must be no heavier than the original
   module and must not affect command import-light guarantees.
4. Update internal imports gradually from the root module to the package path. Do not
   update command modules in a way that imports heavy dependencies at module top level.
5. Once tests and downstream callers no longer use a wrapper, remove that wrapper in a
   separate atomic change and update this map in the same commit.
6. After each package migration, run command smoke tests and import-light tests before
   broader verification.

## First-Stage Cleanup Decision

No implementation files are moved in this stage. The current root modules are widely
referenced by command modules, tests, and web code, so a broad move would create noisy
import churn and raise the risk of violating the command discovery contract. This stage
adds the map and tests that enforce the intended structure before the next migration step.
