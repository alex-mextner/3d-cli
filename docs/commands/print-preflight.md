# `3d print-preflight` — is this G-code ready for that printer?

Read-only checks before a print starts. [`3d printer start`](printer.md) runs the same checks
and refuses on FAIL unless `--force`. Issue: [#96](https://github.com/alex-mextner/3d-cli/issues/96).
Every G-code gate of the [print pipeline](../workflows/print-pipeline.md) runs; a gate whose
input is missing is SKIP and its line says what to pass — it never passes silently.

## Usage

```
3d print-preflight FILE.gcode [--printer NAME] [--axis-limits X0:X1,Y0:Y1|NAME] [--source-3mf FILE.3mf]
                   [--visible [OBJ:]SPEC]... [--json] [--secs S]
```

| Check | When | FAIL means |
|---|---|---|
| `thumbnails` | always | The G-code lacks preview images its `; thumbnails =` spec asks for (fix: `3d gcode-thumbnails add FILE`). |
| `printer` | `--printer` | The printer is printing, paused, busy, or its state cannot be read. |
| `slot Tn` | `--printer` on a printer with per-head data (Snapmaker U1) | Tool *n* is used by the G-code but head T*n+1* is empty, or its colour/material differs from `; filament_colour` / `; filament_type`. |
| `axis travel` | always (SKIP when the limits are unknown) | A move, or the bulge of an arc, leaves the printer's X/Y travel anywhere in the file. See [`3d gcode-bounds`](gcode-bounds.md). |
| `print area` | always | A plate move leaves the G-code's `; printable_area`, or an extruding path crosses `; bed_exclude_area` (vendor blocks exempt: `;TYPE:Custom`; on Bambu Studio G-code everything outside its object blocks, wipe tower and features — see [`3d gcode-bounds`](gcode-bounds.md)). |
| `macros` | when the G-code calls macros | A macro (PAUSE, a vendor toolchange, …) is not in the printer's `trusted_macros`: its moves cannot be checked. |
| `overhangs` | always (SKIP for a G-code without extrusion or in vase mode) | An object prints more than 100 mm² on nothing: per layer, extrusion that the layer below does not carry under the 45° rule and that no bridge reaches. The bridge limit is `--bridge-max`, else the printer's `bridge_max_mm` in `printer-devices.yaml` (`--printer` / `--axis-limits NAME`), else 15 mm; the line names the source. Small ceilings, grass tips and edge step-outs are WARN, listed per object with z and position; a map PNG is written. See [`3d gcode-overhangs`](gcode-overhangs.md). |
| `slicer warn` | always | `NAME.slice-warnings.json` is missing, was recorded for another G-code (SHA-256 differs: re-sliced or edited), or holds a slicer warning/error without a reason. PASS lists every accepted warning with its reason. See [`3d slice-warnings`](slice-warnings.md). |
| `head seq` | the project 3MF: `--source-3mf`, else `NAME.3mf` next to `NAME.gcode` (SKIP without one, or when no object can print on the wrong head: no mixed filaments and every object single-head) | An object prints some layer on a head its filaments do not call for, or is missing from the G-code ([#103](https://github.com/alex-mextner/3d-cli/issues/103); the [`slice-check`](slice-check.md) `head-sequence` check). |
| `visible` | `--visible` (SKIP without: the visible side is a design input) | [`3d visible-faces --gcode`](visible-faces.md) finds support or sagging islands on a declared visible side. |
| `halves` | `--source-3mf` | `3d halves-check FILE.3mf` failed or could not run. |

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
- `--secs S` — Creality: seconds to listen for pushed state: `3d print-preflight out/roof.gcode --printer k1se --secs 5`

Exit: 0 PASS or WARN, 1 FAIL, 2 usage error or missing file.

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
  SKIP  visible     no --visible spec: the visible side is a design input (e.g. --visible 'WALL*:y<104.8')
STATUS: FAIL
```

The overlay that printed its underside in the air, `k1_ovl1_v3.gcode`, FAILs the `overhangs`
gate: 1940.6 mm² printed on nothing in 100 islands, z 0.24–3.84.
