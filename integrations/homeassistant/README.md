# integrations/homeassistant

Source of truth for every 3D artifact of the Home Assistant config; `3d ha deploy` converges it onto the HA host
(`manifest.yaml`, docs: `docs/commands/ha.md`).

## Scheduled Spoolman + PrintOps backup (`packages/printops_backup.yaml`)

Home Assistant is the only scheduler of the backup — no cron job, no systemd timer. At 04:00 the automation
`printops_backup_daily` runs `script.printops_backup_run`, which calls 3d-api `POST /v1/backup/run`; 3d-api runs
`3d printops backup run` as a niced child (SQLite online backup, gzip level 1) into a verified, rotated set on the backup disk.

**One place to edit:** the three `EDIT HERE` spots of the package — the `url:` of `rest_command.printops_backup_run` (3d-api as
HA sees it; it is written into the rest_command and deliberately NOT a service-data field, so a caller of the service cannot
send the bearer token to another host), `variables:` of the script (`target`, `subdir`, `keep_daily/weekly/monthly`,
`min_free_gb`) and the `at:` of the automation. 3d-api never keeps less than the host's `retention_floor:` (in `backup.yaml`,
default 7/4/6), whatever `keep_*` asks for. `3d ha deploy` reloads script/automation/rest_command/input_datetime without a restart.

Secrets: `printops_api_auth: "Bearer <3d-api token, scopes read,write>"` (the same secret `printops_tasks.yaml` uses).

Failure path: HA raises the persistent notification `printops_backup_failed_<target>` when 3d-api is unreachable or the run failed
(wrong/missing disk, no space, integrity), 3d-api opens the PrintOps anomaly `backup_failed`, and `printops_backup_stale`
fires after 36 h without a good run (or when none was ever recorded). A good run of the same target dismisses them.

Disk and restore: `docs/commands/printops.md#backup`. `3d init --env` can install this package and the Spoolman integration
(`docs/commands/init.md`); Home Assistant core has no Spoolman integration, the community one
(`Disane87/spoolman-homeassistant`, HACS) is not vendored here (`vendor.lock`).

## Printer-card loading and recovery

The printer dashboard uses `custom:printer-webrtc-camera` for both cameras and the existing U1 inspector/share cards.
The cameras retain the pinned WebRTC 3.6.1 streaming implementation, mode selection, audio and digital pan/zoom behavior.
The legacy `/webrtc/webrtc-camera.js` resource remains configured because the integration otherwise injects its unguarded
module during early frontend startup. The separate camera tag prevents duplicate registration with that legacy module.

`frontend.extra_module_url` and Lovelace resources reference the same version of `printer-cards-bootstrap.js` with
different `entry` query values. A failed early module URL therefore does not poison the later fallback URL; the singleton
guard still permits only one controller.
The independent frontend entry covers Home Assistant frontend 20260826.7's one-shot resource-list failure: a rejected
request can leave `resourcesLoaded` true without loading any cards. The bootstrap waits for `home-assistant` definition,
then uses the current custom-element registry, avoiding the scoped-registry polyfill's early-registration race.
It immediately registers small outer cards with ordinary loading/retry UI, so a bundle taking more than Home Assistant's
two-second missing-element timeout does not turn these cards into generic configuration errors. The original components
are mounted inside those cards after loading, in config → latest hass → DOM order; further hass updates keep the existing
camera stream, inspector state and sharing form. Browser connection/disconnection events govern the inner components.
It loads while visible and online when `/3d-printers` is open or a printer card is connected on another dashboard.
Other pages without connected printer cards do not request the bundle. The owned camera appears as **Printer Camera**
in the card picker, distinct from the legacy WebRTC Camera. No printer control command or page reload is involved.

One self-contained module contains all three components and their imports. A cycle permits at most four attempts, with a
15-second timeout and 1/4/10-second retry delays. Cycle starts are at least 60 seconds apart: a later online, visibility
or navigation event can start a new cycle once the previous cycle is that old. An explicit Retry button can start a new
bounded cycle immediately after exhaustion. The automatic limit is measured from cycle start,
so a slow exhausted cycle can recover immediately when connectivity returns. Late imports are idempotent; a failed attempt cannot settle twice. Successful registration ends retries.
The browser diagnostic `window.__printerCardResources` exposes phase, missing tags, attempt counts and error type;
it contains no credentials or camera links. Exhaustion logs one warning instead of entering a reload loop.

### Build and validate

Run `python3 integrations/homeassistant/scripts/build_printer_cards.py` after editing a card, then commit the regenerated
bundle, bootstrap version and both configuration URLs. `--check` verifies exact output, input hashes and URL versions.
The fixed-graph builder uses only the Python standard library. Vendored files carry the upstream MIT notice and original
DigitalPTZ attribution. `vendor/webrtc-3.6.1/sources.json` records upstream commit/hashes and the explicit local helper
extraction required by the repository's complexity gate; regression cases preserve stream-selection behavior.

`tests/test_ha_printer_cards.py` runs the actual modules with isolated DOM/network/timer fixtures, including registry
replacement, transport failure, timeout/late completion, bounded retries and the four current dashboard configurations.
Node.js 22+ runs these module regressions; it is mandatory in CI and absent local installations report an explicit skip.
These unit checks are not a substitute for an actual browser screenshot.

### Apply safely

First merge the source fragment into the shared HA `configuration.yaml` without reloading it. Preserve preimages and
unrelated keys; the fragment is a subset, not a replacement. Then use `3d ha deploy` to copy the assets and validate the
configuration. That command checks the fragment against the live YAML before copying assets, but never restarts HA.
Its `plan.restart` does not detect changes to `frontend.extra_module_url`, so an empty restart plan does not confirm
that the new frontend entry is active. Do not reload resources or restart HA between the YAML edit and successful
asset deployment.

On HA 2026.9.3, adding or changing the versioned `frontend.extra_module_url` requires a separate, validated HA restart.
This applies to every card rebuild because the bootstrap embeds the bundle digest. Copying assets and updating YAML
alone does not activate that URL; the running index may still name an older cached bootstrap. Neither
`frontend.reload_themes` nor `lovelace.reload_resources` activates this setting. Use the site's existing safe HA restart
workflow (`scripts/ha_safe_reload.sh` on the deployed HA host) after configuration validation and a fresh review of
printer activity and startup automations. That workflow performs the restart; `3d ha deploy` does not invoke it.

Verify that the actual HA index includes the new extra-module URL, asset hashes match and all four cards render in an
authenticated frontend session. A healthy HTTP asset alone is not visual acceptance evidence.
