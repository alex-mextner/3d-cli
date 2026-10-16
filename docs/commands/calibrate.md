# `3d calibrate` — calibrations per printer head and filament

Issue [#167](https://github.com/alex-mextner/3d-cli/issues/167) (plan §7.2, §8; decision D6).
Talks to 3d-api like [`3d printops`](printops.md) (`PRINTOPS_API`, `PRINTOPS_TOKEN` or
`~/.config/3d-cli/printops.yaml`).

```
3d calibrate plan --printer P [--head T] [--spool ID] [--plate PLATE] [--all]
3d calibrate record KIND --printer P [--head T] [--spool ID | --filament ID] [--lot LOT] [--plate PLATE]
                    --values NAME=V[,NAME=V…] [--method M] [--note TEXT] [--evidence URL[,URL…]]
                    [--valid-until DATE]
3d calibrate ls [--printer P] [--head T] [--kind KIND] [--status planned|recorded|abandoned] [--all] [--limit N]
3d calibrate show ID
3d calibrate check FILE.gcode --printer P [--spools T1=ID,…] [--plate PLATE]
                   [--accept calibration:KIND[,…] --reason TEXT]
  common: [--json] [--api URL] [--token TOKEN]
```

## Why

A flow ratio or pressure advance measured for one filament on one head is wrong for another
material, vendor or nozzle; after a nozzle swap or PTFE change every old value is stale. Nothing
in the slicer stops a print with someone else's numbers. 3d-api keeps every calibration with the
exact scope it was measured on and answers, for the spool loaded on a head right now, what is
missing — and the `calibration_current` gate (`3d calibrate check`) refuses a G-code whose footer carries
other values than the recorded ones.

## The catalog: `fleet/calibrations.yaml`

| Kind | Scope | Lot | Baseline | Invalidated by | G-code footer check |
|---|---|---|---|---|---|
| `flow_ratio` | head × nozzle × filament | yes | required | nozzle_swap, nozzle_diameter, hotend | `filament_flow_ratio` = recorded ± 0.0015 |
| `pressure_advance` | head × nozzle × filament | no | required | + extruder, ptfe | `pressure_advance` = recorded ± 0.0005 (when `enable_pressure_advance`) |
| `temp_tower` | filament | yes | required | — | — (`filament_temp_range`, #164, checks the range) |
| `max_volumetric_speed` | head × nozzle × filament | no | required | nozzle_swap, nozzle_diameter, hotend | `filament_max_volumetric_speed` ≤ recorded |
| `retraction` | head × filament | no | required | ptfe, hotend, extruder | — |
| `input_shaper` | printer | — | assumed | kinematics, belts, move, repair | — |
| `z_offset`, `bed_mesh` | printer × plate | — | assumed | plate, nozzle swap, repair, … | — |
| `head_offsets` (U1 only) | printer | — | assumed | head_swap, nozzle_swap, hotend, repair | — |
| `rotation_distance` | head | — | assumed | extruder | — |
| `belt_tension` | printer | — | assumed | belts, kinematics | — |

*Baseline* `required`: a head without a record for the loaded filament needs one.
*Baseline* `assumed`: no record means the factory state, which holds until maintenance invalidates
it. The file's header documents every key (values with ranges, methods, apply targets, estimates).

## Requirement statuses and the policy

For each usable head (the broken U1 T2 is listed as skipped) the engine takes the spool bound to
it (`3d printops spool load`) and the nozzle on it (#166's nozzle installs when present, else
`fleet/printers.yaml`) and gives every kind a status:

| Status | Meaning | Default level (`fleet/policy.yaml` `calibrations:`) |
|---|---|---|
| `current` | a valid record matches head, nozzle, filament (and lot) | PASS |
| `assumed` | `assumed` kind, never recorded, no maintenance since | PASS |
| `new_lot` | a record for this filament exists, from another lot | **warn** (D6) |
| `new_filament` | this vendor + material is recorded, this product is not | **block** (D6) |
| `new_vendor` | this material type is recorded, not from this vendor | **block** (D6) |
| `new_material_type` | nothing recorded for this material type | **block** (D6) |
| `new_nozzle` | this filament was measured with another nozzle | block |
| `invalidated` | the record predates a `calibration.invalidated` event (maintenance, nozzle swap) | block |
| `expired` | past the record's `valid_until` | warn |
| `gcode_mismatch` | (gate) the G-code carries another value than the record | block |
| `no_spool` | the kind needs a filament and no spool is bound to the head | UNVERIFIED |
| `no_plate` | the kind is per plate, the printer has several and no `--plate` was given | UNVERIFIED |

block = FAIL, warn = WARN, off = not reported. Overrides per kind (`temp_tower` only warns,
`max_volumetric_speed` and `retraction` warn) and per printer (the neighbours' and hackspace
printers are `off`, decision D7). Precedence and schema: `lib/printops/calibration_policy.py`.

## `plan`

Real output (scratch 3d-api with the babushkin-dom spools, a PLA loaded on the K1 SE, nothing
recorded yet):

```
$ 3d calibrate plan --printer k1se
k1se: 2 FAIL, 3 WARN; ~130 min, ~55 g
  FAIL       T1   flow_ratio            new_material_type  no PLA recorded yet (жёлтый)
  FAIL       T1   pressure_advance      new_material_type  no PLA recorded yet (жёлтый)
  WARN       T1   max_volumetric_speed  new_material_type  no PLA recorded yet (жёлтый)
  WARN       T1   retraction            new_material_type  no PLA recorded yet (жёлтый)
  WARN       T1   temp_tower            new_material_type  no PLA recorded yet (жёлтый)
```

On the U1 the broken head is listed as `skipped T2: head T2 does not work (…)`; a head without a
bound spool shows `UNVERIFIED … no_spool`; a nozzle swap logged by maintenance shows
`FAIL T3 pressure_advance invalidated #4 predates nozzle_swap on 2026-10-15: …`.

The estimate adds up the catalog's minutes and grams of every FAIL/WARN item. `--all` also
lists PASS items. `plan` is a report and exits 0.

## `record`

A manual entry: creates the calibration and records its values in one step. The scope is taken
NOW — the head's current nozzle and the spool bound to it (or `--spool` / `--filament`, and
`--lot` to override the spool's lot). A newer record for the identical scope supersedes the
older one. Values are checked against the catalog (`flow_ratio=3` is refused: 0.5..1.5).

```
3d calibrate record pressure_advance --printer u1 --head T3 --values pressure_advance=0.035 --method pa_pattern
3d calibrate record input_shaper --printer k1se --values shaper_type_x=mzv,shaper_freq_x=54.2,shaper_type_y=ei,shaper_freq_y=41 --method k1_auto
3d calibrate record temp_tower --filament 7 --lot B7 --values nozzle_temp_min=205,nozzle_temp_best=215,nozzle_temp_max=230
```

## `check` — the `calibration_current` gate

Reads the G-code's `; key = value` settings (head and tail), the used tools (`; filament used
[mm]`, else `Tn` lines) and its SHA-256, and asks 3d-api for the gate. For every used tool
(G-code `T0` = head `T1`): the head's requirements, plus the footer values against the record:

- `; filament_flow_ratio`, `; pressure_advance`, `; filament_max_volumetric_speed` per
  filament (comma list). **Observed** in Snapmaker Orca, Orca and Bambu Studio exports
  (babushkin-dom `print/`, 2026-10): a multi-filament file often carries ONE value for these keys,
  the first filament's. Then only `T1`'s value is checked; the others are UNVERIFIED, never
  assumed equal.
- `; enable_pressure_advance = 0` leaves K to the firmware: PASS when the record was applied to
  Klipper, WARN otherwise.
- A tool on a head the printer does not have, or on an unusable head, is UNVERIFIED.

The text report collapses the `assumed` kinds into one PASS line; `--json` lists every item.

FAIL exits 1. `--accept calibration:<kind> --reason "…"` turns that kind's FAIL into WARN and
logs a `calibration.accepted` event (who, which G-code SHA, why); it needs a token with the
`write` scope. `3d print-preflight` does not run this gate yet: it joins print-preflight with
the PrintOps checks of [#164](https://github.com/alex-mextner/3d-cli/issues/164), which call the same endpoint.

## Events

`calibration.planned`, `calibration.recorded` (with the superseded ids), `calibration.accepted`.
Consumed: `calibration.invalidated` from maintenance (#166): payload `{printer, head|null,
trigger, kinds|null, reason, maintenance_id, nozzle_install_id}` voids records of that printer
(head) recorded before the event — the listed kinds, or every kind whose `invalidated_by` names
the trigger.

## Lint

`3d lint --printops` adds `calibrations/catalog` (the catalog breaks its schema),
`calibrations/policy` (the policy names an unknown kind or printer), `calibrations/unknown-kind`
and `calibrations/stale-run` (a run planned more than 14 days ago without a result).

## Exit codes

0 success (and `check` without FAIL), 1 `check` FAIL or an API error, 2 usage error or missing file.
