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
boxes-only legacy path reproduced 41 % of the GPU masks). `fast` also processes the full-resolution photo on CPU: SAM2 resizes whatever it gets to 1024x1024 anyway, so
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

Scored by `packages/threed-vision/eval/segment_recall.py` (committed): `warm` = median of 3 further `run()` calls, `cold` = the first `run()` of a fresh `Segmenter` (models built inside it),
**recall 33** = share of the 33 hand labels of `eval/labels.json` matched at bbox IoU >= 0.5, **parity** = share of the 98 masks of the `accurate` GPU run reproduced at mask IoU >= 0.7.
The labels were made on the `accurate` GPU output, so that row is 100 % by construction and every other row can only lose: read recall as "how close to the accurate result", not as ground truth.

```
python packages/threed-vision/eval/segment_recall.py tools.heic --device cuda --warm 3 --masks-dir m/ --overlay-dir ov/ \
    --variant accurate='{"quality":"accurate"}' --variant fast='{"quality":"fast"}'
python packages/threed-vision/eval/segment_recall.py tools.heic --device cpu --reference m/accurate.npz --variant cpu='{}'
```

| run | cold | warm | instances | recall 33 | parity | notes |
|---|---|---|---|---|---|---|
| GPU `--quality accurate` (the GPU default) | 11.5 s | 5.7 s | 98 | 100 % | 100 % | amg 4.2, detect 0.2, fill 0.8, bg_reject 1.1 |
| **GPU `--quality fast`** | 2.2-7.6 s | **1.8 s** | 91 | 91 % (30/33) | 89 % | amg ~0.6, detect 0.2, fill 0.3, bg_reject 0.8; torch peak ~1.7 GB |
| CPU, previous default (boxes only on a 1024 px copy; now `--quality accurate` on CPU) | 11.5 s | 9.0 s | 65 | 39 % | 41 % | |
| **CPU default (`fast`, full resolution)** | 49.9 s | **38.7 s** | 93 | 91 % | **89 %** | 4 OMP threads, load ~6 |

The three labels `fast` misses (two `pliers`, one `roll of tape`) are objects it returns whole where the label marks a part. `fast` finds neither the blue adjustable wrench nor the
hammer head (proof image `docs/img/segment-hammer-wrench.jpg`: photo / accurate overlay / fast overlay; `--owl-tiles 2` finds the wrench but fragments other objects). A `--device cpu` run
needs ~8 s more on a cold process (imports). On a saturated machine (load 23) expect 2-3x the CPU times.

What was tried, the profile, the variant tables (grid size, gate, tiles, queries, `--max-side`), the CPU parity table, the second-background prompt runs and the 2026-10-22 measurements are in
[docs/notes/segment-fast-measurements-2026-10-23.md](../notes/segment-fast-measurements-2026-10-23.md).

**Choosing background prompts for another surface:** list what the surface and its shadows look like as `--bg-prompts` ("a photo of a steel workbench", "a photo of a pegboard"), keep `--fg-prompts`
about tools, run once with `--no-reject-bg` and once with your prompts and compare `n_bg_rejected` in `instances.json` and the overlays. CLIP scores a *crop* of each mask (`keep_bg=True`: the whole bounding box),
so a large irregular patch whose box contains neighbouring tools scores low (the 51 000 px carpet patch has `bg_prob` 0.09) and survives the 0.5 threshold.

Known residual errors (GPU `accurate` overlay): 3-4 carpet / floor patches survive background rejection (fewer with `fast`), the hammer head is split from its handle, small pliers merge, the adjustable
wrench and some thin tools (tweezers, hex keys) are missed. The UI is expected to fix those with click-to-add (`Segmenter.add_by_point` / `add_by_box`), merge and delete.

## Python API (what a service calls)

```python
from threed_vision import io
from threed_vision.segment import Segmenter

seg = Segmenter(device="auto")            # method="sam2", fill="owlv2", reject_bg=True, quality=None (accurate on cuda, fast on cpu); auto = cuda with >= 2000 MB free, else cpu
# Segmenter(quality="fast", bg_prompts=[...], fg_prompts=[...], owl_tiles=2, grid_gate=0.1) - every keyword is validated in the constructor (BadInput, exit 2 on the CLI)
seg.load()                                # optional: weights stay resident until seg.unload()
rgb = io.load_rgb("tools.heic")           # uint8 HxWx3, EXIF-upright
res = seg.run(rgb, progress=lambda stage, frac: print(stage, frac))   # stages: load, amg, detect, fill, bg_reject (detect runs before amg when the fast gate is on)
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
