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
- `lib/cli/heavy.py` is the machine-wide heavy lock (`flock` + `systemd-run` cap), re-entrant through `THREED_HEAVY_HELD` with a bounded wait; backs `3d heavy` and every heavy run inside `3d prep` / `slicing.orca_cli`.
- `lib/cli/remote.py` builds the rsync/ssh argv and loads `~/.config/3d-cli/remote.yaml` for `3d remote`.
- `lib/cli/resources.py` sizes every worker pool and caps native thread pools
  (`THREED_MAX_WORKERS`, default `min(6, cores / 2)`); the dispatcher applies it first.
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
- `fit_check.py`
- `frame_check.py`
- `gcode_resume.py`
- `gcode_labels.py`
- `gcode_overhangs.py`
- `gcode_thumbnails.py`
- `export_formats.py`
- `import_formats.py`
- `lib/geometry/mesh_metrics.py`
- `halves_check.py`
- `mesh_check.py`
- `niche_fit.py`
- `orca_project_3mf.py`
- `packing.py`
- `printability_mesh.py`
- `render.py`
- `standing_props.py`
- `strength.py`
- `usdz.py`
- `visible_faces.py`
- `video.py`

Target package: `lib/geometry/`, with subpackages only when a real boundary appears
(`rendering`, `mesh`, `collision`, `export`).

### Slicing / Print Workflow

- `lib/slicing/printing.py`
- `lib/slicing/orca_cli.py` — the one place for Snapmaker Orca / OrcaSlicer CLI knowledge: binary discovery, the slicing copy (no `<plate>` block), headless `slice_project` under the heavy lock, failure classification and the `KNOWN_QUIRKS` catalogue behind `docs/printers/u1-orca-cli.md` (#431).
- `lib/prep/` — `3d prep` (#431): `pipeline.py` (ordered, cached steps; `cache.py`), `steps.py` (adapters), `cli.py` (flags + spec file), and one library per step: `meshfix.py` (diagnose/repair + backend interface for the public meshfix library), `orient.py`, `overhang.py`, `adapt.py` (cavity fill, 45 degree wedges), `voxel.py` (scan-converted occupancy), `fit.py` (item-table fit envelope), `stability.py`, `regions.py` (landmark boxes -> colour parts/mixes), `layers.py` (adaptive layer-height plan), `project3mf.py` (Orca project 3MF + manifest), `gcode_gates.py` / `gcode_view.py`, and the proof pack (`raster.py`, `panels.py`, `report.py`, `proof.py`). Results follow `contracts.StepResult`.
- `prep_cli.py` — entry script `3d prep` runs through `cli.pyrun` with the prep dependencies (`uv sync --extra prep`).
- `lib/profiles/doctor.py` — `3d profiles doctor`: static checks of user presets (parents, layer-height limits, `.info`).
- `lib/printer_ops/` — real-printer control behind `3d printer` / `3d print-preflight`:
  adapter contract + status model (`base.py`), stdlib WebSocket client (`ws.py`),
  `moonraker.py` (Klipper/Moonraker, Snapmaker U1; also the read-only job queue and history),
  `creality.py` (Creality K1 stock firmware), `devices.py` (`printer-devices.yaml`),
  `dossier.py` / `dossier_render.py` / `webui.py` / `render.py` (`3d printers show NAME`: name
  resolution, the concurrent read-only live reads, the web UI probe, the text; `render.py` is
  the status text shared with `3d printer status`), `preflight.py` (pre-print checks),
  `move_bounds.py` (G-code moves and arc extremes vs axis travel / printable area),
  `watch.py` / `watch_run.py` / `watch_sinks.py` (`3d printer watch`: detectors, streams,
  the event Dispatcher and its staged sinks; agents first, `tg` only as the emergency
  `watch.tg_fallback`), `subscriptions.py` (`3d printer subscribe`: the agent-subscription
  registry `subscriptions.yaml` and the `SubscriptionSink` every watcher adds), and
  `printer_agent.py` (`3d printer agent`: the temporary per-printer omp agent in tmux for
  alerts no live subscriber took, and its handoff).
- `lib/slicing/head_sequence.py` — per-object, per-layer toolhead check of sliced G-code against the project 3MF's mixed-filament patterns (`slice-check --check head-sequence`; `preflight_check()` is the `head seq` gate of `print-preflight`, #103).
- `lib/slicing/mix_faces.py` — every part printed in a mixed filament (pattern or Local-Z row) of an Orca-family project 3MF shows its colour on walls: face areas by normal in bed coordinates, FAIL when the top outweighs half the walls (`3d mix check`; `preflight_check()` is the `mix face` gate of `print-preflight`, #411).
- `lib/slicing/slice_warnings.py` — the slicer's warnings for one G-code (Orca-family CLI log, result.json, slice_info of a sliced 3MF) in `NAME.slice-warnings.json` with the G-code's SHA-256 and a reason per accepted warning; backs `3d slice-warnings` and the `slicer warn` gate of `print-preflight`.
- `lib/printops/` — PrintOps (epic #154) behind `3d printops` and the 3d-api service
  (`services/3d-api/`): `db.py` (SQLite + `migrations/NNNN_<slice>.sql`), `events.py`
  (append-only event log + in-process bus), `tokens.py` (bearer tokens, sha256 file),
  `spoolman.py` (Spoolman v0.27 Protocol, urllib client, extra-field registry), `client.py`
  (HTTP client for frontends), `settings.py`, `backup.py`, `deploy.py`; `api/` is the FastAPI
  app (auto-includes `routes_*.py`), `cli/` the self-registering `3d printops` subs.
  Spools (#160/#163): `spools.py` (lifecycle receive/move/load/unload/use/measure/empty/import),
  `ledger.py` (append-only usage ledger → remaining as an interval), `tares.py` (sourced
  empty-spool weight DB + per-spool tare chain), `importers.py` (`gen/plates.json` → import
  items); `datalint.py` runs every `lint_<slice>.py` data rule (`lint_spools.py`) for
  `GET /v1/lint` / `3d lint --printops`. `head_sync.py` (#400): after `load`, writes the
  printer head to show the spool (`Adapter.set_head_filament`, U1 `SET_PRINT_FILAMENT_CONFIG`)
  and puts back the current pressure-advance calibration the firmware's K reset drops.
  Slot reconciliation (#161): `slots.py` (read-only per-adapter slot readers: U1
  `print_task_config` + `filament_detect`, K1 SE ws runout switch), `color.py` (hex → CIELAB,
  CIEDE2000), `reconcile.py` (Spoolman `printops_slot` bindings vs live slots, plus failed head
  writes from the event log → `anomaly` rows + `slot.mismatch` / `anomaly.*` events), `api/routes_reconcile.py` (`/v1/printers`,
  `/v1/anomalies`, background poller via router lifespan).
  Incidents (#165): `incidents.py` (structured incidents, lifecycle, rates per kg / print-hour),
  `incident_context.py` (auto-context: printer status, G-code state at the cursor via
  `gcode_resume.state_at`, slot spool → lot, drying), `exposure.py` (printing exposure per head
  from Moonraker job history / manual rows), `watch_incidents.py` (drafts from `3d printer watch`
  events), `printer_read.py` (read-only printer access: status, Moonraker GET, G-code download).
  Printer tasks (#281): `printer_tasks.py` (printer_task rows, derivation from faults / due
  rules / calibration requirements, the buffer import), `printer_task_buffer.py` (the offline
  JSONL buffer `<cli.paths.data_dir()>/printer-todo/<printer>.jsonl`), `api/routes_tasks.py`
  (`/v1/printers/{p}/tasks`, `/v1/tasks…`), `todo_cli.py` (`3d printer todo`).
- `lib/topcam/` — the top camera (#340–#343): `v4l2.py` (pure-Python V4L2 controls + mmap YUYV capture, `locked()` resets to camera defaults), `focus.py` (focus modes, software sweep), `node.py` (the Pi Zero W camera-node HTTP service; `NODE_FILES` ship to the Pi), `client.py` (usb/node backends), `frames.py` (YUYV → RGB, averaging, PNG), `registry.py` (3d-api camera-node lookup), `photo.py`/`photo_config.py`/`photo_cli.py` (`3d calibrate photo|rectify`: pose, light, capture, fleet `photo:`).
- `lib/profiles/` — slicer user presets kept in the repo `profiles/` (#180, PrintOps D10), behind `3d profiles`: `slicers.py` (OrcaSlicer / Snapmaker Orca / Bambu Studio data dirs, running check), `preset.py` (preset JSON / `.info` formats, slicer-local keys), `sync.py` (three-way repo <-> slicer sync, base snapshot, backups), `values.py` (`apply_values`, the calibration write path).

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
- `lib/registries/openscad_libs.py` — known OpenSCAD libraries + install/uninstall into
  OpenSCAD's user library folder (backs `3d openscad libs`)
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
- `fit_check.py` — already-printed insert vs its socket: printed-size lock, per-layer clearance (manifold3d slices, shapely distances) and the open path along the insertion direction; backs `3d fit-check`.
- `gcode_resume.py` — resume a stopped multi-head Orca print above a layer (layer walk, machine state at a byte, resume header from the device's `resume:` profile, per-head retraction tracking, `--no-tower` off-bed purges); stdlib-only; backs `3d gcode-resume`. Its per-line parser (`advance`) and the streamed `state_at` (machine state at a printer's byte cursor or a layer) also give PrintOps incidents their G-code context.
- `gcode_labels.py` — the slicer's own labels in a G-code: object labels in both dialects (Klipper `EXCLUDE_OBJECT_*`, Bambu Studio `; start printing object` / `M624`…`M625` blocks named by their `; printing object` comment, `M486`, PrusaSlicer comments), layer-change markers, feature tags and line widths; stdlib-only; the one parser behind head-sequence, overhangs, visible-faces and the print-area plate section.
- `gcode_overhangs.py` — extrusion printed on nothing per object and layer of a sliced G-code (numpy/scipy raster of each layer's carrier, sparse reach/bridge/step-out tests on the pixels that stick out, layers split over forked workers); backs `3d gcode-overhangs` and the `overhangs` gate of `print-preflight`.
- `gcode_thumbnails.py` — printer-screen thumbnail blocks in G-code: stdlib-only check against the profile's `thumbnails` spec, Orca-format block writer, z-buffered toolpath preview render (numpy); backs `3d gcode-thumbnails`.
- `frame_check.py` — render framing checks.
- `ha_deploy.py` — `3d ha deploy`: manifest/snippet/compose/vendor checks for `integrations/homeassistant/`, plan from a host probe, backup → submodule checkout → symlinks/copies → check_config → reloads → service recreate → post-checks with rollback, HA-repo deploy PR.
- `ha_deploy_probe.py` — stdlib-only, read-only probe that `3d ha deploy` runs with the HA host's `python3` over ssh (git/submodule state, dest hashes, compose hashes and mounts, vendor versions, HA API/dashboard/go2rtc signals).
- `hardware.py` — local machine and toolchain capability reporting.
- `hf_auth.py` — optional Hugging Face token loading, validation, and secure storage.
- `img3d_loop.py` — closed image→3D parametric-recovery loop: generate a blockout, view-bank contour fit, monotonic boundary refine with a semantic-feature veto dispose-gate, proof-status gate, and the synthetic parametric-recovery milestone (the `3d recover-blockout` backend).
- `import_formats.py` — import wrapper generation and conversion-plan helpers.
- `inventory.py` — compatibility wrapper re-exporting `registries.inventory`.
- `kinematics.py` — project joint spec validation and deterministic summaries.
- `linting.py` — repository lint helpers.
- `model_lint.py` — OpenSCAD object-model metadata lint rules.
- `match_loop.py` — reference match-loop orchestration.
- `mask_geometry.py` — lightweight mask coverage, bounding-box, and centroid metadata.
- `materials.py` — compatibility wrapper re-exporting `registries.materials`.
- `mesh_check.py` — mesh verification implementation.
- `halves_check.py` — paired-halves gate: loads pairs marked in 3MF object metadata (or STL pairs), maps both halves into the assembly frame, rasterises their cut faces and compares outline, dowel holes, size and islands; backs `3d halves-check`.
- `vision_detect.py` — pixel half of `3d printer vision`: Obico ONNX spaghetti scorer (weights downloaded at runtime, sha256-pinned), `FrameScorer` (ROI/ignore masks + stage-2 verifier), brightness check, live camera loop, `record` and `eval` on recorded footage; decision logic and camera/klippy.log access live in `printer_ops/vision.py`.
- `vision_verify.py` — stage 2 of `3d printer vision`: zero-shot CLIP ViT-B/32 (ONNX, sha256-pinned) on crops of the detector's boxes → verified score q.
- `calib_rectify.py` — calibration photo → rectified bed-mm image: ArUco fiducials (DICT_4X4_50), homography, external scale reference (`3d calibrate photo|rectify`, #342).
- `vision_geometry.py` — printer camera model (pinhole/fisheye), bed/volume/silhouette masks at the current Z, calibration fit CLI; the U1 calibration ships in `lib/data/vision/`.
- `vision_ghost.py` — 3D ghost of the rest of a print over a printer camera frame: collects a job's walls from one G-code pass, paints them through a `vision_geometry` camera into a sparse per-frame blend table (used by the U1 camera proxy).
- `vision_scene.py` — opt-in bed-change detectors for `3d printer vision --scene` (foreign object, moved part, no growth).
- `bed_state.py` — pixel half of the printer bed state (#303): empty-bed references per Z, robust photometric difference in the bed ROI, clear/occupied + plate classifier, change-gated monitor loop (`3d printer bed`, the `3d printer watch` child); config, events, start gate and clear ack live in `printer_ops/bed.py`.
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
- `visible_faces.py` — visible-face gate: per-layer unsupported pieces on a declared visible half-space of the unioned part (manifold3d), and Support moves / sagging islands in the sliced G-code (scipy raster); bridges inside declared openings allowed up to a span.
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
- `standing_props.py` — snap-off props for a part printed standing (the `3d props` backend): per-station back profile from manifold3d slices, sloped ribs with a straight inner edge behind the hindmost back point, tabs, foot strip, keep-out of the part dilated by the gap, height/base stability ratio; reads STL/3MF (core + production components, Orca part extruders) and writes a 3MF with the props as an extra part of the object.
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
