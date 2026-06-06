# `3d [fit-camera](GLOSSARY.md#fit-camera)` — fit a camera to a reference photo

Optimises an [OpenSCAD](GLOSSARY.md#openscad) camera vector so that the rendered [silhouette](GLOSSARY.md#silhouette) of a model aligns with a reference photo or mask. The default objective maximises [IoU](GLOSSARY.md#iou); the experimental `--objective contour` path optimises boundary agreement with edge F1, signed-distance-field loss, Chamfer distance, and p95 boundary distance. The result is saved as a JSON file plus a full-resolution fit PNG and red/cyan diagnostics so you can visually verify alignment.

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
| `--mask-polarity P` | `dark` | Which reference pixels are subject: `dark` for raw dark-on-light photos, `light` for white-subject binary masks |
| `--backplate FILE` | none | Original/reference photo to show in spatial proof panels when fitting against a derived mask |
| `--rand N` | `80` | Random-search samples |
| `--refine N` | `40` | Coordinate-descent refine steps |
| `--seed N` | `7` | RNG seed for reproducibility |
| `--el-range lo,hi` | `-45,85` | Elevation search range in degrees; use `-89,89` to search the full sphere |
| `--draw-axes` | off | Overlay PCA principal axis + bbox contour of both silhouettes |
| `--objective NAME` | `area-iou` | Optimizer objective: `area-iou` or experimental `contour` |
| `--spatial-report DIR` | none | Write `spatial_metrics.json`, `edge_overlay.png`, and `proof_panel.png` with contour-first diagnostics |
| `--trace FILE` | none | Write best-candidate JSONL trace for experiment/demo video tooling |

```bash
3d fit-camera model.scad ref.jpg
3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
3d fit-camera model.scad ref.jpg --el-range -20,75 --seed 11
3d fit-camera model.scad mask.png --mask-polarity light --backplate ref.jpg --objective contour --spatial-report match/spatial --trace match/trace.jsonl
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
- `backplate` for the optional original/reference photo used in proof panels.
- `mask_polarity` for auditing whether the reference was interpreted as dark-subject or light-subject.
- `iou` and `ssim` for the final optimization-resolution mask comparison.
- `fit_render` and `overlay` paths. The overlay is a red/cyan binary mask diagnostic at the optimization resolution: red is the reference mask, cyan is the rendered mask.
- `objective` and `objective_loss` for auditing whether the run used IoU or the contour prototype.
- `spatial_metrics`, `spatial_panel`, `edge_overlay`, and `trace` when the matching flags are enabled.

The spatial metrics include `area_iou`, `edge_f1@2`, `edge_f1@4`, `edge_f1@8`, `edge_chamfer_px`, `boundary_sdf_loss_px`, `hausdorff_p95_px`, `bbox_iou`, `coverage_ratio`, `centroid_delta_px`, border-touch flags, and `spatial_warning`. Treat `spatial_warning` as a real-image diagnostic: it means crop, scale, or boundary mismatch risk remains even if area IoU looks acceptable.

`--spatial-report DIR` writes:

- `spatial_metrics.json`
- `edge_overlay.png`
- `proof_panel.png`

`--trace FILE` writes JSONL rows with `{phase, iteration, coord?, loss, iou, params}` for
candidate-evolution demos.

## Dependencies

Default `area-iou` runs need `numpy` and `pillow` (resolved via `pyrun`). The experimental
`--objective contour` and `--spatial-report` paths also need `scipy` for distance
transforms.
