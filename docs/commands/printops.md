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
3d printops loc tree|add|show|set|mv|archive|unarchive|label|resolve|sync-fleet|resync …
3d printops spool ls|show|receive|move|load|unload|use|measure|empty|import-owned …
3d printops tare ls|add|show …
3d printops dry start DRYER SPOOL_ID... [--temp C] [--hours H] [--rh PCT] [--weights ID=G,...] [--note TEXT]
3d printops dry finish SESSION_ID [--rh PCT] [--weights ID=G,...]
3d printops dry ls [--open] [--spool ID] [--limit N]
3d printops dry due [--all] [--check]
3d printops incidents report|ls|show|edit|confirm|resolve|dismiss|stats|context|from-watch|exposure …
```

| Subcommand | What |
|---|---|
| `health` | 3d-api status: DB migrations, Spoolman reachability and version, PrintOps extra fields registered. No token needed. Exit 0 = ok, 1 = degraded/unreachable. |
| `events` | The PrintOps event log (`slot.mismatch`, `spool.measured`, `drying.due`, …), oldest first; `--follow` streams new ones (SSE). |
| `token` | Mint, list and revoke 3d-api bearer tokens. Only sha256 is stored; `create` prints the token once. |
| `deploy` | Ship Spoolman + 3d-api to the Odroid as the compose project `printops`, install the backup timer, wait until healthy. |
| `backup` | Consistent snapshots of the PrintOps DB, tokens file and Spoolman DB (runs daily on the Odroid). |
| `loc` | The location tree: warehouse cells, dry boxes, dryers and the printer slots from `fleet/printers.yaml`; moves re-sync Spoolman's `location`; printable QR label sheets. |
| `spool` | Spool lifecycle — receive → store/move → load into a printer slot → unload → empty — with remaining as an interval (ledger), weighing and the idempotent `gen/plates.json` import. See [printops-spool.md](printops-spool.md). |
| `tare` | The sourced empty-spool weight DB (SpoolmanDB seed + own weighings) and the tare chain of a spool. See [printops-spool.md](printops-spool.md#3d-printops-tare). |
| `dry` | Drying sessions (start / finish / ls) and the spools due for drying by the `drying:` policy in `fleet/policy.yaml`. |
| `incidents` | Print incidents with an auto-filled snapshot (job, layer, G-code temperatures/speed/flow, spool, lot, drying), drafts from `3d printer watch`, rates per kg and per print-hour by vendor/filament/lot/printer/head. See [printops-incidents.md](printops-incidents.md). |

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

### `loc`

Where spools live ([#159](https://github.com/alex-mextner/3d-cli/issues/159), plan §5.2/§10.2).
A location is a **path** `SCHEME:NAME[/NAME…]` of at most 64 characters — Spoolman's
`location` limit — e.g. `WH:default`, `WH:A/S2/B3/04`, `drybox:1`, `dryer:1`. A scheme is a
letter then letters/digits/`_`/`-` (≤ 16); a segment is letters/digits/`.`/`_`/`-` (≤ 32);
`cell` as a scheme and `contents` as a segment are reserved. The parent of a path is the path
minus its last segment and must exist (and be active) first. Paths are unique ignoring case.
REF in the commands below is a path or the numeric id.

Kinds: `warehouse zone shelf box slot dryer dry_box`; `printer ams printer_slot` belong to
the fleet. **Printer slots** are not added by hand: `slot:<printer>` (kind `printer`),
`slot:<printer>/<head>` (e.g. `slot:u1/T3`, capacity 1; an unusable head keeps the fleet's
reason in its note) and, for AMS-like units, `slot:<printer>/<unit>/<n>` follow
`fleet/printers.yaml`: 3d-api re-syncs them on the first request after the file changed (a
deploy) and on `loc sync-fleet`; slots that left the fleet are archived once empty.

**Spoolman link.** A spool stored in a location has Spoolman `location` = the path and the extra
field `printops_location_id` = the location id (both written in one PATCH). `show` lists the
spools whose id points at the location, plus spools without an id whose `location` string was
typed to the path in the Spoolman UI. `mv` renames the whole subtree in one DB transaction,
then rewrites both fields for every spool inside; if Spoolman is down then, the move stays,
the failure is printed (exit 1), and `loc resync` repairs the drift later. `archive` refuses a
location that still holds spools or active children; archived rows keep their id (it is on
printed tags) and `unarchive` revives them.

**Labels.** `label` renders a printable sheet locally (PDF or SVG, stdlib only): per location a
QR code and the path, kind and `loc-<id>`. The QR text follows decision D8: `cell:<path>`, or
the path itself for `dryer:` and `slot:` locations. Printing records the payload on the
location (`qr`); after a later move `tree`/`show` flag the label as `STALE`, and `resolve`
still maps the old sticker to the moved cell. Default sheet: A4, 3 × 8 labels of 70 × 37 mm.

**NTAG213 on a cell** (written by `3d printops tag`, #175; `label --ntag` prints the bytes):
the NDEF TLV `03 <len> <message> FE`, ≤ 144 bytes for any 64-char path —
record 1 URI `https://www.home-assistant.io/tag/loc-<id>` (HA Companion fires `tag_scanned`
with tag id `loc-<id>`, stable across moves), record 2 Text (`en`, UTF-8) = the QR text. The
tag's UID is stored with `loc set REF --tag-uid UID`. `resolve` accepts any of: path, id,
`cell:<path>`, `dryer:1`, `slot:u1/T3`, `loc-<id>`, a tag UID, or an older printed payload.

- `tree [ROOT]` — the tree (or the subtree under ROOT); `--archived` includes archived ones, `--json` nests.
  `3d printops loc tree` · `3d printops loc tree slot:u1 --json | jq '.[0].children[].path'`
- `add PATH --kind KIND` — with `--capacity N` (spools it holds), `--humidity-sensor ENTITY`
  (HA RH sensor of a dry box; locations below inherit it), `--tag-uid UID`, `--note TEXT`.
  `3d printops loc add WH:default --kind warehouse` ·
  `3d printops loc add drybox:1 --kind dry_box --capacity 4 --humidity-sensor sensor.drybox_1_humidity`
- `show REF` — ancestors, children, label state, RH sensor and the spools inside (`--recursive`: whole subtree; `--json`).
  `3d printops loc show slot:u1/T3`
- `set REF` — change `--kind`, `--capacity`, `--humidity-sensor`, `--tag-uid`, `--note` (`""` clears).
  `3d printops loc set WH:A/S2 --tag-uid 04:A2:2B:1A:7F:61:80`
- `mv REF NEW_PATH` — rename / re-parent with the subtree; prints every renamed path and the Spoolman re-sync.
  `3d printops loc mv WH:A/S2 WH:B/S1`
- `archive REF` / `unarchive REF`.
  `3d printops loc archive WH:A/S2`
- `label REF...` — `--subtree` (every active location below each REF), `-o FILE` (default
  `location-labels.pdf`; SVG pages are `FILE`, `FILE-2.svg`, …), `--format pdf|svg` (default from
  `-o`), `--grid COLSxROWS` (default `3x8`), `--page a4|letter`, `--margin MM`, `--start N` (skip the
  first N cells of a partly used sheet), `--no-border` (no grey cut guides), `--ntag` (print each
  NTAG213 image as hex).
  `3d printops loc label WH:A --subtree -o shelf-a.pdf` ·
  `3d printops loc label dryer:1 slot:u1/T1 --format svg -o labels.svg --start 5`
- `resolve TEXT` — which location a scanned text names.
  `3d printops loc resolve cell:WH:A/S2`
- `sync-fleet` — re-materialise printer slots now. `resync` — rewrite drifted Spoolman `location`s.

REST (scope `read` for GET, `write` otherwise; paths go into the URL as they are):
`GET /v1/locations` (`{"tree": […], "count": N}`; `?flat=true`, `?archived=true`),
`GET /v1/locations/resolve?q=TEXT`, `POST /v1/locations` `{path, kind, capacity?, humidity_sensor_entity?, tag_uid?, note?}`,
`GET /v1/locations/{ref}` (with `ancestors`, `children`, `humidity_sensor`),
`GET /v1/locations/{ref}/contents` (`?recursive=true`), `PATCH /v1/locations/{ref}` (`{path}` moves;
`kind`, `capacity`, `humidity_sensor_entity`, `tag_uid`, `qr`, `note`, `archived: false`),
`DELETE /v1/locations/{ref}` (archive; 409 while not empty), `POST /v1/locations/sync-fleet`,
`POST /v1/locations/resync`. Events: `location.created|updated|moved|archived` (subject `location:<id>`).
### `dry`

Drying sessions record which spools went into which dryer location (kind `dryer` or `dry_box`,
e.g. `dryer:1`), at what temperature and for how long ([#162](https://github.com/alex-mextner/3d-cli/issues/162)).
`ls`/`due` need the `read` scope, the rest `write`; every subcommand takes `--json`, `--api`, `--token`.

- `start DRYER SPOOL_ID...` — open a session. Without `--temp`/`--hours` the spools' recipes decide:
  the **lowest** recommended temperature (nothing softens) and the **longest** time; a spool rated
  more than 5 °C away from the session temperature gets a warning. Emits `drying.started`.
  `3d printops dry start dryer:1 12 13`
- `finish SESSION_ID` — close it: every spool gets Spoolman `last_dried_at` = now and
  `dry_state` = `fresh`; emits `drying.finished` (`trigger: finish`). When target time elapses on a
  still-open session, 3d-api emits `drying.finished` (`trigger: timer`) once, as the "take them
  out" reminder.
  `3d printops dry finish 4`
- `--temp C`, `--hours H` — session temperature (20..150 °C) and target time (≤ 96 h).
  `3d printops dry start dryer:1 21 --temp 80 --hours 12`
- `--weights ID=G,...` — gross scale weights per spool, before (`start`) or after (`finish`). The
  difference is written as a ledger **note** (water lost — not filament usage: no Spoolman `/use`,
  no remaining-weight anchor).
  `3d printops dry finish 4 --weights 21=1171.5`
- `--rh PCT` — dryer humidity before/after. Default: read from the dryer location's Home Assistant
  humidity sensor when one is set (`3d printops loc` `--humidity-sensor`) and 3d-api has
  `PRINTOPS_HA_URL` configured (read-only `GET /api/states/<entity>`).
  `3d printops dry start dryer:1 12 --rh 48`
- `--note TEXT` — free text kept with the session.
- `ls [--open] [--spool ID] [--limit N]` — sessions, newest first.
  `3d printops dry ls --open`
- `due [--all] [--check]` — spools due (or wet) for drying, with the reason. Read-only; `--all`
  shows every active spool, `--check` first runs the periodic check now.
  `3d printops dry due --all --json | jq '.spools[] | {spool_id, state, reasons}'`

```
due     spool 14 (Elegoo PETG PETG Rapid black)  [medium]  @ WH:A/S2/B3  opened 41.2 d ago ≥ 30 d (medium)
wet     spool 3 (Polymaker PA6-CF PA6-CF)  [extreme]  dried 7.5 d ago ≥ 2 × 3 d (extreme)
```

**How "due" is decided.** `fleet/policy.yaml` → `drying:` gives every hygroscopic class
(`low|medium|high|extreme`) three limits: `max_days_opened` (days since Spoolman `opened_at` for a
spool we never dried), `max_days_since_dried` (days since `last_dried_at`) and `max_rh` (% RH of the
spool's storage location — its own or nearest ancestor's `humidity_sensor_entity`, read from Home
Assistant). The class comes from the filament's material (`materials:` table: PLA low, PETG/ABS/ASA
medium, TPU/PC/PP/PET-CF high, PA*/PVA/BVOH extreme; unknown → `default_class`), overridden by the
filament extra field `hygroscopic_class`; temperature/time likewise by `drying_temp_c` /
`drying_hours`. The material recipes cite their sources in the file (Bambu Lab's material table,
Prusa's drying KB); the day/RH limits are house policy.

| `dry_state` | When |
|---|---|
| `fresh` | dried less than `fresh_days` ago |
| `ok` | within every limit |
| `due` | a day limit reached, or the storage RH above the class `max_rh` |
| `wet` | exposure ≥ limit × `wet_factor` |
| *(unknown)* | never opened and never dried per Spoolman — left untouched |

Every `check_every_min` 3d-api writes changed `dry_state` values to Spoolman and emits
`drying.due` when a spool **enters** `due` or `wet`. `due`/`wet` are sticky: only a finished
drying session (or a human editing `dry_state` in Spoolman) clears them. Spools in an open
session show as `drying` and are never flagged.

## Exit codes

`0` success / healthy · `1` degraded, API error (401/403/5xx/unreachable) or a failed backup
item · `2` bad invocation (unknown option, bad scope, duplicate token name).
