# `3d silhouette` — camera-locked render → binary silhouette mask

Renders a model at a fixed camera, then thresholds the image to a binary mask (white shape on black background). Used as the first step in the reference-match pipeline.

**Why it exists.** To compare a 3D model against a 2D reference photo, you need a reproducible silhouette from the model. Locking the camera and thresholding the render gives a deterministic mask that can be scored with IoU / AE.

## Usage

```
3d silhouette <file.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>_mask.png` | Output mask PNG |
| `--cam ex,ey,ez,cx,cy,cz` | auto ISO via `viewall` | 6-param **vector** camera |
| `--size WxH` | `1200x900` | Image size |
| `--ortho` | off | Orthographic projection (recommended for reference overlay) |
| `-D k=v` | — | Pass-through define (repeatable) |

```bash
3d silhouette model.scad -o mask.png --ortho --cam 130,-600,52,130,0,52 --size 1600x700
```

## Implementation notes

Requires ImageMagick (`magick`). The default camera is auto-fit via `--autocenter --viewall`. For reproducible scoring against a reference, use `--cam` with a value produced by `3d fit-camera`.
