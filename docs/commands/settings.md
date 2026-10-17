# `3d settings` — the printer settings-change journal

Issue [#264](https://github.com/alex-mextner/3d-cli/issues/264) (plan §7.4). Talks to 3d-api like
[`3d calibrate`](calibrate.md) (`PRINTOPS_API`, `PRINTOPS_TOKEN` or `~/.config/3d-cli/printops.yaml`).

```
3d settings import [FILE.jsonl…] [--spool UID=ID[,…]] [--method UID=METHOD[,…]] [--dry-run]
3d settings history --printer P [--kind KIND] [--since DATE] [--limit N]
  common: [--json] [--api URL] [--token TOKEN]
```

## Why

Calibrations and settings were changed at the printer — a pressure-advance line picked by eye, a
flow tile, a max-volumetric-speed band pushed into the Orca presets, an input-shaper run — while
3d-api was not involved. Agents wrote each change into an offline buffer. Until it is imported,
`3d calibrate check` knows none of it and blocks a correctly sliced print on "no record".

## The buffer

`~/.local/share/3d-cli/printer-history/<printer>.jsonl` (`$XDG_DATA_HOME/3d-cli`, i.e.
`cli.paths.data_dir()`), one change per line, schema `printops.settings_change/1`: `uid`,
`printer`, `at`, `kind`, `key`, `old`, `new`, `status` (measured | measured_not_applied |
applied | restored | observed | rolled_back), `source`, `actor`, `command`, binding (`head`,
`plate`, `nozzle`, `spool_id`, `filament_id`, `lot_nr`, `spool{filament, color_hex}`), `evidence`,
plus free keys (`method`, `measured`, `note`, `refs`, …) kept verbatim in the journal row.

A buffer from before #264 in `~/.local/share/3d/printer-history/` is moved into place by the
first `3d settings import` without files (under a lock, after `flock` on every legacy file, so an
append in flight finishes first). When both dirs hold files the command stops and prints the
import commands for both — imports are idempotent, so import both, then
`mv ~/.local/share/3d/printer-history ~/.local/share/3d/printer-history.imported`.
Buffer files are only ever read.

## `import`

Each file goes to `POST /v1/settings/import`. Per line:

| Outcome | Meaning |
|---|---|
| imported | a `settings_change` row (append-only), plus a calibration record when the line is one |
| skipped | its uid is already imported with the identical entry — a second import adds 0 |
| refused | nothing of the line is written (unknown printer/head/status, a calibration that cannot be bound to a spool, values outside the catalog, the uid already imported with a different entry); fix the cause and import again |

A line that is not JSON (a torn write) stops the import of its file with the line number. Exit 1
when a line was refused; `--dry-run` runs everything in a transaction that is rolled back.

### Which lines become calibration records

The decision of #264, against the catalog `fleet/calibrations.yaml`:

- **Kind.** The line's `kind` is a catalog kind (`pressure_advance`, `flow_ratio`,
  `input_shaper`, …), or it is a `slicer_profile` line with a `measured` block whose key ends in
  `/ <orca key>` that a kind applies: `… / filament_max_volumetric_speed` →
  `max_volumetric_speed`. Accelerations and other profile keys stay journal-only.
- **Status.** `applied` and `measured` → a record. `measured_not_applied` → a record only when
  the measured values equal the values in force (`new` == `old`): the run confirmed what the
  printer runs. Otherwise journal-only — the printer still runs `old`, and a record of `new` would
  make the `calibration_current` gate demand a value no profile carries. `restored`, `observed`,
  `rolled_back` are not measurements: journal-only.
- **Values.** Per catalog value: the value name in `new`, else its G-code footer key, else (one-
  value kinds) its Orca key — `{"flow_ratio": 0.931}` and `{"filament_max_volumetric_speed": 22}`
  both work.
- **Spool** (kinds scoped to a filament), first that applies: `--spool UID=ID`; the line's
  `spool_id` / `filament_id`; the `spool_binding` of the same printer and head in force at the
  line's time (the last change before it, else the first one recorded — bindings are often written
  down after the spool was loaded; this file's importable lines and already imported ones), whose
  colour must equal the line's `spool.color_hex`. A later re-spool with the same colour therefore
  binds later measurements to the new spool and earlier ones to the old. Spoolman colours alone are not used: two spools may share one (the K1's orange #13
  and Boris' orange #9). Unbound → refused, with the `--spool` to pass.
- **Method.** `--method UID=M` (a catalog method of the kind); else the line's method when the
  catalog lists it for the kind; else `manual`, the original kept in the record's
  `params.buffer_method`. A kind without `manual` (input_shaper, bed_mesh, …) is refused instead,
  naming the methods to pass — the importer never guesses how a value was measured.
- **Time.** The record's `recorded_at` is the line's `at` (a bare date → 00:00Z of that day,
  the conservative side; offsets → UTC), not the import time: maintenance invalidations after the
  measurement make it stale, and superseding follows that time — an old buffer never displaces a
  later calibration of the same scope.
- **Nozzle.** The line's diameter with the head's material/type from the fleet (no install id:
  the nozzle on the head today may postdate the measurement; a swap invalidates by event).
- **Applied.** `applied` lines fill `applied_to` with the kind's first apply target, before/after,
  time, who decided, and the line's own `applied_to` text.

### Real run

The K1 SE buffers of 2026-10-15/16 against a scratch 3d-api (Spoolman copy; spool #13 orange PLA
bound to k1se/T1). Before the import `3d calibrate check` of an `Orange PLA @K1SE` G-code FAILs
flow and pressure advance with `new_material_type`; after it:

```
$ 3d settings import ~/.local/share/3d/printer-history/k1se.jsonl ~/.local/share/3d/printer-history/u1.jsonl
…/k1se.jsonl: 28 lines — 24 imported (5 calibration records), 0 skipped, 4 refused
  line 1    refused   input_shaper       input_shaper: the line's method (none) is not in the catalog and the kind has no `manual`; pass --method 01a140fe-3d83-768e-b634-af490ca0bd5b=<klipper_shaper_calibrate|k1_auto|u1_auto>
  line 4    refused   pressure_advance   pressure_advance is per filament and the line names babushkin-dom black PLA #000000: no spool_binding of k1se/T1 has that colour; pass --spool 01a140fe-3d83-768e-b634-af4ce4c1c2cc=<spool id>
  …
  line 10   imported  pressure_advance   pressure_advance pressure_advance=0.05, spool #13 (bound to k1se/T1, colour #E8732A) → calibration #1
  line 11   imported  flow_ratio         flow_ratio flow_ratio=0.931, spool #13 (bound to k1se/T1, colour #E8732A) → calibration #2
  line 12   imported  input_shaper       input_shaper shaper_type_x=ei, shaper_freq_x=69, shaper_type_y=ei, shaper_freq_y=69 → calibration #3
  line 20   imported  slicer_profile     max_volumetric_speed max_volumetric_speed=20, spool #13 (bound to k1se/T1, colour #E8732A) → calibration #4
  line 27   imported  slicer_profile     max_volumetric_speed max_volumetric_speed=22, spool #13 (bound to k1se/T1, colour #E8732A) → calibration #5
…/u1.jsonl: 1 lines — 1 imported (0 calibration records), 0 skipped, 0 refused

$ 3d calibrate check k1se_dense2_orange_pla.gcode --printer k1se
CALIBRATION k1se k1se_dense2_orange_pla.gcode
  WARN       T1   retraction            new_material_type  no PLA recorded yet (оранжевый)
  WARN       T1   temp_tower            new_material_type  no PLA recorded yet (оранжевый)
  PASS       -    input_shaper          current            #3 shaper_freq_x=69, shaper_freq_y=69, shaper_type_x=ei, shaper_type_y=ei (klipper_shaper_calibrate, 2026-10-15)
  PASS       T1   flow_ratio            current            #2 flow_ratio=0.931 (manual, 2026-10-15)
  PASS       T1   max_volumetric_speed  current            #5 max_volumetric_speed=22 (manual, 2026-10-16)
  PASS       T1   pressure_advance      current            #1 pressure_advance=0.05 (manual, 2026-10-15)
  PASS       -    factory state assumed (never recorded, no maintenance since): bed_mesh, belt_tension, rotation_distance, z_offset
STATUS: WARN
```

A second import: `28 lines — 0 imported (0 calibration records), 24 skipped, 4 refused`. With
`--method 01a140fe-…-af490ca0bd5b=klipper_shaper_calibrate` line 1 becomes record #6 (70.6 Hz),
superseded by #3 at once because it was measured earlier. The black PLA lines stay refused until
their spool is named with `--spool`.

## `history`

`GET /v1/settings/history`, newest change first: id, time, printer/head, kind, status, source,
key, `new`, and the calibration record it produced.

## Examples

```
3d settings import --dry-run
3d settings import
3d settings import ~/.local/share/3d-cli/printer-history/k1se.jsonl --method 01a140fe-3d83-768e-b634-af490ca0bd5b=klipper_shaper_calibrate
3d settings history --printer k1se --kind pressure_advance
3d settings history --printer k1se --since 2026-10-16 --json | jq '.changes[] | {at, kind, key, new}'
```

Not yet (#264): `3d calibrate record` writing journal rows, `config_blob` with diffs, `3d settings
rollback`, the read-only `drift_scan`, and the CLI's own offline append.
