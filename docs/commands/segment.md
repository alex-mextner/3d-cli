# `3d segment` — object instances from one photo

Splits a photo with many objects (tools on a carpet) into one mask per object. Implemented by
`threed_vision.segment.Segmenter` in `packages/threed-vision`; `3d segment` is a thin launcher
(`lib/cli/vision_env.py`) that runs `python -m threed_vision segment` in the vision venv.

**Why it exists.** Every photo->3D stage (recognise, scale, silhouette fit) starts from per-object masks. rembg / GrabCut
find one to four blobs on this kind of photo; SAM2 automatic masks alone miss thin or dark tools and keep carpet patches.

## Pipeline

1. **amg** — SAM2.1-tiny automatic mask generator (`points_per_side` grid, `points_per_batch` 32, pred-IoU >= 0.8, stability >= 0.9, box NMS 0.6).
2. **detect** — OWLv2 open-vocabulary boxes for `--queries`.
3. **fill** — SAM2 *box prompts* for every OWLv2 box that is less than 60 % covered by existing masks and whose new mask overlaps existing ones by < 30 %.
4. **bg_reject** — CLIP ViT-B/32 zero-shot: masks whose crop is more "carpet / parquet / cabinet / shadow / foot / bin / cable" than "tool / box / tape ..." are dropped (`bg_prob >= 0.5`).
5. an area filter always applies: `--min-area` (150 px) .. `--max-area-frac` (0.03 of the image).

`--method u2net` is the single-object fast path (u2net matte, largest component; 0.05 s GPU / 0.5 s CPU).

## Usage

```
3d segment <image> -o DIR [--method sam2|sam2-dense|u2net] [--fill owlv2|none] [--queries "a,b,c"]
           [--reject-bg|--no-reject-bg] [--points-per-side N] [--max-side PX] [--min-area PX]
           [--max-area-frac F] [--device auto|cuda|cpu] [--json]
```

```bash
3d segment tools.jpg -o seg/ --json                      # default pipeline
3d segment tools.jpg -o seg/ --fill none --no-reject-bg  # AMG only
3d segment one-tool.jpg -o seg/ --method u2net           # one tool on a plain background
3d segment tools.heic -o seg/ --device cpu --max-side 1024
3d segment tools.jpg -o seg/ --queries "a hand tool,a box" --points-per-side 24
```

| flag | meaning |
|---|---|
| `-o, --out DIR` | output directory (required) |
| `--method` | `sam2` AMG grid (default), `sam2-dense` 64x64 grid with batch 16, `u2net` one salient object |
| `--fill` | `owlv2` (default) add SAM2 masks for OWLv2 boxes the AMG missed; `none` |
| `--queries` | OWLv2 text queries, comma separated (default: hand tool, power tool, box, tube of glue, tape roll, pliers, screwdriver) |
| `--reject-bg` / `--no-reject-bg` | CLIP background rejection (default on) |
| `--points-per-side N` | AMG grid per side. Default 32 on GPU; on CPU `0` (no grid, OWLv2 boxes only) or 16 with `--fill none` |
| `--max-side PX` | downscale the longest side before SAM2 (`0` = never). Default: none on GPU, 1024 on CPU. Masks are always returned at the input resolution |
| `--min-area`, `--max-area-frac` | area filter (150 px, 0.03) |
| `--device` | `auto` picks cuda only when >= 2000 MB VRAM is free (the card is shared), else cpu; GPU out-of-memory falls back to CPU; explicit `cuda` never falls back |
| `--json` | print exactly one JSON object (the `instances.json` content) |

Exit codes: 0 ok, 2 bad input, 127 a model or package is missing (the message names `3d vision-models pull <name>`; models are never downloaded implicitly).

## Output

`DIR/instances.json`, `labels.png` (uint16, pixel value = instance id, 0 = background, larger masks painted first), `masks/NNN.png`, `overlay.jpg`.

```json
{"image":{"path":"tools.jpg","w":2000,"h":1500},"method":"sam2+owlv2","device":"cuda",
 "timing_s":{"load":0.97,"amg":5.52,"detect":2.38,"fill":1.05,"bg_reject":2.42,"total":13.76},"vram_mb":1935,
 "n_amg":95,"n_owl_added":35,"points_per_side":32,"process_scale":1.0,
 "instances":[{"id":1,"source":"amg|owl","bbox_xywh":[x,y,w,h],"area_px":1234,"bg_prob":0.02,
               "stability":0.93,"mask":"masks/001.png","polygon":[[x,y],"..."]}]}
```

`source` is `amg` (grid), `owl` (OWLv2 box -> SAM2 box prompt), `click` (UI add), `merged` (UI merge), `u2net`.
`stability` is SAM2's stability score for `amg` and the SAM2 predicted IoU for prompted masks. `timing_s.load` appears only when the
models were loaded during that call. `vram_mb` is the torch peak allocation (ONNX CLIP adds ~0.5 GB outside torch's counter).

## Measured (carpet photo 2000x1500, RTX 4060 8 GB shared, load average 5-10)

| run | time | instances | notes |
|---|---|---|---|
| GPU default, cold (loads SAM2, OWLv2, CLIP) | 13.8-15.4 s | 98 (65 amg + 33 owl) | torch peak 1935 MB |
| GPU default, warm (models resident) | 6.8-7.0 s | 98 | amg 4.07, detect 0.18, fill 0.77-0.80, bg_reject 1.07-1.19; torch peak 2255 MB |
| **CPU default**, cold | 23.8 s | 65 (all `owl`) | load 0.8, detect 9.0, fill 8.1 (includes the SAM2 image embedding), bg_reject 3.8 |
| **CPU default**, warm | 15.8 s | 65 | detect 5.6, fill 7.4, bg_reject 2.1 |
| `bin/3d segment` end to end, GPU (cold process, load average ~8, 3.9 GB held by other processes) | 27.8 s wall (`timing_s.total` 19.4) | 98 | nvidia-smi peak +2.5 GB for the whole process (torch CUDA context + ONNX CLIP) |
| `bin/3d segment --device cpu` end to end, **load average 23** | 70 s wall (`timing_s.total` 64.7: detect 17.5, fill 34.2, bg_reject 7.3) | 65 | the same CPU run is 2-3x slower on a saturated machine |
| CPU `--fill none` (16x16 AMG on a 1024 px copy) | 36.1 s | 29 | AMG 30.1 s: not worth it |
| CPU 32x32 AMG at full resolution (spike) | 85-92 s | 87 | unacceptable, never the default |
| `--method u2net` | 0.046 s GPU / 0.50 s CPU (warm) | 1 | one salient object only |

Why the CPU default skips the AMG grid: the grid costs ~0.12 s per prompt point on CPU, OWLv2 (fixed 960 px input, ~6 s) plus one SAM2 box
prompt per box (~0.06 s each when batched through one image embedding) is cheaper and finds the tools the grid is sampling for.
Quality (IoU >= 0.7 match against the GPU masks): the CPU default reproduces 41 % of the 98 GPU masks and 62 % of its own masks have a GPU
counterpart; it has fewer carpet patches but merges neighbouring tape rolls / hammer+wrench and drops the multimeter. Use a GPU for
production quality; the CPU path is the "no GPU, still usable" fallback.

Known residual errors (GPU default overlay): 3-4 carpet / floor patches survive background rejection, the hammer head is split from its
handle, small pliers merge, some thin tools (tweezers, hex keys, the blue wrench) are missed. The UI is expected to fix those with
click-to-add (`Segmenter.add_by_point` / `add_by_box`), merge and delete.

## Python API (what a service calls)

```python
from threed_vision import io
from threed_vision.segment import Segmenter

seg = Segmenter(device="auto")            # method="sam2", fill="owlv2", reject_bg=True; auto = cuda with >= 2000 MB free, else cpu
seg.load()                                # optional: weights stay resident until seg.unload()
rgb = io.load_rgb("tools.heic")           # uint8 HxWx3, EXIF-upright
res = seg.run(rgb, progress=lambda stage, frac: print(stage, frac))   # stages: load, amg, detect, fill, bg_reject
for inst in res["instances"]:             # id, source, bbox_xywh, area_px, bg_prob, stability, mask (np.bool_ HxW)
    ...
insts = seg.add_by_point(rgb, res["instances"], [[640, 410]])          # click-to-add (new list; new one is last, id = max+1)
insts = seg.add_by_box(rgb, insts, [x0, y0, x1, y1])
insts = Segmenter.merge(insts, [3, 7])    # numpy-only bookkeeping; also Segmenter.delete(insts, [5]) and Segmenter.pick(insts, x, y) -> id
seg.unload()                              # frees SAM2 (+ detector / CLIP it created); injected ones stay with their owner
```

Share one CLIP and one OWLv2 between stages (`Segmenter(clip=ClipEncoder("auto"), detector=OwlDetector("auto"))`); injected engines are not
unloaded by `Segmenter.unload()`. `segment_single_object(rgb, device="auto") -> bool mask` is the u2net fast path. Models come from
`3d vision-models pull`; a missing one raises `MissingDependency` (exit 127).
