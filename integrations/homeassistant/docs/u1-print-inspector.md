# U1 Print Inspector — deployed implementation

19 September 2026. This document describes the implemented local inspector, not the proposed conversation-to-object platform.

## What is installed

The existing U1 JPEG/MJPEG proxy now polls read-only Moonraker telemetry, indexes the selected annotated G-code and renders an information strip into camera frames. Home Assistant and anonymous signed-camera links receive the same rendered pixels. The viewer does not need a new token or a new public endpoint for telemetry. The existing Python sharing command is unchanged.

The 3D Printers dashboard (`/3d-printers/overview`) includes **Что делает U1** in the U1 section. The card shows operation provenance, freshness, layer count, physical head/material and temperatures. Enter a layer number and press **Разобрать слой** to view its planned features and explanations. **Текущий слой** returns to following the current or last-reported layer.

## Source and confidence contract

Firmware machine/action codes take precedence over inferred G-code features. Paused, failed, unavailable and completed states never inherit a stale “ironing” label from the last parsed feature. Unknown action codes remain identified but unexplained.

A `gcode_cursor` operation is an estimate from the file read position, not proof of the exact physical nozzle action. The UI uses an approximation marker and explains buffering. When the cursor layer differs from the printer-reported layer, both the mismatch and its limitation are reported. No estimate of buffer duration is fabricated.

A layer answer is a **plan**, not a claim that every listed feature is currently happening. Ironing comes from slicer feature annotations, never merely from low speed. The exposed positive extrusion length includes positive E increments/unretractions; it is not material mass or net consumption. Tool identifiers in the G-code are logical; the currently reported physical tool and material come from live printer objects.

`complete` means the printer reported completion. It does not certify dimensional accuracy, quality, successful collection or delivery.

## Read-only API

The proxy exposes these routes only on its existing internal Docker network:

- `GET /status.json`: normalised current observation, source, staleness, active/last artifact fingerprint, index summary and current-layer plan.
- `GET /layers/186?job=<gcode_sha256>`: the selected layer plan guarded by the current artifact identity. HTTP 409 means the artifact changed; 404 means no such layer; 503 means the index is not ready.

HA wraps them in `rest_command.u1_inspector_status` and `rest_command.u1_inspect_layer`. Both support service responses and were tested with the dedicated non-admin, read-only HA sharing account. A WS request looks like:

```json
{"id": 1, "type": "call_service", "domain": "rest_command", "service": "u1_inspect_layer", "service_data": {"layer": 186, "job": "SHA256_FROM_STATUS"}, "return_response": true}
```

The normalised response is under `response.content`, with `response.status` carrying the HTTP status. The browser uses HA authentication; no credentials are embedded in dashboard YAML, public JavaScript or images.

## Runtime behaviour and bounds

Telemetry is polled every 2 seconds; the HA card requests the cached projection every 5 seconds while visible. More than 12 seconds without a successful observation is stale. Cached camera frames older than 15 seconds are not served as live frames. The proxy renders once per fetched frame and shares that result with viewers; it does not re-index G-code for each viewer.

A single worker indexes the current/last selected file. Files are fetched from the fixed configured printer origin with GET requests; redirects and path traversal are rejected. Text `.gcode`, `.gco` and `.gc` files are supported, up to 128 MiB. Binary G-code, archives, 3MF, oversized lines and excessive index structures are rejected. File size, metadata changes, print restart/cursor reset and filename changes invalidate the index; SHA-256 identifies the parsed artifact. Metadata is rechecked every 30 seconds, not continuously.

The index provides layer heights, feature labels/explanations, special commands, logical tools, movement-command counts and byte ranges. It is not a complete motion simulator, geometry validator or per-feature duration estimator. Remaining time is a labelled slicer M73 estimate when available, not a guaranteed completion time.

The recent-event ring and G-code index are memory-only. This is not a durable audit log or a production-attempt database. No high-frequency HA sensors were added, so this feature does not write its polling stream into HA's recorder.

## Security and scope

No router ports, DNS records, public service or external AI upload were added. Guest images do not include filenames or raw G-code. The internal JSON API has network isolation rather than separate authentication; it must not be published directly. Existing expiry rules continue to be enforced by Home Assistant on each new signed-image request.

Current links are scoped to the **camera**, not to an order or print attempt. Until they expire, they may show a subsequent job. Order-scoped revocable permissions are a required future platform feature before multi-customer sharing. A G-code hash identifies content, not a unique run of that content.

## Files and deployment

New Python modules: `scripts/u1_gcode_index.py`, `scripts/u1_print_inspector.py`. Updated proxy: `scripts/u1_mjpeg_proxy.py`. New UI: `www/u1-share/u1-inspector-card.js`. New HA package: `packages/u1_print_inspector.yaml`. Dashboard/resource registration is in the existing YAML files.

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
