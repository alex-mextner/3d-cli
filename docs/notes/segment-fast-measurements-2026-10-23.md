# `3d segment`: measurements of `--quality fast`, 2026-10-23

Experiment log behind `docs/commands/segment.md` (hsw-editor #10, 3d-cli #478 #479 #480 #508, PR #543). Everything below was measured on the tools-on-carpet photo (2000x1500),
RTX 4060 8 GB shared with other processes, one run at a time under `flock` + `systemd-run MemoryMax=6G`. It documents what was tried, including what did not help; it is a dated record and
may go stale without the command doc being wrong.

## Method

Scored by `packages/threed-vision/eval/segment_recall.py` (committed; `warm` = median of 3 further `run()` calls, `cold` = first `run()` of a fresh `Segmenter`, weights in the page cache but the
models built in that call). **recall 33** = fraction of the 33 hand labels of `eval/labels.json` that some instance matches at bbox IoU >= 0.5; **parity** = fraction of the 98 masks of the
`accurate` GPU run reproduced at mask IoU >= 0.7 (the CPU-vs-GPU number of #478). The labels were made on the `accurate` GPU output, so that row is 100 % by construction and every other
row can only lose: read the recall column as "how close to the accurate result", not as ground truth (see the three "missed" labels below).

```
python packages/threed-vision/eval/segment_recall.py tools.heic --device cuda --warm 3 --masks-dir m/ --overlay-dir ov/ \
    --variant accurate='{"quality":"accurate"}' --variant fast='{"quality":"fast"}'
python packages/threed-vision/eval/segment_recall.py tools.heic --device cpu --reference m/accurate.npz --variant cpu='{}'
```

### Before / after

| run | cold | warm | instances | recall 33 | wrench / hammer | parity vs accurate GPU | notes |
|---|---|---|---|---|---|---|---|
| GPU `--quality accurate` (the previous default, unchanged) | 11.5 s | 5.7 s | 98 | 100 % | no / no | 100 % | amg 4.2, detect 0.2, fill 0.8, bg_reject 1.1; torch peak 2245 MB |
| **GPU `--quality fast`** | 2.2-7.6 s | **1.8 s** | 91 | 91 % (30/33) | no / no | 89 % | amg ~0.6, detect 0.2, fill 0.3, bg_reject 0.8; torch peak ~1.7 GB |
| GPU accurate `--max-side 1024` | 5.7 s | 4.4 s | 91 | 82 % | no / no | 71 % | the stock generator is not resolution-bound: little gain, much loss |
| GPU fast `--method sam2-dense` (64x64 grid, gated) | 4.6 s | 3.6 s | 118 | 88 % | no / no | 87 % | more parts, not better |
| CPU, previous default (boxes only on a 1024 px copy) | 11.5 s | 9.0 s | 65 | 39 % | no / yes | **41 %** | detect 4.9, fill 4.7, bg_reject 3.0 |
| **CPU default now = `--quality fast`, full resolution** | 49.9 s | **38.7 s** | 93 | 91 % | no / no | **89 %** | detect 4.2, amg 27, fill 5.4, bg_reject 2.5; 4 OMP threads, load ~6 |

Cold = the first call of a fresh `Segmenter` in a process whose weights are in the page cache (building SAM2 / OWLv2 / CLIP is inside it; the GPU `fast` cold time was 2.2, 2.8, 3.0 and 7.6 s in
four runs, the `accurate` one 11.5-11.8 s). A cold *process* (`bin/3d segment`: imports + torch CUDA context) adds ~8 s on top (earlier measurement below). `warm` varies by about +-0.3 s with the
machine's load (the same `fast` configuration measured 1.6-1.9 s in five runs).

The three labels `fast` misses (two `pliers`, one `roll of tape`) are not missing objects. The labels were taken from the accurate masks, where the pliers are split into handles and tips and a tape
roll into rings; `fast` returns the whole pliers / the whole roll as one mask (overlays of both runs viewed side by side), so the bbox IoU with the label part drops below 0.5. Of the 98 accurate masks,
10 have no counterpart at IoU 0.5 in `fast`: two are carpet patches (one is 51 000 px), four are pieces of pliers / a tape roll that `fast` returns whole, the rest were not inspected.

**wrench / hammer** (`labels.json` `extra_labels`, boxes drawn by eye, independent of any mask): the blue 12-inch adjustable wrench and the hammer with its head, each as ONE instance (bbox IoU >= 0.5).
Proof image `docs/img/segment-hammer-wrench.jpg` (photo / accurate overlay / fast overlay): **neither run segments the wrench at all and both return only the hammer's wooden handle** (the head is a
separate dark object nobody prompts). Variants that fix part of it are in the next table. The CPU boxes-only legacy path happens to return the hammer whole.

### Where the 7 s went, and what did not help

cProfile of a warm AMG-only run (`--max-side 1024 --fill none`, 85 masks): of 4.1 s, **2.6 s is `amg.filter`** (the stock generator upsamples every candidate mask - 3 per grid point - to the image and
scores it there), 0.5 s is mask bounding boxes (`np.nonzero` on photo-sized masks) and the SAM2 decoder itself is 0.14 s. Everything in `fast` follows from that: filter the candidates on the decoder's 256x256
logits (the logits the full-size masks are upsampled from), upsample only the survivors, bounding boxes from row / column projections (`np.nonzero` on 3 MP masks cost 0.65 s per photo in `crop_masked` alone), a
resident fp16 copy of the decoder (agreement with fp32: mask IoU 0.9988, predicted IoU differs by 5e-4; 1024 prompts in 1.6 s vs 2.8 s fp32 and 2.5 s with `torch.autocast`, which re-casts the weights per
call), and OWLv2 objectness gating. Measured on the same photo: stock generator 5.7-6.5 s warm; filters at 256x256 with the fp32 decoder: `amg` stage 3.1 s; with the fp16 decoder `amg` 1.7 s (2.6-3.5 s
warm total); gating at 0.1: **1.6-1.9 s warm total**.

GPU, `--quality fast` unless noted (one session, load ~4-6; parity = share of the 98 accurate masks reproduced at mask IoU 0.7):

| variant | warm | recall 33 | wrench / hammer | parity | verdict |
|---|---|---|---|---|---|
| gate 0.1, 32x32 (default) | 1.8 s | 91 % | no / no | 89 % | kept |
| gate 0.05 | 2.1 s | 91 % | no / no | 91 % | same masks, slower |
| no gate (`--grid-gate 0`) | 2.7 s | 91 % | no / no | 91 % | the gate costs 2 points of parity and saves 1 s |
| gate 0.15 | 1.55 s | 85 % | no / no | 83 % | drops thin tools |
| gate 0.3 | 1.1 s | 52 % | no / **yes** | 61 % | the sparse grid leaves the hammer to OWLv2's box, which returns it whole; 40 % of the other labels are lost |
| grid 24x24 / 16x16 / 40x40 | 1.35 / 1.3 / 2.2 s | 79 / 58 / 76 % | no / no, no / **yes**, no / no | 73 / 65 / 80 % | the thin tools need the 32 grid; 40 fragments |
| `--max-side 1024` | 1.15 s | 79 % | no / no | 62 % | never worth it |
| `--owl-tiles 2` | 2.3 s | 88 % (29/33) | **yes** / no | 80 % | finds the wrench (one mask) and the two small spanners, fragments other objects: opt-in |
| `--owl-tiles 3` | 2.5 s | 88 % | **yes** / no | 83 % | same |
| accurate + `--owl-tiles 2` | 6.4 s | 94 % | **yes** / no | 91 % | same wrench gain on the stock generator |
| extra queries (adjustable wrench, hammer, spanner, tweezers, hex key) | 1.8 s | 91 % | no / no | 89 % | OWLv2 does not box them better than the default queries |

**#479 result.** The recall gain the ticket asks for (>= 10 points over the baseline) cannot be measured here: the 33 labels come from the baseline's own masks, so the baseline scores 100 % and every variant
can only lose (follow-up: #544). Tiled OWLv2 does what it was meant for on the by-eye labels (the wrench becomes one instance, +1 of 2), costs 3-12 points on the mask-derived labels because it fragments
objects, and leaves the hammer split (the dark head is never prompted; OWLv2's "a hammer" box is rejected by the fill stage because its mask overlaps the handle mask by more than 30 %). Tiling is
therefore opt-in (`--owl-tiles`), not a default; merging boxes into parent masks (the ticket's third idea) was not tried.

MobileSAM was not tried: it is not installed in the vision venv and nothing may be downloaded at request time (the spike measured it at 9-10 s for the 32x32 grid, slower than SAM2-tiny).
`torch.compile` / TensorRT for the decoder were not tried (the decoder is now ~25 % of the run).

### CPU parity (#478)

| CPU variant (4 OMP threads, load ~6) | warm | recall 33 | parity vs GPU accurate (IoU 0.7) |
|---|---|---|---|
| boxes only on a 1024 px copy, the previous default | 9.0 s | 39 % | 41 % |
| fast, 16x16 gated, 1024 px copy | 18.2 s | 52 % | 47 % |
| fast, 24x24 gated, 1024 px copy | 24.2 s | 64 % | 51 % |
| fast 16x16 + `--owl-tiles 2`, 1024 px copy | 34.7 s | 58 % | 44 % |
| fast 24x24 gated, full resolution | 27.2 s | 79 % | 73 % |
| **fast 32x32 gated, full resolution (the CPU default)** | **38.7 s** | 91 % | **89 %** |

The target of #478 (>= 65 % at IoU 0.7 in <= 60 s warm at load < 8; the ticket text also says 70 %) is met: **89 % in 39 s**. It comes from dropping the 1024 px copy, not from tiling: SAM2 resizes any photo to
1024x1024 itself, so downscaling only costs mask detail (even the GPU loses 18-27 points of parity with it), and the 256x256 filtering makes the full-resolution grid affordable where the stock generator needed
85-92 s. Tiled OWLv2 was measured and rejected (rows above). The GPU default is unchanged (`accurate`: 100 % of its own masks). Gate 0.05 at full resolution took 57 s in an earlier run. On a saturated machine
(load 23) expect 2-3x the CPU times. Further CPU speed-ups are #545.

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

