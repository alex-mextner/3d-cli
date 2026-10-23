# `3d recognise` — what is each instance? (CLIP text classes + bank retrieval)

Proposes a class for every instance of a [`3d segment`](segment.md) run. Implemented by `threed_vision.recognise.Recogniser` in
`packages/threed-vision`; `3d recognise` is a thin launcher (`lib/cli/vision_env.py`).

**Text proposes, retrieval re-ranks, the user confirms.** Stage 1 scores each masked crop against text prompts for every category with CLIP
ViT-B/32 and **calibrates** the scores; stage 2 (optional, `--bank`) looks up the most similar *bank entries*, but only inside the classes
stage 1 proposed. Retrieval never overrides the text class: `text_topk` is identical with and without a bank, and a bank that holds
nothing of the proposed classes returns an empty `bank_topk` instead of a guess. The UI is expected to let the user confirm the class.

- **Calibration (`--calibrate`, default).** Raw CLIP text similarity is biased towards a few classes (on the carpet photo "battery" fires on tape
  rolls, bit sets and the caliper). Subtracting each class's mean similarity over the photo's instances removes most of that bias. The mean is
  taken over a **calibration pool**: the 60 largest instances covering at least 0.083 % of the image (2500 px of 2000x1500; `--pool-size`,
  `--pool-min-area-frac`), because small fragments distort it. It needs a population: with fewer than 8 pool instances every instance is
  used, and with fewer than 8 instances the raw scores are returned and `calibrated` is `false`
  (`Recogniser.classify(prior=...)` takes explicit per-class means, e.g. kept from an earlier photo, and works for a single mask).
- **Prompts.** Each category is asked through 3 templates (`a photo of a {}.`, `a top-down photo of a {} lying on a carpet.`, `a {} tool.`);
  the class embedding is the normalised mean over templates and over the category's phrases.
- **Bank retrieval.** Restricted to bank entries whose `category` is one of the best `--rerank-classes` (default 3) text classes;
  metrics and index format are in [bank.md](bank.md). The object is cut out and rotated upright like the bank thumbnails and matched both ways up.
  **Image retrieval needs a textured bank** (see the requirement in [bank.md](bank.md#textured-thumbnails-are-required-for-image-retrieval)); with
  a grey bank `--metric auto` falls back to the silhouette. hsw-editor's current bank holds *holders*, not tools, so recognition there is
  text-prompt based until a textured tool library exists.

## Usage

```
3d recognise <image> --instances DIR/instances.json [--categories FILE|a,b,c] [--bank INDEX_DIR] [--topk 5] [--bank-topk N]
             [--rerank-classes 3] [--metric auto|clip|dino|sil|clip+dino|all] [--calibrate|--no-calibrate]
             [--pool-size 60] [--pool-min-area-frac 0.000833] [--ids 3,7] [--device auto|cuda|cpu] [--json]
```

```bash
3d segment tools.heic -o seg/
3d recognise tools.heic --instances seg/instances.json --json
3d recognise tools.heic --instances seg/instances.json --categories "screwdriver,pliers,hammer" --topk 3
3d recognise tools.heic --instances seg/instances.json --categories tool-classes.json --bank bank/ --ids 70,87
3d recognise tools.heic --instances seg/instances.json --bank bank/ --metric sil --rerank-classes 5 --bank-topk 3   # shape retrieval, wider class set
3d recognise tools.heic --instances seg/instances.json --no-calibrate --device cpu                                    # raw CLIP scores, CPU
3d recognise tools.heic --instances seg/instances.json --pool-size 0                                                  # calibrate over every instance
3d recognise tools.heic --instances seg/instances.json --pool-size 40 --pool-min-area-frac 0.002                      # smaller, larger-object pool
```

Needs the `clip-b32` model (`3d vision-models pull clip-b32`; `--bank` also needs `dinov2-s`). Exit 0 ok, 2 bad input, 127 missing model.

### Category vocabulary (`--categories`)

Default: the spike's 22 workshop categories (screwdriver, pliers, hammer, tape measure, wrench, scissors, drill bit case, cordless drill,
clamp, roll of tape, glue, multimeter, caliper, saw, utility knife, box, battery, hex key set, bit set, tin snips, wire stripper, power supply).
The vocabulary is data: pass a comma list, or a file in any of these forms (`label` is what the result and the bank `category` carry):

```text
# categories.txt: one per line, optional synonyms after a colon
screwdriver
tape-roll: tape, duct tape, masking tape
```

```json
["screwdriver", {"id": "tape-roll", "prompts": ["tape", "duct tape"]}]
```

A JSON object `{"id", "name", "synonyms"}` without `prompts` takes its phrases from `name` + `synonyms`, each a string, a list or a per-language
dict of which `en` is used (CLIP is an English model); a JSON object wrapping the list under `categories` or `toolClasses` works too. That is
the shape of the hsw-editor bank tool classes (`packages/bank/src/toolClasses.ts`: `{id, name: {ru, en}, synonyms: {ru: [...], en: [...]}}`), so
`--categories` accepts its exported tool-class list as is and the result labels are the tool-class ids; nothing hsw-specific is built in.
Labels must be unique.

### Output

```json
{"instances":[{"id":20,"text_topk":[{"label":"cordless drill","score":3.112,"raw":29.077},{"label":"multimeter","score":3.087,"raw":27.552},
                                    {"label":"battery","score":2.366,"raw":30.201}],
               "bank_topk":[{"bank_id":"digital_multimeter","name":"digital_multimeter","category":"multimeter","score":0.9231,"metric":"sil",
                             "extents_mm":[146.0,88.0,45.0],"thumb":"bank/digital_multimeter.png"},
                            {"bank_id":"spare_drill_battery","name":"spare_drill_battery","category":"battery","score":0.6947,"metric":"sil",
                             "extents_mm":[85.0,70.0,46.5],"thumb":"bank/spare_drill_battery.png"}],
               "bank_restricted_to":["cordless drill","multimeter","battery"],"calibrated":true}],
 "timing_ms_per_crop":47.0,"device":"cuda","n_crops":98,"categories":22,"calibrated":true}
```

`score` is the (calibrated) similarity x 100 used for ranking, `raw` the uncalibrated one; `bank_restricted_to` / `bank_topk` are present with `--bank`.
The record above is real: instance 20 of the carpet photo (the multimeter), `--bank` = the 34 untextured STL tools, `--metric sil`. Note the honest
margin: the text stage prefers `cordless drill` by 0.03 over `multimeter`, retrieval restricted to the best 3 classes returns `digital_multimeter`
(IoU 0.92) first. The text ranking is not changed by retrieval; the UI shows both and the user confirms.

## Python API (what a service calls)

```python
from threed_vision import bank, io
from threed_vision.clip import ClipEncoder, DinoEncoder
from threed_vision.recognise import Recogniser, load_categories

clip, dino = ClipEncoder("auto"), DinoEncoder("auto")           # the service's resident encoders (shared with Segmenter / bank.query)
rec = Recogniser("auto", clip=clip, dino=dino)                  # without arguments it creates its own and `unload()` frees them
rgb = io.load_rgb("tools.heic")
res = seg.run(rgb)                                              # Segmenter.run -> {"instances": [{"id", "mask", ...}, ...]}
cats = load_categories("tool-classes.json")                     # or None (22 defaults), "a,b,c", or a list of str / {"id","prompts"} objects
out = rec.classify(rgb, res["instances"], cats, calibrate=True, topk=5)
# [{"id": 70, "text_topk": [{"label", "score", "raw"}], "bank_topk": [], "calibrated": True}, ...]  (one per instance, in input order)
index = bank.load_index("bank/")                                # optional: loaded once, numpy + json only
out = rec.classify(rgb, res["instances"], cats, bank=index, rerank_classes=3, bank_topk=3)
# one mask: out = rec.classify(rgb, [mask], cats, prior=class_means)   # calibration without a population
rec.unload()                                                    # frees only the encoders it created
```

`masks` is a list of instance dicts (`id` + `mask`; ids must be unique) or bare boolean HxW arrays (ids 1..N). `categories` entries may be strings,
`{"id", "prompts"}` objects or `Category` records. `Recogniser.similarity(rgb, masks, categories)` returns the raw (N, C) similarity matrix and
`recognise.calibrate_scores(sim)` the calibration alone. Errors are `threed_vision.errors.VisionError` subclasses (`BadInput` exit 2,
`MissingDependency` exit 127).

## Measured

Eval: `packages/threed-vision/eval/recog_eval.py` scores the 33 hand-labelled crops of the carpet photo (labels in
`eval/labels.json`, identified by bbox + area so they survive instance-id changes). Reproduce:

```bash
3d segment tools-on-carpet-2026-10-21.heic -o seg/                 # GPU; 98 instances
3d bank thumbs refit/tools -o bank/ && 3d bank index bank/         # the spike's 34 untextured STL tools
python packages/threed-vision/eval/recog_eval.py tools-on-carpet-2026-10-21.heic seg/instances.json --bank bank/
```

Carpet photo 2000x1500, `3d segment` default (GPU: 98 instances), 33 labelled crops, **all 33 labels matched** an instance (min bbox IoU 0.989,
max area deviation 0.9 %). Top-K = the true class is among the K best classes (text rows) or the K best bank entries (bank rows, entries mapped
to their class); `drill bit case` / `bit set` / `hex key set` count as equivalent, like in the spike. Spike columns are RESULT.md section 3.

| method | top-1 | top-3 | top-5 | spike top-1 / 3 / 5 |
|---|---|---|---|---|
| CLIP text, raw prompts | 0.45 | 0.79 | 0.91 | 0.42 / 0.79 / 0.88 |
| CLIP text, **class-mean calibrated** | **0.64** | 0.82 | 0.82 | 0.61 / 0.79 / 0.82 |
| DINOv2 -> untextured bank | 0.36 | 0.48 | 0.52 | 0.21 / 0.39 / 0.39 |
| CLIP image -> untextured bank | 0.18 | 0.30 | 0.39 | 0.21 / 0.30 / 0.48 |
| silhouette -> untextured bank | 0.18 | 0.42 | 0.61 | 0.24 / 0.45 / 0.58 |
| (spike protocol) DINOv2, raw crop vs raw thumbnail | 0.21 | 0.42 | 0.42 | 0.21 / 0.39 / 0.39 |
| (spike protocol) CLIP, raw crop vs raw thumbnail | 0.27 | 0.36 | 0.48 | 0.21 / 0.30 / 0.48 |

Deviations from the spike, and why:

- **Text rows differ from the spike by one crop of 33** in four cells (raw top-1 0.45 vs 0.42 and top-5 0.91 vs 0.88, calibrated top-1 0.64 vs 0.61 and top-3 0.82 vs 0.79), all in our favour. The calibration pool is the same
  protocol (60 largest instances above 2500 px) but not the same masks: of the spike's 60 pool masks `3d segment` reproduces 44 with identical
  bbox and area; the other 16 differ by under 1 % area (GPU non-determinism), which moves a few similarities.
- **DINOv2 retrieval is better than the spike (top-1 0.36 vs 0.21, top-3 0.48 vs 0.39)** because the library aligns the object before embedding: the masked
  crop is rotated so its longest axis is vertical like the lie-flat thumbnails, thumbnails are cropped to the object at the same scale as the query crop,
  and both 180-degree orientations are tried. The two `(spike protocol)` rows re-run the spike's way (unrotated crop against the raw 192 px thumbnail) in
  the same run and land within one crop of the spike's numbers.
- **CLIP image retrieval is not better: top-1 0.18 (spike 0.21), top-3 0.30 (0.30), top-5 0.39 (0.48).** Alignment and tight cropping did not help CLIP
  on grey renders (the spike-protocol rerun gives top-1 0.27, top-3 0.36); with 33 crops these differences are a few crops and I did not investigate them.
- **Silhouette: top-1 0.18 vs 0.24, top-3 0.42 vs 0.45, top-5 0.61 vs 0.58** (two, one and one crops of 33): the descriptor is now warped from the mask,
  centred on the middle of the object's extent, instead of scattering its pixels. Differences are not investigated further.
- The grey bank stays a coin flip whatever the metric: the spike counted only ~12 of its label classes in the 34-tool bank and the thumbnails carry
  no appearance. **None of these rows says anything about a textured bank**; no textured tool library exists yet ([#485](https://github.com/alex-mextner/3d-cli/issues/485)).

Other measurements: with `--bank` restricted to the 3 proposed classes and the silhouette metric, the true class is among the 5 returned entries for 58 %
of the labelled crops (`bank_topk_inside_text_top3_has_true_class`, the ceiling is the text top-3 = 0.82 and the bank lacking some classes).
**The calibration pool matters** (measured on this one photo; more labelled photos are tracked in [#486](https://github.com/alex-mextner/3d-cli/issues/486)): calibrating over all 98 instances instead of the 60 largest costs 9 points of top-1 (0.55 vs 0.64; top-3 0.79),
which is why `classify` pools only instances covering >= 0.083 % of the image (`--pool-size 0` restores "all"). Latency measured through `bin/3d`
on the RTX 4060 (shared, load average 4-8): 33-57 ms per crop averaged over all 98 instances, bank retrieval included (the spike measured 3.9 ms/crop for
the CLIP image encoder alone; this figure also covers cropping, calibration and, with `--bank`, rotating and silhouette matching).
