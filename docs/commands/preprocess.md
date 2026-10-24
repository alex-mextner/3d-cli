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
| `--backdrop` | off | Studio render / product shot on a smooth neutral backdrop: mask = everything that is not the backdrop. Skips the model and grabCut tiers (see below) |
| `--backdrop-saturation S` | `0.14` | `--backdrop`: highest HSV saturation of a backdrop pixel |
| `--backdrop-value LO,HI` | `0.38,0.80` | `--backdrop`: HSV value range of a backdrop pixel |

```bash
3d preprocess ref.jpg -o work/
3d preprocess ref.jpg --force-fallback
3d preprocess studio_render.png --backdrop -o work/
3d preprocess studio_render.png --backdrop --backdrop-saturation 0.2 --backdrop-value 0.3,0.9
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

## `--backdrop`: studio renders and product shots

grabCut and salient-object models treat black fur, white paws or a pink nose on a light-grey render as background or
shadow. `--backdrop` flips the question: the backdrop is the connected region of low-saturation, mid-value,
texture-free pixels that touches the frame border, and the subject is everything else (largest component, holes
filled). A floor shadow is mid-grey and low-saturation, so it counts as backdrop, not subject. On six neutral-grey
studio tiles the mask matches the project's reference masks at IoU 0.997-0.998.

Assumes a smooth neutral backdrop that is visible along the frame border (it fails with exit 2 and a hint when no
border pixel is backdrop, or when the whole frame is); kernel sizes are tuned for a 427 px frame and scale with
the image. Do not use it for a photo with a cluttered background. Needs `scipy` as well (resolved via `pyrun`).

## Dependencies

Needs `opencv-python-headless`, `numpy`, `pillow` (resolved via `pyrun`). SAM2 and Depth-Anything are optional heavy tiers.
