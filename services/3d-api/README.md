# services/3d-api — Spoolman + 3d-api on the Odroid

The PrintOps backend (plan §5, §11; issue [#158](https://github.com/alex-mextner/3d-cli/issues/158)):

- **Spoolman v0.27.0** (`ghcr.io/donkie/spoolman:0.27.0`, multi-arch incl. arm64) — spool
  identity, remaining weight, lot, price, location, PrintOps extra fields.
- **3d-api** (this repo, `lib/printops/api/`) — FastAPI + SQLite. The single writer to
  Spoolman; owns the PrintOps DB and the event log; bearer-token auth.

Both run as the docker compose project **`printops`** on the Odroid (`ultra@192.168.0.39`),
next to — but separate from — the Home Assistant compose project. Nothing here touches the HA
compose file or restarts HA.

## Deploy

```
3d printops deploy --dry-run     # what would change
3d printops deploy               # rsync → compose up --build → backup timer → wait healthy
PRINTOPS_API=http://192.168.0.39:8765 3d printops health
```

`deploy` rsyncs `bin/3d lib fleet pyproject.toml services/3d-api` to
`/home/ultra/printops/src` (the build context) and runs
`docker compose -p printops -f services/3d-api/compose.yaml up -d --build` there, with
`PRINTOPS_VERSION=<git sha>` (shown by `/v1/health`). On start 3d-api migrates its DB and
registers every PrintOps extra field in Spoolman (`printops.spoolman.EXTRA_FIELDS`), retrying
until Spoolman answers and re-checking every 10 minutes.

## Ports (LAN + tailnet only)

| Port | Service | Auth |
|---|---|---|
| `7912` | Spoolman web UI + REST (`/api/v1`) | none (Spoolman has no auth by design) |
| `8765` | 3d-api (`/v1/...`) | bearer token on everything except `GET /v1/health` |

Ports are published on all host interfaces. The Odroid sits behind the home router's NAT with
no port forwards for these ports, so that means the LAN plus the Tailscale tailnet. **Remote
access is via Tailscale only** (`http://100.116.57.66:8765`, `http://100.116.57.66:7912`);
never add a router port-forward or a public reverse proxy for them.

Accepted risk: Spoolman's REST API has no auth, so any LAN/tailnet client can change spools
directly, bypassing 3d-api's token scopes. This is deliberate (plan §5.1, D4): Moonraker's
`[spoolman]` on the U1 and the Spoolman web UI need direct LAN access. 3d-api treats
Spoolman as the source of truth and reconciles against it rather than assuming exclusivity.

## Volumes (on the WD HDD)

`/home/ultra` on the Odroid is a bind mount of `/mnt/wd500/odroid-rt/ultra` (the WD 500 GB HDD,
see `/etc/fstab`), so everything below lives on the HDD, not on the eMMC.

| Host path | Container path | What |
|---|---|---|
| `/home/ultra/printops/data/spoolman/` | `printops-spoolman:/home/app/.local/share/spoolman` | `spoolman.db` + Spoolman's own rolling backups |
| `/home/ultra/printops/data/api/` | `printops-api:/data` | `printops.db` (WAL), `tokens.json` |
| `/home/ultra/printops/data/spoolman/` | `printops-api:/spoolman-data` | read by `3d printops backup` only |
| `/home/ultra/homeassistant/printops-backup/` | `printops-api:/backup` | daily snapshots (below) |

Both containers run as uid/gid 1000 (`ultra`), so the files stay owned by the host user.

## Backup

The Odroid's existing backup contour is Home Assistant's automatic daily backup
(`backup.local`, 3 copies, includes the whole config dir `/home/ultra/homeassistant` except
logs, `*.db-shm`, `tts/` and the backup tars themselves). PrintOps adds to it instead of running
a parallel system:

1. `printops-backup.timer` (systemd `--user`, installed by `deploy`) fires daily at 04:15 and
   runs `docker exec printops-api 3d printops backup`.
2. That writes consistent snapshots (SQLite online backup = `sqlite3 .backup`, then
   `PRAGMA integrity_check`, then atomic rename) to
   `/home/ultra/homeassistant/printops-backup/{printops.db,spoolman.db,tokens.json}`. The dir holds
   a `.gitignore` with `*`, so the HA git checkout stays clean.
3. HA's automatic backup (it ran 04:50–05:11 on recent days) packs that dir into its daily tar
   in `/home/ultra/homeassistant/backups/`.

Check / run by hand:

```
ssh ultra@192.168.0.39 systemctl --user list-timers printops-backup.timer
ssh ultra@192.168.0.39 docker exec printops-api 3d printops backup
```

Restore: stop the stack (`docker compose -p printops -f services/3d-api/compose.yaml down` in
`/home/ultra/printops/src`), copy `printops.db` → `data/api/printops.db`, `tokens.json` →
`data/api/tokens.json`, `spoolman.db` → `data/spoolman/spoolman.db` (delete any `-wal`/`-shm`
next to the targets), then `up -d`.

## Tokens

Clients (HA, 3d-bot, the CLI, the ESPHome station, voice) each get their own bearer token
with the scopes they need: `read`, `write`, `printer-control`. Only `sha256(token)` is stored,
in `/home/ultra/printops/data/api/tokens.json` (mode 0600; `/data/tokens.json` in the
container). 3d-api re-reads the file when it changes — no restart.

```
3d printops token create bot --scopes read,write --ssh ultra@192.168.0.39   # prints po_… once
3d printops token list --ssh ultra@192.168.0.39
3d printops token revoke bot --ssh ultra@192.168.0.39
```

Use: `Authorization: Bearer po_…` on every request; for `WS /v1/ws` send
`{"type": "auth", "token": "po_…"}` as the first message (tokens never go in URLs, which land
in access logs).

## API (core)

| Endpoint | Scope | |
|---|---|---|
| `GET /v1/live` | — | liveness (container healthcheck): `{ok}`, DB only, never calls Spoolman |
| `GET /v1/health` | — | `{status: ok\|degraded, version, db, spoolman{version, extra_fields}}`; 503 only if the DB is broken |
| `GET /v1/events` | read | SSE stream (`id`/`event`/`data`, `: ping` every 15 s; resumes from `since` or `Last-Event-ID`); with `Accept: application/json` a page `{events, cursor}` by `since`/`limit` or `last` |
| `WS /v1/ws` | read | auth message, then `{type: "event", event}` frames |

## API (reconcile, #161)

| Endpoint | Scope | |
|---|---|---|
| `GET /v1/printers` | read | fleet printers + latest live reading + per slot: printer material/colour/tag, bound spools, anomalies |
| `GET /v1/printers/{name}` / `…/{name}/slots` | read | one printer / just its slots (404 unknown) |
| `GET /v1/anomalies` | read | `status=open,ack` (default) \| `resolved` \| `all`, `printer=`, `limit=` |
| `POST /v1/anomalies/{id}/ack` / `…/resolve` | write | `{note?}`; 409 when the status does not allow it |

A background poller reads each contactable fleet printer (U1 Moonraker, K1 SE ws) READ-ONLY, one
request per `PRINTOPS_POLL_S` seconds per printer (default 30, floor 10, `0` = off) and
reconciles slots with Spoolman bindings; `PRINTOPS_COLOR_DE_MAX` (default 15) is the CIEDE2000
colour threshold. API requests only show the cached reading — they never reach a printer.

Slices add `lib/printops/api/routes_<slice>.py` (auto-included) and
`lib/printops/migrations/NNNN_<slice>.sql` from their reserved range.

## API (drying, #162)

| Endpoint | Scope | |
|---|---|---|
| `POST /v1/drying/sessions` | write | `{dryer, spools[], temp_c?, hours?, rh_before?, weights_before?{id: g}, source?, note?}` → 201 `{session, warnings}` |
| `POST /v1/drying/sessions/{id}/finish` | write | `{rh_after?, weights_after?}` → Spoolman `last_dried_at` + `dry_state=fresh`; weights → ledger notes |
| `GET /v1/drying/sessions` | read | `?open=true&spool=ID&limit=N` → `{sessions}` |
| `GET /v1/drying/sessions/{id}` | read | one session |
| `GET /v1/drying/due` | read | `?all=true` → `{checked, spools[assessment]}`; writes nothing |
| `POST /v1/drying/check` | write | run the periodic check now → `{checked, changed, alerts, errors, due}` |

A background task (router lifespan) fires session timers every minute (`drying.finished`,
`trigger: timer`) and every `drying.check_every_min` of `fleet/policy.yaml` updates Spoolman
`dry_state` and emits `drying.due`. The policy file is re-read each time (image path
`/app/fleet/policy.yaml`, override with `PRINTOPS_POLICY`).

**Humidity (read-only Home Assistant).** A location's `humidity_sensor_entity` is read with
`GET /api/states/<entity>` when `PRINTOPS_HA_URL` is set (compose passes it through from the
deploy environment, e.g. `PRINTOPS_HA_URL=http://192.168.0.39:8123`) and an HA long-lived token
is in `/home/ultra/printops/data/api/ha_token` (`PRINTOPS_HA_TOKEN_FILE`, mode 0600) or
`PRINTOPS_HA_TOKEN`. Nothing is ever written to HA. Unset → humidity is simply unknown.

## API (calibrations, #167)

| Endpoint | Scope | |
|---|---|---|
| `GET /v1/calibrations` | read | `?printer=&head=&kind=&status=&current=true&limit=N` → `{calibrations}` newest first |
| `GET /v1/calibrations/required` | read | `?printer=P&head=T` (or `slot=T`) `&spool=ID&plate=&all=true` → `{printer, overall, items, skipped, estimate}` |
| `POST /v1/calibrations/check` | read (write with `accept`) | `{printer, footer{key: value}, used_tools[], spools?{tool: id}, accept?["calibration:<kind>"], reason?, plate?, gcode_sha?, file?}` → the `calibration_current` gate `{overall, items}`; each acceptance → `calibration.accepted` |
| `POST /v1/calibrations/runs` | write | `{kind, method, printer?, head?, spool_id?\|filament_id?, lot_nr?, plate?, params?, gcode_path?, gcode_sha?, note?}` → 201 planned calibration |
| `POST /v1/calibrations/{id}/result` | write | `{values, evidence?, note?, valid_until?}` → recorded; supersedes the same scope; `calibration.recorded` |
| `POST /v1/calibrations` | write | run fields + `{values, …}` → 201 recorded (manual entry) |
| `GET /v1/calibrations/{id}` | read | one calibration |

The catalog `fleet/calibrations.yaml` (override `PRINTOPS_CALIBRATIONS`), the policy section
`calibrations:` of `fleet/policy.yaml` and `fleet/printers.yaml` are re-read on every request;
a broken file answers 503 and shows in `3d lint --printops` (`calibrations/*`). Maintenance (#166)
invalidates records with `calibration.invalidated` events. Details: docs/commands/calibrate.md.

## API (incidents, #165)

`/v1/incidents` (report with auto-context, list, show, edit, confirm, resolve, dismiss),
`/v1/incidents/stats` (rates per kg / print-hour), `/v1/incidents/context`,
`/v1/incidents/from-watch` and `/v1/exposure` (+ `/sync`): see
[docs/commands/printops-incidents.md](../../docs/commands/printops-incidents.md#rest).

**Printers (read-only).** The auto-context reads the printer named in a report: one status read
and, for Moonraker, one `GET /server/history/list`. `POST /v1/exposure/sync` pages through
Moonraker job history (≤ 1 request / 5 s). Offline fleet printers are never contacted.

**G-code for the context.** `PRINTOPS_GCODE_DIRS` (`:`-separated, inside the container) is
searched for the job's file; compose mounts the host's `PRINTOPS_GCODE_HOST_DIR` (default
`/home/ultra/printops/gcodes`) read-only at `/gcodes`, the default dir. `PRINTOPS_GCODE_FETCH=1`
lets 3d-api download a Moonraker job's file once (`GET /server/files/gcodes/…`) into
`/data/gcode-cache/`; off by default. Without the G-code an incident still gets job, layer, the
live heater targets, the spool and its lot.

## API (printer tasks, #281)

| Endpoint | Scope | |
|---|---|---|
| `GET /v1/printers/{p}/tasks` | read | `?status=open` (default: open, in progress, snoozed) `\|all\|done\|dismissed\|…` → `{printer, summary{open, blocking, waiting, snoozed, needs_human, next_idle}, tasks, sync}` |
| `POST /v1/printers/{p}/tasks` | write | `{title, why?, type?, head?, slot?, component?, blocks?, when?, when_note?, duration_min?, needs_human?, depends_on?[id], refs?, calibration_kinds?, spool_id?, note?, source?: manual\|calibration_followup}` → 201 task |
| `GET /v1/tasks` | read | `?printer=&status=` → `{tasks}` |
| `GET /v1/tasks/{id\|uid}` | read | one task (`waiting_on`, `done_hint` included) |
| `POST /v1/tasks/{ref}/done` | write | `{note?}`; 409 for a derived task — the detail names the command that records the work |
| `POST /v1/tasks/{ref}/dismiss` | write | `{reason}` |
| `POST /v1/tasks/{ref}/snooze` | write | `{until?, reason?}` (no `until` = until reopened) |
| `POST /v1/tasks/{ref}/reopen` | write | snoozed/done/dismissed → open |
| `POST /v1/tasks/sync` | write | `?printer=` derive now → `{opened, closed, updated, errors}` |
| `POST /v1/tasks/import` | write | `{lines: [JSONL buffer lines]}`, `?dry_run=true` rolls back → `{created, merged, applied, refused, notices, skipped, items, warnings}` |

Open faults, due maintenance rules (`maintenance_due`) and the calibration requirements are
materialised as tasks on every tasks request and every 15 minutes in the background (events
`printer_task.opened|updated|closed`). Details: docs/commands/printer.md#to-do-list-3d-printer-todo.

## Operations

```
ssh ultra@192.168.0.39 docker ps --filter name=printops-
ssh ultra@192.168.0.39 docker logs --tail 80 printops-api
ssh ultra@192.168.0.39 docker logs --tail 80 printops-spoolman
```
