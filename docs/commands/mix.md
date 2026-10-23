# `3d mix` — colour mixing by layers (Snapmaker Orca mixed filaments)

The U1 mixes colours by layers: Snapmaker Orca's mixed filaments (Full Spectrum) print **one**
physical filament per layer and alternate them from layer to layer, either by a manual pattern
(`431` = head 4, head 3, head 1, repeat) or by Local-Z ratio rows (sublayers). The eye mixes the
stripes only on **walls**; a horizontal top shows the last layer, one plain filament.
Epic: [#410](https://github.com/alex-mextner/3d-cli/issues/410), plan §20.

## Usage

```
3d mix check FILE.3mf [--json]
3d mix recipes --target HEX (--slots N=HEX,... | --printer NAME) [--count N] [--max-cycle N]
               [--layer MM] [--printops-api URL] [--json]
3d mix tester  --target HEX (--slots N=HEX,... | --printer NAME) -o OUT/NAME [--count N]
               [--max-cycle N] [--layer MM] [--height MM] [--base-3mf FILE.3mf]
               [--printops-api URL] [--json]
```

## `recipes` — which layer patterns come closest to a target

The recipe model of [#116](https://github.com/alex-mextner/3d-cli/issues/116)
(lib/slicing/mix_model.py). Every integer weight vector over the loaded filaments with a cycle of
at most `--max-cycle` layers is a candidate; its layers are spread evenly (Snapmaker Orca's
`build_weighted_gradient_sequence`), and its wall colour is the **mean in linear light**. The score
is CIEDE2000 to the target plus 0.2 × the stripe ΔE seen from 30 cm (the wall profile blurred by
the eye, σ = 1 arcmin). The best candidate comes first, then one variant per rule (lighter,
darker, warmer, greener, pure, short), then the next best.

Because the mean is taken in linear light, black + white at 1:1 shows `#BCBCBC`, not `#808080`;
for `#808080` the best cycle up to 3 layers is `141` (two black, one white, `#9C9C9C`).

## `tester` — a numbered plate of swatches for one target

Writes three files ([#413](https://github.com/alex-mextner/3d-cli/issues/413)):

- `NAME.3mf` — a Snapmaker Orca project. The loaded spools are the physical filaments. Each
  candidate gets a `mixed_filament_definitions` row (manual pattern, components 1/2, so every
  digit is a head) and a standing swatch 20 mm wide, 3 mm deep, `--height` tall. Each loaded spool
  gets a pure reference swatch, used for white balance and spool correction. A flat
  seven-segment number lies in front of every swatch, printed in the darkest loaded spool. The
  prime tower goes in a free corner. Swatches stand, so `3d mix check` passes the plate.
- `NAME.mix.json` — the manifest, schema `printops.mix_tester/1`: target, slots (head, hex,
  spool and Spoolman filament id when known), candidates (number, role, pattern, weights,
  predicted hex, ΔE, stripe ΔE, virtual filament, swatch and label boxes on the bed as
  `[x0, y0, x1, y1]`, `calibration: {kind: mix_color, mix: <key or null>}`), references
  (`calibration: {kind: filament_color, filament_id}`), and the file names.
- `NAME.png` — the swatch sheet: for each number, the target next to the predicted colour.

The project settings come from the installed Snapmaker Orca: its CLI exports a probe project
with the U1 system presets (`Snapmaker U1 (0.4 nozzle)`, the `--layer` process,
`Snapmaker PLA Basic @U1`). `$SNAPMAKER_ORCA` names the binary; otherwise the extracted AppImage
`~/Applications/snapmaker_orca/AppRun` or the macOS app is used. `--base-3mf` takes the settings
from a saved project instead. Slice the result in Snapmaker Orca, then run `3d print-preflight`;
its head-sequence and mix-face gates read `NAME.3mf` next to the G-code. The Snapmaker Orca
01.10.01.50 **CLI** segfaults on any project with a `<plate>` block (even its own exports), so for
a headless slice drop that block from `Metadata/model_settings.config` and drop
`Metadata/plate_1.json` in a copy. The GUI opens the file as written.

For example, `3d mix tester --target '#B5654F' --printer u1` with T1 Snapdragon `#F6C700`, T3
orange `#E8732A` and T4 bordeaux `#B0284F` gives 6 mixes (best `34`, ΔE 6.0) and 3 references.
Snapmaker Orca estimates 49 min and 8.9 g for that plate, mostly head changes (a change every
layer). `--count 3 --height 4` brings it to 30.5 min and 5.5 g.

## Options

- `--target HEX` — the colour to match: `3d mix recipes --target '#B5654F' --printer u1`
- `--slots N=HEX,...` — the loaded filaments by U1 head number (1-9): `--slots 1=#000000,3=#1B5E20,4=#FFFFFF`
- `--printer NAME` — read the usable slots of NAME from 3d-api (`GET /v1/printers/NAME/slots`, then
  `/v1/spools/ID` for the filament id). Read-only. With filament ids, the manifest has mix keys: `--printer u1`
- `--count N` — candidates to keep (default 6, 1-20): `--count 4`
- `--max-cycle N` — longest layer cycle (default 3, 1-12). Short cycles read best (SPEC 58): `--max-cycle 4`
- `--layer MM` — layer height (default 0.12). Without `--base-3mf` it must be a U1 system process: 0.08, 0.12, 0.16 or 0.2: `--layer 0.16`
- `--height MM` — swatch height for `tester` (default 6, 3-40): `--height 10`
- `--base-3mf FILE` — project settings from this Snapmaker Orca project: `--base-3mf print/u1.3mf`
- `-o OUT/NAME` — output path without an extension (`tester`): `-o out/mix/terracotta`
- `--printops-api URL` — the 3d-api for `--printer` (default `$PRINTOPS_API`, else `printops.yaml`): `--printops-api http://192.168.0.39:8765`
- `--json` — the same summary as JSON: `3d mix tester ... --json | jq .candidates`

## `check` — mixed parts show their colour on walls

Lints a project 3MF before slicing ([#411](https://github.com/alex-mextner/3d-cli/issues/411)).
For every part whose filament is a mixed one (a `mixed_filament_definitions` row: manual pattern
or Local-Z ratio row; a part with extruder `0` takes its object's filament), its faces in bed
coordinates (build and component transforms applied) are summed by normal:

| Class | Faces | Why |
|---|---|---|
| top | looking up, within 30° of horizontal | shows the last layer: one filament |
| side | within 30° of vertical | shows every layer: the mix |
| slope | the rest looking up | terraces: each step shows one layer |

Faces looking down sit on the bed or under the part and are not counted.

- **FAIL** when top > side / 2: the part shows its colour mainly on a horizontal face. From any
  one direction a viewer sees at most about half of a part's walls but all of its top. Stand the
  part up so its coloured face is vertical.
- **WARN** when slope > side / 2 (a roof at 45°).
- **PASS** otherwise. A part in a physical filament is not checked.

Exit 1 on FAIL, 0 otherwise; 2 when the file is not an Orca-family project 3MF (needs
`3D/3dmodel.model`, `Metadata/project_settings.config`, `Metadata/model_settings.config`).

`3d print-preflight` runs the same check as its `mix face` line on the project 3MF next to the
G-code (`--source-3mf`, else `NAME.3mf` next to `NAME.gcode`); see
[print-preflight](print-preflight.md).

## Examples

```bash
3d mix check out/u1_tiles.3mf
3d mix check out/u1_tiles.3mf --json
3d mix recipes --target '#B5654F' --slots 1=#F6C700,3=#E8732A,4=#B0284F
3d mix tester --target '#B5654F' --printer u1 -o out/mix/terracotta
3d mix tester --target '#808080' --slots 1=#000000,4=#FFFFFF --base-3mf print/u1.3mf -o out/grey
```

A 20×20×3 mm tile lying flat in mix `431` next to a reference tile in a physical filament:

```
$ 3d mix check flat.3mf
mix check flat.3mf: FAIL
  FAIL PTA / PTA: filament 5 (431) — visible face horizontal: top 400 mm² > half the walls 120 mm²; stand the part up so its coloured face is vertical (a layer mix reads only on walls)
```

The same tile standing (20 wide, 3 deep, 20 tall): `PASS PTA / PTA: filament 5 (431) — walls 920 mm², top 60 mm²`.

`--json` prints `{check, status, mixed_rows, failures, parts: [{object, part, extruder, recipe,
top_mm2, side_mm2, slope_mm2, status, detail}]}`.
