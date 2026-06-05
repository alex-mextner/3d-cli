# `3d score` — silhouette AE + IoU scoring

Compares a render or a `.scad` model against a reference image and prints machine-parseable `KEY=VALUE` lines: `AE`, `AE_NORM`, `IoU`, `CLOSENESS`, `FRAME`, and `OVERLAY`.

**Why it exists.** The match loop and CI gates need an objective numeric score. Keeping the output as plain `KEY=VALUE` lines makes it easy to parse with `grep`, `awk`, or Python without pulling in a JSON parser.

## Usage

```
3d score <render.png|file.scad|mask.png> <reference|mask.png> [-o outdir] [options]
```

### Modes

| Mode | Flag | What |
|---|---|---|
| Render | *(default, first arg is `.scad` or `.png`)* | Renders the `.scad` at a locked camera, then masks both images and compares. |
| Mask | `--masks` | Both args are already binary masks (white shape, black bg); compared directly. |

### Options

| Option | Default | What |
|---|---|---|
| `-o DIR` | `/tmp/3dscore` | Output dir for masks and overlay PNGs |
| `--cam ex,..,cz` | `125,-330,52,125,28,44` | 6-param vector camera for `.scad` render |
| `--size WxH` | `1200x900` | Render size for `.scad` |
| `--ortho` | off | Orthographic projection for `.scad` render |
| `-D k=v` | — | Define passed to the `.scad` render (repeatable) |

```bash
3d score model.scad ref.jpg
3d score render.png ref.jpg -o work/
3d score mask_a.png mask_b.png --masks
```

## Dependencies

Requires ImageMagick (`magick`).
