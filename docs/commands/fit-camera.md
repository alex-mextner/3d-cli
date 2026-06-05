# `3d [fit-camera](GLOSSARY.md#fit-camera)` — fit a camera to a reference photo

Optimises an [OpenSCAD](GLOSSARY.md#openscad) camera vector so that the rendered [silhouette](GLOSSARY.md#silhouette) of a model maximises [IoU](GLOSSARY.md#iou) against a reference photo. The result is saved as a JSON file plus a full-resolution fit PNG and a red/cyan overlay so you can visually verify alignment.

**Why it exists.** When matching a model to a reference image, the camera angle is usually unknown. Guessing by hand is slow. The optimiser finds the viewpoint automatically and writes a reproducible `camera.json` that can be reused for `render`, `score`, and `silhouette`.

## Usage

```
3d fit-camera <model.scad> <reference> [options]
```

| Option | Default | What |
|---|---|---|
| `--out FILE` | `./camera.json` | Output JSON with the fitted camera vector |
| `--center "x,y,z"` | bbox centroid | Initial look-at point |
| `--opt-size WxH` | ~300 px wide @ ref aspect | Low-res render size for the optimiser (fast) |
| `--final-size WxH` | reference native resolution | Full-res render size for the final fit PNG |
| `--thresh N` | `150` | Reference subject darkness threshold (0–255) |
| `--rand N` | `80` | Random-search samples |
| `--refine N` | `40` | Coordinate-descent refine steps |
| `--seed N` | `7` | RNG seed for reproducibility |
| `--el-range lo,hi` | `-45,85` | Elevation search range in degrees; use `-89,89` to search the full sphere |
| `--draw-axes` | off | Overlay PCA principal axis + bbox contour of both silhouettes |

```bash
3d fit-camera model.scad ref.jpg
3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
3d fit-camera model.scad ref.jpg --el-range -20,75 --seed 11
```

## Using the result

```bash
openscad --render --camera="$(jq -r .camera_arg camera.json)" -o view.png model.scad
```

## Output contract

The JSON contains:

- `camera_arg` and `camera` for replaying the exact OpenSCAD camera.
- `params` for the fitted azimuth, elevation, distance, and pan offsets.
- `center`, `model_diag`, `opt_size`, and `final_size` for auditing the scale and frame.
- `ref` for the reference image path used during fitting.
- `iou` and `ssim` for the final optimization-resolution mask comparison.
- `fit_render` and `overlay` paths. The overlay is a red/cyan binary mask diagnostic at the optimization resolution: red is the reference mask, cyan is the rendered mask.

## Dependencies

Needs `numpy` and `pillow` (resolved via `pyrun`).
