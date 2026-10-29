# `3d calibrate` — calibrations per printer head and filament

Issue [#167](https://github.com/alex-mextner/3d-cli/issues/167) (plan §7.2, §8; decision D6).
Talks to 3d-api like [`3d printops`](printops.md) (`PRINTOPS_API`, `PRINTOPS_TOKEN` or
`~/.config/3d-cli/printops.yaml`).

```
3d calibrate plan --printer P [--head T] [--spool ID] [--plate PLATE] [--all]
3d calibrate record KIND --printer P [--head T] [--spool ID | --filament ID] [--lot LOT] [--plate PLATE]
                    [--mix KEY] --values NAME=V[,NAME=V…] [--method M] [--note TEXT]
                    [--evidence URL[,URL…]] [--valid-until DATE]
3d calibrate ls [--printer P] [--head T] [--kind KIND] [--mix KEY] [--status planned|recorded|abandoned] [--all] [--limit N]
3d calibrate show ID
3d calibrate check FILE.gcode --printer P [--spools T1=ID,…] [--plate PLATE]
                   [--accept calibration:KIND[,…] --reason TEXT]
3d calibrate gen pa|flow|combo|temp|shaper|plate1|dense|dense2|fit --printer P -o OUT [--material PLA] [--color #RRGGBB]
                 [--nozzle-temp C] [--only T,…] [--skip T,…] [--pa-range LOW:HIGH] [--pa-step S]
                 [--machine-preset NAME] [--process-preset NAME] [--filament-preset NAME]
                 [--head T] [--spool ID] [--api URL] [--token TOKEN]
3d calibrate fit plan|gen|quick|read|record|check …   quick fit calibration (section `fit` below)
  common (all but gen, which takes --api/--token only for its default filament preset): [--json] [--api URL] [--token TOKEN]
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
| `filament_color` | filament | yes | required, policy `off` | — | — (read by the mix model, `3d mix`) |
| `mix_color` | printer × mix | — | assumed | — | — (read by the mix check of a plate, #418) |
| `fit_clearance` | head × nozzle × filament | no | required, policy `warn` | nozzle_swap, nozzle_diameter, hotend | — (the readings of `3d calibrate fit`, read by `3d fits resolve`) |

*Baseline* `required`: a head without a record for the loaded filament needs one.
*Baseline* `assumed`: no record means the factory state, which holds until maintenance invalidates
it. The file's header documents every key (values with ranges, methods, apply targets, estimates).

**Colour kinds** ([#412](https://github.com/alex-mextner/3d-cli/issues/412), plan §20.4) are never
required before a print and never appear in `plan`: `filament_color` (the colour a filament really
prints: the per-spool correction of the Spoolman hex) is `off` in `fleet/policy.yaml`, and the
engine skips every `mix`-scoped kind. Their values have the type `color` (`#RRGGBB`, stored upper
case). `mix_color` is keyed by `--mix`, the canonical key of a colour mix
(`lib/printops/mix_key.py`): the layer height and the Spoolman filament ids of its layer cycle,
reduced to the shortest repeating unit and rotated to the smallest id sequence compared as
numbers (`h0.12:f3.f12.f7`; patterns `431` and `314` on the same filaments are one mix), or a
Local-Z row's whole-percent shares by filament id (`h0.12:lz:f1=25,f4=75`). Any rotation is
accepted and stored canonical; a newer record of the same printer and key supersedes the older,
a record of another key never does.

## Requirement statuses and the policy

For each usable head (a head marked `usable: false` in the fleet is listed as skipped) the engine takes the spool bound to
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

A head marked unusable in the fleet is listed as `skipped T<n>: <reason from the fleet>`; a head without a
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
3d calibrate record filament_color --spool 12 --values hex=#C8902A,spoolman_hex=#F6C700 --method mix_tester_photo
3d calibrate record mix_color --printer u1 --mix h0.12:f3.f12.f7 --values hex=#B5654F,predicted_hex=#A86A55
3d calibrate ls --kind mix_color --mix h0.12:f12.f7.f3
```

Calibrations done before 3d-api knew of them (written to the offline settings-history buffer) are
brought in by [`3d settings import`](settings.md): those records keep the time of the measurement,
not the import time, and never supersede a later record of the same scope.

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

Slices with the Orca CLI (`SLICER=/path/to/orca` to force one) with the presets the
printer and the spool really print with ([#293](https://github.com/alex-mextner/3d-cli/issues/293)):
`--machine-preset`/`--process-preset` (default: the printer's `slicer: {machine, process}` in
`fleet/printers.yaml`) and `--filament-preset` (the spool's preset). Without the flag and with 3d-api configured
([#294](https://github.com/alex-mextner/3d-cli/issues/294)) the preset is the `slicer_filament_preset`
of the spool bound to `--printer P` (`--head T` on a multi-head printer; `--spool ID` takes that
spool instead of the binding), read through 3d-api: the spool's own Spoolman extra field, else its
filament's; set it with `3d printops spool preset ID NAME [--filament]`. A head with no (or several)
bound spools, a spool without a preset, or 3d-api unreachable is an error with the fix — never the
stock preset; without 3d-api configured `--filament-preset` stays required. The G-code stamp and
`NAME.json` record where the name came from (`3d-api spool #12
slicer_filament_preset on filament, bound to k1/T1`, or `--filament-preset`). User presets are
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
  `--nozzle-temp` is refused. K1 SE, PLA: ~43 min, ~8 g. `print-preflight`'s
  `filament_temp_range` FAILs when the tower's first (hottest) temperature, 230 °C for PLA or
  250 °C for PETG, is outside the loaded spool's `nozzle_temp_min/max`; the colder blocks are
  never flagged. The tower is meant to cross the window: read the other checks and start it
  with a [forced start](printer.md#forcing-a-start) (Telegram approval).
- `plate1` — the filament plate of the calibration series (`lib/slicing/calibration_plate.py`,
  `lib/slicing/calibration_plates/filament.py`): PA lines, a max-volumetric-speed strip (label =
  mm³/s), flow tiles (15×15 mm, 5 solid layers), bridges 8/11/14 mm, overhangs 45–60°, a retraction
  tower (0.4–1.2 mm, its own retractions rescaled per block) and a temperature tower (230→200 °C,
  0.3 mm layers). Objects set their parameter on entry and restore it on exit; the temperature
  tower prints last, alone (`print_sequence` by object), `extruder_clearance_radius` behind the
  rest and under `extruder_clearance_height_to_rod`. `--only`/`--skip` pick tests (`pa`, `mvs`,
  `flow`, `bridge`, `overhang`, `retraction`, `temp`); the layout is packed again, so a skipped test
  leaves no hole. Next to the G-code: `NAME.json`, the map `calib-plate/1` (object → parameters, bbox,
  Z bands, labelled items, how to read it, the `record` command, the `underlay` block). K1 SE, PLA:
  ~25 min (all tests). Every plate stands on the [shared underlay](#the-shared-underlay-320).
  `--pa-range LOW:HIGH --pa-step S` set the PA lines (combo and plates; refused when `pa` is
  skipped); `-o` must not end in `.json` (that name is the map's). A plate keeps at least one
  test Orca slices by layer (`flow`, `bridge`, `overhang`, `retraction`): for PA lines alone use
  `gen combo --pa-range`.

  ```bash
  3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --skip pa,flow -o k1se_plate1_orange.gcode
  3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --only pa,flow --pa-range 0.03:0.05 --pa-step 0.0025 -o k1se_pa_fine.gcode
  ```
- `dense` — one K1 SE bed (`lib/slicing/calibration_plates/dense.py`, fixed layout for 220×220):
  plate1's tests without PA/flow (MVS strip, bridges 11/14 and 22 mm, overhangs 50–60°, retraction
  tower = the stringing test, temperature tower last and alone) plus mechanics: a ringing tube
  (2 walls, ACCEL 2000→12000 in 2 mm bands, walls at 150 mm/s: ripple spacing s gives f = 150/s Hz),
  a VFA tube (wall speed 50→300 mm/s in the same bands, ACCEL 6000), a 50×50 frame (size; skew from
  the diagonals), pins printed in place (gaps 0.1–0.5), hinges (0.3/0.4), M3/M4 holes, slots for a
  2 mm insert with the insert, Ø1–4 holes, thin walls 0.3–1.0 along X and Y, first-layer patches
  (`--no-underlay` only: the bare underlay is the first-layer map) and a top-surface patch. Ringing and VFA print by layer: their bands rewrite only their own lines,
  and after each visit the engine re-sends the slicer's acceleration and fan and balances any
  extrusion a band changed. Labels are one-layer digits on the bed. The 22 mm bridge needs
  `print-preflight --bridge-max 25`. K1 SE, PLA: ~44 min + START_PRINT, ~13 g; OpenSCAD on PATH.
- `dense2` — the second K1 SE bed (`lib/slicing/calibration_plates/dense2.py`): only what `dense`
  left open, on a 7×7 grid of 30 mm cells, every sample facing the same way with a raised
  seven-segment label (one size, 5 mm) at the front of its cell. Hole tiles side by side: plain
  mesh (PL), polyholes (PH), arc fitting (AF), xy hole compensation +0.1 (HC); Orca's CLI has no
  per-object settings for STLs, so PH and HC are drawn into the mesh and AF's visits come from a
  second slice with `enable_arc_fitting` (swapped only where both slices enter and leave a visit
  at the same place). Ovality targets Ø3/5/8/10 with X/Y edges, a 5 mm ruler and a Ø10 ring; pins
  in holes 0.15/0.25/0.35 and, with a 0.4 mm first-layer chamfer, 0.10/0.15/0.20; elephant-foot
  blocks without/with the chamfer; M3 2.7/3.0/3.6, M4 3.5/4.0/4.7; slots 2.05/2.15/2.25 and the
  2 mm tab; hinges 0.25/0.35; thin walls 0.25/0.35/0.45 along Y and X; the 14 mm bridge and the
  60° overhang on 8 mm bases; MVS strip 24–32 mm³/s; stringing pillars; a top tile; a temperature
  mini-tower last and alone; corner fiducials (ring + rulers) and, with `--no-underlay`, 20 mm
  one-layer patches on the free cells. Brims: only with `--no-underlay` (or for an object outside
  the underlay), 3 mm on every object whose base side is under 10 mm or that is taller than
  its base is wide (`Plate.brim`): hand-written for objects printed by layer (Orca's own brim
  would go on every object), Orca's own in the sequential slice for the tower. On the underlay the
  bridge and overhang stand on their 8 mm bases with no brim, the thin walls and stringing pillars
  on no tile at all. MVS 20
  and inner walls at 8000 come from the user presets (#266); the map's `slicer_overrides` lists
  what the plate sets on top of them. The engine now picks a Z band by the layer's range (Orca's
  `;Z:` is the layer's top), so a band's setting starts on the layer its step does (#301).
- `shaper` — a text file of console commands for printers with an accelerometer (K1 SE: ADXL345):
  G28, nozzle and part fan off, `TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=38` (a hot nozzle keeps
  the hotend fan on, and its vibration masks the resonance; the K1's G28 heats it to 140 °C),
  ACCELEROMETER_QUERY, MEASURE_AXES_NOISE, one `SHAPER_CALIBRATE` for both axes (Creality's fork
  copies an `AXIS=Y` result into X). No SAVE_CONFIG: check the values first, then save.

`TUNING_TOWER` and `M221` are inert for the macro check (they never move the head). Orca's
`calc_exclude_triangles` error is accepted in the sidecar only when the machine has no bed
exclude area.

#### The shared underlay ([#320](https://github.com/alex-mextner/3d-cli/issues/320))

Every plate of `gen` (`plate1`, `dense`, `dense2`) prints all its tests on ONE underlay: a solid
layer under the whole test area, so the plate comes off in one piece and stays as a labelled sample
of the spool. It replaces the per-test brims and the separate tile bases.

- **Layers.** A single-head printer prints exactly 1 layer. A multi-head printer prints 1–2
  (`calibrations.underlay_layers` in `fleet/policy.yaml`, default 1; anything else fails `3d lint`),
  in its own contrasting filament: the head is the one that contrasts most with the tests'
  (CIEDE2000 ≥ 40, the [#263](https://github.com/alex-mextner/3d-cli/issues/263) check), or
  `--underlay-tool` (the same check applies). `gen` on a printer with several heads needs the colours
  loaded on them (`--head-colours T1=#000000,T4=#FFFFFF`; the tests' head, `--head`, defaults to the
  first and to `--color`): the G-code selects the underlay's head before the first layer and changes
  to the tests' head once, after the underlay's last layer (`T3` … `T0`). A single-head printer has
  no tool change and rejects `--underlay-tool`. The U1's own plate (one Orca object per test, white
  and black parts) is [below](#u1-plate-1-every-test-on-a-white-underlay-320-322-308).
- **Outline.** The union of every test's footprint, the label strips and the hand-written blocks
  (PA lines, MVS strip), each grown by 3 mm, joined into one connected piece. A test stands on it
  (`on`: a foot of the underlay's height under its base, so it starts at the underlay's top) or in a
  hole of it (`cutout`, the footprint plus 1 mm). The outline minus the cut-outs stays one piece;
  the generator refuses a layout that splits it.
- **Catalog.** Every test declares `on` or `cutout` with a reason (`UNDERLAY` of its plate
  module). Print-in-place parts (pins in holes, hinges), loose parts (the insert, the slot tab),
  elephant-foot blocks and through holes that are measured with a pin (hole tiles, M3/M4 holes,
  small holes) are `cutout`; a test with a movable part cannot be `on`.
- **Order.** The underlay is the first object of the print. The hand-written blocks, lifted by its
  height, print right after its layers; the sequential temperature tower is sliced alone and lifted
  by its height, so it starts on the finished underlay; the gantry clearance check includes the
  underlay height.
- **Brims.** None on an underlay plate (`Plate.brim`/`brim_width` apply only to an object outside
  it) and no first-layer patches: the bare underlay between the tests is the first-layer map.
- **Map.** `NAME.json` (`calib-plate/1`) carries `underlay`: `layers`, `top_mm`, `bbox`, `area_mm2`,
  `pieces` (1), `cutouts`, `bare` (the cells where nothing stands), `head` and `read.light`
  (`raking` on a single-head printer, `contrast` on a multi-head one).
- **Single tests.** `pa`, `flow`, `combo` and `temp` carry no underlay: each declares `cutout` with
  its reason (`KIND_UNDERLAY` in `lib/slicing/calibration_gen.py`: a lone tower or tile row has no
  small part to tip, and a sheet would hide the first layer or shift the Z steps they read);
  `--no-underlay` and the head flags are refused for them.
- `--no-underlay` prints on the bare bed with the old brim rule and the first-layer patches.

`3d calibrate underlay-map PLATE.json --rectify R.json [--image IMG] [--cell-mm N] [--json]` reads
the bare underlay of a rectified photo (`calibrate rectify`) as the first-layer map: per cell the
mean brightness and texture, the uniformity of the plate and the hints `bed_tilt`, `z_offset`
(gaps = nozzle too high under contrast light; rough cells under raking light) and `patchy`, with
their place in bed mm. The hints are advice to confirm, never applied; fewer than 4 whole cells
in the photo is `needs_manual` (exit 1).

```bash
# multi-head printer: underlay in T4 (white), tests in T1 (black); the check refuses a pair under ΔE 40
3d calibrate gen plate1 --printer MULTI --filament-preset 'Orange PLA @K1SE' --color '#000000' \
  --head-colours T1=#000000,T3=#2D9E59,T4=#FFFFFF --underlay-tool T4 --head T1 -o multi_plate1.gcode
3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --no-underlay -o k1se_bare.gcode
# read the first-layer map from a rectified photo, in 5 mm cells
3d calibrate underlay-map k1se_plate1.json --rectify rect/rect.json --image rect/rectified.png --cell-mm 5 --json
```

### U1 plate 1: every test on a white underlay ([#320](https://github.com/alex-mextner/3d-cli/issues/320), [#322](https://github.com/alex-mextner/3d-cli/issues/322), [#308](https://github.com/alex-mextner/3d-cli/issues/308))

The Snapmaker U1 plate is generated as a Snapmaker Orca project by
`lib/slicing/calibration_plates/u1_underlay.py`. It is not a `gen` sub-plate: the K1 engine
writes one object per STL, and the U1 needs each test as one object with white and black parts.

```sh
cd lib && uv run --project .. --with segno --with pillow \
  python -m slicing.calibration_plates.u1_underlay -o ~/.cache/u1-calib/u1_plate1_underlay_bw.gcode [--date YYYY-MM-DD] [--preview-only]
```

- The underlay is 1–2 layers (`calibrations.underlay_layers`, default 1; `--underlay-layers`), no
  brim, part fan 0 and the bed at 70 °C for the first layer, in the head that contrasts most with
  the tests' T1 (T4 white by default; `--underlay-tool T2|T3|T4`; `--slot-colours` names what is
  loaded; the contrast check refuses ΔE < 40, override included). The tests are in black on T1
  (`T0`) from the layer above. The G-code has exactly one tool change, underlay head → T1, above
  the underlay (#308), and no prime tower. The map's `underlay` block has the head, ΔE, `bare`
  cells and `read.light: contrast`.
- Each test is its own Orca object, so it has its own `EXCLUDE_OBJECT`. The test's white base
  overlaps the underlay sheet by 0.05 mm so the perimeters fuse. Loose pieces (pins, the tab,
  hinges, elephant-foot blocks) stand on white feet in cut-outs of the base.
- Per-object parameters:
  - ironing is an object setting;
  - flow, ringing/VFA ACCEL and speed, retraction and temperature are G-code hooks on the black
    visits; bands are chosen by the layer's mid height (#301);
  - the temperature bands start above every other test;
  - PA lines and the MVS strip are hand-written and run inside their own objects.
- The map `NAME.json` lists each test's `read_by` (`camera_top` / `camera_side` / `manual`,
  tg#22399), the hooks applied per object and layer, the per-layer tool and fan scan, and the
  label (`C<date>P1` and its QR when `segno` is installed).
- Before printing: `3d gcode-thumbnails add`, `3d slice-warnings record` with
  `NAME.slice.log`, and `3d print-preflight --printer u1`.

## `fit` — quick fit calibration ([#573](https://github.com/alex-mextner/3d-cli/issues/573))

One short print (about 10 minutes) and the fit table knows your printer and plastic. The coupon is six
LADDERS of 7 steps, each a fit type on a shape stepped around the value of the
[fit table](../contracts/fits.md) (`lib/data/fits.yaml`):

| # | Ladder | Part | Gaps per face (mm) |
|---|---|---|---|
| 1 | sliding / round | Ø4 pin in holes | 0.05 0.10 … 0.35 |
| 2 | tight / round | the same pin | 0.00 0.05 … 0.30 |
| 3 | press / round | the same pin, UNDERSIZED holes | −0.15 −0.125 … 0.00 (negative = interference) |
| 4 | sliding / slot | 2 mm tab in slots | 0.05 … 0.35 |
| 5 | snap / slot | the tab of a snap hook | 0.15 … 0.45 |
| 6 | press / slot | the tab, undersized slots | −0.15 … 0.00 |

Each step has a raised seven-segment digit (strokes 1 mm, 0.6 mm high) on a lip in front of it; the ladder number
is at the left end of its row; step 1 is the smallest gap. Two spare pins Ø4.00 and two tabs 2.00×6.00 are printed
upright, like the holes, so the XY accuracy that fits depend on is what you measure. `--gap-offset MM` shifts every
gap (negative = tighter). The coupon is 98.5×55×4.5 mm and fits both the K1 SE (220×220) and the U1 (270×270) bed
with a 5 mm margin.

```
3d calibrate fit plan --printer P [--material M] [--fit snap,sliding,tight,press] [--shape round,slot] [--nozzle MM] [--gap-offset MM]
3d calibrate fit gen --printer P [--material M] --out coupon.3mf [--fit ..] [--shape ..] [--gap-offset MM]
3d calibrate fit quick --printer P [--material M] [--out FILE.3mf] [--yes]
3d calibrate fit read --printer P [--material M] --fit F --shape S (--result R | --measured D1,..,D7) [--mating-mm MM] | --from-readout FILE.json
3d calibrate fit record (the flags of read) [--head T1] [--spool ID | --filament ID] [--coupon NAME] [--note TEXT] [--yes]
3d calibrate fit check [--printer P] [--material M] [--max-age-days N] [--nozzle MM]
  common: [--json] (schema `3d.calibrate_fit/1`)   3d-api: [--api URL] [--token TOKEN] (read, record, check)
```

- **`plan`** lists the coupons, their gaps and hole/slot sizes, footprint and bed fit, the print-time estimate
  (house model: plastic volume at 10 mm³/s plus 0.6 s per object visit of a layer, without heating) and the
  exact readout instruction and `record` command of every ladder. Both the K1 SE and the U1 are known (bed, nozzle
  and head come from `lib/data/printers.yaml` and `fleet/printers.yaml`).
- **`gen`** renders every object with OpenSCAD and writes `NAME.3mf` (one build item per ladder, millimetres, in
  bed coordinates, centred on the printer's bed), `NAME.stl` (one watertight mesh, passes `3d mesh`), `NAME.scad`
  and `NAME.json` (the map: plan + object boxes). Take the 3MF through the usual flow: `3d prep` / `3d arrange` /
  `3d retarget` for the printer, so the slicer settings are those of the real profile. For G-code with the
  printer's slicer presets in one step, the same coupon is a plate of the existing machinery:
  `3d calibrate gen fit --printer k1se --filament-preset 'Orange PLA @K1SE' -o fit.gcode` (`--only
  sliding_round,snap_slot` prints single ladders).
- **`quick`** = plan + gen with the defaults (all ladders, the printer's nozzle and head and material). Dry run
  until `--yes`; the file is `fit-coupon-<printer>-<material>.3mf` unless `--out`.
- **`read`** is the result reader. Per ladder push the spare pin/tab into every step and report one word per step:
  `free` goes in and out by hand without force, `press` needs a push and then holds, `fused` will not go in (hole
  closed) or cracked. Pass `--result step=N` (N = the first `free` step; for a press ladder the last step that still
  grips), seven words (`fused,press,press,free,free,free,free`) or `K=word` pairs. With calipers pass
  `--measured D1,…,D7` (the hole diameters / slot widths; `--mating-mm` is the measured pin/tab, default the
  nominal): a step reads `free` when its real gap `(D − pin)/2` reaches the table's value. The reader picks, per
  ladder, the smallest `free` gap (the largest `press` gap for press fits) and prints `delta_mm` against the table
  and the clearance before → after.
  - two readings of one gap keep the tighter result (reported); a looser step below a tighter one is
    **non-monotonic**: recorded with a warning; every step free (or every press step gripping) records with a
    warning that the ladder did not bracket the fit from that side;
  - **no `free` step** (all fused, or only fused/press) is exit 1: `every step is fused: … widen the range (print the
    coupon with a positive --gap-offset, e.g. --gap-offset 0.3)`; nothing is stored. The same for a press ladder that
    never grips or always cracks. Read the wider coupon with the same `--gap-offset`.
- **`record`** is `read` plus the store: a `fit_clearance` calibration record
  (`fleet/calibrations.yaml`, scope head × nozzle × filament, values `readings` = JSON list of the S0 contract
  `{fit_type, shape, gap_mm, result}`). Dry run (the pick, the clearance before → after, where it would go)
  unless `--yes`. The new ladders replace the older readings of the same fit × shape; the others are carried over
  (a record of the same scope supersedes the previous one in 3d-api). In 3d-api the record takes the spool bound to
  the head (`3d printops spool load`) or `--spool ID` / `--filament ID`; a `--material` that is not what is loaded is
  refused before anything is stored. Without 3d-api (no URL configured, or it does not answer) the record is one
  `printops.settings_change/1` line in `~/.local/share/3d-cli/printer-history/<printer>.jsonl` — the offline buffer
  `3d settings import --spool UID=ID` replays into 3d-api (idempotent by uid; the imported record replaces the
  buffer line in every read).
- **`check`** reports per printer × material (all fleet printers × the materials they have records for and the
  model's default, or `--printer`/`--material`): `MISSING` (no record), `STALE` (older than `--max-age-days`,
  default 180, or taken with another nozzle than the printer has now), `PARTIAL` (a ladder never read), `OK`. Exit 1
  when any pair is MISSING or STALE; every line says the command that fixes it. Maintenance invalidations
  (nozzle swap, hotend) and a new material type show up in `3d calibrate plan --printer P` as the kind
  `fit_clearance` (policy `warn`, never a block).

`3d fits resolve --fit sliding --shape round --printer k1se --material PLA` then shows
`status calibrated`, the row `-0.05 mm [calibrated, measured 2026-10-24T…Z] printer k1se, material PLA, nozzle 0.4,
shape round, fit sliding (smallest free gap 0.15 mm of 5 reading(s); calibration record #12)`; the same number
goes into `3d fit-check --fit` and `3d fit-niche --fit` (`lib/fits_sources/calibration.py`). Rows of other
printers, materials, nozzles and shapes are untouched. `THREED_FIT_CALIBRATION_API=off` keeps every `3d fits` run
away from 3d-api (the offline buffer only); with 3d-api configured but unreachable the buffer answers and stderr
says so.

```bash
3d calibrate fit plan --printer k1se --material PLA --json | jq '.coupons[] | {ladder, id, readout}'
3d calibrate fit quick --printer k1se --material PLA --yes     # fit-coupon-k1se-pla.3mf (+ .stl .scad .json)
3d calibrate fit record --printer k1se --material PLA --fit sliding --shape round --result step=3 --yes
3d calibrate fit record --printer k1se --material PLA --fit tight --shape round --measured 4.02,4.07,4.12,4.17,4.22,4.27,4.32 --yes
3d calibrate fit record --printer k1se --material PLA --from-readout readout.json --yes
3d calibrate fit check --printer k1se --material PLA
3d calibrate fit check --printer k1se --material PLA --nozzle 0.6 --max-age-days 90 --api http://192.168.0.39:8765
3d calibrate fit plan --printer u1 --material PETG --fit sliding,snap --shape slot
3d calibrate fit gen --printer k1se --material PLA --out wide.3mf --fit tight --gap-offset 0.3
3d calibrate fit read --printer k1se --material PLA --fit sliding --shape round --measured 4.1,4.2,4.3,4.4,4.5,4.6,4.7 --mating-mm 4.05
3d calibrate fit record --printer k1se --material PLA --fit press --shape round --result step=3 --spool 12 --coupon "k1se-pla-2026-10-24" --note "grey spool" --head T1 --yes
3d calibrate fit record --printer k1se --material PLA --fit snap --shape slot --result step=5 --filament 7 --yes
```

## `photo` — the top-camera photo mode ([#342](https://github.com/alex-mextner/3d-cli/issues/342))

```bash
3d calibrate photo --printer P [--camera C] [--res WxH] [--focus auto|N|sweep] [--focus-roi R] [--exposure E] [--gain G] [--wb K]
                   [--ctrl N=V,…] [--frames N] [--skip K] [--average mean|median|none] [--led on|off]
                   [--settle-ms MS] [--printer-light on|off] [--pose X,Y,Z[,F]] [--move --yes] [--home]
                   [--manifest M | --fiducials ID:X,Y;… [--fiducial-size MM]] [--px-per-mm N]
                   [--area X0,Y0,X1,Y1] [--scale-ref FILE] [--camera-height-mm H] [--no-rectify]
                   [--out DIR] [--save] [--session ID] [--json]
3d calibrate rectify PHOTO (--manifest M | --fiducials ID:X,Y;…) [--fiducial-size MM] [--px-per-mm N]
                   [--area X0,Y0,X1,Y1] [--scale-ref FILE] [--camera-height-mm H] [--out DIR] [--json]
```

`photo` takes the plate photo the vision readout ([#273](https://github.com/alex-mextner/3d-cli/issues/273))
measures, the same way every time. The camera is the top camera (`3d camera`, see
[camera.md](camera.md)): a Pi Zero W camera node or the C920 on this PC. The steps:

1. **Settings.** CLI options override the printer's `photo:` block in `fleet/printers.yaml`, which
   overrides the defaults (`camera: topcam`, `res: 2304x1536`, `frames: 4`, `skip: 3`,
   `average: mean`, `px_per_mm: 10`). The record lists where every value came from (`origin`).
2. **Pose** (only with `--move --yes`). The printer must be idle (`printing`/`paused` is refused
   before any G-code) and homed (`--home` sends `G28` first). The pose must lie inside the
   printer's axis range. The move is `G90`, then Z first when it goes up (the bed moves away) or
   last when it comes down, then `M400`, all through Moonraker `/printer/gcode/script`.
3. **Light.** `--printer-light on` runs the fleet `printer_light.on` G-code and runs `.off` after
   the photo, even when the capture fails. The light G-code differs per printer, so 3d never
   guesses it. `--led on` switches the camera node's LED ring for the photo.
4. **Capture.** The camera is reset to its defaults, then focus (`auto`, `N` or a `sweep`), exposure,
   gain and white balance are locked before streaming starts, and after the frames it is reset to
   its defaults again on any exit. N
   uncompressed frames are averaged (`mean` or `median`) into `DIR/photo.png` (16-bit RGB). The
   record keeps the temporal noise, mean level and the fraction of clipped pixels: if a lot is
   clipped, lower `--exposure`.
5. **Rectify.** The plate's ArUco fiducials (DICT_4X4_50, ids 0–3, plan §8.3.4) come from the
   manifest (`calib-plate/1` `fiducials`) or `--fiducials`. At least 3 must be found (12 corners,
   least-squares homography). The result is `DIR/rectified.png` in bed millimetres at `--px-per-mm`
   with bed +Y up, plus `DIR/detections.png`. A marker lies axis-aligned with its top edge towards
   bed +Y; a manifest `rotation_deg` turns it.
6. **External scale.** Fiducials printed by the printer carry its own scale error, so they
   cannot measure it. `--scale-ref FILE` names a rigid card with markers whose true centre
   positions were measured with calipers:

   ```json
   {"schema": "scale-ref/1", "name": "steel card", "dictionary": "DICT_4X4_50", "marker_size_mm": 10,
    "markers": {"40": [0, 0], "41": [100.02, 0], "42": [100.01, 59.98], "43": [0, 60.03]},
    "thickness_mm": 0.5}
   ```

   The card's marker centres are measured in bed millimetres and fitted to the true positions
   (affine with 3+ markers, isotropic with 2). The result is `true_mm_per_bed_mm` per axis:
   multiply a length measured in the rectified image by it to get true millimetres. The card's
   top surface sits `thickness_mm` above the bed and looks bigger by H/(H−t), so give
   `--camera-height-mm` (or `photo.camera_height_mm`) for the correction. Without it the record
   says `parallax_corrected: false` with the size of the error.

`DIR/calib-photo.json` (`calib-photo/1`) holds the settings and their origin, the pose and light
G-code sent, the camera source, the requested lock and the control read-back, the photo
statistics, and the `calib-rectify/1` result (`fiducials_found`, `ids`, `missing`, `homography`
from image px to bed mm, `residual_mm`, `area_mm`, `scale`). `--save` writes the effective
non-default settings back as the printer's `photo:` block; other lines and comments stay as they
are, and the file is re-validated first. `--session ID` uploads the photo to a calibration
session (`POST /v1/calibration/sessions/{id}/photos`, multipart `file` + `source=camera` +
`meta`). That endpoint comes with the session API ([#277](https://github.com/alex-mextner/3d-cli/issues/277)); until it is
deployed the upload ends with the API's 404.

`rectify` runs steps 5–6 on any photo: a phone shot, a Telegram picture, an older still.

| Option | What |
|---|---|
| `--printer P` | fleet printer whose `photo:` block gives the defaults (required) |
| `--camera C` | `usb`, `usb:/dev/videoN`, `http://host:port`, `host:port` or a node name |
| `--res WxH` | still resolution (default 2304x1536) |
| `--focus auto\|N\|sweep[:LO-HI[:STEP]]` | autofocus, a manual position, or the software focus sweep (sharpest position in `--focus-roi`, kept for the series; see camera.md) |
| `--focus-roi R` | sweep region `X0,Y0,X1,Y1` as frame fractions |
| `--exposure/--gain/--wb N` | the manual lock; exposure in 100 µs, white balance in K |
| `--ctrl N=V,…` | any other control by v4l2-ctl name or alias |
| `--frames N` | frames averaged (default 4); more than 8 become several node requests |
| `--skip K` | frames dropped after streaming starts (default 3) |
| `--average M` | `mean`, `median` or `none` |
| `--led on\|off` | camera-node LED ring during the photo |
| `--settle-ms MS` | wait after the LED switches |
| `--printer-light on\|off` | fleet `printer_light` G-code before and after the photo |
| `--pose X,Y,Z[,F]` | photo pose (mm, F in mm/min); used only with `--move` |
| `--move` | move to the pose first (needs `--yes`) |
| `--home` | home first when the printer is not homed |
| `--yes` | confirm the move |
| `--manifest M` | plate manifest with the fiducials |
| `--fiducials L` | fiducials as `ID:X,Y;…` in bed mm |
| `--fiducial-size MM` | marker size for `--fiducials` (default 10) |
| `--px-per-mm N` | rectified resolution (default 10) |
| `--area X0,Y0,X1,Y1` | rectified bed area (default the fiducials' bounding box + 10 mm) |
| `--scale-ref FILE` | external scale reference (`scale-ref/1`) |
| `--camera-height-mm H` | lens height above the bed, for the card's parallax |
| `--no-rectify` | only capture |
| `--out DIR` | output directory |
| `--save` | store the settings as the printer's `photo:` block |
| `--session ID` | upload to calibration session ID |

The fleet block, with every key optional:

```yaml
k1se:
  photo:
    camera: topcam
    res: 2304x1536
    focus: 30
    exposure: 300
    gain: 0
    wb: 4500
    ctrl: {sharpness: 0}
    frames: 8
    average: median
    led: on
    printer_light: {on: "SET_PIN PIN=LED VALUE=1", off: "SET_PIN PIN=LED VALUE=0"}
    pose: {x: 110, y: 220, z: 150, speed: 6000}
    camera_height_mm: 300
    px_per_mm: 10
    scale_ref: scale-refs/steel-card.json   # relative to the fleet file
```

```bash
3d calibrate photo --printer k1se --camera usb --fiducials '0:12,12;1:208,12;2:208,208;3:12,208' --out shot/
3d calibrate photo --printer k1se --camera usb --focus sweep --focus-roi 0.1,0.1,0.9,0.9 --no-rectify
3d calibrate photo --printer u1 --camera topcam --exposure 250 --wb 4500 --frames 8 --average median --led on
3d calibrate photo --printer k1se --move --yes --pose 110,220,150,6000 --printer-light on --manifest plate1.json
3d calibrate photo --printer k1se --manifest plate1.json --scale-ref fleet/scale-refs/steel-card.json --camera-height-mm 300
3d calibrate photo --printer u1 --focus 30 --exposure 300 --gain 0 --wb 4500 --settle-ms 500 --save --no-rectify
3d calibrate photo --printer u1 --manifest plate1.json --session 17 --json
3d calibrate rectify phone.jpg --manifest plate1.json --px-per-mm 8 --area 0,0,220,220 --out rect/
```

Exit codes for `photo`/`rectify`: 0 rectified (or captured with `--no-rectify`); 1 fewer than 3
fiducials, the scale card not seen, the printer busy or not homed, or the camera failed (the
photo and the record are still written when the capture worked); 2 bad options.

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
`calibrate fit`: 0 ok; 1 a readout that cannot be used (no `free` step: widen the range) or `fit check` with a
MISSING/STALE pair; 2 bad input (unknown printer/fit/shape, a `--result` that does not fit the ladder, a `--material`
that is not what is loaded); 127 OpenSCAD missing (`gen`, `quick --yes`).
