# `3d dimension` — renders that carry their sizes

A plain render does not let the owner confirm a model before a print: they have to SEE the
sizes on the picture. `3d dimension` draws the real mesh, flat-shaded, from top / front / side /
iso, with double-headed arrows and the value in mm on each dimension, and then proves the
labels can be read. Issue: [#477](https://github.com/alex-mextner/3d-cli/issues/477).

## Usage

```
3d dimension MODEL.(stl|3mf|obj|scad) -o OUT.png [--bed WxD] [--dim NAME:x,y,z:x,y,z]... [--part SEL=NAME[:ROLE]]... [-D NAME=VALUE]... [--up Z|Y|X] [--overall auto|always|never] [--dims-from SPEC] [options]
3d dimension --check IMAGE.png [--json] [--require-dims]
```

Example (the spoon150 print pose on a 220 x 220 bed):

```
3d dimension print/spoon150_k1se_oriented.stl -o ~/.cache/3d-cli/spoon150_dims.png --bed 220x220
3d dimension --check ~/.cache/3d-cli/spoon150_dims.png
```

## What is drawn

| Item | When |
|---|---|
| `X`, `Y`, `Z` — overall bounding box | every view where the edge is visible (top: X, Y; front: X, Z; side: Y, Z; iso: all three) |
| `span` — the longest distance between two points of the part | when the part's oriented bounding box is more than 3% longer than the axis-aligned one (a part lying slanted or tilted: 150.2 mm for spoon150 vs 117.9 mm of bbox) |
| `--dim "name:x,y,z:x,y,z"` — any feature, points in model mm | views where the arrow keeps >= 90% of its true length (an arrow shown foreshortened would lie about the value) |
| `--bed WxD` — dashed bed outline + `bed W x D mm` in the top view | model coordinates are bed coordinates, as in a print-pose STL; `PART OUTSIDE BED` in red when it sticks out |
| part name + overall size | header |

Render options besides `--bed` and `--dim`:

| Option | Meaning | Example |
|---|---|---|
| `-o`, `--out OUT.png` | output image (required); the sidecar is `OUT.png.dims.json` | `3d dimension part.stl -o previews/part_dims.png` |
| `--name TEXT` | part name in the header (default: the file name) | `--name "spoon150 K1 SE"` |
| `--width PX` | image width, 640..8192 (default 1280; the legibility rules scale with it) | `--width 1920` |
| `--label-px PX` | label font size at 1280 px width (default 20; below 14 `--check` fails) | `--label-px 24` |
| `-D NAME=VALUE` | override an OpenSCAD variable of a `.scad` input (repeatable; any other input is a usage error) | `-D standin=true` |
| `--part SEL=NAME[:ROLE]` | rename a part and/or set its role (`printed`, `vitamin`, `reference`, `stand_in`); SEL is a part name or a `#RRGGBB` colour (repeatable) | `--part "#2a62c9=PCB:stand_in"` |
| `--up Z\|Y\|X` | the model's vertical axis (default `Z`). A Y-up model gets a TRUE top view; labels keep the model's own X/Y/Z letters; not with `--bed` ([#595](https://github.com/alex-mextner/3d-cli/issues/595)) | `--up Y` |
| `--overall auto\|always\|never` | overall X/Y/Z of the whole model. `auto` (default) draws them, except on an exploded assembly or a plate whose parts do not touch: there each separate part gets its own footprint dims (`X left 10.0 mm`) and the header says `N separate parts`. `always` on such a layout is refused (exit 2) ([#596](https://github.com/alex-mextner/3d-cli/issues/596)) | `--overall never` |
| `--dims-from SPEC` | JSON/YAML list (or `{dims: [...]}`) of `{name, from: [x,y,z], to: [x,y,z]}` measurements that MUST be drawn: the render exits 2 naming any that fits no view, and the sidecar records them as `required_dims` ([#594](https://github.com/alex-mextner/3d-cli/issues/594)) | `--dims-from dims.yaml` |

`--dim` points are model-space mm and may be negative (`--dim "width:-30,0,0:30,0,0"`).
`span` is shown only when the part's oriented bounding box is more than 3% longer than its
axis-aligned one, so an axis-aligned block, strap or pyramid gets no space-diagonal "length".

A `.scad` input is exported through `openscad` first (to 3MF, which keeps every `color()`). Each label is placed at the first
offset/side/position that clears the part's silhouette, the other labels and the panel edge;
an extra dimension (`--dim`, `span`) with no clean spot in a view is left out of that view
instead of covering geometry. Arrows and extension lines avoid labels already placed where a clean
spot exists (a `bbox` dimension with none is still drawn), and the bed caption is reserved so no
label can cover it.

## Parts, colours and the legend

Different parts are ALWAYS drawn in different colours, and a legend strip under the four views lists
colour -> part. (Before [#580](https://github.com/alex-mextner/3d-cli/issues/580) every model was one
flat blue: `.scad` `color()` was dropped by the STL export, a coloured 3MF failed without `lxml`, and an
OBJ's materials were never read. The 3MF and OBJ readers are now stdlib-only: no `lxml`, no `networkx`.)

**Where the parts come from**

| Input | One part per | Name |
|---|---|---|
| `.3mf` with several objects / build items | object (an object with several colours is split: `lid / #D2B07A`) | the object's `name` (or the slicer project's) |
| `.3mf` with ONE object, per-triangle colours (every OpenSCAD export) | colour group | the material name, or the hex (`#7A5A3A`) when the name is generated (`Color 3`) |
| `.obj` + `.mtl` | `o` / `g` group (or `usemtl` material when the file has one object) | the group / material name |
| `.scad` | `color()` group (openscad exports 3MF) | the hex of the `color()`; geometry without `color()` is `uncoloured` |
| geometry with no colour at all (STL, uncoloured `.scad`) | separate body (connected pieces) | `body 1`, `body 2`, ... (fragments under 2% of the model join the biggest body; more than 24 bodies is treated as one noisy mesh) |

A single-part model keeps one colour and gets no legend. Rename a part, or give it a role, with
`--part SELECTOR=NAME[:ROLE]`: SELECTOR is a part name or a colour (`#2a62c9`, `#abc`), NAME may be empty
(`--part "#2a62c9=:stand_in"`). A selector that matches nothing is an error listing what the model has.

**How the colour is chosen** (deterministic: the same assembly gives the same colours in any order)

1. the authored colour (3MF material, OBJ `Kd`, OpenSCAD `color()`), if it differs clearly (CIE76 dE >= 20)
   from every colour already taken, parts in name order;
2. else the shared part palette of `lib/parts.py` (`assign_styles`, keyed by the part NAME), its hue stepped
   until it clears every taken colour. So two parts never share a colour, however the file was authored.

**Roles and the muted style.** A part's role is `printed` (default), `vitamin`, `reference` or `stand_in`
(`lib/parts.py`): from the 3MF object tag `3d:role` / `3d:printable`, or `--part SEL=NAME:ROLE`. A
`stand_in` is drawn MUTED (its palette colour washed 55% toward white, authored colour ignored) and the
legend marks it `(stand-in)`, so a bought or imaginary part cannot be mistaken for a printed one;
`vitamin` / `reference` keep their colour and are marked in the legend.

Parts of a file that carry a role tag (`3d:role=stand_in|vitamin|reference`) are always included in the render, muted; for a `.scad` the model's own defines decide what exists (`-D standin=true`).

Example (the soil-sensor mole: shells, nose/paws, eyes, and the purchased probe as stand-ins):

```
3d dimension assembly_mole.scad -D standin=true -o ~/.cache/garage-band/soil-sensor-case/mole.png \
  --part "#7a5a3a=back shell" --part "#d2b07a=front shell" --part "#e8909a=nose + paws" --part "#15110e=eyes" \
  --part "#2a62c9=PCB:stand_in" --part "#d4a017=prongs:stand_in" --part "#1a1a1a=connector:stand_in" \
  --part "#c01818=wire red:stand_in" --part "#6b3a1e=wire brown:stand_in"
3d dimension --check ~/.cache/garage-band/soil-sensor-case/mole.png
```

Dimension arrows and labels keep their own colours and white label boxes, so they stay legible on any part
colour; the legibility gate below is unchanged.

## No dimension across separate parts

On an **exploded assembly or a plate** (the parts do not touch: a `separated_parts` layout, recorded in the sidecar) a measurement between two parts only measures the gap between them, so it is made impossible:

| What | Result |
|---|---|
| automatic overall X/Y/Z | not drawn; each separate part gets its own footprint dims (`--overall auto`); `--overall always` is refused (exit 2) |
| automatic `span` of a slanted part | not drawn across separated parts |
| `--dim` / `--dims-from` with both endpoints on ONE part | drawn |
| `--dim` / `--dims-from` with endpoints on DIFFERENT parts (each endpoint is on the parts whose bounding box, grown by 0.5 mm, holds it) | refused, exit 2, naming both parts and the allowed form |
| an endpoint on no part | refused, exit 2 |
| `{name, kind: gap, parts: [A, B], from, to}` in `--dims-from`, one endpoint on each named part | drawn as the declared mating gap (`kind: gap` in the sidecar) |

`--check` fails an image whose `.dims.json` holds a dimension with `spans_separated: true` that is not a `kind: gap` (rule `spans-separated-parts`), so an image from a tool that did not enforce this cannot pass as legible when its sidecar says so. Touching parts (an assembled model) are not restricted.

## Occlusion: only what the eye sees can be measured

An explicit measurement (`--dim`, `--dims-from`) is drawn in a view only if BOTH endpoints are visible in that view: a ray from each endpoint toward the (orthographic) eye must not meet a surface more than 0.5 mm away. A PCB sealed inside its case is hidden in every view, so its arrow can no longer float in the air next to the case. A dimension hidden in a view is listed in the sidecar as `hidden_in_view` (`{"name": ["front", "side"]}`); it is still drawn in the views that do see it. A `--dims-from` measurement that is drawn in NO view fails the render (exit 2, "lies inside the model, hidden in every view" when that is why), and `--check --require-dims` fails an image that declares it but does not show it. The automatic overall dims and per-part footprints, whose endpoints are bounding-box corners, are not tested.

To measure something inside, cut the model so the feature is exposed (`-D cut=true` on an assembly that supports it, or measure on a cut STL): the exposed faces are visible geometry. An explicit measurement is anchored ON the part: its arrow may run over the part and is then drawn **dashed**; only its label must find clean paper (the legibility gate is unchanged). Name the measurement after the object it measures (`--dim "PCB head + plug (inside):0,1.1,0:0,1.1,27.6"`): the name is the label text. A cut-face anchored dimension in `3d section` itself is not implemented yet (#632).

Reserved names: the automatic dimensions are labelled `span`, `X`, `Y`, `Z` and `X <part>` / `Y <part>` / `Z <part>`, and the renderer tells them from yours by that text. A `--dim` or `--dims-from` name equal to `span`, `X`, `Y`, `Z`, or starting with `X `, `Y `, `Z ` is refused (exit 2) so it cannot bypass the separated-parts and occlusion checks. A `--dims-from` YAML file needs pyyaml; without it the command exits 127 (missing dependency) while JSON files still work.

## Sidecar `OUT.png.dims.json`

`image` (path, width, height, sha256), `part`, `bbox_mm`, optional `bed`, `views`
(`rect` of each panel and a coarse `silhouette_grid` of the part), and `dimensions`: one entry
per drawn label with `label`, `value_mm`, `view`, `kind` (`bbox` / `custom`), `text`,
`font_px`, `box` (padded label box, px), `text_box` (the text only) and the `arrow` end points.

Since [#580](https://github.com/alex-mextner/3d-cli/issues/580) it also carries `parts` (per part:
`name`, `role`, `printable`, displayed `color`, the `authored_color` of the file, `color_source`
`authored` / `palette` / `default`, `muted`, `faces`), `legend` (`box` of the strip, `rows`; absent for a
single part) and `warnings` (e.g. an OBJ whose MTL is missing); each dimension also carries `spans_separated` (and `parts`), and the sidecar `separated_parts`. `--check` ignores these: it judges the
labels only.

## `--check` — the legibility gate

`--check IMAGE --require-dims` additionally FAILS (`missing-required-dim`) when a measurement declared with `--dims-from` is not in the image, or (`no-required-dims`) when the image declares none. Without the flag nothing changes.

Reads the sidecar beside the image AND measures the pixels. Exit 0 = legible, 1 = offenders
listed (`--json` for machine output), 2 = usage error.

| Rule | Fails when |
|---|---|
| `no-sidecar` / `bad-sidecar` / `bad-image` / `sidecar-mismatch` | the sidecar is missing or malformed (any entry without its fields), the image cannot be read, or the sidecar was written for another image (sha256 or size differ — an edited image must be re-rendered) |
| `too-few-dimensions` | fewer than 3 labelled dimensions |
| `font-too-small` | font < 14 px at 1280 px image width (scales with a wider image) |
| `text-too-short` | the drawn text is shorter than a 14 px font at 1280 px gives, scaled to the image width (the sidecar cannot lie about size) |
| `outside-image` / `outside-panel` / `unknown-view` | a label box leaves the image or its view panel, or names a view the sidecar does not have |
| `overlaps-label` | two label boxes intersect |
| `covers-geometry` | more than 10% of a label box lies on the part's silhouette |
| `no-text` / `low-contrast` | the label box has no readable ink, or text/background contrast is under 4.5:1 (WCAG AA) |
| `value-not-shown` | the label text does not end in `<value_mm> mm` (to one decimal) |

`--label-px 8` produces an image the gate rejects (used by the tests). The tg-cli send gate
([tg-cli#785](https://git.hyperide.ai/ultrabricks/tg-cli/issues/785)) calls this same check
before a model render reaches the owner.
