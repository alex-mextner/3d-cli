# Print-quality monitor: why stringing went unnoticed, and the tier that sees it

Issue [#733](https://github.com/alex-mextner/3d-cli/issues/733). Code: `lib/print_quality.py` (pixels),
`lib/printer_ops/quality.py` (decisions), `quality_report.py`, `jobwatch_quality.py` (job watch),
`lib/vision_quality.py` (printer vision). User docs: [printer.md](../commands/printer.md#print-quality-monitor),
[printer-vision.md](../commands/printer-vision.md#print-quality-stringing-pattern-loss-first-layer).

## What happened (2026-11-05)

A 10 h sheet of small tags (Snapmaker U1, 270 x 270 mm, beige PETG "Wooden", 232 °C, bed 80 °C, many islands)
stringed from layer 2 on: white thread debris over the layers, "everything in stripes" on the real part. The part
stayed attached, so nothing was lost — and nothing flagged it. `3d printer vision u1 --once` returned
`findings: []`, `p 0.0`, `q 0.0`, `boxes: []`, `part_detached: false`, `trend 0.0` on every one of 12 frames, and the
job watch (`printer watch u1 --job`, PR #727) stayed silent. The orchestrator noticed the stringing in the chat
only and did not send a `tg --photo` (process gap: a visible defect on the camera is an owner alert, not a chat
remark — the monitor now does exactly that).

## Why nothing fired (measured on the 12 frames)

| component | what it asks | on the 12 frames |
|---|---|---|
| Obico spaghetti model (stage 1), `vision_detect.py` | loose filament piles (class "failure") | **0 boxes above CONF_MIN 0.08** on every frame; `p 0.0`. `masked 0`: nothing was dropped by a mask — the model never fired. |
| CLIP verifier (stage 2) | confirms the boxes stage 1 kept | never ran: `q = 0.0` is the **empty-sum** of zero boxes (`verified_score([])`), not a measurement |
| trend tracker | EWM of `q` minus the lifetime mean | 0.0 by construction |
| tier (b), whole-frame CLIP | zero-shot prompts: spaghetti / part detached / empty plate | off by default unless the weights are cached; its prompts have no stringing class; "stringing" is documented as *not an alert* in [printer-vision-tuning.md](printer-vision-tuning.md) |
| part-detached / scene detectors | geometry of the part's silhouette | stringing does not change the silhouette |
| job watch camera (`bed_part.py`) | is the changed footprint still there? | `changed` 0.23-0.52, a part that stands: debris on top of a part is not a missing part. (On the real frames its footprint compare also reads the toolhead/light changes as `partial` -> a `part_moved` WARNING; it is not about stringing.) |
| `z` from Moonraker | used by the camera masks (bed volume at Z) | yes, `ContextTap` supplies it; it only feeds masks, so it could not help |

Root cause: **no component has a defect class for print *quality*.** The stack detects failures (spaghetti, a part
off the plate, a foreign object). Stringing, a lost bead pattern and an uneven first layer are not failures of
the job, so every detector is correct to stay quiet — and the owner learns of them from the finished part.

## The quality tier

Per frame, inside the plate region at the print's Z (camera calibration or `watch.bed.roi`), at 640 px width:

* `fleck_frac` — share of the region covered by small (5-130 px) bright **neutral** specks and strands:
  white top-hat (17 px ellipse) of the **minimum colour channel**, divided by the local mean brightness, over 0.18.
  The minimum channel is high only where R, G and B are all high (white debris); beige part, golden specular grain and the
  blue-lit toolhead shadow stay low. Chosen by a grid search over channel / kernel / threshold / area against the
  12 frames; plain edge density, gray top-hat and colour (R/B) statistics were tried first and do **not** separate
  (auto-white-balance swings R/B between 0.6 and 1.7 from frame to frame; clean first-layer bars are as edgy as debris).
* `order` — energy-weighted structure-tensor coherence (window sigma 5.5 px): how regular the bead pattern is.
  Clean parallel beads 0.83-0.85; debris, webs and broken lines lower it; the empty plate and the stringy phone
  photos read 0.3-0.7.

The judge (`quality.py`) compares them with the **job's own first layers** (the first 4 readings with the part on the
plate), with persistence (2 of the last 4 readings, 3 of 4 for severe), one alert per issue and level, a bounded
baseline drift, a debris trend, and an absolute floor on `order` for first layers. Never CRITICAL: a quality finding
is a `tg --photo` WARNING with the frame, never a pause or a cancel.

### Calibration table (full-size frames of the job, chronological; presence from `bed_part` against f1; the layer column is the replay's assumption: f1-f5 first layer, f6 layer 2, f7-f9 layer 3, f10-f12 layer 4, readings 15 s apart on layer 1 and 60 s after)

| frame | layer | before: vision `--once` (p / q / findings) | before: job watch | `fleck_frac` | `order` | after: this reading | after: monitor verdict |
|---|---|---|---|---|---|---|---|
| f1 | empty plate | 0.0 / 0.0 / [] | empty (changed 0.00) | 0.089 | 0.68 | not judged (no part) | nothing |
| f2 | 1 | 0.0 / 0.0 / [] | seen | 0.026 | 0.83 | baseline | nothing |
| f3 | 1 | 0.0 / 0.0 / [] | seen | 0.011 | 0.84 | baseline | nothing |
| f4 | 1 | 0.0 / 0.0 / [] | seen | 0.022 | 0.83 | baseline | nothing |
| f5 | 1 | 0.0 / 0.0 / [] | seen | 0.015 | 0.85 | baseline (median 0.018 / 0.84) | nothing |
| f6 | 2 | 0.0 / 0.0 / [] | seen | 0.080 | 0.80 | severe (single reading) | nothing yet (1 of 2 needed) |
| f7 | 3 | 0.0 / 0.0 / [] | armed | 0.085 | 0.74 | severe | **WARNING `quality_stringing_warning`** |
| f8 | 3 | 0.0 / 0.0 / [] | present | 0.073 | 0.73 | severe | **SEVERE `quality_stringing_severe`** (second message) |
| f9 | 3 | 0.0 / 0.0 / [] | present | 0.057 | 0.68 | warning | severe (latched) |
| f10 | 4 | 0.0 / 0.0 / [] | present | 0.073 | 0.68 | severe | severe (latched) |
| f11 | 4 | 0.0 / 0.0 / [] | present | 0.039 | 0.66 | warning | severe (latched) |
| f12 | 4 | 0.0 / 0.0 / [] | present | 0.041 | 0.70 | warning | severe (latched) |

f1-f5 raise nothing; every frame from f7 on is at least WARNING (f6, the first strings, is one reading too few to
alert: a single frame is never a finding). `vision --once` on one frame has no job history and reports the
**absolute** verdict: f5 none, f7-f10 `quality_stringing_severe`/`warning`, f11-f12 `warning`; above the first layers
only, because the bare plate sparkles (f1 reads 0.089).

Phone photos of the failed first attempt (whole-frame region, no camera calibration): `599472757` order 0.33,
`599472758` 0.42, the coupon `599472764` 0.47 — all under the first-layer floor 0.6 against 0.83+ for a clean first
layer, so they would read `first_layer` warning/severe. The coupon photos' blob at the end and the bowed frame are
not separate measurements.

### Limits (read before trusting it)

* **One camera.** The constants are calibrated on 12 frames of one U1 view and 4 phone photos. Margins on the
  weakest frames are real but small (f11/f12 are 2.1-2.2x the first-layer baseline against a warning ratio of 2.0).
  JPEG recompression at 640 px moves `fleck_frac` by up to 30 % (the test fixtures use the 640 px copies). Another
  camera/lens/light needs its own look; every threshold is a `watch.quality` key.
* **Texture, under-/over-extrusion, first-layer gaps, non-squished beads, bowed corners** are measured only through
  `order` (pattern regularity) and, for a lifted part, the existing `part_moved` (footprint). No frame of a
  *known* under-extruded layer exists, so `texture` is calibrated for "no false alarm on clean frames", not for recall.
  A bowed corner is not told apart from other disorder on one frame.
* **Baseline = the job's first layers.** A job whose first layers are already stringy teaches the monitor that this
  is normal; the absolute levels and the first-layer floor still apply. A supervisor started mid-print learns from
  what it sees then.
* **Toolhead over the plate** removes part of the region and moves both numbers; persistence (2 of 4) absorbs most
  of it.
* The job watch's part check (PR #727) produced a `part_moved` WARNING of its own on these frames because the
  toolhead and the light differ from frame to frame; that is not part of this change.

## Recommendations to prevent the stringing itself (hypotheses, NOT measured on the printer)

None of this was tested on the U1; each is a first thing to try for beige PETG at 232 °C with many islands.

1. **Temperature.** Lower the nozzle 5-10 °C (to 222-227 °C) or run a temperature tower at 215-235 °C; keep the
   bed at 80 °C. Stringing grows steeply with temperature; 232 °C is at the hot end for PETG.
2. **Retraction and wipe.** Raise retraction distance/speed in small steps (direct drive: ~0.8-1.5 mm at 30-40 mm/s;
   add 0.2 mm at a time), enable wipe on retract and "wipe before retract"-style small coasting, and avoid zero-length
   retractions on short hops; a retract test (one tower of two thin pillars) shows the effect in minutes.
3. **Travel strategy.** Enable *avoid crossing perimeters/walls* and travel inside already printed areas, raise the
   minimum travel distance that triggers a retraction less than the island gaps, and use *z-hop only where needed*
   (a hop with a hot, oozing nozzle drags a strand across the part).
4. **Fewer islands, fewer travels.** Order the objects/tags so the nozzle finishes a region before leaving it, group
   them into fewer, bigger connected islands (join tags with a thin bridge or a shared frame so one wall run covers many),
   and print in one pass per layer area instead of hopping between 100+ separate islands each layer.
5. **Wet or hot? Diagnose in this order.** Wet PETG pops, hisses and strings with fine bubbly "hair": dry it
   (65 °C for 4-6 h) and repeat the same file — if the debris is gone it was moisture, not settings. If dry filament
   still strings on travel moves, it is retraction/temperature (1-4). Change one variable per test and look at the
   same camera view; the monitor's `fleck_frac` is a cheap number to compare between runs.
