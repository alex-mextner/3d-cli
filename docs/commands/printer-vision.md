# `3d printer vision` — watch the printer camera for failed prints

Issue: [#150](https://github.com/alex-mextner/3d-cli/issues/150). Events go through the same
dispatcher and sinks as [`3d printer watch`](printer.md) (stdout JSONL, `tg`, `steer`, `exec`
from the device's `watch:` block in `printer-devices.yaml`).

## Usage

```text
3d printer vision NAME [--interval S] [--once] [--save OUT.jpg] [--dry-run] [--min-severity LEVEL]
                       [--snapshot URL|PATH] [--no-keepalive] [--no-npu] [--evidence DIR] [--verbose]
                       [--no-verify] [--no-geometry] [--scene] [--model | --no-model]
3d printer vision eval --pos PATH... --neg PATH... [--every N] [--warmup N] [--interval S]
                       [--lead-in N] [--printer NAME] [--no-verify] [--scene] [--json]
                       [--model [--model-class CLASS]]
3d printer vision record NAME DIR [--interval S] [--max N] [--snapshot URL|PATH] [--no-keepalive]
3d printer vision fetch-model [--tier a|b|all]
3d printer vision fit-head --class CLASS --pos PATH... --neg PATH... [--every N] [--l2 X]
                       [--fpr R] [--folds N] [--out FILE] [--json]
```

- `NAME` — a Moonraker printer from `3d printer list` (the rooted K1 SE runs Moonraker
  and its USB webcam is served by mjpg-streamer; `fleet/printers.yaml` sets its `watch.vision.snapshot`
  to `http://192.168.0.27:8080/?action=snapshot`, a full URL fetched directly).
- `--interval` — seconds between frames (default 10; the thresholds were tuned for 10 s).
- `--once` — score the current frame, print one JSON verdict and exit; `--save` also writes the
  frame (with detection boxes when there are any).
- `--snapshot` — camera URL or Moonraker path; default `/server/files/camera/monitor.jpg` (U1).
- `--no-keepalive` — do not send `camera.start_monitor` (see U1 camera below).
- `--no-npu` — do not read the printer's own detector verdicts from klippy.log.
- `--evidence` — where event frames are saved (default `~/.cache/3d-cli/vision/evidence/`).
- `--no-verify` — skip stage 2 (the CLIP verifier); the trend then runs on the raw Obico score,
  which on the U1 fires on speckled walls. For debugging only.
- `--no-geometry` — ignore the camera calibration (no bed-volume mask, no bed-change detectors).
- `--scene` — also run the bed-change detectors (`vision_foreign_object`, `vision_part_moved`,
  `vision_no_growth`). Off by default: they are still noisy (see
  [printer-vision-tuning.md](../research/printer-vision-tuning.md)). Same as `scene: true` in the
  device's `watch: vision:` block. Needs the camera calibration (a usage error without it).
- `--model` / `--no-model` — tier (b), the whole-frame vision model (see "Three tiers" below):
  `--model` forces it on (downloads the 173 MB weights on first use, structured error when it
  cannot), `--no-model` turns it off; without either the device's `watch.vision.model.enabled`
  decides, and when that is unset it is on exactly when the weights are already cached
  (`fetch-model` caches them; a live loop never starts a download by itself). `guard` never uses it.
- `eval` — replay footage: each `--pos`/`--neg` path is a directory of frames, one image, or a
  video (a U1 timelapse `.mp4`); each path counts as one print. Prints per-frame
  precision/recall/false-positive rate at several score thresholds, and whether the live
  tracker would have raised an alert on each sequence.
- `record` — while the printer prints, save every frame with the print context (Z, objects)
  into `DIR` (`meta.jsonl`), for `eval --printer NAME` later.
  `eval --model` also scores every frame with tier (b) (no cadence, no debounce) and prints, for
  `--model-class` (default `spaghetti`; `part_detached`, `empty_bed`) taking the `--pos` frames as
  that class, precision/recall/FPR at thresholds 0.1..0.9, the mean score of every class per
  side, the worst negatives and the seconds per frame, for the device it ran on.
- `fetch-model` — download the weights now (otherwise the first run does it): `--tier a` the
  Obico detector and the CLIP verifier, `--tier b` the tier (b) model, `all` (default) both.
  Idempotent: a cached file that passes its sha256 is not fetched again.
- `fit-head` — fit tier (b)'s logistic head for one class on labelled frames you provide
  (`--pos` shows the condition, `--neg` must not raise it; directories, images or videos),
  report block cross-validation and write `~/.cache/3d-cli/vision/models/tier-b-head.json`
  (other classes already in the file are kept). `--fpr` (default 0.005) sets the threshold at
  that quantile of the out-of-fold negatives; `--l2` the ridge strength; `--every N` keeps one
  frame in N. Frames come from `vision record`, sorted into folders by you.

## What it detects

| kind | severity | when |
|---|---|---|
| `vision_spaghetti` | warning, then critical | the trend of the verified score q crosses the alert / the escalated threshold while printing |
| `vision_spaghetti_vetoed` | info | the raw detector's trend would warn, but the verifier confirms less than 0.3 of it (texture, not filament) |
| `vision_model_spaghetti` | warning | tier (b): the whole-frame model scores loose filament >= 0.5 on 3 model runs (30 s apart) in a row; a second opinion next to `vision_spaghetti`, never a pause |
| `vision_model_part_detached`, `vision_model_empty_bed` | warning | tier (b), **off by default** (no skill zero-shot): only with a fitted head (`fit-head`) or a `threshold` in `watch.vision.model`; `vision_model_empty_bed` is only judged once the print is at least `min_z` (10 mm) high |
| `vision_foreign_object`, `vision_part_moved` | warning | only with `--scene`: something new on the bed outside the parts / a part's silhouette changed |
| `vision_no_growth` | info | only with `--scene`: the newest layers of a part look empty (under- or no extrusion) |
| `printer_noodle_verdict` | warning | the U1's own NPU detector logs a defect label (table below) at or above 0.83 (once per episode, 5 min quiet ends it) |
| `vision_camera_stale` | warning | printing, but the picture has not changed (or could not be fetched) for 120 s |
| `vision_camera_dark` | warning | printing with a mean brightness below 20/255 (light off, lens covered) |

Each event fires once per episode. Camera events (`vision_*`) carry the saved frame path in
`data.frame` (with boxes); `printer_noodle_verdict` comes from the log and has no frame.
Nothing is sent to the printer except `camera.start_monitor`: vision never pauses or cancels.
Decide on the event (`3d printer status`, Fluidd), then pause yourself.

## One-job automatic pause

`watch`, `vision` and alert delivery are observation paths. Receiving a critical alert does
not prove a printer was paused. The separate `guard` command explicitly authorizes an
automatic native PAUSE of one current Moonraker job:

```bash
3d printer guard u1 --job-id 00006D --yes
3d printer guard u1 --job-id 00006D --dry-run --interval 10 --evidence ~/.cache/3d-cli/guard-check
```

Get the actual native job ID from Moonraker history immediately after the start is acknowledged.
`--job-id` is required; a filename alone cannot distinguish repeated attempts of the same file.
The printer is positional: use `printer guard NAME`, not the `-n/--name` selector reserved
for `printer todo`/`issues`; the CLI rejects that selector with the positional spelling.
The guard binds the latest `in_progress` history record's job ID, filename and start time to
the printer's current `printing` state. It refuses read-only and non-Moonraker devices and
disarms on a pause, completion, cancellation, another job, or a reset print duration. It never
resumes or cancels and never changes firmware detection settings.

The guard uses the configured camera, geometry and existing Obico/CLIP scorer and failure
thresholds. The verifier must be enabled. Two consecutive **distinct, fresh frames** must
reach the existing critical trend with verified failure regions. It checks frame acquisition
time, source `Last-Modified` when supplied, JPEG content changes and brightness; old, repeated,
dark, unverified or unreadable evidence cannot authorize a pause. Healthy frames decay old
failure history. Scene findings and native NPU log entries alone do not trigger this guard.
An undecodable frame, OpenCV processing error, or ONNX Runtime `Fail`, `RuntimeException`,
`EngineError` or `EPFail` during scoring produces `blind` with `score_error` and clears the
temporal evidence. Valid later frames can recover. Other exceptions, including programming
errors and model/input-contract errors, propagate; identity, state-file and PAUSE failures
are outside the scoring handler. A JPEG warning alone does not imply a decoding failure.
The existing first-six-frame warmup and model limitations remain: this does not guarantee
recognition of an intact displaced panel before it turns into visible spaghetti.
Every blind outcome replaces the complete failure tracker, including its long-term baseline;
repeated interrupted warmups cannot train away a later real failure.

The current policy is version 1, with a 30-second maximum frame age and a 10-second default
capture interval (`--interval` accepts 2..30 seconds). Shorter intervals change the elapsed
time represented by the existing frame-count tracker. Keep 10 seconds for its tested cadence.
Acquisition continuity has a separate limit: the greater of 30 seconds or the configured
interval plus a 2-second scheduling margin. Thus `--interval 30` tolerates a small capture
delay, while the default still discards temporal evidence after a gap over 30 seconds.
This margin never increases the maximum age of an individual frame, including the final
freshness check immediately before PAUSE.
When polling outruns the camera, a fresh duplicate produces `waiting` with
`camera_ready: false`. It adds no confirmation, does not reset a continuous trend, and does
not advance the last distinct image's acquisition time. A stale, invalid or reordered frame,
or a gap beyond that continuity limit, still produces `blind` and resets the full tracker.
The guard remembers the last 64 unique JPEG hashes in insertion order. Repeated reads do
not refresh their position, and elapsed time or blindness does not clear this bounded cache.
Only 64 newer unique images can evict an old image; this is a recent replay check, not a
history of every image in the job. Frozen or alternating cached images therefore cannot
refresh the continuity bound, even without source timestamps. When supplied, `Last-Modified`
must not move backward from the latest accepted distinct image's source timestamp; this
high-water mark survives blindness and a duplicate's rewritten header cannot advance it.
Equal source timestamps are allowed for distinct images because HTTP
timestamps may have coarser resolution than the camera.
`--evidence DIR` changes the saved-frame directory; the default is `~/.cache/3d-cli/guard`.
`--dry-run` records `would_pause` and exits without a PAUSE, job or motion command, or an
attempt claim. Camera keepalive (`camera.start_monitor`) still runs when configured.

**Clock synchronization is a precondition.** The guard host, Moonraker history and camera
server must use synchronized wall clocks. Acquisition time must not be in the host's future
and must be at or after the native job start. When supplied, `Last-Modified` must also be at
or after that start, no more than 30 seconds old, and no more than 5 seconds ahead of the host.
The guard does not estimate offsets, change clocks or relax these checks. A `blind` reason
identifies a future acquisition, future source timestamp, source age, or pre-job image;
clock skew is a possible cause, not a proven diagnosis. Compare host/printer time and obtain
a new frame before claiming readiness. If that cannot resolve the diagnostic within a
30-second readiness check, treat the guard as unavailable and inspect the clock/camera
instead of waiting indefinitely. For an active print, this requires operator attention;
the guard does not pause from missing evidence.

Decisions are printed as JSONL and saved atomically to
`~/.local/state/3d-cli/guard/NAME.status.json` (XDG paths apply). `armed` means the scorer is
loaded and the native job is bound (`camera_ready: false`). A fresh `observing` record with
`camera_ready: true`, `frame_ts` and, when available, `frame_modified` establishes that a
usable frame has passed through the policy.
Check its `pid`, `ts`, `job_id`, `filename`, `action: pause`, `authorized: true`,
`dry_run: false` and `policy_version: 1`. A dead process, stale status, `waiting` or `blind` record does
not establish protection.

Before PAUSE, the guard saves the raw evidence and an exclusive durable claim at
`~/.local/state/3d-cli/guard/NAME-JOBID.json`, then re-reads the current native job and checks
frame age again. If that final read has a communication error, or the same job's evidence
expires before the request, it durably releases only its own definitely-unsent claim and
returns to `blind` with all temporal evidence reset. New usable frames can recover. A changed
job, manual pause or reset duration at that final read also releases the owned unsent claim,
then disarms. This does not resume the printer or automatically rearm the guard. A claim
replaced by another owner is preserved, and the guard disarms
without sending a request. Filesystem failures propagate; they never report a successful
release. It sends `/printer/print/pause` once and polls for the same job to report
`paused`. An acknowledged request without that readback is `pause_unverified` (exit 1).
A lost response or firmware refusal still gets readback and a terminal result. Once the
PAUSE actuator has been entered, its claim is retained even if the request is refused.
An uncertain request is never resent,
including after a process restart. Inspect the claim and printer before taking any further
action. The API does not provide an atomic conditional pause; the final identity read narrows
the unavoidable read/write race.

The guard has no notification dispatcher, subscriber, temporary agent or Telegram dependency.
Run it as a finite per-job process and check its fresh status after attaching it to the
acknowledged job. Any process restart must retain the exact job ID and persistent claim
directory; it reloads the models and starts fresh evidence, including the six-frame warmup.
It must never start or resume a print. A new job needs a new explicit invocation with its
own native ID.

Labels of the U1's own detector (unisrv `camera.detect_capture`), from Snapmaker u1-klipper
`defect_detection.py` and the U1's `klippy.log`:

| label | where it comes from | raises `printer_noodle_verdict` |
|---|---|---|
| `noodle` | print-time check (~40 s); the firmware pauses on it, exception code 2 | yes |
| `residue` | print-time check when enabled; pause, code 3 | yes |
| `item` | clean-bed check at the start (`DEFECT_DETECTION_DETECT_BED`): a foreign object, "dirty bed", code 1 | yes |
| `nozzle` | in the clean-bed answer and the nozzle check; a high p means the nozzle was found clean | no ([#306](https://github.com/alex-mextner/3d-cli/issues/306)) |
| `legend_text` | in the clean-bed answer: the markings printed on the build plate | no ([#324](https://github.com/alex-mextner/3d-cli/issues/324)) |
| `extrusion_line` | in the clean-bed answer: a purge/extrusion line; the firmware ignores it | no |
| anything else | a label a later firmware may add | yes (better a question than a miss) |

## How it decides

1. **Frame.** The U1 has no video stream on stock firmware; `unisrv` rewrites
   `camera/monitor.jpg` about twice a second (~2 fps) while a client keeps it awake with the JSON-RPC
   `camera.start_monitor` (what Snapmaker Orca does). vision sends it once a minute through
   Moonraker's websocket. A Moonraker 500 or a truncated JPEG (the file is being rewritten) is
   retried.
2. **Spaghetti score.** Obico's open failure detector (YOLOv2, one class) runs on the CPU with
   onnxruntime: the frame is resized to 416×416, boxes with confidence > 0.08 survive greedy NMS
   (IoU 0.45), and the raw frame score `p` is the sum of their confidences. ~0.1 s per frame.
   With the camera calibration (the U1's ships with 3d-cli) boxes whose centre lies outside the
   bed volume of the current print (Z and `exclude_object` footprints from Moonraker) are
   dropped, as are boxes inside the `ignore:` polygons.
3. **Verifier.** Each of the 8 strongest boxes is cropped and scored zero-shot by OpenAI CLIP
   ViT-B/32 (failure prompts vs healthy-print prompts); the verified score is
   `q = Σ confidence × P(failure)`. Speckled or multicolour walls that Obico boxes get q near 0.
   Numbers: [printer-vision-tuning.md](../research/printer-vision-tuning.md).
4. **Trend.** Obico's published decision scheme, re-implemented, on q: an exponential moving average
   of q (span 12) minus the printer's long-term mean of q (7200 frames, so static clutter that
   always scores is cancelled), alert above 0.38 (or above 0.78, or 3.8× the print's short-term
   excess), critical when the same test passes after dividing by 1.75. The first 6 frames of a
   print never alert (Obico uses 30; on the U1 healthy frames score ≤ 0.11, so a short warm-up
   still catches a print that fails right after a resume).
5. **Classic checks.** Byte-identical or missing frames (stale camera) and mean brightness.
6. **The printer's own AI.** The U1 runs a YOLO "noodle" model on its NPU every ~40 s while
   printing and logs each verdict to klippy.log; vision tails that log over HTTP Range requests.
   Moonraker does not expose the detector itself (`camera.detect_capture` → -32601).

## Three tiers

1. **(a) cheap checks, every frame.** The pipeline above: stale/dark camera, Obico boxes + CLIP
   crop verifier, bed-change geometry.
2. **(b) a mid-size model on the whole frame, every 30 s** (`lib/vision_model.py`). OpenAI CLIP
   ViT-B/16 (MIT; fp16 ONNX, 173 MB, sha256-pinned, revision `342fdf2f67aded64d138ff074745fb4a5d2bba5f`
   of Xenova/clip-vit-base-patch16) embeds the frame squashed to 224 px and scores it against
   prompt sets (spaghetti, part detached, empty plate, healthy), or against a head you fitted with
   `fit-head`. It adds `vision_model_*` Findings next to tier (a), as warnings only. Device: the
   GPU when onnxruntime has the CUDA provider and >= 2 GiB of VRAM are free (6 ms per frame,
   +379 MiB), else the CPU (160 ms); `THREED_VISION_DEVICE=cpu|cuda|auto` or
   `watch.vision.model.device` override, and `cuda` without the provider is an error, not a silent
   CPU run. Only `spaghetti` is on by default: zero-shot has no skill on a lifted/detached part or
   an empty plate (numbers: [printer-vision.md](../research/printer-vision.md)), so those two
   classes need a fitted head or an explicit threshold.
3. **(c) the LLM look.** A Finding or event wakes the subscribed agent session or the temporary
   printer agent, which receives the frame and judges it (`3d printer subscribe`, `agent`).

GPU onnxruntime is optional and not a project dependency: `uv pip install onnxruntime-gpu
nvidia-cudnn-cu13 nvidia-cublas nvidia-cuda-runtime nvidia-curand nvidia-cufft` replaces the CPU
package in the environment that runs `3d printer vision` (use the CUDA-12 wheels with an
onnxruntime-gpu built for CUDA 12). Without it everything runs on the CPU.

## Model and licence

Tier (b)'s weights are downloaded the same way (`fetch-model --tier b`) from
`https://huggingface.co/Xenova/clip-vit-base-patch16` (revision above; OpenAI CLIP ViT-B/16, MIT,
ONNX export by Xenova/transformers.js), checked against sha256
`3e261924f7d66d04f10bb131f7f212162d9ee82199544771aa2dd318bb7e2b7b`, and cached in the same
directory. The prompt embeddings (`lib/data/vision/model_prompts.json`) are small text and ship
with 3d-cli.

The tier (a) weights are **not** part of 3d-cli (MIT). On first use they are downloaded from
`https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx` (202 MB) into
`~/.cache/3d-cli/vision/models/` and checked against sha256
`0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa`. They come from
[obico-server](https://github.com/TheSpaghettiDetective/obico-server) (AGPL-3.0; no separate
weights licence). 3d-cli's inference and decision code is its own and contains no Obico code.
Python deps: numpy, opencv-python-headless, onnxruntime (the `vision` extra; `uv sync
--all-extras`, or pulled on the fly by `uv run --with`).

## Results on real U1 footage (2026-10-14)

See [docs/research/printer-vision.md](../research/printer-vision.md) for the data set, the
numbers and the alternatives that were tried (PrintGuard, the U1 NPU model).

## The U1 camera in Fluidd

Fluidd's camera entry (Moonraker database, namespace `webcams`, "U1 Camera") points at
`/api/ingress/u1_printer/u1-camera/u1.mjpeg` (stream) and `/api/ingress/u1_printer/u1-camera/u1.jpg`
(snapshot): a Home Assistant ingress proxy that turns `monitor.jpg` into MJPEG. That URL works
only when Fluidd is opened through Home Assistant. Opened directly at `http://192.168.0.26/` the
same path hits Moonraker and answers 404, and nginx `/webcam/` answers 502 (no streamer on stock
firmware). The direct, always-working snapshot is
`http://192.168.0.26/server/files/camera/monitor.jpg`, which is what `3d printer vision` reads; it
does not depend on the Fluidd entry. To switch Fluidd to it (or back), edit the camera in Fluidd
→ Settings → Cameras, or `POST /server/webcams/post_item` with uid
`7736714c-f38d-42be-890f-eea8d44db3f5`.

## Configuration

Optional, under the device's `watch:` block in `printer-devices.yaml`:

```yaml
u1:
  adapter: moonraker
  host: 192.168.0.26
  watch:
    tg_fallback: true        # emergency only: the phone gets what no agent could take
    vision:
      snapshot: /server/files/camera/monitor.jpg   # default for Moonraker printers
      keepalive: true                              # default: on for the U1 monitor.jpg only
      verify: true                                 # stage 2 (CLIP verifier); default on
      geometry: true                               # bed-volume mask from the camera calibration
      scene: false                                 # bed-change detectors; default off (noisy)
      ignore: [[[0.8, 0.0], [1.0, 0.0], [1.0, 0.3], [0.8, 0.3]]]  # polygons (0..1) to drop boxes in
      model:                       # tier (b); every key optional, a bare true/false works too
        enabled: true              # default: on when the weights are cached; false = off
        device: auto               # auto (CUDA if >= 2 GiB free) | cpu | cuda; THREED_VISION_DEVICE wins
        every_s: 30                # seconds between model runs (not per frame)
        threshold: {spaghetti: 0.5}  # per class, or one number for all; empty_bed/part_detached need one (or a head)
        confirm: 3                 # runs in a row at/above the threshold before the Finding
        clear: 2                   # runs below it before the Finding clears
        min_z: 10                  # mm; empty_bed is judged only once the print is this high (default 10)
        crop: [0.0, 0.3, 1.0, 1.0] # normalised x1,y1,x2,y2 of the bed area; default the whole frame
        head: ~/.cache/3d-cli/vision/models/tier-b-head.json   # default; from `fit-head`
```

The block is per device, so every Moonraker printer in the fleet (U1, K1 SE, others) gets its own
camera `snapshot`, thresholds and `crop`; nothing in tier (b) is U1-specific.

## Examples

```bash
3d printer vision u1 --once --save /tmp/u1.jpg   # one verdict + the frame
3d printer vision u1 --dry-run --verbose        # run the loop, print what would be sent
3d printer vision eval --pos ~/footage/spaghetti --neg ~/footage/healthy/*.mp4
3d printer vision eval --pos ~/footage/spaghetti --neg ~/footage/healthy/*.mp4 --model   # + tier (b)
3d printer vision fetch-model --tier b           # cache the tier (b) weights (idempotent)
3d printer vision k1se --once --model            # force tier (b) on for one verdict
THREED_VISION_DEVICE=cpu 3d printer vision u1    # keep the GPU free for something else
3d printer vision fit-head --class part_detached --pos ~/vision/detached/* --neg ~/vision/ok/*
```
