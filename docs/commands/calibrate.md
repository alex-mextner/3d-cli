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
