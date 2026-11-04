# Spoolman native fields vs PrintOps extras

Rule: **Spoolman is the single store for anything it natively has; PrintOps stores only what Spoolman
cannot.** Audit against the real Spoolman 0.27.0 `GET /api/v1/openapi.json` (schemas `Vendor`,
`Filament`, `Spool`, `Tag`; checked on a throwaway container). Native fields:

- vendor: `name, comment, empty_spool_weight, external_id`
- filament: `name, vendor, material, price, density, diameter, weight, spool_weight, article_number, comment,
  settings_extruder_temp, settings_bed_temp, color_hex, multi_color_hexes, multi_color_direction, external_id`, tags
- spool: `first_used, last_used, price, initial_weight, spool_weight, used_weight, remaining_weight, location,
  lot_nr, comment, archived`, tags (`POST /spool/{id}/tag`)

## Extra fields (`EXTRA_FIELDS`)

| Field | Verdict | Evidence |
|---|---|---|
| vendor `spool_tare_g` | **drop + migrate** → `vendor.empty_spool_weight` | Spoolman's own `/measure` and remaining maths resolve tare as spool.spool_weight > filament.spool_weight > vendor.empty_spool_weight; the extra was the same number. (The vendor schema key is `empty_spool_weight`, not `spool_weight`.) |
| spool `printops_slot` | **drop + migrate** → `spool.location = slot:<printer>/<head>` | `load` already wrote both; the slot is `location` minus `slot:`. One placement record means `location_conflict` cannot exist any more (anomaly kind removed). |
| spool `printops_location_id` | **drop** | Duplicated the `location` text. Its only use was surviving a location rename; PrintOps keeps former paths (`location_alias`) and `resync` rewrites former-path text, so the text alone is enough. Native `PATCH /location/{old}` renames a location for all spools, but answers 404 for names containing `/` (probed), so PrintOps keeps its own subtree rename. |
| spool `opened_at` | **drop + migrate** → `spool.first_used` | `first_used` accepts writes; `/use` and `/measure` only set it when empty, so `load` / `receive --opened` set it and a later first use never overwrites it. Migration keeps the earlier of the two (`adjust`). |
| spool `tag_uids` | **drop + migrate** → Spoolman tags | Native tags: `POST /spool/{id}/tag` (UID normalised to upper-case hex, unique across spools and filaments, 409 names the holder, re-linking to the holder is a no-op). Reconcile compares the printer's RFID UID with these. |
| filament `nozzle_temp_min/max`, `bed_temp_min/max` | **keep** (+ fill native) | The gates need a range; native `settings_extruder_temp` / `settings_bed_temp` are single integers. The migration fills an empty native value with the midpoint so Spoolman's UI shows something, never overwrites one. |
| spool `remaining_confidence_g` | keep | No native uncertainty. |
| spool `last_dried_at`, `dry_state`; filament `hygroscopic_class`, `drying_temp_c`, `drying_hours`, `abrasive`, `max_volumetric_mm3_s` | keep | No native equivalent. |
| filament `max_speed_mm_s` (mm/s, new) | keep | No native print-speed field (filament has only `settings_extruder_temp` / `settings_bed_temp`). Default 300; 600 for PLA+ (material `PLA+` or a name containing `PLA+`), from ONE rule table in `printops.filament_defaults`; applied when PrintOps creates a filament and by `3d printops fields fill-defaults` on existing ones, only where the field is empty. Not `max_volumetric_mm3_s` (mm³/s). |
| filament `measured_hex` | keep | Different meaning from `color_hex` (what the spool looks like, not the catalogue colour); reconcile prefers it. |
| vendor `spool_kind` | keep | No native spool-type field. |
| spool `external_key` | keep | Native `external_id` exists on vendor and filament only, not on spool. It is now the only record of an import key (see below). |
| spool/filament `slicer_filament_preset` | keep | No native equivalent. |

## PrintOps DB shadow copies

| Table / column | Verdict |
|---|---|
| `spool_import` | **dropped** (migration `0106_drop_spool_import.sql`): a shadow of the extra `external_key`, which `receive` always wrote. Imports and `receive` now look the key up in Spoolman inside the write transaction. |
| `spool_tare_choice` | keep: provenance (where the tare PrintOps wrote came from), not a copy of the value. |
| `ledger` | keep: the interval history; `tare_g` / `gross_g` there are the values AT weighing time. |
| incident / exposure / calibration / settings_change rows with vendor, material, lot | keep: snapshots at event time = history. |
| `location`, `location_alias` | keep: the location tree (kinds, capacity, labels, former paths) has no Spoolman equivalent; only the path text is shared. |
| `spool_sync` (migration 0105) | the sync engine's bookmark of the last Spoolman state it processed. |

## Migration

`3d printops fields migrate-native [--apply]` (REST `GET|POST /v1/fields/migrate-native`): dry run first,
idempotent, never overwrites a native value (`conflict`), deletes a legacy field definition only when no
item holds a value any more (Spoolman destroys stored values with the definition). See
[printops.md](../commands/printops.md#fields). `3d printops health` reports leftovers as
`spoolman.legacy_fields`.
