# `3d printops incidents` — print incidents, their context and rates

Structured incidents per filament, vendor, lot, printer and head (plan §6,
[#165](https://github.com/alex-mextner/3d-cli/issues/165)): what went wrong, how bad, and a
snapshot of what was printing — filled by 3d-api automatically. `stats` turns counts into
**rates per kg printed and per print-hour**, so a lot that printed 5 kg is not "worse" than one
that printed 200 g just because it printed more.

```
3d printops incidents report KIND --severity S [--printer P] [--head T3] [--spool ID] [--at ISO]
3d printops incidents ls [--status S[,S]] [--printer P] [--head H] [--kind K] [--spool ID] [--lot LOT] [--since 90d] [--limit N]
3d printops incidents show ID
3d printops incidents edit ID [--kind K] [--severity S] [--note TEXT] [--tag T]... [--untag T]... [--photo URL]... [--set FIELD=VALUE]...
3d printops incidents confirm ID [--kind K] [--severity S] [--note TEXT] [--set FIELD=VALUE]...
3d printops incidents resolve ID RESOLUTION [--text TEXT] [--calibration ID] [--at ISO]
3d printops incidents dismiss ID [--reason TEXT]
3d printops incidents stats --by vendor|filament|lot|printer|head [--since 90d] [--kind K]
3d printops incidents context PRINTER [--head T3] [--spool ID]
3d printops incidents from-watch            (reads one `3d printer watch` event JSON from stdin)
3d printops incidents exposure ls [--printer P] [--since 90d] [--limit N]
3d printops incidents exposure add PRINTER --hours H [--grams G] [--head T1] [--spool ID] [--at ISO] [--note TEXT]
3d printops incidents exposure sync PRINTER [--limit N]
  common: [--json] [--api URL] [--token TOKEN]
```

`report` also takes `[--note TEXT] [--tag T]... [--photo URL]... [--set FIELD=VALUE]... [--draft] [--no-context]`.
`ls`, `show`, `stats`, `context` and `exposure ls` need the `read` scope; the rest `write`.

## The model

| Field | Values |
|---|---|
| kind | `clog` `stringing` `oozing_blobs` `warping` `delamination` `under_extrusion` `over_extrusion` `moisture_popping` `brittleness` `poor_bed_adhesion` `layer_shift` `colour_off` `runout` `tangle` `heat_creep` `nozzle_drag` `other` (aliases: `layer_split`, `snap`, `oozing`, `blobs`, `jam`, `color_off`) |
| severity | `cosmetic` `part_lost` `print_lost` `hardware_risk` |
| status | `draft` (automatic, awaiting a human) → `open` → `resolved`; `draft`/`open` → `dismissed` |
| resolution | `dried` `temp_change` `speed_change` `retraction_change` `cleaned_nozzle` `replaced_nozzle` `glue_plate` `recalibrated` `discarded_spool` `none` |
| snapshot | printer, head, spool_id, filament_id, vendor_id, vendor, material, filament_name, lot_nr, job_id, job_file, layer, z_mm, feature, nozzle_temp, bed_temp, chamber_temp, speed_mm_s, volumetric_mm3_s, fan_pct, days_since_dried, dry_box_rh, room_rh |
| also | reported_by (`voice` `telegram` `cli` `station` `watch` `vision` `api`), confidence 0..1, note, tags, photos, linked_calibration_id |

The snapshot is stored with the incident (migration 0060): reloading the head, moving or editing
the spool later never rewrites history. Free-text notes on a spool/filament stay in Spoolman's
`comment`; an incident is the structured record.

## Auto-context

`report --printer u1 --head T3` (or the voice "на U1 голова 3 пошли сопли") is enough; 3d-api fills
the rest and records where each value came from (`show` lists the sources):

| Field | Source |
|---|---|
| job_file, job_id | printer status (read-only) + Moonraker history (the in-progress job id). An idle printer gives its **last** job, with a warning and no layer. |
| layer | `print_stats.info.current_layer` (Moonraker) / `layer` (Creality ws) |
| z_mm, feature, nozzle/bed/chamber temperature, speed, volumetric flow, fan | the job's G-code at the printer's file position (`virtual_sdcard.file_position`), else at the first extrusion of the current layer — read by the same per-line parser as `3d gcode-resume` (`gcode_resume.state_at`, streamed, constant memory). Temperatures are the G-code targets of the head's tool (M104/M109, M140/M190, M141/M191); fan = M106 P0 in %; speed = feed rate of the last extruding move; flow = speed × the slicer's `;WIDTH:` × `;HEIGHT:` bead. Without the G-code: the printer's live heater targets. |
| head | given; else the printer's only head; else the G-code's active tool |
| spool, filament, vendor, lot | the spool bound to the head (`3d printops spool load`), or `--spool` |
| days_since_dried, dry_box_rh | the drying assessment of that spool (`last_dried_at`; RH from the spool location's humidity sensor in Home Assistant, read-only) |
| room_rh | `--set room_rh=…` (no room sensor is wired) |

Anything given with `--set FIELD=VALUE` (or in the JSON body) wins over the auto value;
`--no-context` reads nothing. A failing source is a warning, never a failed report — except an
unknown printer or head (404). Offline fleet printers (D7) are never contacted.

**Where 3d-api finds the G-code.** A file of the printer's job name in `PRINTOPS_GCODE_DIRS`
(`:`-separated, paths inside the container; the compose file mounts the host's
`PRINTOPS_GCODE_HOST_DIR` read-only at `/gcodes`, the default). A printer-side name from `3d printer upload` (`<stem>-<16 hex>.gcode`) also
matches a local `<stem>.gcode` whose SHA-256 starts with those 16 hex; the size must equal what the
printer reports. With `PRINTOPS_GCODE_FETCH=1` a Moonraker printer's file is downloaded once
(`GET /server/files/gcodes/…`, read-only) into `<db dir>/gcode-cache/<printer>/`. Fetching is off by
default: a 130 MB read from a printer mid-print is a deliberate choice.

```
$ 3d printops incidents report clog --severity print_lost --printer u1 --head T3
recorded #7 [open] 2026-10-15 09:12  clog (print_lost)  u1/T3  spool #12 Elegoo PLA+ RAPID PLA+ Green lot L2409
    job u1_walls_main_v3-de0f43df5562b59c.gcode (00005A), layer 600 z 72.74, inner wall, nozzle 215 °C, bed 65 °C, speed 150 mm/s, flow 7.64 mm³/s, fan 100 %, dried 3.2 d ago
```

## Drafts from the watch

```
3d printer watch u1 --exec '3d printops incidents from-watch'
```

The watch's exec sink pipes every event's JSON to `from-watch`, which posts it to
`POST /v1/incidents/from-watch`. Warning/critical events that describe a failing print become
drafts: filament runout (`runout`) or motion stop (`clog`, low confidence), printer exceptions (U1
noodle detection → `other` + tag `spaghetti`), G-code errors, Klipper shutdown and heater faults
(`hardware_risk`), lost homing, the bed switched off mid-job (`poor_bed_adhesion`), a stalled job —
only while a job prints or is paused (Klipper shutdown/error always). Camera detections from
`3d printer vision` (`vision_spaghetti`, the U1's `printer_noodle_verdict`) become `vision` drafts
(`other` + `spaghetti`, confidence = the detector's score, the evidence frame as photo). Snapshots,
milestones, camera health, connection flaps and plain state changes are skipped. Events of the same printer and job within
15 min (either way: a delayed event may be older than the draft) join the open draft as evidence (a pause is several events at once); an event that names
no file (camera, Klipper) takes the printer's current file from one status read, so a detection on
a new job opens its own draft. Two events of one failure that arrive together (watch and vision)
still make one draft, and a draft confirmed or dismissed meanwhile takes no more evidence: the
event opens a new draft. The same event key twice is a no-op, even after its draft was closed.
The exec sink kills its command after 60 s, so a watch draft's G-code scan ends 45 s after the
event arrived, printer reads included; past that the draft is recorded without the G-code
settings (a warning) instead of losing the event; a watch draft never downloads the G-code, even
with `PRINTOPS_GCODE_FETCH=1`. Confirm
(`confirm ID [--kind …]`) or `dismiss` each draft; drafts and dismissed incidents never count in
rates.

## Rates

```
$ 3d printops incidents stats --by lot --since 90d
incidents by lot since 2026-07-17: 5 incident(s), 7 kg / 60 h printed; overall 0.714/kg  0.0833/h
     2.500/kg    0.3000/h    3 in    1.2 kg /   10.0 h  lot L1 · Elegoo PLA+ Green  [clog 3]  vs Elegoo 0.800/kg
     0.500/kg    0.0500/h    1 in      2 kg /   20.0 h  lot S1 · Sunlu PETG Black  [warping 1]  vs Sunlu 0.500/kg
     0.263/kg    0.0333/h    1 in    3.8 kg /   30.0 h  lot L2 · Elegoo PLA+ Green  [stringing 1]  vs Elegoo 0.800/kg
```

Rate = confirmed incidents of the group in the window ÷ exposure of the same group in the window.
A lot is compared with its vendor, a filament with its vendor, a head with its printer. Groups
with under 0.25 kg and under 2 h of exposure are flagged `low_exposure` ("little exposure").
Incidents or exposure without a spool fall into an "unknown" group.

**Exposure** (`exposure` table) is what was printed, per printer head, with the spool it printed
from:

- `exposure sync PRINTER` — the printer's finished jobs from Moonraker job history (read-only
  `GET /server/history/list`, ≤ 1 request / 5 s). One row per job and head that extruded: grams =
  the slicer's per-tool `filament_weight` × the share actually printed (`filament_used` /
  `filament_total`); hours = `print_duration` split across the heads in proportion to their grams.
  The spool is the one the slot history (`slot.loaded`/`slot.unloaded` events) shows in the head
  when the job started; a slot with no history uses its current binding (noted on the row).
  In-progress jobs wait for the next sync; syncing twice adds nothing.
- `exposure add PRINTER --hours H [--grams G]` — offline printers (never contacted) and anything
  the history lost. The Creality K1 stock firmware has no job history API: its exposure comes from
  `add` until the job ledger (#170) books jobs.

## REST

| Method | Path | Scope | Body / query |
|---|---|---|---|
| POST | `/v1/incidents` | write | `{kind, severity, printer?, head?, spool_id?, at?, note?, tags?, photos?, reported_by?, confidence?, draft?, auto_context?, <snapshot field>…}` → 201 `{incident, context}` |
| GET | `/v1/incidents` | read | `?status=open,draft&printer=&head=&kind=&spool=&lot=&since=90d&limit=` |
| GET | `/v1/incidents/stats` | read | `?by=vendor\|filament\|lot\|printer\|head&since=90d&kind=` |
| GET | `/v1/incidents/context` | read | `?printer=&head=&spool=` — what a report would fill, without storing |
| POST | `/v1/incidents/from-watch` | write | a watch event JSON → 201 created / 200 merged or skipped (`reason`) |
| GET / PATCH | `/v1/incidents/{id}` | read / write | PATCH any editable field, `add_tags`, `remove_tags`, `add_photos` |
| POST | `/v1/incidents/{id}/confirm` | write | optional corrections; draft → open |
| POST | `/v1/incidents/{id}/resolve` | write | `{resolution_kind, text?, calibration_id?, at?}` |
| POST | `/v1/incidents/{id}/dismiss` | write | `{reason?}` |
| GET | `/v1/exposure` | read | `?printer=&since=&limit=` → rows + totals |
| POST | `/v1/exposure` | write | `{printer, head?, spool_id?, hours, grams?, started_at?, note?}` |
| POST | `/v1/exposure/sync` | write | `{printer, limit?}` |

Errors: `{"detail": …}` with 404 (unknown incident/printer/head), 409 (wrong state, offline
printer, duplicate automatic report), 422 (invalid), 502/503 (printer, Spoolman or fleet unreadable).
Events: `incident.created|confirmed|updated|resolved|dismissed` (subject `incident:<id>`),
`exposure.synced|recorded` (subject `printer:<name>`).

## Lint

`3d lint --printops` adds `incidents/draft-stale` (a draft unconfirmed for over 7 days — it is left
out of the rates) and `incidents/hardware-risk-open` (a `hardware_risk` incident open for over a day).
