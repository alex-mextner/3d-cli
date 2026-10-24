# `3d gcode-mix-gate` — did a multi-colour G-code print the colours it was meant to?

Gates a sliced multi-colour (mixed-filament) G-code on **what was printed**: which tool printed each
layer of each colour volume, whether the M600 pause sits where the pocket closes, which heads ran, and
(optionally) that a pocket stayed void. It never contacts a printer, so agents and CI can run it freely.
Issue: [#551](https://github.com/alex-mextner/3d-cli/issues/551) (ported from the Chekhov cat phone stand
on the Snapmaker U1).

Why: the slicer indexes a mixed-filament pattern by layer number, a recipe that validates can still print
shifted by a layer, and the wrap-around of `GWG` is `GWGGWG`. Only the G-code says what was printed. The
first version of this gate also failed on false positives (fringe layers of a 2 mm iris sliver judged by a
neighbour's strand); a layer is now judged on its **own** outer-wall evidence.

## Usage

```
3d gcode-mix-gate FILE.gcode --config GATES.json [--parts DIR] [--default-slot N] [--cavity FILE]
                  [--layer-heights MIN:MAX] [--max-layer-step MM] [--margin MM] [--bed-tol MM]
                  [--min-evidence MM] [--max-stripe MM] [--max-stripe-de DE] [--warn-stripe-de DE]
                  [--view-cm CM] [--mix-teeth] [--teeth-volume NAME]
                  [--cavity-teeth] [--json]
```

The G-code is streamed layer by layer (`lib/gcode_scan.py`), never read as one string; per layer only
compact observations are kept. A 75 MB, 650-layer plate with five colour volumes takes about two minutes.

| Check | What | FAIL means |
|---|---|---|
| `colour volume X: pure …` | Every outer-wall point is attributed to the part STL whose surface it lies on (within `--bed-tol`, and not within `--margin` of another volume's surface). A pure part prints one tool. | More than 1 % of its layers show another tool. |
| `colour volume X: mix … follows the pattern` | A mixed part prints the pattern's tool at layer number *n* (index = layer number from the first layer, offset 0); the default tool may print beside it, no other tool may. A layer is **solid** when its own attributed outer wall is ≥ `--min-evidence` (1.0 mm); a solid layer has zero tolerance. A layer with less is re-read at a 0.08 mm margin (≤ 5 % off); with < 1.0 mm even there, or with no unambiguous point at the strict margin at all, it is counted as *not judged* in the row. | A solid layer is off, or more than 5 % of the layers are. |
| `colour volume X: no stripe thicker …` | No tool prints a single-colour stripe thicker than `--max-stripe` (0.10 mm) on neighbouring layers (a default-tool-only run does not count). | The mix paints a visible band (a single layer thicker than the limit counts as a band; the bed layer is exempt). |
| `colour volume X: stripe contrast dE00 <= 3 at 30 cm` | The **visible banding** of a mixed (or dither) region. From the real per-layer heights and the tool the scanner attributed to each layer of the region, the layers' colours (config `filament_colours`, else the G-code's `filament_colour`; a layer with several tools is their linear-light mean, the default tool only counts when nothing else prints) are blurred with the eye's line-spread (Gaussian, 1 arcmin at `--view-cm`, linear light) and compared with their mean over one pattern period: the worst CIEDE2000 per layer. Same model as the babushkin-dom `stripe_view`; reference at 0.08 mm / 30 cm: `GKGKWK` 22.9, `GWGWGK` 15.1, `GWGK` 10.7, `GKW` 2.6, `GW` 0.2, `GK` 0.1. Runs of consecutive judged layers are measured separately; layers within the blur window of a run's end are not measured (a run shorter than that is read as one repeating period). | Worst dE > `--max-stripe-de` (3). Between `--warn-stripe-de` (2) and the limit the row is `WARN` (exit 0). `SKIP` when no filament colour is known or a tool has none. |
| accent (dither) volume | `assign.accent`: the accent tool on exactly the pattern's accent layers, never on two neighbours, each ≤ 0.10 mm; the real pattern must explain the layers better than the pattern shifted by one. | Accent on base layers, missing accents, or the shifted pattern fits as well. |
| thin volume | A mix of < 25 mm³ (nostril slits) is read from the extrusion inside it and must cover ≥ 70 % of its layers. | Layers of the volume have no extrusion inside. |
| `pause` | With `pause` in the config: exactly one M600, in the layer whose top Z is `pause.print_z`, before the layer extrudes, with the previous layer's top at `pause.last_layer_before.top`. Without `pause`: no M600 at all. | A pause in the wrong layer or after extrusion, two pauses, a pause nobody asked for. |
| `tools` | The heads that used filament (`; filament used [g]`) are exactly `heads_slots`; a prime tower is printed iff `prime_tower` (default: more than one head). Reports tool changes and grams per tool / feature from the extrusion moves next to the slicer's footer (`stats` in `--json`). | A head never used or an unexpected one; tower missing or present. |
| `cavity` | `--cavity`: no extrusion inside the pocket outline (shrunk 0.8 mm) between its floor and the pause; the central strip under the ridge is open from the pause up to the ridge and closed above it. | The pocket is filled, or never closes. |
| `layers` | `--layer-heights 0.08:0.32`: heights inside the window (first layer exempt), Z steps equal `;HEIGHT`, neighbours differ ≤ `--max-layer-step`. | A layer outside the window or a Z step that is not the layer height. |
| `settings` | `expect_settings` in the config: the footer's `; key = value`. | The slicer used another value. |

**Limit of the stripe check:** it models a vertical wall (a band is as wide as the layer is high). On a near-horizontal
curved surface each layer is 1-2 mm wide in plan and the bands are *more* visible than the dE says; the slope is not
derived from the G-code, so judge those regions with a stricter `--max-stripe-de`. `--max-stripe` (single-layer
thickness, above) is unchanged and does not measure contrast: `GKGKWK` passes it and still scores 20+ here.

A check without its input is a `SKIP` that names it (no `--parts`, no `--cavity`, no `heads_slots`), never a PASS.

## Teeth: a gate that cannot fail proves nothing

`--mix-teeth` re-runs the colour-volume gate on a deliberately wrong print, for every thick mixed volume
with solid layers (restrict with `--teeth-volume NAME`):

- the real print must PASS;
- the pattern shifted by one layer must FAIL;
- one solid layer per expected tool, printed with a wrong tool, must FAIL (one layer of 30 is inside the 5 %
  tolerance of fringe layers, so only the solid-layer rule can catch it).

`--cavity-teeth` (needs `--cavity`): the pocket outline moved over solid body must FAIL the void gate, and a
pause Z 8 mm too high must FAIL the pause gate.

Each control prints `control CAUGHT` / `control NOT CAUGHT`. One `NOT CAUGHT` (or a volume without solid
layers, where the controls are impossible) ends with `TEETH FAILED` and exit 1: the gate is vacuous.

## Config

JSON; slots are 1-based (slot *n* = tool `T(n-1)`). A build script's manifest with the same keys works as is.

```json
{"parts": [{"name": "iris", "stl": "2_iris.stl", "assign": {"mix": "121242", "mix_src": "GKGKWK"}},
           {"name": "base", "assign": {"slot": 2}},
           {"name": "hair", "assign": {"mix": "11211", "accent": 2}}],
 "default_slot": 2, "filament_colours": ["#1B5E20", "#080A0D", "#E72F1D", "#FFFFFF"], "heads_slots": [1, 2, 3, 4], "prime_tower": true,
 "pause": {"print_z": 37.2392, "last_layer_before": {"top": 36.9994}},
 "shift": [135.0, 134.5, 0.0], "expect_settings": {"wall_loops": "4"}, "parts_dir": "work/parts"}
```

- `parts[].stl`: file name inside `--parts DIR` (or `parts_dir`, relative to the config). Without it the
  files of the directory, sorted by name, are the parts in config order (the count must match). The STLs are
  in **bed coordinates**.
- `default_slot` (or `--default-slot N`): the tool allowed beside any mix (pupil slit, edge fringe).
- `filament_colours` (optional): `#RRGGBB` per slot, for the stripe check; without it the G-code's `filament_colour`.
- `shift`: places the cavity JSON's object coordinates on the bed.
- Cavity JSON: `{"plan_mm": {"x": [-20, 20], "y": [-22, 14], "corner_r": 6}, "floor_z": 1.2, "pause_z": 37.0, "roof_ridge_z": 48.0}`.

## Options

| Option | Meaning |
|---|---|
| `--config FILE` | gate config (required), e.g. `--config out/cat.manifest.json` |
| `--parts DIR` | part STLs, e.g. `--parts work/cat.parts` |
| `--default-slot N` | default tool slot, e.g. `--default-slot 2` |
| `--cavity FILE` | pocket outline, e.g. `--cavity cavity.json` |
| `--layer-heights A:B` | height window, e.g. `--layer-heights 0.08:0.32` |
| `--max-layer-step MM` | neighbour step limit, e.g. `--max-layer-step 0.08` |
| `--margin MM` | strict attribution margin, e.g. `--margin 0.9` |
| `--bed-tol MM` | largest point-to-surface distance, e.g. `--bed-tol 0.45` |
| `--min-evidence MM` | own outer wall needed to judge a layer strictly, e.g. `--min-evidence 1.0` |
| `--max-stripe MM` | thickest single-tool stripe, e.g. `--max-stripe 0.10` |
| `--max-stripe-de DE` | worst stripe dE00 of a mixed region before FAIL, e.g. `--max-stripe-de 3` (default 3) |
| `--warn-stripe-de DE` | dE00 above which the row is a WARN, e.g. `--warn-stripe-de 2` (default 2, at most `--max-stripe-de`) |
| `--view-cm CM` | viewing distance of the stripe check, e.g. `--view-cm 30` (default 30) |
| `--mix-teeth` | colour-volume controls, e.g. `3d gcode-mix-gate plate.gcode --config gates.json --parts parts --mix-teeth` |
| `--teeth-volume NAME` | restrict `--mix-teeth`, e.g. `--teeth-volume iris` |
| `--cavity-teeth` | cavity / pause controls, e.g. `--cavity cavity.json --cavity-teeth` |
| `--json` | JSON report on stdout, e.g. `--json \| jq '.gates[] \| select(.status=="FAIL")'` |

Exit: 0 PASS, 1 FAIL (a gate, or a teeth control that did not trip), 2 usage error or bad / missing input.

## Examples

```
3d gcode-mix-gate out/cat.gcode --config out/cat.manifest.json --parts work/cat.parts --default-slot 2
3d gcode-mix-gate out/cat.gcode --config gates.json --parts parts --mix-teeth --teeth-volume iris
3d gcode-mix-gate out/cat.gcode --config gates.json --cavity cavity.json --cavity-teeth --layer-heights 0.08:0.32
3d gcode-mix-gate out/cat.gcode --config gates.json --parts parts --json | jq '.gates[] | select(.status=="FAIL")'
```

Related: [`3d print-preflight`](print-preflight.md) (`mix layers`: the layer rules per object from heads
alone, no meshes), [`3d gcode-bounds`](gcode-bounds.md).
