# `3d print-preflight` — is this G-code ready for that printer?

Read-only checks before a print starts. [`3d printer start`](printer.md) runs the same checks
and refuses on FAIL unless the owner approves a [forced start](printer.md#forcing-a-start) on Telegram. Issue: [#96](https://github.com/alex-mextner/3d-cli/issues/96).
Every G-code gate of the [print pipeline](../workflows/print-pipeline.md) runs; a gate whose
input is missing is SKIP and its line says what to pass — it never passes silently.

## Usage

```
3d print-preflight FILE.gcode [--printer NAME] [--axis-limits X0:X1,Y0:Y1|NAME] [--source-3mf FILE.3mf]
                   [--visible [OBJ:]SPEC]... [--bridge-max MM] [--printops-api URL] [--offline]
                   [--printops-snapshot FILE] [--accept preset_match --reason TEXT] [--json] [--secs S] [--no-bed-move]
```

| Check | When | FAIL means |
|---|---|---|
| `thumbnails` | always | The G-code lacks preview images its `; thumbnails =` spec asks for (fix: `3d gcode-thumbnails add FILE`). |
| `printer` | `--printer` | The printer is printing, paused, busy, or its state cannot be read. |
| `bed` | `--printer` with `watch.bed.enabled` (SKIP otherwise) | The camera sees objects on the bed or the build plate missing against the empty-bed reference; WARN when it cannot tell (no reference at this Z, plate out of view, camera down). See [Bed state](printer.md#bed-state). On an idle printer with `watch.bed.auto_view` (the U1: its camera sees 15 % of the plate at the Z 200 rest) the bed is moved in Z only to where the whole plate is in view, judged there and put back, and the line names the Z used; never while printing/paused, never homing; `--no-bed-move` judges at the current Z. |
| `slot Tn` | `--printer` on a printer with per-head data (Snapmaker U1) | Tool *n* is used by the G-code but head T*n+1* is empty, or its colour/material differs from `; filament_colour` / `; filament_type`. |
| `axis travel` | always (SKIP when the limits are unknown) | A move, or the bulge of an arc, leaves the printer's X/Y travel anywhere in the file. See [`3d gcode-bounds`](gcode-bounds.md). |
| `print area` | always | A plate move leaves the G-code's `; printable_area`, or an extruding path crosses `; bed_exclude_area` (vendor blocks exempt: `;TYPE:Custom`; on Bambu Studio G-code everything outside its object blocks, wipe tower and features — see [`3d gcode-bounds`](gcode-bounds.md)). |
| `macros` | when the G-code calls macros | A macro (PAUSE, a vendor toolchange, …) is not in the printer's `trusted_macros`: its moves cannot be checked. |
| `overhangs` | always (SKIP for a G-code without extrusion or in vase mode) | An object prints more than 100 mm² on nothing: per layer, extrusion that the layer below does not carry under the 45° rule and that no bridge reaches. The bridge limit is `--bridge-max`, else the printer's `bridge_max_mm` in `printer-devices.yaml` (`--printer` / `--axis-limits NAME`), else 15 mm; the line names the source. Small ceilings, grass tips and edge step-outs are WARN, listed per object with z and position; a map PNG is written. See [`3d gcode-overhangs`](gcode-overhangs.md). |
| `slicer warn` | always | `NAME.slice-warnings.json` is missing, was recorded for another G-code (SHA-256 differs: re-sliced or edited), or holds a slicer warning/error without a reason. PASS lists every accepted warning with its reason. See [`3d slice-warnings`](slice-warnings.md). |
| `head seq` | the project 3MF: `--source-3mf`, else `NAME.3mf` next to `NAME.gcode` (SKIP without one, or when no object can print on the wrong head: no mixed filaments and every object single-head) | An object prints some layer on a head its filaments do not call for, or is missing from the G-code ([#103](https://github.com/alex-mextner/3d-cli/issues/103); the [`slice-check`](slice-check.md) `head-sequence` check). |
| `mix face` | the same project 3MF as `head seq` (SKIP without one, or when no part prints in a mixed filament) | A part in a mixed filament (manual layer pattern or Local-Z row) shows its colour on a horizontal face: its faces looking up within 30° of horizontal outweigh half its walls (faces within 30° of vertical). A layer mix reads only on walls; a flat top shows one filament. Mostly sloped upward faces (terraces) are WARN. Per part with top/wall areas: [`3d mix check`](mix.md) ([#411](https://github.com/alex-mextner/3d-cli/issues/411)). |
| `mix layers` | the G-code; the project 3MF as for `head seq` makes it exact (without one only objects that print one head per layer and alternate are measured; SKIP when no mixed object is found or the G-code has no `;Z:` layer comments) | The additional (minor) colour is printed on two or more consecutive layers, a colour forms a band thicker than one capped layer, or a mixed layer is thicker than 0.12 mm — measured on the real per-layer heads and layer Z, variable layer heights included. The layers, head and band in mm are named. [`3d mix gcode`](mix.md#gcode--what-the-printer-was-told-to-print) runs it alone with `--layer-cap`/`--max-run-mm` ([#116](https://github.com/alex-mextner/3d-cli/issues/116)). |
| `visible` | `--visible` (SKIP without: the visible side is a design input) | [`3d visible-faces --gcode`](visible-faces.md) finds support or sagging islands on a declared visible side. |
| `halves` | `--source-3mf` | `3d halves-check FILE.3mf` failed or could not run. |

### PrintOps material gates

[#164](https://github.com/alex-mextner/3d-cli/issues/164), plan §9.1. They run for the target
printer — `--printer`, `--axis-limits NAME`, else the one printer in `fleet/printers.yaml` whose
`model` is the G-code's `; printer_model` — and compare the G-code's per-tool slicer settings
with what PrintOps knows: the fleet topology and, from 3d-api, the spool bound to each slot
(Spoolman filament + its PrintOps extra fields, the ledger's remaining interval, the drying
assessment). Code: `lib/printer_ops/printops_checks.py`; state: `lib/printops/gates.py`.

Levels: **PASS / WARN / FAIL / UNVERIFIED**. A check without the data it needs (no 3d-api, no
spool bound, a filament without a temperature window) is **UNVERIFIED** and says what is
missing — never PASS. UNVERIFIED does not block on its own; `STATUS:` counts them. A hard finding
is FAIL under the policy level `block`, WARN under `warn`; `off` hides the check
(`fleet/policy.yaml` → `gates:`, per printer under `gates.printers`).

| Check | Default level | Hard finding (FAIL under `block`) | Soft (always WARN) |
|---|---|---|---|
| `printops` | — | — | where the state came from: live 3d-api, snapshot (WARN when older than `snapshot_max_age_h`), or fleet only (UNVERIFIED) |
| `printer_profile_match` | block | `; printer_model` is not the target's model (a K1 SE G-code on the U1) | |
| `nozzle_compat` | block | `; nozzle_diameter` ≠ the head's nozzle; a positive `; required_nozzle_HRC` exceeds its material's nominal hardness; independently, abrasive filament (a true Spoolman `abrasive` flag or a CF/GF/GLOW/WOOD/METAL token in the material/name/type) on a nozzle outside policy `abrasive_ok` (default: hardened, ruby, tungsten) | |
| `filament_temp_range` | block | `; nozzle_temperature`, `; nozzle_temperature_initial_layer` outside the filament's `nozzle_temp_min..max`; any body `M104/M109` for that tool above the max (standby temps below the min are fine); the plate's footer temps (`curr_bed_type` → `textured_plate_temp` …) or any `M140/M190` outside `bed_temp_min..max` | |
| `loaded_filament_match` | block | the bound spool's `material` ≠ `; filament_type` (aliases: `gates.material_aliases`); colour ΔE2000 (spool `measured_hex`, else the filament colour, vs `; filament_colour`) above `fail_delta_e` | ΔE above `warn_delta_e`; vendor ≠ `; filament_vendor` (re-check flow/PA). Thresholds per project: `gates.colour.projects` (shell glob on the G-code path). **UNVERIFIED, never FAIL/WARN, on colour** when the slice's recorded slicer warnings (`NAME.slice-warnings.json`, same SHA-256) contain Orca's `no filament colors found in projects`: the project declares no filament colour (an STL slice), so `; filament_colour` is the filament profile's default and says nothing about the spool; the material check still applies |
| `slot_binding_known` | warn | | no spool bound to a used slot: usage will not be booked; two spools bound to one slot |
| `filament_sufficient` | block | `; filament used [g]` + `purge_g_per_tool` + max(`margin_g`, `margin_pct`) above the high end of the remaining interval | need inside the interval: weigh the spool (`3d printops spool measure ID GROSS_G`) |
| `drying_due` | warn | the spool is `wet` | `due`, or in an open drying session |
| `maintenance_due` | block | the head is `usable: false` in the fleet; an open fault on it (from #166) | maintenance due (from #166) |
| `enclosure_material` | warn | a `gates.enclosure_material` material (ABS, ASA, PC, PA…) on a printer with `enclosure: none` | the same on `enclosure: partial` |
| `plate_material` | warn | `; curr_bed_type` is not among the printer's `plates`; a `gates.plate_material` pairing (PETG/TPU on smooth PEI, ABS/PC/PA on the Cool Plate) | |
| `preset_match` | block | `; printer_settings_id` / `; print_settings_id` are not the fleet entry's `slicer: {machine, process}` presets (a plate sliced with Orca's stock presets, [#293](https://github.com/alex-mextner/3d-cli/issues/293)); `; default_acceleration` / `; outer_wall_acceleration` differ from the resolved process preset (an unsaved edit in the slicer); with a spool bound to a used tool that names a filament preset: `; filament_settings_id` is not that preset, or `; filament_flow_ratio` / `; pressure_advance` / `; enable_pressure_advance` differ from its resolved values ([#506](https://github.com/alex-mextner/3d-cli/issues/506)) | |

`preset_match` is UNVERIFIED when the fleet entry has no `slicer:` presets. It compares the
process values only when the presets are on this machine (Orca's system profiles, the repo's
`profiles/` and the slicers' user preset folders; `ORCA_PROFILES` / `ORCA_USER_PRESETS` point
elsewhere); otherwise it says so under a PASS on the names. The filament preset joins when the
spool bound to a used tool names one (the `slicer_filament_preset` extra field, set with `3d printops spool preset`, [#294](https://github.com/alex-mextner/3d-cli/issues/294)); a calibration print (`3d calibrate gen`) is compared by the filament preset's name only, because it varies flow and pressure advance on purpose. `; print_settings_id` is compared by name with the fleet process preset. The spool's preset names its printer's profile (`Orange PLA @K1SE`): moving the spool to another printer means setting its preset again (`3d printops spool preset`) or accepting the mismatch. A mismatch is a FAIL that `--accept preset_match --reason TEXT` turns into a WARN carrying the reason (`--accept` takes no other check; `--reason` is required with it). To print a
plate with other presets on purpose: a [forced start](printer.md#forcing-a-start) (Telegram approval), or `preset_match: warn` for
the printer under `gates.printers` in fleet/policy.yaml.

Nozzle hardness and filament abrasiveness are separate checks, applied only to tools used by
the job. A positive `required_nozzle_HRC` is a minimum; `0` skips that numeric requirement.
The [Orca compatibility values](https://github.com/OrcaSlicer/OrcaSlicer/wiki/printer_basic_information_accessory)
are brass `2`, stainless `20`, hardened `55`, and tungsten `85`. They are nominal slicer
conventions, not measurements of a particular nozzle. Thus a plain-PLA preset declaring
HRC `3` passes this requirement on stainless, but HRC `21` does not. A positive requirement
with unknown installed hardness, including ruby, is UNVERIFIED. Independently, a true
abrasive flag or positive material/name/type marker still requires an allowed nozzle;
a false spool flag cannot hide that evidence, and unknown hardness cannot hide a hard
abrasive-policy failure. The [filament setting's documented minimum](https://github.com/OrcaSlicer/OrcaSlicer/wiki/material_basic_information#required-nozzle-hrc)
is preserved rather than converted into an abrasive flag.

Within the nozzle gate, a spool marker value other than text or null, or an `abrasive`
flag other than boolean or null, leaves abrasiveness UNVERIFIED unless another valid
positive flag or marker establishes it.
Malformed metadata is not treated as verified nonabrasive filament.

Lot changes and calibration values (`; pressure_advance`, `; filament_flow_ratio`) are the
`calibration_current` gate of the calibration engine ([#167](https://github.com/alex-mextner/3d-cli/issues/167)).

**Where the state comes from.** `--printops-api URL`, else `$PRINTOPS_API`, else `api:` in
`~/.config/3d-cli/printops.yaml` (token: `$PRINTOPS_TOKEN` / `token:`) → live
`GET /v1/gates/state?printer=NAME` (scope `read`). `--offline` → the snapshot file written by
[`3d printops gates snapshot`](printops.md#gates) (`--printops-snapshot`, `$PRINTOPS_SNAPSHOT`,
default `~/.cache/3d-cli/printops-snapshot.json`), and the printer is not contacted (its fleet
profile gives the axis limits). Neither → fleet facts only: the spool checks are UNVERIFIED.

Tool numbering: G-code `Tn` and the *n*-th entry of `; filament_colour` are head index *n*,
which the U1 screen calls T*n+1*. Used tools are read from `; filament used [mm]` (non-zero
entries), else from `Tn` tool-change lines, else tool 0.

Options:

- `--printer NAME` — a printer from [`3d printer list`](printer.md); its state is read, never changed. Axis limits come from its `axis_limits` in `printer-devices.yaml`, else Moonraker printers report Klipper's `stepper_x`/`stepper_y` `position_min`/`position_max`: `3d print-preflight out/part.gcode --printer u1`
- `--axis-limits SPEC` — X/Y travel as `X0:X1,Y0:Y1`, or a printer name whose `axis_limits` (and `trusted_macros`) come from `printer-devices.yaml` (no network); overrides the limits `--printer` reports: `3d print-preflight out/roof.gcode --axis-limits -5:229,-0.5:227`, `3d print-preflight out/roof.gcode --axis-limits k1se`
- `--source-3mf FILE` — the project 3MF the G-code was sliced from: runs the head-sequence check on it and `3d halves-check`: `3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf`
- `--visible [OBJ:]SPEC` — a visible side for `3d visible-faces --gcode` (`x<V`, `y>V`, `all`; repeatable): `3d print-preflight out/walls.gcode --visible 'WALL*:y<104.8'`
- `--bridge-max MM` — longest span the overhangs gate counts as a bridge; overrides the printer's `bridge_max_mm`: `3d print-preflight out/porch.gcode --axis-limits u1 --bridge-max 25`
- `--json` — JSON report (`checks[]` with `name`, `status`, `detail`, `lines`): `3d print-preflight out/part.gcode --printer u1 --json | jq '.checks[] | select(.status=="FAIL")'`
- `--no-bed-move` — the `bed` line judges at the current Z: the printer's bed is not moved to the best inspection Z (`watch.bed.auto_view`): `3d print-preflight out/part.gcode --printer u1 --no-bed-move`
- `--secs S` — Creality: seconds to listen for pushed state: `3d print-preflight out/roof.gcode --printer k1se --secs 5`
- `--printops-api URL` — 3d-api for the PrintOps gates (default `$PRINTOPS_API`, else `printops.yaml`): `3d print-preflight out/part.gcode --printer u1 --printops-api http://192.168.0.39:8765`
- `--offline` — no network: no printer contact, PrintOps state from the snapshot (the slicer hook mode); together with `--printops-api` it is a usage error: `3d print-preflight out/part.gcode --offline`
- `--accept preset_match --reason TEXT` — print with other presets on purpose: the `preset_match` FAIL becomes a WARN that lists the reason. No other check can be accepted; `--reason` is required: `3d print-preflight out/plate.gcode --printer k1se --accept preset_match --reason 'comparing stock flow on purpose'`.
- `--printops-snapshot FILE` — the snapshot for `--offline`: `3d print-preflight out/part.gcode --offline --printops-snapshot ~/snap.json`

Exit: 0 PASS or WARN (UNVERIFIED lines included), 1 FAIL, 2 usage error or missing file.

## Examples

```bash
3d print-preflight out/part.gcode
3d print-preflight out/part.gcode --printer u1
3d print-preflight out/base.gcode --printer u1 --source-3mf print/base.3mf
3d print-preflight out/roof.gcode --printer k1se --secs 5
3d print-preflight out/roof.gcode --axis-limits -5:229,-0.5:227
3d print-preflight out/roof.gcode --axis-limits k1se
3d print-preflight print/k1se/k1_base_4_v3.gcode --axis-limits k1se
3d print-preflight out/walls.gcode --visible 'WALL*:y<104.8'
3d print-preflight out/part.gcode --printer u1 --json | jq '.checks[] | select(.status=="FAIL")'
3d print-preflight out/part.gcode --printer u1 --printops-api http://192.168.0.39:8765
3d print-preflight out/part.gcode --offline
```

BASE4 of the babushkin-dom K1 SE queue, sliced before slicer warnings were recorded (offline,
`--axis-limits` reads `printer-devices.yaml` only):

```
$ 3d print-preflight print/k1se/k1_base_4_v3.gcode --axis-limits k1se
PREFLIGHT print/k1se/k1_base_4_v3.gcode
  PASS  thumbnails  300x300, 96x96
  PASS  axis travel all moves x 22.63..197.36, y 24.29..195.68 within X -5..229, Y -0.5..227 (k1se in …/printer-devices.yaml)
  PASS  print area  plate moves x 22.63..197.36, y 24.29..195.68 within 0..220 x 0..220
  PASS  macros      all trusted (printer-devices.yaml): END_PRINT×1 (line 1112317), START_PRINT×1 (line 648)
  WARN  overhangs   1 object(s), largest uncarried 0 mm2 (BASE4_—_BASE4_id_0_copy_0); max 100 mm2 per object, bridges <= 15 mm
                   WARN BASE4_—_BASE4_id_0_copy_0: 0.0 mm2 in 0 island(s) (<= 100 mm2: ceilings, tips); edge step-outs <= 0.6 mm wide: 239.1 mm2; base z=0.12
                        islands: ledge z4.44 (167.07, 134.34) 36.92 mm2, ledge z4.44 (72.63, 187.61) 4.58 mm2, ledge z4.44 (41.79, 185.65) 4.1 mm2
                   map: ~/.cache/3d-cli/overhangs/k1_base_4_v3-583b75803925.png
  FAIL  slicer warn no k1_base_4_v3.slice-warnings.json: the slicer's warnings for this G-code were never recorded (3d slice-warnings record k1_base_4_v3.gcode --log SLICER.log [--result result.json])
  SKIP  head seq    nothing to sequence: no mixed filaments and every object on one head (1 object(s), k1_base_4_v3.3mf next to the G-code)
  SKIP  mix face    no part prints in a mixed filament (0 mixed row(s)) (k1_base_4_v3.3mf next to the G-code)
  SKIP  mix layers  nothing measured: (G-code): no layer Z comments (;Z: / ; Z_HEIGHT:): the layer thickness cannot be measured (k1_base_4_v3.3mf next to the G-code)
  SKIP  visible     no --visible spec: the visible side is a design input (e.g. --visible 'WALL*:y<104.8')
STATUS: FAIL
```

The overlay that printed its underside in the air, `k1_ovl1_v3.gcode`, FAILs the `overhangs`
gate: 1940.6 mm² printed on nothing in 100 islands, z 0.24–3.84.
