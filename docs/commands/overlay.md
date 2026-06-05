# `3d overlay` — difference / ghost / canny edge diagnostics

Compares a render against a reference image and produces three diagnostic overlays: difference map, 50% ghost blend, and canny edge-on-edge composite. Useful for visually spotting misalignment, missing features, or geometry drift.

**Why it exists.** Numeric scores (IoU, AE) tell you *how far* the model is from the reference, but not *where*. The overlays show exactly which regions mismatch.

## Usage

```
3d overlay <render.png> <reference.{png,jpg}> [-o outdir]
```

```bash
3d overlay render.png ref.jpg -o work/
```

## Output files

All written to `outdir` (default: the render’s directory):

- `overlay.png` — difference map (auto-leveled)
- `ghost.png` — 50% opacity blend
- `edge_overlay.png` — canny edges of reference in red + render in cyan, composited

The command also prints `AE(fuzz5%)` (mismatched pixel count) as a diagnostic number. `0` means identical after thresholding.

## Dependencies

Requires ImageMagick (`magick`).
