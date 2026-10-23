# `3d detect` — open-vocabulary boxes

Boxes for text queries in a photo (OWLv2-base-patch16-ensemble, fp16 on GPU / fp32 on CPU). Implemented by
`threed_vision.detect.OwlDetector`; `3d detect` is a thin launcher around `python -m threed_vision detect`.

**Why it exists.** Text-prompted boxes drive `3d segment`'s fill stage and let a UI find "a hand tool" or "pliers" by name.

## Usage

```
3d detect <image> --queries "q1,q2" [--backend owlv2] [--threshold F] [--nms F] [--max-area-frac F] [--masks DIR] [--device auto|cuda|cpu] [--json]
```

```bash
3d detect tools.jpg --queries "a hand tool,a box" --json
3d detect tools.jpg --queries pliers --threshold 0.25 --nms 0.3 --json
3d detect tools.jpg --queries "a tape roll" --max-area-frac 0.02 --device cpu
3d detect tools.jpg --queries pliers --backend owlv2     # the only backend today
3d detect tools.jpg --queries "a hand tool,pliers" --masks out/ --json     # boxes + a SAM2 mask per box
```

| flag | meaning |
|---|---|
| `--queries` | comma-separated text queries (required) |
| `--backend owlv2` | only backend |
| `--threshold` | minimum sigmoid score (default 0.15) |
| `--nms` | class-agnostic IoU above which the lower-scored box is dropped (default 0.5) |
| `--max-area-frac` | drop boxes larger than this image fraction (default 0.05) |
| `--masks DIR` | also prompt SAM2.1-tiny with every box (one mask each) and write a `3d segment`-compatible directory (below) |
| `--device` | `auto` cuda only with >= 700 MB free VRAM; GPU OOM falls back to CPU |

Output (`--json`): `{"boxes":[{"xyxy":[x0,y0,x1,y1],"score":0.31,"label":"a hand tool"}],"timing_s":0.7,"device":"cuda","image":{"path","w","h"}}`.
With `--masks DIR` the JSON gains `"masks": {"dir", "instances": "instances.json", "n"}` and `DIR` receives `instances.json` (same schema as
[`3d segment`](segment.md); `source` is `owl`, `method` is `owlv2+sam2-box`, and each record also has `label`, `score`, `box_xyxy`), `masks/NNN.png`
(ids 1..N in box order; a box whose prompt gives an empty mask is skipped), `labels.png` and `overlay.jpg`. The boxes-only default never loads SAM2.
Exit codes: 0 ok, 2 bad input, 127 weights missing (`3d vision-models pull owlv2-base`).

**OWLv2 pads the image to a square** (bottom/right) before inference, so its normalised boxes are relative to the padded square: they are
scaled by `max(H, W)` on both axes and clipped to the real image (`detect.boxes_from_square`). Scaling by W and H squashes every box.

Measured on the carpet photo (2000x1500): 0.18 s warm GPU, 2.4-3.6 s cold GPU (includes the first forward), 5.6-9 s CPU (fixed 960 px input).

## Python API

```python
from threed_vision.detect import OwlDetector

det = OwlDetector(device="auto")          # load() is implicit on first detect(); det.unload() frees the weights
out = det.detect(rgb, ["a hand tool", "pliers"], threshold=0.15, nms=0.5, max_area_frac=0.05)
out["boxes"]      # [{"xyxy": [x0, y0, x1, y1], "score": 0.31, "label": "a hand tool"}, ...] sorted by score
out["timing_s"]

# boxes -> masks (SAM2 box prompts; the image is embedded once)
from threed_vision.segment import Segmenter
seg = Segmenter(method="sam2", fill="none", reject_bg=False, device="auto")
insts = seg.masks_for_boxes(rgb, out["boxes"])   # [{id, source:"owl", label, score, box_xyxy, bbox_xywh, area_px, mask, ...}]
seg.unload()
```
