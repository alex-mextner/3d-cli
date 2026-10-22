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
3d calibrate gen pa|flow|combo|temp|shaper|plate1|dense|dense2 --printer P -o OUT --filament-preset NAME
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
| `filament_color` | filament | yes | required, policy `off` | — | — (read by the mix model, `3d mix`) |
| `mix_color` | printer × mix | — | assumed | — | — (read by the mix check of a plate, #418) |

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
  `--nozzle-temp` is refused. K1 SE, PLA: ~43 min, ~8 g. `print-preflight`'s
  `filament_temp_range` FAILs when the tower's first (hottest) temperature, 230 °C for PLA or
  250 °C for PETG, is outside the loaded spool's `nozzle_temp_min/max`; the colder blocks are
  never flagged. The tower is meant to cross the window: read the other checks and start it
  with `3d printer start … --force`.
- `plate1` — the filament plate of the calibration series (`lib/slicing/calibration_plate.py`,
  `lib/slicing/calibration_plates/filament.py`): PA lines, a max-volumetric-speed strip (label =
  mm³/s), flow tiles (15×15 mm, 5 solid layers), bridges 8/11/14 mm, overhangs 45–60°, a retraction
  tower (0.4–1.2 mm, its own retractions rescaled per block) and a temperature tower (230→200 °C,
  0.3 mm layers). Objects set their parameter on entry and restore it on exit; the temperature
  tower prints last, alone (`print_sequence` by object), `extruder_clearance_radius` behind the
  rest and under `extruder_clearance_height_to_rod`. `--only`/`--skip` pick tests (`pa`, `mvs`,
  `flow`, `bridge`, `overhang`, `retraction`, `temp`); the layout is packed again, so a skipped test
  leaves no hole. Next to the G-code: `NAME.json`, the map object → parameters, bbox, Z bands,
  labelled items, how to read it, the `record` command. K1 SE, PLA: ~25 min (all tests).
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
  and a top-surface patch. Ringing and VFA print by layer: their bands rewrite only their own lines,
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
  mini-tower last and alone; corner fiducials (ring + rulers) and 20 mm one-layer patches on the
  free cells. Brims: 3 mm on every object whose base side is under 10 mm or that is taller than
  its base is wide (`Plate.brim`): hand-written for objects printed by layer (Orca's own brim
  would go on every object), Orca's own in the sequential slice for the tower. MVS 20
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
