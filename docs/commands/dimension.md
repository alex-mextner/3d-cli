# `3d dimension` — renders that carry their sizes

A plain render does not let the owner confirm a model before a print: they have to SEE the
sizes on the picture. `3d dimension` draws the real mesh, flat-shaded, from top / front / side /
iso, with double-headed arrows and the value in mm on each dimension, and then proves the
labels can be read. Issue: [#477](https://github.com/alex-mextner/3d-cli/issues/477).

## Usage

```
3d dimension MODEL.(stl|3mf|obj|scad) -o OUT.png [--bed WxD] [--dim NAME:x,y,z:x,y,z]... [options]
3d dimension --check IMAGE.png [--json]
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

`--dim` points are model-space mm and may be negative (`--dim "width:-30,0,0:30,0,0"`).
`span` is shown only when the part's oriented bounding box is more than 3% longer than its
axis-aligned one, so an axis-aligned block, strap or pyramid gets no space-diagonal "length".

A `.scad` input is exported through `openscad` first. Each label is placed at the first
offset/side/position that clears the part's silhouette, the other labels and the panel edge;
an extra dimension (`--dim`, `span`) with no clean spot in a view is left out of that view
instead of covering geometry. Arrows and extension lines avoid labels already placed where a clean
spot exists (a `bbox` dimension with none is still drawn), and the bed caption is reserved so no
label can cover it.

## Sidecar `OUT.png.dims.json`

`image` (path, width, height, sha256), `part`, `bbox_mm`, optional `bed`, `views`
(`rect` of each panel and a coarse `silhouette_grid` of the part), and `dimensions`: one entry
per drawn label with `label`, `value_mm`, `view`, `kind` (`bbox` / `custom`), `text`,
`font_px`, `box` (padded label box, px), `text_box` (the text only) and the `arrow` end points.

## `--check` — the legibility gate

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
