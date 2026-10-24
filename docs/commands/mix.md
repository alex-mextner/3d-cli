# `3d mix` — colour mixing by layers (Snapmaker Orca mixed filaments)

The U1 mixes colours by layers: Snapmaker Orca's mixed filaments (Full Spectrum) print **one**
physical filament per layer and alternate them from layer to layer, either by a manual pattern
(`431` = head 4, head 3, head 1, repeat) or by Local-Z ratio rows (sublayers). The eye mixes the
stripes only on **walls**; a horizontal top shows the last layer, one plain filament.
Every mix is also bound by the [layer rules](#the-layer-rules): the additional colour is one thin
layer between the others, never on two layers in a row, and layers over a mixed part are at most
0.12 mm. Epic: [#410](https://github.com/alex-mextner/3d-cli/issues/410), plan §20.

## Usage

```
3d mix check FILE.3mf [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix profile FILE.3mf -o OUT.3mf [--layer-cap MM] [--min-layer MM] [--max-layer MM] [--json]
3d mix gcode FILE.gcode [--project FILE.3mf] [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix pattern PATTERN [--layer MM | --heights MM,MM,...] [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix recipes --target HEX (--slots N=HEX,... | --printer NAME) [--count N] [--max-cycle N]
               [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--printops-api URL] [--json]
3d mix tester  --target HEX (--slots N=HEX,... | --printer NAME) -o OUT/NAME [--count N]
               [--max-cycle N] [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--height MM]
               [--base-3mf FILE.3mf] [--printops-api URL] [--json]
```

## The layer rules

Mixing is physical layer alternation, so what the eye averages is a stack of stripes one layer
thick. Three rules, one validator (`lib/slicing/mix_pattern.py`) behind `pattern`, `recipes`,
`tester`, `check` and `gcode`:

| Rule | Fails when | Default |
|---|---|---|
| `minor-adjacent` | the **additional (minor) colour** is printed on two or more consecutive layers. Minor = every colour that is not a strict majority (more than half) of the cycle; for a 1:1 mix both colours are minor. A cyclic pattern wraps: `WGGGW` repeats as `…W·W…` | always, not relaxable |
| `run-too-thick` | any colour forms a band thicker than `--max-run-mm` | one capped layer (`--layer-cap`), i.e. no colour repeats on adjacent layers. `GWG` repeats as `GWGGWG`: a double-thick G band. `--max-run-mm 0.24` lets a **majority** colour double (`GWG`, `KKW`) |
| `layer-too-thick` | a layer of the mixed region is thicker than `--layer-cap` | 0.12 mm (the first layer is exempt) |

Thickness is measured in mm from the real layer heights, so variable layer heights count:
`3d mix pattern WGW --heights 0.12,0.2,0.12` fails the 0.2 mm layer. A single-colour sequence is not
a mix and always passes. With the default limits only patterns where no colour touches itself
qualify: 1:1 pairs (`WR`), 1:1:1 triples, 2:1:1 and so on; for two colours that is only `WR`. A target
that needs a 3:1 mix of two spools gets the nearest legal mix, or a pure spool.


## `recipes` — which layer patterns come closest to a target

The recipe model of [#116](https://github.com/alex-mextner/3d-cli/issues/116)
(lib/slicing/mix_model.py). Every integer weight vector over the loaded filaments with a cycle of
at most `--max-cycle` layers is a candidate; its layers are spread evenly (Snapmaker Orca's
`build_weighted_gradient_sequence`), and its wall colour is the **mean in linear light**. The score
is CIEDE2000 to the target plus 0.2 × the stripe ΔE seen from 30 cm (the wall profile blurred by
the eye, σ = 1 arcmin). The best candidate comes first, then one variant per rule (lighter,
darker, warmer, greener, pure, short), then the next best.

Candidates that break the [layer rules](#the-layer-rules) are **rejected, not ranked low**; pure
filaments always qualify. Because the mean is taken in linear light, black + white at 1:1 shows
`#BCBCBC`, not `#808080`. The closest short cycle for `#808080` is `141` (two black, one white,
`#9C9C9C`), but it repeats as `141141` — two black layers in a row — so by default the best is `14`
(ΔE 18.5); `--max-run-mm 0.24` brings `141` back. A `--layer` above `--layer-cap` is an error (no
mix is possible at that thickness); `--max-cycle 1` ranks the pure filaments only. The cycle order is
chosen so equal layers do not meet across the repeat seam where that is possible
(`A B C A` repeats as `…A A…`, `A B A C` does not).

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
orange `#E8732A` and T4 bordeaux `#B0284F` gives 4 mixes (best `34`, ΔE 6.1, then `134`, `14`, `13`) and 3
references: the layer rules reject the 2:1 cycles. Snapmaker Orca estimates 45 min and 8.1 g for that plate,
mostly head changes (a change every layer); `3d print-preflight` passes its `head seq`, `mix face` and
`mix layers` lines (thickest layer 0.120 mm). `--count 3 --height 4` makes it shorter.

## Options

- `--target HEX` — the colour to match: `3d mix recipes --target '#B5654F' --printer u1`
- `--slots N=HEX,...` — the loaded filaments by U1 head number (1-9): `--slots 1=#000000,3=#1B5E20,4=#FFFFFF`
- `--printer NAME` — read the usable slots of NAME from 3d-api (`GET /v1/printers/NAME/slots`, then
  `/v1/spools/ID` for the filament id). Read-only. With filament ids, the manifest has mix keys: `--printer u1`
- `--count N` — candidates to keep (default 6, 1-20): `--count 4`
- `--max-cycle N` — longest layer cycle (default 3, 1-12). Short cycles read best (SPEC 58): `--max-cycle 4`
- `--layer MM` — layer height (default 0.12). Without `--base-3mf` it must be a U1 system process: 0.08, 0.12, 0.16 or 0.2: `--layer 0.16`
- `--layer-cap MM` — the thickest layer allowed in a mixed region (default 0.12): `--layer-cap 0.16`
- `--max-run-mm MM` — the thickest band of one colour (default: the layer cap = one layer):
  `--max-run-mm 0.24`. A minor colour is never allowed on adjacent layers, whatever this says.
- `--height MM` — swatch height for `tester` (default 6, 3-40): `--height 10`
- `--heights MM,MM,...` — `pattern` only: one layer height per pattern character (variable layer
  height): `--heights 0.12,0.08,0.12`
- `--project FILE.3mf` — `gcode` only: the plate project the G-code was sliced from, to know each
  object's mixed rows: `--project out/cat.3mf`
- `--min-layer MM`, `--max-layer MM` — `profile` only: the thinnest / thickest layer the written
  profile may use (defaults 0.08 and 0.32): `--min-layer 0.06 --max-layer 0.28`
- `--base-3mf FILE` — project settings from this Snapmaker Orca project: `--base-3mf print/u1.3mf`
- `-o OUT/NAME` — output path without an extension (`tester`): `-o out/mix/terracotta`; for `profile` the
  output project `-o out/cat.capped.3mf` (never the input)
- `--printops-api URL` — the 3d-api for `--printer` (default `$PRINTOPS_API`, else `printops.yaml`): `--printops-api http://192.168.0.39:8765`
- `--json` — the same summary as JSON: `3d mix tester ... --json | jq .candidates`

## `check` — faces and layers of a project before slicing

Lints a project 3MF before slicing ([#411](https://github.com/alex-mextner/3d-cli/issues/411)).

**Faces.** For every part whose filament is a mixed one (a `mixed_filament_definitions` row: manual
pattern or Local-Z ratio row; a part with extruder `0` takes its object's filament), its faces in bed
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

**Layers.** `recipe` lines: every mixed row's pattern, decoded (below), passes the
[layer rules](#the-layer-rules), judged at the thickest layer over the parts that use it. `layers`
lines: for every part in a mixed filament, the layer stack Orca will slice over its height — simulated
exactly as Orca builds it, from the object's `Metadata/layer_heights_profile.txt` entry when it has
one, else `layer_height` under the first layer's `initial_layer_print_height` — has no layer thicker
than `--layer-cap`. FAIL names the layers and the thickest one in mm. A `raft_layers` raft shifts the
stack: the layer lines are SKIP then. `plate` lines: an object profile Orca would silently drop (its
last z is not the object height) is a FAIL, and so is a prime tower over several objects whose
layer heights differ (Orca refuses to slice: the tower needs the same variable layer height on every
object; make the mixed parts one multi-part object, or switch the tower off).

Pattern tokens: in a `mixed_filament_definitions` row `a,b,…,cm1,<pattern>` the token `1` means the
row's component_a, `2` its component_b and `3`-`9` that filament itself. The tester writes a=1,
b=2, so its digits are the heads; a row `2,3,…,12` prints heads 2,3,2,3. `check`, `gcode`, the
`head seq` gate and the recipes all decode them the same way.

Exit 1 on FAIL, 0 otherwise; 2 when the file is not an Orca-family project 3MF (needs
`3D/3dmodel.model`, `Metadata/project_settings.config`, `Metadata/model_settings.config`).

`3d print-preflight` runs the faces check as its `mix face` line on the project 3MF next to the
G-code (`--source-3mf`, else `NAME.3mf` next to `NAME.gcode`); without a project it measures the
G-code itself (top-surface vs outer-wall extrusion of every object printed by more than one head, WARN
only, #419) and the line says which source it used; see [print-preflight](print-preflight.md).

## `profile` — cap the layers over the mixed parts

`3d mix profile cat.3mf -o cat.capped.3mf` writes a copy of the project with a variable layer height
profile (`Metadata/layer_heights_profile.txt`, `object_id=N|z;h;z;h…`, N = the object's 1-based position
in the build) that keeps the project's own layer height — or its existing profile — everywhere except over the
mixed parts, where layers are at most `--layer-cap` thick (default 0.12 mm). The transition is smooth
(neighbouring layers differ by at most about 20 %), the last z equals the object height (Orca drops a
profile otherwise, silently), and `--min-layer`/`--max-layer` (defaults 0.08 and 0.32, the U1's limits)
bound the profile. It then re-runs the layer lint on the written file and exits 1 if it still fails. The
input is never overwritten. Orca takes one profile per object and, with a prime tower, needs the same on
every object, so with a tower and several objects the command stops (error): make the mixed parts one
multi-part object. A raft is refused too.

On a 20 mm object whose upper half (10-20 mm) is mixed, sliced at 0.2 mm, the real Snapmaker Orca
01.10.01.50 gave 49 mixed layers at 0.200 mm before and 87 layers, thickest 0.118 mm, after; the
simulation in `check` predicted exactly those 87 layers.

## `gcode` — what the printer was told to print

Measures a sliced G-code against the [layer rules](#the-layer-rules): per object, the head of every
layer and the real layer Z (so variable layer heights are measured, not assumed). FAIL (exit 1) when the
additional colour is printed on consecutive layers, when a colour forms a band thicker than
`--max-run-mm`, or when a mixed layer is thicker than `--layer-cap`; the layers, the head and the band in
mm are named. The first (bed) layer is exempt from the cap; raft layers are skipped.

With `--project FILE.3mf` the mixed rows say which heads each object's mix uses, and the recipe says
which colour is the additional one. The mix head of a layer is the head printed on it that the object's
solid parts do not use, or the mix's own head when only shared heads printed; the check covers the layers
from the first to the last one on which a mix-only head printed, so a mix part that shares a head with
the solid colour of its object is never guessed at its edges (a missed edge layer, not a false FAIL);
a stretch of shared-head-only layers longer than the recipe itself prints in a shared head is solid colour
between two mixed regions and ends the measured stretch. A mix whose every head is also a solid head is
SKIP and says so, as is a Local-Z ratio row. Layers whose Z does not rise (a by-object print restarts Z for
every object) or has no `;Z:` comment have no measurable thickness and are left out. Without a project only
objects that print exactly one head per layer are measured, and only where they alternate: stretches of at
least five consecutive runs of at most three layers each, minus the first and last run of the stretch.
Solid colour bands, however many are stacked, are never measured.

`3d print-preflight` runs the same check as its `mix layers` line (project 3MF: as for `head seq`).
`--layer-cap`/`--max-run-mm` apply to `3d mix gcode`; the preflight line uses the defaults.

## Examples

```bash
3d mix pattern GWG                       # FAIL: G forms a 0.240 mm band across the repeat seam
3d mix pattern GW                        # PASS: one thin layer of each colour
3d mix pattern GWG --max-run-mm 0.24     # PASS: the majority colour may double, W stays isolated
3d mix pattern WGW --heights 0.12,0.2,0.12   # FAIL: a 0.2 mm layer in a mixed region
3d mix profile out/cat.3mf -o out/cat.capped.3mf
3d mix gcode out/cat.gcode --project out/cat.3mf
3d mix gcode out/cat.gcode --project out/cat.3mf --max-run-mm 0.24 --json
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
top_mm2, side_mm2, slope_mm2, status, detail, z_lo_mm, z_hi_mm}], layers: {check, status, failures,
findings: [{scope, subject, status, detail}]}}`; `status` is the worst of faces and layers.

`3d mix pattern GWG`:

```
mix pattern GWG: FAIL (repeating, cap 0.12 mm, run limit 0.12 mm)
  minor colour(s): W
  FAIL run-too-thick: G forms a 0.240 mm band over 2 consecutive layers (layer 2 and layer 0 across the repeat seam), above the 0.12 mm limit
```

`3d mix gcode` on a real slice of a recipe `141` (head 1 on two of three layers):

```
mix gcode gwg.gcode: FAIL (cap 0.12 mm, run limit 0.12 mm)
  FAIL BLOCK / filament 5 (141): run-too-thick: colour on head 1 printed on consecutive layers 55 times, first on layers 2-3, up to a 0.240 mm band
```
