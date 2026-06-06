# `3d overlay` — structured debug overlays

Compares a render against a reference image and produces selectable diagnostic overlays: difference map, 50% ghost blend, and canny edge-on-edge composite. Useful for visually spotting misalignment, missing features, or geometry drift.

**Why it exists.** Numeric scores ([IoU](GLOSSARY.md#iou), [AE](GLOSSARY.md#ae)) tell you *how far* the model is from the reference, but not *where*. The overlays show exactly which regions mismatch.

## Usage

```
3d overlay <render.png> <reference.{png,jpg}> [-o outdir] [options]
```

```bash
3d overlay render.png ref.jpg -o work/
3d overlay preview.png photo.jpg
3d overlay render.png ref.jpg -o diff/
3d overlay render.png ref.jpg --mode edge
3d overlay render.png ref.jpg --mode difference,ghost --json
3d overlay render.png ref.jpg --advice-only --json
```

## Options

- `-o, --out DIR` — output directory, defaulting to the render image directory.
- `--mode MODE` — output mode: `difference`, `ghost`, `edge`, or `all`. May be repeated and comma-separated lists are accepted.
- `--json` — print a machine-readable JSON summary instead of human output.
- `--advice-only` — print the planned artifacts without invoking ImageMagick.

## Output files

All selected artifacts are written to `outdir` (default: the render’s directory):

- `overlay.png` — difference map (auto-leveled)
- `ghost.png` — 50% opacity blend
- `edge_overlay.png` — canny edges of reference in red + render in cyan, composited

Without `--mode`, the command writes all three artifacts. The command also prints `AE(fuzz5%)` (mismatched pixel count) as a diagnostic number. `0` means identical after thresholding, and the advisory bucket suggests whether to inspect camera/scale alignment or smaller model parameters.

## Dependencies

Requires ImageMagick (`magick`) unless `--advice-only` is used.
