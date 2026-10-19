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
3d calibrate gen pa|flow|combo|temp|shaper|plate1|dense --printer P -o OUT --filament-preset NAME
                 [--machine-preset NAME] [--process-preset NAME] [--material PLA] [--color #RRGGBB]
                 [--nozzle-temp C] [--only T,…] [--skip T,…] [--pa-range LOW:HIGH] [--pa-step S]
  common (all but gen): [--json] [--api URL] [--token TOKEN]
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

## `gen` — calibration prints ([#168](https://github.com/alex-mextner/3d-cli/issues/168))

Local, no API. Slices with the Orca CLI (`SLICER=/path/to/orca` to force one) with the presets the
printer and the spool really print with ([#293](https://github.com/alex-mextner/3d-cli/issues/293)):
`--machine-preset`/`--process-preset` (default: the printer's `slicer: {machine, process}` in
`fleet/printers.yaml`) and `--filament-preset` (the spool's preset; required). User presets are
found in the repo's `profiles/snapmaker-orca/` (D10), then Orca's user folder, and inherit from the
system presets of the printer model (`slicing/calibration_gen.py` `PRESETS`; today the Creality
K1 SE). No preset → an error, never Orca's stock presets (a calibration sliced with stock flow, PA
and acceleration measures settings you do not print with). The G-code ends with
`; 3d calibrate presets: machine=… (source); process=…; filament=…`, plates also write them to
`NAME.json`. Per-test overrides go on top; then thumbnails and a slice-warnings record, so
`3d print-preflight FILE --printer P` can PASS it.

- `pa` — hollow 40×40×30 mm single-wall tower, ACCEL=500, SQUARE_CORNER_VELOCITY=1, 100 mm/s
  walls, slicer PA off; `TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0
  FACTOR=0.005` after `START_PRINT` ([Klipper method](https://www.klipper3d.org/Pressure_Advance.html);
  our own code, no Ellis pattern). PA = best height (mm) × 0.005. The stock PA is restored before `END_PRINT`.
- `flow` — 5 tiles, one object each; `M221 S90…S110` after each `EXCLUDE_OBJECT_START`. The
  report prints the resulting flow_ratio per tile (preset ratio × factor).
- `combo` — one plate for the [express calibration](../printops/express-calibration.md) of a new
  spool. Front: PA lines on the bare plate right after `START_PRINT`, by default PA 0…0.10 in steps
  of 0.01 (PETG 0…0.15), or `--pa-range LOW:HIGH --pa-step S` for a fine pass; each is labelled
  with seven-segment digits = PA × 1000 (with a point: `42.5` = 0.0425); a line is 6 mm slow
  (20 mm/s), 14 mm fast (120 mm/s), 6 mm slow, with `SET_PRESSURE_ADVANCE` before it. Back: the 5 flow
  tiles (20×20×3 mm). PA, M221 and the acceleration are global, so the block starts with `M221 S100`
  and ends at the stock PA and the process acceleration; it needs a relative-E slice; each tile re-sets its `M221` whenever the slicer enters it, and `END_PRINT` is
  preceded by `M221 S100` and the stock PA. The temperature tower stays a separate print.
- `temp` — temperature tower: one 8 mm block per 5 °C, hottest at the bottom (PLA 230→200,
  PLA+ the same, PETG 250→220). A block is two 9 mm pillars with a 14 mm bridge slab on top:
  the gap shows stringing, the slab bridging. Each block's temperature stands 0.6 mm up from its
  floor in the gap (seven-segment digits, no overhang). `M104 S<t>` goes on the first layer of
  each block (after Orca's `;Z:` marker); the first block prints at the slice's own temperature.
  `--nozzle-temp` is refused. K1 SE, PLA: ~43 min, ~8 g.
- `plate1` — the filament plate of the calibration series (`lib/slicing/calibration_plate.py`,
  `lib/slicing/calibration_plates/filament.py`): PA lines, a max-volumetric-speed strip (label =
  mm³/s), flow tiles (15×15 mm, 5 solid layers), bridges 8/11/14 mm, overhangs 45–60°, a retraction
  tower (0.4–1.2 mm, its own retractions rescaled per block) and a temperature tower (230→200 °C,
  0.3 mm layers). Objects set their parameter on entry and restore it on exit; the temperature
  tower prints last, alone (`print_sequence` by object), `extruder_clearance_radius` behind the
  rest and under `extruder_clearance_height_to_rod`. `--only`/`--skip` pick tests (`pa`, `mvs`,
  `flow`, `bridge`, `overhang`, `retraction`, `temp`); the layout is packed again, so a skipped test
  leaves no hole; `pa` and `mvs` are hand-written lines and need at least one other test beside
  them (`--only pa,temp`; `--only temp` prints the temperature tower alone). Next to the G-code:
  `NAME.json`, the map object → parameters, bbox, Z bands,
  labelled items, how to read it, the `record` command. K1 SE, PLA: ~25 min (all tests).
- `dense` — one K1 SE bed (`lib/slicing/calibration_plates/dense.py`, fixed layout for 220×220):
  plate1's tests without PA/flow (MVS strip, bridges 11/14 and 22 mm, overhangs 50–60°, retraction
  tower = the stringing test, temperature tower last and alone) plus mechanics: a ringing tube
  (2 walls, ACCEL 2000→12000 in 2 mm bands, walls at 150 mm/s: ripple spacing s gives f = 150/s Hz),
  a VFA tube (wall speed 50→300 mm/s in the same bands, ACCEL 6000), a 50×50 frame (size; skew from
  the diagonals), pins printed in place (gaps 0.1–0.5), hinges (0.3/0.4), M3/M4 holes, slots for a
  2 mm insert with the insert, Ø1–4 holes, thin walls 0.3–1.0 along X and Y, first-layer patches
  and a top-surface patch. Ringing and VFA print by layer: their bands rewrite only their own lines,
  and after each visit the engine re-sends the slicer's acceleration and fan and balances any
  extrusion a band changed. Labels are one-layer digits on the bed. The 22 mm bridge needs
  `print-preflight --bridge-max 25`. K1 SE, PLA: ~44 min + START_PRINT, ~13 g; OpenSCAD on PATH.
- `shaper` — a text file of console commands for printers with an accelerometer (K1 SE: ADXL345):
  G28, nozzle and part fan off, `TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=38` (a hot nozzle keeps
  the hotend fan on, and its vibration masks the resonance; the K1's G28 heats it to 140 °C),
  ACCELEROMETER_QUERY, MEASURE_AXES_NOISE, one `SHAPER_CALIBRATE` for both axes (Creality's fork
  copies an `AXIS=Y` result into X). No SAVE_CONFIG: check the values first, then save.

`TUNING_TOWER` and `M221` are inert for the macro check (they never move the head). Orca's
`calc_exclude_triangles` error is accepted in the sidecar only when the machine has no bed
exclude area.

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
