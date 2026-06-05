# `3d fit-camera` — fit a camera to a reference photo

Optimises an OpenSCAD camera vector so that the rendered silhouette of a model maximises IoU against a reference photo. The result is saved as a JSON file plus a full-resolution fit PNG and a red/cyan overlay so you can visually verify alignment.

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
| `--draw-axes` | off | Overlay PCA principal axis + bbox contour of both silhouettes |
| `--seed N` | `7` | RNG seed for reproducibility |

```bash
3d fit-camera model.scad ref.jpg
3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
```

## Using the result

```bash
openscad --render --camera="$(jq -r .camera_arg camera.json)" -o view.png model.scad
```

## Dependencies

Needs `numpy` and `pillow` (resolved via `pyrun`).
