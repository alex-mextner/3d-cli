# `3d segment` — object instances from one photo

Splits a photo with many objects (tools on a carpet) into one mask per object. Implemented by
`threed_vision.segment.Segmenter` in `packages/threed-vision`; `3d segment` is a thin launcher
(`lib/cli/vision_env.py`) that runs `python -m threed_vision segment` in the vision venv.

**Why it exists.** Every photo->3D stage (recognise, scale, silhouette fit) starts from per-object masks. rembg / GrabCut
find one to four blobs on this kind of photo; SAM2 automatic masks alone miss thin or dark tools and keep carpet patches.

## Pipeline

1. **amg** — SAM2.1-tiny grid of point prompts (`points_per_side` per side, pred-IoU >= 0.8, stability >= 0.9, box NMS 0.6). Two implementations, `--quality`:
   * `accurate`: the stock `SAM2AutomaticMaskGenerator`: every candidate mask (3 per grid point) is upsampled to the photo and scored there.
   * `fast`: the same prompts, but the pred-IoU / stability / NMS filters run on the decoder's own 256x256 logits, the decoder runs in fp16 on CUDA, and only the
     surviving masks are upsampled to the photo (`threed_vision.sam_grid`). With `--grid-gate` (default 0.1) OWLv2 runs *first* and only the grid points next to
     a patch with OWLv2 objectness >= the gate are decoded: the bare carpet is never prompted (and so produces no carpet patches).
2. **detect** — OWLv2 open-vocabulary boxes for `--queries` (`--owl-tiles N` adds N x N overlapping tiles; measured below: it does not help).
3. **fill** — SAM2 *box prompts* for every OWLv2 box that is less than 60 % covered by existing masks and whose new mask overlaps existing ones by < 30 % (`fast`: one batched decoder pass).
4. **bg_reject** — CLIP ViT-B/32 zero-shot: masks whose crop is more like a background prompt (`--bg-prompts`, default "carpet / parquet / cabinet / shadow / foot / bin / cable") than like a foreground prompt (`--fg-prompts`, default "tool / box / tape ...") are dropped (`bg_prob >= 0.5`).
5. an area filter always applies: `--min-area` (150 px) .. `--max-area-frac` (0.03 of the image).

`--quality` defaults per device: **`accurate` on cuda** (the pre-existing default, unchanged) and **`fast` on cpu** (the stock generator needs 85-92 s there, and the
boxes-only legacy path reproduced 38 % of the GPU masks). `fast` also processes the full-resolution photo on CPU: SAM2 resizes whatever it gets to 1024x1024 anyway, so
the former 1024 px downscale saved nothing and cost mask detail.

`--method u2net` is the single-object fast path (u2net matte, largest component; 0.05 s GPU / 0.5 s CPU).

## Usage

```
3d segment <image> -o DIR [--method sam2|sam2-dense|u2net] [--quality fast|accurate] [--fill owlv2|none] [--queries "a,b,c"]
           [--reject-bg|--no-reject-bg] [--bg-prompts "a,b"|FILE] [--fg-prompts "a,b"|FILE] [--points-per-side N]
           [--grid-gate F] [--owl-tiles N] [--max-side PX] [--min-area PX] [--max-area-frac F]
           [--device auto|cuda|cpu] [--json]
```

```bash
3d segment tools.jpg -o seg/ --json                      # default pipeline (accurate on cuda, fast on cpu)
3d segment tools.jpg -o seg/ --quality fast              # ~1.9 s warm on the RTX 4060 instead of ~6 s, same photo
3d segment tools.jpg -o seg/ --quality fast --grid-gate 0   # decode every grid point (no OWLv2-gated prompts)
3d segment bench.jpg -o seg/ --bg-prompts "a photo of a steel workbench,a photo of a pegboard" --fg-prompts "a photo of a hand tool"
3d segment bench.jpg -o seg/ --bg-prompts bg.txt          # one prompt per line, # comments
3d segment tools.jpg -o seg/ --owl-tiles 2               # OWLv2 on 2x2 tiles too (measured: more fragments, no gain)
3d segment tools.jpg -o seg/ --fill none --no-reject-bg  # AMG only
3d segment one-tool.jpg -o seg/ --method u2net           # one tool on a plain background
3d segment tools.heic -o seg/ --device cpu --max-side 1024
3d segment tools.jpg -o seg/ --queries "a hand tool,a box" --points-per-side 24
3d segment tools.jpg -o seg/ --min-area 400 --max-area-frac 0.05    # drop specks, allow larger objects
3d segment tools.jpg -o seg/ --reject-bg --device cuda              # explicit GPU: errors instead of falling back
```

| flag | meaning |
|---|---|
| `-o, --out DIR` | output directory (required) |
| `--method` | `sam2` grid (default), `sam2-dense` 64x64 grid (runs its grid on CPU too), `u2net` one salient object |
| `--quality` | `fast` or `accurate`; default `accurate` on cuda, `fast` on cpu (see Pipeline). Ignored by `u2net` |
| `--grid-gate F` | `fast` only: decode only grid points near a patch whose OWLv2 objectness is >= F (0 = every point; default 0.1; needs `--fill owlv2`) |
| `--owl-tiles N` | also run OWLv2 on an N x N grid of 25 %-overlapping tiles (1..4, default 1); boxes cut by a tile edge are dropped |
| `--bg-prompts`, `--fg-prompts` | CLIP prompts, a comma list or a file (one per line, `#` comments); replace the defaults. Both lists must be non-empty while background rejection is on |
| `--fill` | `owlv2` (default) add SAM2 masks for OWLv2 boxes the AMG missed; `none` |
| `--queries` | OWLv2 text queries, comma separated (default: hand tool, power tool, box, tube of glue, tape roll, pliers, screwdriver) |
| `--reject-bg` / `--no-reject-bg` | CLIP background rejection (default on) |
| `--points-per-side N` | grid per side, `0` (OWLv2 boxes only, needs `--fill owlv2`) or 1..128. Default 32 (64 for `sam2-dense`); `--quality accurate` on CPU: `0`, or 16 with `--fill none` |
| `--max-side PX` | downscale the longest side before SAM2 (`0` = never, else 64..8192). Default: none, except 1024 for `--quality accurate` on CPU. Masks are always returned at the input resolution |
| `--min-area`, `--max-area-frac` | area filter (150 px, 0.03) |
| `--device` | `auto` picks cuda only when >= 2000 MB VRAM is free (the card is shared), else cpu; GPU out-of-memory falls back to CPU; explicit `cuda` never falls back |
| `--json` | print exactly one JSON object (the `instances.json` content) |

Option values are validated before any model loads: an out-of-range `--max-side` / `--points-per-side` / `--owl-tiles` / `--grid-gate`, empty prompt lists, `--points-per-side 0 --fill none` exit 2 with the value named.
A `--device cpu` run never initialises CUDA (no `torch.cuda` call reaches a driver; `tests/test_vision_segment.py::test_cpu_runs_never_touch_cuda`); `auto` still falls back to CPU when
the GPU runs out of memory, including when onnxruntime's CUDA provider cannot initialise for CLIP.

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

## Measured (carpet photo 2000x1500, RTX 4060 8 GB shared, load average 4-7)

Scored by `packages/threed-vision/eval/segment_recall.py` (committed; `warm` = median of 3 further `run()` calls, `cold` = first `run()` of a fresh `Segmenter`, weights in the page cache but the
models built in that call). **recall 33** = fraction of the 33 hand labels of `eval/labels.json` that some instance matches at bbox IoU >= 0.5; **parity** = fraction of the 98 masks of the
`accurate` GPU run reproduced at mask IoU >= 0.7 (the CPU-vs-GPU number of #478). The labels were made on the `accurate` GPU output, so that row is 100 % by construction and every other
row can only lose: read the recall column as "how close to the accurate result", not as ground truth (see the three "missed" labels below).

```
python packages/threed-vision/eval/segment_recall.py tools.heic --device cuda --warm 3 --masks-dir m/ --overlay-dir ov/ \
    --variant accurate='{"quality":"accurate"}' --variant fast='{"quality":"fast"}'
python packages/threed-vision/eval/segment_recall.py tools.heic --device cpu --reference m/accurate.npz --variant cpu='{}'
```

### Before / after (this PR)

| run | cold | warm | instances | recall 33 | wrench / hammer | parity vs accurate GPU | notes |
|---|---|---|---|---|---|---|---|
| GPU `--quality accurate` (the previous default, unchanged) | 11.8 s | 5.8 s | 98 | 100 % | no / no | 100 % | amg 4.2, detect 0.2, fill 0.8, bg_reject 1.1; torch peak 2245 MB |
| **GPU `--quality fast`** | 2.8 s | **1.7 s** | 91 | 91 % (30/33) | no / no | 84 % | amg ~0.6, detect 0.2, fill 0.3, bg_reject 0.8; torch peak ~1.7 GB |
| GPU accurate `--max-side 1024` | 5.6 s | 4.5 s | 91 | 82 % | - | 67 % | the stock generator is not resolution-bound: little gain, much loss |
| GPU fast `--method sam2-dense` (64x64 grid, gated) | 4.8 s | 3.8 s | 118 | 88 % | - | - | more parts, not better |
| CPU, previous default (boxes only on a 1024 px copy) | 11.3 s | 10.6 s | 65 | 39 % | no / yes | **38 %** | detect 4.9, fill 4.7, bg_reject 3.0 |
| **CPU default now = `--quality fast`, full resolution** | 46.3 s | **41.1 s** | 93 | 91 % | no / no | **84 %** | detect 4.2, amg 27 (grid prompts), fill 5.4, bg_reject 2.5; 4 OMP threads, load ~7 |

Cold = the first call of a fresh `Segmenter` in a warm process (weights in the page cache; building SAM2 / OWLv2 / CLIP is inside it). A cold *process* (`bin/3d segment`, imports + torch CUDA context) adds
~8 s on top of the earlier measurement below. `warm` varies by about +-0.3 s between runs with the machine's load (the same `fast` configuration measured 1.6-1.9 s in four runs).

The three labels `fast` misses (two `pliers`, one `roll of tape`) are not missing objects. The labels were taken from the accurate masks, where the pliers are split into handles and tips and a tape
roll into rings; `fast` returns the whole pliers / the whole roll as one mask (overlays of both runs viewed side by side), so the bbox IoU with the label part drops below 0.5. Of the 98 accurate masks,
10 have no counterpart at IoU 0.5 in `fast`: two are carpet patches (one is 51 000 px), four are pieces of pliers / a tape roll that `fast` returns whole, the rest were not inspected.

**wrench / hammer** (`labels.json` `extra_labels`, boxes drawn by eye, independent of any mask): the blue 12-inch adjustable wrench and the hammer with its head, each as ONE instance (bbox IoU >= 0.5).
Proof image `docs/img/segment-hammer-wrench.jpg` (photo / accurate overlay / fast overlay): **neither run segments the wrench at all and both return only the hammer's wooden handle** (the head is a
separate dark object nobody prompts). Variants that fix part of it are in the next table.

### Where the 7 s went, and what did not help

cProfile of a warm AMG-only run (`--max-side 1024 --fill none`, 85 masks): of 4.1 s, **2.6 s is `amg.filter`** (the stock generator upsamples every candidate mask - 3 per grid point - to the image and
scores it there), 0.5 s is mask bounding boxes (`np.nonzero` on photo-sized masks) and the SAM2 decoder itself is 0.14 s. Everything in `fast` follows from that: filter the candidates on the decoder's 256x256
logits (the logits the full-size masks are upsampled from), upsample only the survivors, bounding boxes from row / column projections (`np.nonzero` on 3 MP masks cost 0.65 s per photo in `crop_masked` alone), a
resident fp16 copy of the decoder (agreement with fp32: mask IoU 0.9988, predicted IoU differs by 5e-4; 1024 prompts in 1.6 s vs 2.8 s fp32 and 2.5 s with `torch.autocast`, which re-casts the weights per
call), and OWLv2 objectness gating. Measured on the same photo: stock generator 5.8-6.5 s warm; filters at 256x256 with the fp32 decoder: `amg` stage 3.1 s; with the fp16 decoder `amg` 1.7 s (2.6-3.5 s
warm total); gating at 0.1: **1.6-1.9 s warm total**.

Same run (load ~7), GPU, `--quality fast` unless noted:

| variant | warm | recall 33 | wrench / hammer | parity | verdict |
|---|---|---|---|---|---|
| gate 0.1, 32x32 (default) | 1.9 s | 91 % | no / no | 84 % | kept |
| gate 0.05 | 2.1 s | 91 % | - | 85 % | same masks, slower |
| no gate (`--grid-gate 0`) | 2.6 s | 91 % | - | 86 % | the gate costs nothing measurable and saves 0.7 s |
| `--owl-tiles 2` | 2.2-2.4 s | 88 % (29/33) | **yes** / no | 73 % | finds the wrench (one mask) and the two small spanners, fragments other objects: opt-in |
| `--owl-tiles 3` | 2.4-2.6 s | 88 % | **yes** / no | 76 % | same |
| accurate + `--owl-tiles 2` | 6.5 s | 94 % | **yes** / no | 91 % | same wrench gain on the stock generator |
| extra queries (adjustable wrench, hammer, spanner, tweezers, hex key) | 1.9 s | 91 % | no / no | 84 % | OWLv2 does not box them better than the default queries |

Other fast settings measured in a quieter run (load ~5): gate 0.15 1.65 s / 85 % / parity 80 %, gate 0.3 1.25 s / 52 % / 54 % (drops thin tools); grid 24x24 1.6 s / 79 % / 68 %, 16x16 1.35 s / 58 % / 58 %
(the thin tools need the 32 grid), 40x40 2.3 s / 76 % / 73 % (more fragments); `--max-side 1024` 1.2 s / 79 % / 57 % (never worth it).

**#479 result.** The recall gain the ticket asks for (>= 10 points over the baseline) cannot be measured here: the 33 labels come from the baseline's own masks, so the baseline scores 100 % and every variant
can only lose. Tiled OWLv2 does what it was meant for on the two by-eye labels (the wrench becomes one instance, +1 of 2), costs 3-12 points on the mask-derived labels because it fragments objects, and
leaves the hammer split (the dark head is never prompted; OWLv2's "a hammer" box is rejected by the fill stage because its mask overlaps the handle mask by more than 30 %). Tiling is therefore opt-in
(`--owl-tiles`), not a default; merging boxes into parent masks (the ticket's third idea) was not tried.

MobileSAM was not tried: it is not installed in the vision venv and nothing may be downloaded at request time (the spike measured it at 9-10 s for the 32x32 grid, slower than SAM2-tiny).
`torch.compile` / TensorRT for the decoder were not tried (the decoder is now ~25 % of the run).

### CPU parity (#478)

| CPU variant (4 OMP threads, load ~7) | warm | recall 33 | parity vs GPU accurate |
|---|---|---|---|
| boxes only on a 1024 px copy, the previous default | 10.6-12.9 s | 39 % | 38 % |
| fast, 16x16 gated, 1024 px copy | 20.2 s | 52 % | 41 % |
| fast, 24x24 gated, 1024 px copy | 27.2 s | 64 % | 48 % |
| fast 16x16 + `--owl-tiles 2`, 1024 px | 35.4 s | 58 % | 37 % |
| fast 24x24 gated, full resolution | 28.3 s | 79 % | 68 % |
| **fast 32x32 gated, full resolution (the CPU default)** | **39.3-41.1 s** | 91 % | **84 %** |
| fast 32x32 gate 0.05, full resolution | 57.3 s | 91 % | 85 % |

The target of #478 (>= 65 % at IoU 0.7 in <= 60 s warm at load < 8; the ticket text also says 70 %) is met: **84 % in 41 s**. It comes from dropping the 1024 px copy, not from tiling: SAM2 resizes any photo to
1024x1024 itself, so downscaling only costs mask detail (even the GPU loses 33 points of parity with it), and the 256x256 filtering makes the full-resolution grid affordable where the stock generator needed
85-92 s. Tiled OWLv2 was measured and rejected (rows above). The GPU default is unchanged (`accurate`: 100 % of its own masks). On a saturated machine (load 23) expect 2-3x the CPU times.

### Second background and background prompts (#480)

`3d segment --bg-prompts / --fg-prompts` replace the CLIP prompt lists (`Segmenter(bg_prompts=, fg_prompts=)`); `n_bg_rejected` in `instances.json` counts what CLIP dropped. Choosing prompts for another
surface: list what the *surface and its shadows* look like as bg prompts ("a photo of a steel workbench", "a photo of a pegboard"), keep fg prompts about tools, then run once with
`--no-reject-bg` and once with your prompts and compare `n_bg_rejected` and the overlays. CLIP only sees a crop of each mask, so prompts that describe the *texture* of the surface work, and
prompts for things that are part of the scene (a pegboard that is also a tool holder) reject tools.

Measured on the two other spike photos with `--quality fast` (default prompts vs `--bg-prompts "a photo of a wall,a photo of a honeycomb pegboard,a photo of a floor,a photo of a rug,a photo of a shadow"`):

| photo | prompts | candidates rejected by CLIP | instances kept |
|---|---|---|---|
| IMG_4934 (1536x1152; another **carpet** photo, not a wall: the ticket's "wall photo" is IMG_4937) | default (carpet / parquet / cabinet ...) | 18 | 85 |
| IMG_4934 | custom (wall / pegboard / floor / rug / shadow) | 9 | 95 |
| IMG_4934 | `--no-reject-bg` | 0 | 101 |
| IMG_4937 (1152x1536, the honeycomb wall, almost empty) | default | 14 | 33 |
| IMG_4937 | custom | 14 | 33 |
| IMG_4937 | `--no-reject-bg` | 0 | 46 |

The custom set rejects fewer crops on the carpet photo (9 vs 18: "carpet / parquet" prompts describe it better than "floor / rug") and the same 14 on the wall photo. Nothing else was tuned or investigated.

Why 3-4 background patches survive on the carpet photo with the default prompts (`accurate` run, `bg_prob` of the survivors that are not tools): the 51 000 px patch at the lower left has `bg_prob` 0.09, the grey
patch at the lower right 0.24, the brown shape at the bottom right 0.12; a mask is dropped only at >= 0.5. Large irregular patches score low because their crop (`keep_bg=True`, the whole bounding box) contains the
neighbouring tools, so CLIP sees a scene with tools rather than a carpet - that is the likely cause, not tested with a masked-out background. `--quality fast` shows fewer of them (the brown shape remains, the
other two do not appear) because OWLv2 objectness never prompts the bare carpet.

## Earlier measurements (2026-10-22, before `--quality`)

| run | time | instances | notes |
|---|---|---|---|
| GPU default, cold (loads SAM2, OWLv2, CLIP) | 13.8-15.4 s | 98 (65 amg + 33 owl) | torch peak 1935 MB |
| GPU default, warm (models resident) | 6.8-7.0 s | 98 | amg 4.07, detect 0.18, fill 0.77-0.80, bg_reject 1.07-1.19; torch peak 2255 MB |
| `bin/3d segment` end to end, GPU (cold process, load average ~8, 3.9 GB held by other processes) | 27.8 s wall (`timing_s.total` 19.4) | 98 | nvidia-smi peak +2.5 GB for the whole process (torch CUDA context + ONNX CLIP) |
| `bin/3d segment --device cpu` end to end, **load average 23** | 70 s wall (`timing_s.total` 64.7: detect 17.5, fill 34.2, bg_reject 7.3) | 65 | the same CPU run is 2-3x slower on a saturated machine |
| CPU `--fill none` (16x16 AMG on a 1024 px copy) | 36.1 s | 29 | AMG 30.1 s: not worth it |
| CPU 32x32 AMG at full resolution, stock generator (spike) | 85-92 s | 87 | unacceptable, never the default |
| `--method u2net` | 0.046 s GPU / 0.50 s CPU (warm) | 1 | one salient object only |

Known residual errors (GPU `accurate` overlay): 3-4 carpet / floor patches survive background rejection, the hammer head is split from its handle, small pliers merge, some thin tools (tweezers, hex keys) are missed.
The blue adjustable wrench is not segmented by either quality (`--owl-tiles 2` finds it). The UI is expected to fix the rest with click-to-add (`Segmenter.add_by_point` / `add_by_box`),
merge and delete.

## Python API (what a service calls)

```python
from threed_vision import io
from threed_vision.segment import Segmenter

seg = Segmenter(device="auto")            # method="sam2", fill="owlv2", reject_bg=True, quality=None (accurate on cuda, fast on cpu); auto = cuda with >= 2000 MB free, else cpu
# Segmenter(quality="fast", bg_prompts=[...], fg_prompts=[...], owl_tiles=2, grid_gate=0.1) - every keyword is validated in the constructor (BadInput, exit 2 on the CLI)
seg.load()                                # optional: weights stay resident until seg.unload()
rgb = io.load_rgb("tools.heic")           # uint8 HxWx3, EXIF-upright
res = seg.run(rgb, progress=lambda stage, frac: print(stage, frac))   # stages: load, amg, detect, fill, bg_reject
# res also carries "quality", "n_amg", "n_owl_added", "n_bg_rejected", "points_per_side", "process_scale", "timing_s", "vram_mb"
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
