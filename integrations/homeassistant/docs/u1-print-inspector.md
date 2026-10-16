# U1 Print Inspector — deployed implementation

19 September 2026. This document describes the implemented local inspector, not the proposed conversation-to-object platform.

## What is installed

The existing U1 JPEG/MJPEG proxy now polls read-only Moonraker telemetry, indexes the selected annotated G-code and renders an information strip into camera frames. Home Assistant and anonymous signed-camera links receive the same rendered pixels. The viewer does not need a new token or a new public endpoint for telemetry. The existing Python sharing command is unchanged.

The 3D Printers dashboard (`/3d-printers/overview`) includes **Что делает U1** in the U1 section. The card shows operation provenance, freshness, layer count, physical head/material and temperatures. Enter a layer number and press **Разобрать слой** to view its planned features and explanations. **Текущий слой** returns to following the current or last-reported layer.

## Source and confidence contract

Firmware machine/action codes take precedence over inferred G-code features. Paused, failed, unavailable and completed states never inherit a stale “ironing” label from the last parsed feature. Unknown action codes remain identified but unexplained.

A `gcode_cursor` operation is an estimate from the file read position, not proof of the exact physical nozzle action. The UI uses an approximation marker and explains buffering. When the cursor layer differs from the printer-reported layer, both the mismatch and its limitation are reported. No estimate of buffer duration is fabricated.

A layer answer is a **plan**, not a claim that every listed feature is currently happening. Ironing comes from slicer feature annotations, never merely from low speed. The exposed positive extrusion length includes positive E increments/unretractions; it is not material mass or net consumption. Tool identifiers in the G-code are logical; the currently reported physical tool and material come from live printer objects. Heads are named T1..T4 as on the printer (Klipper index 0..3); a G-code tool change is shown with the head that `print_task_config.extruder_map_table` assigns to it.

`complete` means the printer reported completion. It does not certify dimensional accuracy, quality, successful collection or delivery.

## Ghost of the rest of the print

While a job prints, the proxy draws what is still to be printed over the camera image: a semi-transparent 3D ghost of every wall above the current layer, in the filament colour of its tool (3d-cli issue #261). The same G-code pass that builds the index collects the walls (outer and overhang walls, prime tower) with 3d-cli's `lib/vision_ghost.py`; the scripts are symlinks into the 3d-cli submodule, so the library is imported from `<3d-cli>/lib` next to them. The ghost is projected through the bundled U1 camera calibration (`lib/data/vision/u1-camera.yaml`, the same model `3d printer vision` uses).

How it reads: the walls are painted as vertical quads in depth order, so a near ghost wall covers a far one. The printed part occludes too, but only where it can: seen from a camera below the current top it hides ghost walls behind it; the U1 camera sits ~13 mm above the nozzle plane, so every sight line to the ghost passes above the printed part and only the band printed now is painted (for its glow). Ghost walls take the filament colour washed towards a pale cyan (so they never pass for the real part), are shaded by how squarely they face the camera, fade from 80 % opacity near to 28 % far, get a 2 px light outline at silhouettes and depth steps and a light diagonal hatch; the band printed now glows amber.

Cost: collecting the walls rides the existing index pass — on the ODROID-N2+ (HA container, 1 CPU) the 135 MB `u1_walls_main_v3` indexed in 122 s without and 142 s with the collector (718 k wall points), well inside the 60 s + 4 s/MiB deadline. The ghost is then rendered once per (job, layer, frame size) on its own worker thread and cached — about 2 s for layer 729 of that job; each frame only blends the cached table (about 20 ms at 1920 x 1080). While the next layer renders, the previous ghost stays up if it is at most 1 mm below the current layer (a few layers, under a pixel at this camera's scale); before the first render of a job the frame has no ghost. Fail-safe: no ghost when the switch is off, the library or calibration is missing, the shell was truncated, the snapshot is stale, the state is not printing/paused, or the printer's file cursor does not map into the indexed file — never a ghost of another job, nor one more than 1 mm behind the current layer.

Switch: `input_boolean.u1_camera_ghost` (tile under the U1 camera on the 3D Printers dashboard) is the authority. An automation sends it to the proxy (`rest_command.u1_camera_ghost_set` → `POST /ghost`) on change, at HA start and every 5 minutes, because the proxy starts with `U1_GHOST` (default on) after a container restart. HA restores the helper's last state; a newly created helper starts off, so turn it on once after the first deploy. Public share links receive the same pixels, ghost included. Coordinates reach the ghost as absolute millimetres (G90/G91, G20/G21 and G92 are applied; G2/G3 arcs keep their curvature). The K1 SE has no ghost yet: it needs its own camera calibration and a frame path through a proxy like this one.

## Read-only API

The proxy exposes these routes only on its existing internal Docker network:

- `GET /status.json`: normalised current observation, source, staleness, active/last artifact fingerprint, index summary and current-layer plan.
- `GET /layers/186?job=<gcode_sha256>`: the selected layer plan guarded by the current artifact identity. HTTP 409 means the artifact changed; 404 means no such layer; 503 means the index is not ready.
- `GET /ghost`: the ghost switch and render state (`enabled`, last rendered `layer` and whether it produced a ghost `layer_has_ghost`, `rendering_layer`, `render_seconds`, last `error` with its message). `POST /ghost` with `{"enabled": true|false}` is the one write route; it only switches the drawing, never the printer.

HA wraps them in `rest_command.u1_inspector_status` and `rest_command.u1_inspect_layer`. Both support service responses and were tested with the dedicated non-admin, read-only HA sharing account. A WS request looks like:

```json
{"id": 1, "type": "call_service", "domain": "rest_command", "service": "u1_inspect_layer", "service_data": {"layer": 186, "job": "SHA256_FROM_STATUS"}, "return_response": true}
```

The normalised response is under `response.content`, with `response.status` carrying the HTTP status. The browser uses HA authentication; no credentials are embedded in dashboard YAML, public JavaScript or images.

## Runtime behaviour and bounds

Telemetry is polled every 2 seconds; the HA card requests the cached projection every 5 seconds while visible. More than 12 seconds without a successful observation is stale. Cached camera frames older than 15 seconds are not served as live frames. The proxy renders once per fetched frame and shares that result with viewers; it does not re-index G-code for each viewer.

A single worker indexes the current/last selected file. Files are fetched from the fixed configured printer origin with GET requests; redirects and path traversal are rejected. Text `.gcode`, `.gco` and `.gc` files are supported, up to 512 MiB (multi-head jobs pass 128 MiB: a 1193-layer four-head job is 135 MB). Indexing runs at about 0.8 MB/s on the ODROID-N2+, so the deadline scales with file size (60 s + 4 s per MiB); a failed index is retried after 10 minutes, not every metadata check. Until the index is ready the operation reads «Печать слоя N» from the printer's own layer counter. Binary G-code, archives, 3MF, oversized lines and excessive index structures are rejected. File size, metadata changes, print restart/cursor reset and filename changes invalidate the index; SHA-256 identifies the parsed artifact. Metadata is rechecked every 30 seconds, not continuously.

The index provides layer heights, feature labels/explanations, special commands, logical tools, movement-command counts and byte ranges. It is not a complete motion simulator, geometry validator or per-feature duration estimator. Remaining time is a labelled slicer M73 estimate when available, not a guaranteed completion time.

The recent-event ring and G-code index are memory-only. This is not a durable audit log or a production-attempt database. No high-frequency HA sensors were added, so this feature does not write its polling stream into HA's recorder.

## Security and scope

No router ports, DNS records, public service or external AI upload were added. Guest images do not include filenames or raw G-code. The internal JSON API has network isolation rather than separate authentication; it must not be published directly. Existing expiry rules continue to be enforced by Home Assistant on each new signed-image request.

Current links are scoped to the **camera**, not to an order or print attempt. Until they expire, they may show a subsequent job. Order-scoped revocable permissions are a required future platform feature before multi-customer sharing. A G-code hash identifies content, not a unique run of that content.

## Files and deployment

New Python modules: `scripts/u1_gcode_index.py`, `scripts/u1_print_inspector.py`, `scripts/u1_ghost.py` (ghost worker; imports 3d-cli `lib/vision_ghost.py` and `lib/vision_geometry.py`). Updated proxy: `scripts/u1_mjpeg_proxy.py`. New UI: `www/u1-share/u1-inspector-card.js`. New HA package: `packages/u1_print_inspector.yaml`. Dashboard/resource registration is in the existing YAML files.

The proxy uses Pillow from the existing HA container image. `/usr/share/fonts` on the host is mounted read-only for the existing DejaVu font; no font binary is distributed in the source bundle.

Pre-change backup: `/home/ultra/.local/share/u1-share/inspector-backup-20260919T175906Z`. Only the camera proxy was recreated. HA resources and REST commands were reloaded through the HA API; HA and the printer were not restarted. The inspector never starts, pauses, cancels or changes a print.

For rollback, compare the named backup with current files and reverse only inspector changes. Do not blindly restore a backup over newer work. Validate YAML and all Lovelace dashboards, recreate only the proxy if its code/mount changed, and reload REST commands/resources through HA. Do not modify `.storage`, databases, credentials or unrelated submodules.

## Evidence collected on the real device

The selected 3,132,992-byte Snapmaker Orca 2.3.5 file indexed 250 layers in 1.076 seconds, including download in that measured call. It contained ironing on layers 186, 187, 210 and 244–250. Layer 186 included inner/outer walls, sparse infill and ironing. The printer was complete at layer 250, and the live operation correctly said **Печать завершена**.

The read-only HA account successfully queried status and layer 186. An incorrect artifact fingerprint produced HTTP 409. Public signed JPEG access returned HTTP 200 with the overlay, and the same URL returned HTTP 403 after expiry; the anonymous viewer cleared the frame.

There was no active physical ironing operation during these checks. Ironing classification is verified against actual file annotations and automated replay tests, not by claiming to have observed the moving printer. See `u1-print-inspector-verification.md` for final automated and browser checks.

## Primary references

- Klipper status fields: https://www.klipper3d.org/Status_Reference.html
- Moonraker file metadata and downloads: https://moonraker.readthedocs.io/en/latest/external_api/file_manager/
- Snapmaker firmware state/action definitions: https://github.com/Snapmaker/u1-klipper/blob/main/klippy/extras/machine_state_manager.py

Mappings were checked on 19 September 2026; future firmware/slicer changes require regression checks. Local measured results above come from the deployed device rather than those general references.
