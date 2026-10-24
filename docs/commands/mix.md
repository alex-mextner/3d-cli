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
3d mix target IMAGE --box x0,y0,x1,y1 (--white x0,y0,x1,y1 | --no-wb) [--white-level N] [--json]
3d mix recipes TARGET (--slots N=HEX,... | --printer NAME [--shelf]) [--count N] [--max-cycle N]
               [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--printops-api URL] [--json]
3d mix tester  TARGET (--slots N=HEX,... | --printer NAME [--shelf]) -o OUT/NAME [--count N]
               [--max-cycle N] [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--height MM]
               [--base-3mf FILE.3mf] [--printops-api URL] [--json]
  TARGET: --target HEX | --target-photo IMAGE --box x0,y0,x1,y1 (--white x0,y0,x1,y1 | --no-wb)
          [--white-level N]
               [--shelf: --max-swaps N --material NAME --min-g G]
3d mix readout PHOTO --manifest NAME.mix.json -o OUT/NAME [--white card|N] [--white-level N]
               [--no-qr-check] [--json]
3d mix fit READOUT.json [READOUT.json ...] [-o OUT/NAME] [--lambda W] [--json]
3d mix record READOUT.json --printer NAME [--fit FIT.json] [--yes] [--offline] [--printops-api URL] [--json]
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


## `target` — the colour to match, read from a mockup photo or a render

[#415](https://github.com/alex-mextner/3d-cli/issues/415), plan §20.2. The target of a mix is
usually a spot in a photo of the mockup, not a hex. `3d mix target IMAGE --box x0,y0,x1,y1
--white x0,y0,x1,y1` reads it (a port of `photo_colours` of babushkin-dom `gen/colour_match.py`):

1. the pixels of `--box` and of `--white` (a region of white trim or paper on the same photo) are
   cut from the upright image (EXIF orientation applied; pixel coordinates as a viewer shows
   them, `x1`/`y1` exclusive; a box outside the image is an error, never clipped);
2. **white balance**: a per-channel gain in linear light maps the median of the white region to
   `--white-level` (default 235, the level babushkin-dom maps its trim to). It removes the colour
   cast and the exposure in one step. A white region with a channel median under 64 is refused
   (it is not white);
3. the balanced pixels of the box go to CIELAB (D65) one by one and the target is the **median
   in Lab** of the box, so a shadow edge or a glint inside the box does not move it. A box above
   one million pixels is read with a fixed stride.

A render (Blender, a mockup tool) has no colour cast: `--no-wb` skips step 2. A photo needs
`--white`; the command refuses a photo without one rather than silently trusting the camera.

Text and `--json` carry the same numbers. The text starts with the target hex, then the Lab, the
white region's median RGB and gain, and the image's sha256; `--json` is the `mix-target/1` record
(`target`, `lab`, `kind` photo/render, `image`, `sha256`, `image_size`, `box`, `samples`,
`white_balance`, `white_box`, `white_level`, `white_rgb`, `gain`).

`recipes` and `tester` take `--target-photo IMAGE --box ... (--white ... | --no-wb)` instead of
`--target HEX` (not both) and use the same reading. The tester manifest keeps `target` as the
hex (the readout and the records read it) and adds `target_source`: `kind`, `image`, `sha256`,
`image_size`, `box`, `white_balance`, `white_box`, `white_level`, so the colour can be traced to
the pixels it came from. The summary and `--json` of both commands repeat it.

```
3d mix target mockup.jpg --box 410,220,470,300 --white 20,30,80,90
3d mix tester --target-photo mockup.jpg --box 410,220,470,300 --white 20,30,80,90 --printer u1 -o out/mix/wall
```

## `readout` — a photo of the tester card → the measured colour of every swatch

[#416](https://github.com/alex-mextner/3d-cli/issues/416), plan §20.5. A standing swatch shows the top
camera only its top (one layer), so the printed swatches come off the bed and lie **face up on a
white card**. `3d mix tester` writes that card next to the project: `NAME.card.png`, 102 mm wide, at
10 px/mm — print it **at 100 %** (no fit-to-page; the PNG carries 254 dpi) on plain white paper.
It has four ArUco fiducials (DICT_4X4_50, ids 0-3, 12 mm, the corners), a QR naming the manifest
(`3dmix:<id>`, the manifest's `id`), three blank paper regions (keep them blank: they are the white
reference), and a numbered frame per swatch: lay swatch *n* in frame *n*, wide face up. The layout
is the manifest's `readout` section (`mix-card/1`, card millimetres, +Y up, as `3d calibrate
rectify` takes plate fiducials), so the reader takes every box from the manifest.

Then photograph the card — a phone, or the top camera (`3d camera still --depth 16 --out frame.png`)
— and `3d mix readout frame.png --manifest out/mix/wall.mix.json -o out/mix/wall-readout`:

1. **Rectify.** The four fiducials are found (`calib_rectify.detect`; 3 of 4 are enough) and fit a
   homography image px → card mm; the photo is warped to a straight 10 px/mm image. Fewer than 3
   fiducials, or a card under 2.5 px/mm in the photo, is refused with what to reshoot. A fit worse
   than 1 mm rms is a warning (a bent card, a distorting lens).
2. **Which tester.** The QR is decoded and must be this manifest's. A card that names another
   tester is refused (exit 1, both ids in the message); so is a card whose QR cannot be read, unless
   `--no-qr-check` says to read it anyway (the record then says `QR skipped`).
3. **White balance** on the blank paper regions (`--white card`, default): a per-channel gain in
   linear light that maps the paper to `--white-level` (235, as `3d mix target`), fitted as a plane
   over the three regions so an uneven light across the card is corrected. A paper region darker
   than 64 in any channel (a shadow) is refused. With `--white N` the balance is on the pure
   reference swatch *N* (a white spool) instead, mapped to that spool's own colour.
4. **Per swatch**, the median in CIELAB of its **patch**: the face without its 1.5 mm border (the
   rounded edge never reads; the number is printed on the card outside the face). The record has
   the measured hex and Lab, ΔE2000 to the predicted colour and to the target, the pixel count and
   the patch's spread (90th-percentile Lab distance from its median). A spread over 8 is a
   warning: the swatch is not flat — not in its frame, glare, a shadow across it.
5. **Files.** `OUT/NAME.readout.json` (`mix-readout/1`: manifest id and target, photo sha256 and
   size, fiducials, QR, white balance, every swatch, a summary with the mix closest to the target,
   warnings), `OUT/NAME.readout.png` (the rectified card with every patch boxed, beside the
   predicted | measured | target colours and ΔE per swatch) and `OUT/NAME.rectified.png`.

Exit 1 when the photo cannot be read; exit 2 for a bad argument or a manifest without a card layout
(a tester written before #416: run `3d mix tester` again). OpenCV is needed
(`uv sync --extra vision`); the command is import-light, the reader runs through `cli.pyrun`.

```
3d mix tester --target '#B5654F' --printer u1 -o out/mix/wall      # writes out/mix/wall.card.png
3d mix readout phone.jpg --manifest out/mix/wall.mix.json -o out/mix/wall-readout
3d mix readout frame.png --manifest out/mix/wall.mix.json -o out/mix/wall-readout --white 9 --json
3d mix fit out/mix/wall-readout.readout.json -o out/mix/wall
3d mix record out/mix/wall-readout.readout.json --printer u1 --fit out/mix/wall.fit.json --yes
```

## `fit` — per-filament colours from the measured mixes

[#417](https://github.com/alex-mextner/3d-cli/issues/417), plan §20.3. `3d mix fit READOUT.json
[READOUT.json ...] [-o OUT/NAME] [--lambda W]` reads one or more `mix-readout/1` records
(`readout`) and fits, per filament, the colour it shows on a wall in a mix:

- the **pure reference swatch** of each filament is its printed colour (measured, not Spoolman's);
- the **mixes** then adjust it: least squares over every measured mix of the CIELAB distance between
  the model colour (the mean of the layers in linear light, as `recipes`) and the measured swatch,
  with 3 parameters per filament (linear sRGB, bounded 0..1). A light filament that looks darker in
  thin layers over a dark one gets its effective colour from this;
- a **regulariser** pulls each filament back to its pure swatch (`--lambda`, default 0.1: the pure
  swatch counts as 0.1 of a mix swatch). A filament that no mix constrains (black next to a white
  that dominates every mix) stays near its pure swatch; one with no mix swatch at all keeps it
  exactly and is listed under `notes`.

The report lists per filament the nominal (Spoolman), pure and fitted colours, and per mix the ΔE
of the prediction from the nominal, the pure and the fitted colours, then the mean error **before
and after**:

```
mix fit of 1 readout(s): 3 filaments, 4 mixes (lambda 0.1)
  filament   slot  nominal   pure      fitted     dE nominal>fitted
  f21           1  #000000   #1E1E21   #1D1D20    ...
  mean mix error: nominal 7.29 > pure 1.32 > fitted 0.23
```

Several readouts pool by Spoolman filament id (else by head number): two testers of the same
spools fit together. `-o OUT/NAME` writes `OUT/NAME.fit.json` (`mix-fit/1`: filaments, mixes,
summary, notes). numpy and scipy are needed (the reader runs through `cli.pyrun`).

## `record` — measured colours into 3d-api

`3d mix record READOUT.json --printer NAME [--fit FIT.json] [--yes] [--offline] [--printops-api URL]`
writes the readout's measurements as calibration records of the printer, in the order of plan
§20.4: first one **`filament_color`** per pure swatch whose Spoolman filament id the tester knew
(`hex` = the fitted colour when `--fit` is given, else the measured swatch; `spoolman_hex` = the
nominal colour it corrects; `delta_e`; `lab_l/a/b`), then one **`mix_color`** per mix swatch with a
mix key (`hex` = measured, `predicted_hex`, `delta_e_predicted`). Method `mix_tester_photo`. A swatch
without a filament id or mix key is listed as `skipped` with the reason, never recorded under a guess.

Every record carries in `params` the **sha256 of the readout file** (`readout_sha256`), the photo's
sha256, the swatch number and the manifest id, and lists the photo and the readout as `evidence`.

- Without `--yes` it only prints the plan (exit 0).
- With `--yes` it `POST`s `/v1/calibrations` (`GET /v1/calibrations` and `3d calibrate ls` show the
  records). **Repeating the same readout adds nothing**: a record of the same readout and swatch is
  reported as `already recorded #N`. A later readout of the same filament or mix supersedes the
  earlier record (§20.4).
- When 3d-api cannot be reached (or with `--offline`) the same records are **appended to the
  settings-history buffer** `<data dir>/printer-history/<printer>.jsonl` (`printops.settings_change/1`,
  status `measured`, uid from the readout sha256 and the record's scope; a repeat appends nothing).
  `3d settings import` brings them into 3d-api later, idempotent by uid; a mix line maps to a `mix_color`
  record by its `mix` key. A refusal by 3d-api (unknown filament, validation) is reported per record and
  exits 1.

## Measured colours in `recipes` and `tester`

`3d mix recipes` and `3d mix tester` take `--fit FIT.json`: the fitted colour of a head replaces the
slot's colour. With `--printer NAME` the **current `filament_color` records** of 3d-api replace the
colours of the loaded spools' filaments and the **current `mix_color` records** of the printer
replace the model's prediction of those mixes (plan §20.3, first in the order of trust); the list is
re-ranked with them. Measured colours are marked: `T4 #EFEBE1 measured (nominal #FFFFFF)` in the
header, `measured` after a mix, `"measured": true` and `"nominal_hex"` in `--json` (slots and
candidates). If the records cannot be read, the nominal colours are used and a line on stderr says so.

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

Writes four files ([#413](https://github.com/alex-mextner/3d-cli/issues/413)):

- `NAME.3mf` — a Snapmaker Orca project. The loaded spools are the physical filaments. Each
  candidate gets a `mixed_filament_definitions` row (manual pattern, components 1/2, so every
  digit is a head) and a standing swatch 20 mm wide, 3 mm deep, `--height` tall. Each loaded spool
  gets a pure reference swatch, used for white balance and spool correction. A flat
  seven-segment number lies in front of every swatch, printed in the darkest loaded spool. The
  prime tower goes in a free corner. Swatches stand, so `3d mix check` passes the plate.
- `NAME.mix.json` — the manifest, schema `printops.mix_tester/1`: target (with `--target-photo`, also `target_source`: image, sha256, boxes), slots (head, hex,
  spool and Spoolman filament id when known), candidates (number, role, pattern, weights,
  predicted hex, ΔE, stripe ΔE, virtual filament, swatch and label boxes on the bed as
  `[x0, y0, x1, y1]`, `calibration: {kind: mix_color, mix: <key or null>}`), references
  (`calibration: {kind: filament_color, filament_id}`), and the file names.
- `NAME.png` — the swatch sheet: for each number, the target next to the predicted colour.
- `NAME.card.png` — the white card for the readout photo (see [`readout`](#readout--a-photo-of-the-tester-card--the-measured-colour-of-every-swatch)); the manifest's `id` and `readout` describe it.

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
- `--target-photo IMAGE` — take the target from a region of an image instead of `--target`: `--target-photo mockup.jpg --box 410,220,470,300 --white 20,30,80,90`
- `--box x0,y0,x1,y1` — the region to match, in pixels (`x1`, `y1` exclusive; origin top-left as a viewer shows the image): `--box 410,220,470,300`
- `--white x0,y0,x1,y1` — a region of white on the same photo (the white balance): `--white 20,30,80,90`
- `--fit FILE` — a `mix-fit/1` file whose fitted colours replace the slots' (`recipes`, `tester`, `record`): `--fit out/mix/wall.fit.json`
- `--lambda W` — `fit`: weight of the pure swatch against one mix swatch (default 0.1): `--lambda 0.5`
- `--yes` — `record`: write the records (otherwise only the plan): `--yes`
- `--offline` — `record`: write the settings-history buffer, do not call 3d-api: `--offline`
- `--manifest FILE` — `readout`: the tester manifest the photo belongs to: `--manifest out/mix/wall.mix.json`
- `--white card|N` — `readout`: balance on the card's paper (default) or on pure reference swatch N: `--white 9`
- `--no-qr-check` — `readout`: read a card whose QR is unreadable without confirming the tester: `--no-qr-check`
- `--no-wb` — the image is a render, no colour cast: `3d mix target render.png --box 100,100,160,160 --no-wb`
- `--white-level N` — the 8-bit value the white region is mapped to (default 235, 1-255): `--white-level 240`
- `--slots N=HEX,...` — the loaded filaments by U1 head number (1-9): `--slots 1=#000000,3=#1B5E20,4=#FFFFFF`
- `--printer NAME` — read the usable slots of NAME from 3d-api (`GET /v1/printers/NAME/slots`, then
  `/v1/spools/ID` for the filament id). Read-only. With filament ids, the manifest has mix keys: `--printer u1`
- `--shelf` — with `--printer`: also use the spools in stock but not loaded (3d-api `GET /v1/spools`, Spoolman).
  Loadouts = the loaded spools plus up to `--max-swaps` (default 2, at most 4) shelf spools, each on a free head or
  on the head of a loaded spool taken out; ranked by the best mix they reach (ΔE + stripes), equal results by fewer
  swaps. The plan names each spool, its head and ΔE before/after, and prints `3d printops spool load ID PRINTER/Tn`
  (`--replace-to LOCATION` where a loaded spool comes out). Left out, with the reason printed: spools under
  `--min-g` (default 30 g, "empty") and of another material than the loaded ones (`--material NAME` to choose).
  A current `filament_color` record replaces a spool's Spoolman colour. `tester --shelf` builds the plate for
  option 1 (its manifest names the spools): load them first. `3d mix recipes --target '#E59520' --printer u1 --shelf`
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
3d mix target mockup.jpg --box 410,220,470,300 --white 20,30,80,90
3d mix target render.png --box 100,100,160,160 --no-wb --json
3d mix tester --target-photo mockup.jpg --box 410,220,470,300 --white 20,30,80,90 --printer u1 -o out/mix/wall
3d mix tester --target '#808080' --slots 1=#000000,4=#FFFFFF --base-3mf print/u1.3mf -o out/grey
3d mix readout phone.jpg --manifest out/mix/wall.mix.json -o out/mix/wall-readout
3d mix readout frame.png --manifest out/mix/wall.mix.json -o out/mix/wall-readout --white 9 --json
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
