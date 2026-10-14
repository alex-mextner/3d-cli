# `3d printops` — PrintOps through 3d-api

PrintOps keeps track of filament spools, printer slots, drying and print gates for the printer
fleet. The data lives on the Odroid: spool identity and remaining weight in
[Spoolman](https://github.com/Donkie/Spoolman) v0.27.0, everything else in the PrintOps SQLite
DB owned by the **3d-api** service. `3d printops` is a thin client: it talks HTTP to 3d-api
(the only writer to Spoolman). Service layout, ports, volumes and backups:
[services/3d-api/README.md](../../services/3d-api/README.md). Epic:
[#154](https://github.com/alex-mextner/3d-cli/issues/154); this command: [#158](https://github.com/alex-mextner/3d-cli/issues/158).

## Configuration

`health` and `events` need the API URL; `events` also needs a token with the `read` scope.
First match wins:

1. `--api URL` / `--token TOKEN`
2. `PRINTOPS_API` / `PRINTOPS_TOKEN` environment variables
3. `~/.config/3d-cli/printops.yaml`:

```yaml
api: http://192.168.0.39:8765
token: po_...
```

From outside the LAN use the Odroid's Tailscale address (`http://100.116.57.66:8765`); the
service is never exposed to the internet.

## Usage

```
3d printops health [--json] [--api URL] [--token TOKEN]
3d printops events [--since ID | --last N] [--follow] [--json] [--api URL] [--token TOKEN]
3d printops token create NAME --scopes SCOPES [--file PATH | --ssh HOST]
3d printops token list [--file PATH | --ssh HOST]
3d printops token revoke NAME [--file PATH | --ssh HOST]
3d printops deploy [--dry-run] [--host USER@HOST] [--dir DIR] [--backup-dir DIR] [--api URL]
3d printops backup [--dest DIR] [--spoolman-db PATH]
3d printops spool ls|show|receive|move|load|unload|use|measure|empty|import-owned …
3d printops tare ls|add|show …
```

| Subcommand | What |
|---|---|
| `health` | 3d-api status: DB migrations, Spoolman reachability and version, PrintOps extra fields registered. No token needed. Exit 0 = ok, 1 = degraded/unreachable. |
| `events` | The PrintOps event log (`slot.mismatch`, `spool.measured`, `drying.due`, …), oldest first; `--follow` streams new ones (SSE). |
| `token` | Mint, list and revoke 3d-api bearer tokens. Only sha256 is stored; `create` prints the token once. |
| `deploy` | Ship Spoolman + 3d-api to the Odroid as the compose project `printops`, install the backup timer, wait until healthy. |
| `backup` | Consistent snapshots of the PrintOps DB, tokens file and Spoolman DB (runs daily on the Odroid). |
| `spool` | Spool lifecycle — receive → store/move → load into a printer slot → unload → empty — with remaining as an interval (ledger), weighing and the idempotent `gen/plates.json` import. See [printops-spool.md](printops-spool.md). |
| `tare` | The sourced empty-spool weight DB (SpoolmanDB seed + own weighings) and the tare chain of a spool. See [printops-spool.md](printops-spool.md#3d-printops-tare). |

Subcommands are self-registering modules in `lib/printops/cli/<sub>.py`; later PrintOps slices
add theirs there.

### `health`

- `--json` — print the raw `/v1/health` body.
  `3d printops health --json | jq '.spoolman.extra_fields.missing'`
- `--api URL` — 3d-api base URL.
  `PRINTOPS_API=http://192.168.0.39:8765 3d printops health`
- `--token TOKEN` — bearer token (health itself does not need one).

```
3d-api http://192.168.0.39:8765  ok  (version 3f9c2a1b7d10)
  db        ok  migrations 1  last event #0
  spoolman  ok  v0.27.0  extra fields ok
```

### `events`

- `--last N` — the newest N events (default 20 when `--since` is not given).
  `3d printops events --last 100 --json | jq -r 'select(.kind == "slot.mismatch") | .subject'`
- `--since ID` — events after event `#ID`; the plain output ends with the cursor to pass next time.
  `3d printops events --since 42`
- `--follow` — keep streaming (Server-Sent Events) until Ctrl-C; with `--since`, replay from there first.
  `3d printops events --follow`
- `--json` — one JSON object per line: `{id, at, kind, subject, payload, actor}`.
  `3d printops events --follow --since 42 --json > events.jsonl`

### `token`

Tokens carry independent scopes: `read`, `write`, `printer-control`. The tokens file on the
Odroid is `/home/ultra/printops/data/api/tokens.json` (`/data/tokens.json` in the
`printops-api` container); the API re-reads it on change, so no restart is needed.

- `--scopes LIST` — scopes for `create`.
  `3d printops token create ha --scopes read --ssh ultra@192.168.0.39`
- `--ssh HOST` — run the command inside the `printops-api` container on HOST over ssh; the new
  token is printed locally and stored nowhere but as sha256 on the Odroid.
  `3d printops token create bot --scopes read,write --ssh ultra@192.168.0.39`
  `3d printops token list --ssh ultra@192.168.0.39`
  `3d printops token revoke bot --ssh ultra@192.168.0.39`
- `--file PATH` — operate on a local tokens file instead (tests, a local 3d-api).
  `3d printops token create cli --scopes read --file ./tokens.json`

### `deploy`

Steps, each printed before it runs: create `DIR/data/{api,spoolman}` and the backup dir →
rsync `bin/3d lib fleet pyproject.toml services/3d-api` to `DIR/src` →
`docker compose -p printops -f services/3d-api/compose.yaml up -d --build` → enable
`printops-backup.timer` (systemd `--user`) → poll `/v1/health` until `ok` (up to 5 minutes).
It never touches the Home Assistant compose project and never restarts HA.

- `--dry-run` — run rsync with `--dry-run --itemize-changes` (shows what would change) and only print the rest.
  `3d printops deploy --dry-run`
- `--host USER@HOST` — ssh target (default `ultra@192.168.0.39`).
  `3d printops deploy --host ultra@odroidn2 --api http://100.116.57.66:8765`
- `--dir DIR` — remote base dir (default `/home/ultra/printops`).
  `3d printops deploy --dir /home/ultra/printops`
- `--backup-dir DIR` — snapshot dir inside HA's backed-up config dir (default `/home/ultra/homeassistant/printops-backup`).
  `3d printops deploy --backup-dir /home/ultra/homeassistant/printops-backup`
- `--api URL` — health URL to wait on (default `http://<host>:8765`).

### `backup`

Writes `DIR/printops.db`, `DIR/tokens.json` (if present) and `DIR/spoolman.db` (if
`--spoolman-db`/`$SPOOLMAN_DB` is set), each via the SQLite online-backup API, checked with
`PRAGMA integrity_check`, then atomically renamed. Exit 1 if any item failed. On the Odroid
`printops-backup.timer` runs it daily at 04:15 into `/home/ultra/homeassistant/printops-backup/`,
which Home Assistant's automatic daily backup (it ran 04:50–05:11 on the last three days; 3 copies kept) then includes.

- `--dest DIR` — backup directory (default `$PRINTOPS_BACKUP_DIR`).
  `3d printops backup --dest ~/printops-backup`
- `--spoolman-db PATH` — Spoolman's `spoolman.db` (default `$SPOOLMAN_DB`).
  `3d printops backup --dest /backup --spoolman-db /spoolman-data/spoolman.db`

Run it by hand on the Odroid: `ssh ultra@192.168.0.39 docker exec printops-api 3d printops backup`.

## Exit codes

`0` success / healthy · `1` degraded, API error (401/403/5xx/unreachable) or a failed backup
item · `2` bad invocation (unknown option, bad scope, duplicate token name).
