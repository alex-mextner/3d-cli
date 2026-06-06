# `3d preprocess` — subject mask + proportional depth from a photo

Takes a reference photograph and produces two outputs: `mask.png` (subject silhouette) and `depth.png` (proportional depth map). These are used as inputs for the reference-match pipeline.

**Why it exists.** A raw photo has background clutter and no 3D information. Separating the subject and estimating depth gives the match loop clean data to compare against the model silhouette.

The command also prints deterministic mask metadata: coverage percentage, `bbox_xywh`, and `centroid_xy`. Use these values to catch obvious segmentation failures, such as a mask that covers almost the whole frame or a centroid far from the intended subject.

## Usage

```
3d preprocess <reference.jpg> [-o outdir] [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out DIR` | alongside the image | Output directory |
| `--force-fallback` | off | Skip model tiers; use OpenCV / numpy floor only |
| `--sam2-checkpoint P` | — | Enable [SAM2](GLOSSARY.md#sam2) mask tier (path to a `.pt` checkpoint) |
| `--depth-model ID` | `depth-anything/Depth-Anything-V2-Small-hf` | Hugging Face model ID for depth estimation |

```bash
3d preprocess ref.jpg -o work/
3d preprocess ref.jpg --force-fallback
```

Example output:

```text
[mask ] tier=grabcut(cv2-fallback) (0.1s)  -> work/mask.png  (coverage=34.7% of frame)
[mask ] bbox_xywh=(152, 80, 791, 612)  centroid_xy=(551.4, 392.8)
```

## Tiers (auto-degrade)

1. **SAM2** or **rembg** for mask; **[Depth-Anything-V2](GLOSSARY.md#depth-anything)** for depth
2. **[OpenCV](GLOSSARY.md#opencv) grabCut** for mask; pseudo-depth from OpenCV
3. Always writes both outputs — never crashes if heavy models are missing

## Dependencies

Needs `opencv-python-headless`, `numpy`, `pillow` (resolved via `pyrun`). SAM2 and Depth-Anything are optional heavy tiers.
