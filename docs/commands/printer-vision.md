# `3d printer vision` — watch the printer camera for failed prints

Issue: [#150](https://github.com/alex-mextner/3d-cli/issues/150). Events go through the same
dispatcher and sinks as [`3d printer watch`](printer.md) (stdout JSONL, `tg`, `steer`, `exec`
from the device's `watch:` block in `printer-devices.yaml`).

## Usage

```text
3d printer vision NAME [--interval S] [--once] [--save OUT.jpg] [--dry-run] [--min-severity LEVEL]
                       [--snapshot URL|PATH] [--no-keepalive] [--no-npu] [--evidence DIR] [--verbose]
                       [--no-verify] [--no-geometry] [--scene] [--no-detach]
3d printer vision eval --pos PATH... --neg PATH... [--every N] [--warmup N] [--interval S]
                       [--lead-in N] [--printer NAME] [--no-verify] [--scene] [--no-detach] [--json]
3d printer vision record NAME DIR [--interval S] [--max N] [--timeout S] [--snapshot URL|PATH] [--no-keepalive]
3d printer vision fetch-model
```

- `NAME` — a Moonraker printer from `3d printer list` that has a camera. Where the frames come
  from is the printer's `watch.vision.camera` ([Fleet registry](printer.md#cameras-watchvisioncamera):
  the U1 is `moonraker-monitor`, the rooted K1 SE `mjpg-streamer` on `:8080`); a printer without
  one is refused with `no camera`, never given another printer's camera path.
- `--interval` — seconds between frames (default 10; the thresholds were tuned for 10 s).
- `--once` — score the current frame, print one JSON verdict and exit; `--save` also writes the
  frame (with detection boxes when there are any).
- `--snapshot` — camera URL or Moonraker path, overriding the printer's `watch.vision.camera`.
- `--no-keepalive` — do not send `camera.start_monitor` (only `moonraker-monitor` cameras need it; see U1 camera below).
- `--no-npu` — do not read the printer's own detector verdicts from klippy.log (read only where the printer has one: `watch.vision.npu`, default on for a `moonraker-monitor` camera).
- `--evidence` — where event frames are saved (default `~/.cache/3d-cli/vision/evidence/`).
- `--no-verify` — skip stage 2 (the CLIP verifier); the trend then runs on the raw Obico score,
  which on the U1 fires on speckled walls. For debugging only.
- `--no-geometry` — ignore the camera calibration (no bed-volume mask, no bed-change or part-detached detectors).
- `--no-detach` — skip the part-detached check (`vision_part_detached`, below); same as `detach: false` in the
  device's `watch: vision:` block, which can also hold a mapping that tunes it. With `--once` the report adds
  `detach_gap` (0..1, null = the check abstained) and `part_detached` (gap at or above the absolute threshold).
- `--scene` — also run the bed-change detectors (`vision_foreign_object`, `vision_part_moved`,
  `vision_no_growth`). Off by default: they are still noisy (see
  [printer-vision-tuning.md](../research/printer-vision-tuning.md)). Same as `scene: true` in the
  device's `watch: vision:` block. Needs the camera calibration (a usage error without it).
- `eval` — replay footage: each `--pos`/`--neg` path is a directory of frames, one image, or a
  video (a U1 timelapse `.mp4`); each path counts as one print. Prints per-frame
  precision/recall/false-positive rate at several score thresholds, and whether the live
  tracker would have raised an alert on each sequence.
- `record` — while the printer prints or is paused, save every frame with the print context (state, Z,
  objects) into `DIR` (`meta.jsonl`), for `eval --printer NAME` later. In any other state it saves nothing
  and says so on stderr; `--timeout S` makes it exit 1 when no frame was saved within S seconds (without it
  it waits for a print), `--max N` stops after N frames.
- `fetch-model` — download the detector and verifier weights now (otherwise the first run does it).

## What it detects

| kind | severity | when |
|---|---|---|
| `vision_spaghetti` | warning, then critical | the trend of the verified score q crosses the alert / the escalated threshold while printing |
| `vision_spaghetti_vetoed` | info | the raw detector's trend would warn, but the verifier confirms less than 0.3 of it (texture, not filament) |
| `vision_part_detached` | warning, then critical | the printed part looks lifted/peeled off the plate: bare build plate shows where the near wall of the part should be (details below); on by default with the camera calibration; never pauses |
| `vision_foreign_object`, `vision_part_moved` | warning | only with `--scene`: something new on the bed outside the parts / a part's silhouette changed |
| `vision_no_growth` | info | only with `--scene`: the newest layers of a part look empty (under- or no extrusion) |
| `printer_noodle_verdict` | warning | the U1's own NPU detector logs a defect label (table below) at or above 0.83 (once per episode, 5 min quiet ends it) |
| `vision_camera_stale` | warning | printing, but the picture has not changed (or could not be fetched) for 120 s |
| `vision_camera_dark` | warning | printing with a mean brightness below 20/255 (light off, lens covered) |

Each event fires once per episode. Camera events (`vision_*`) carry the saved frame path in
`data.frame` (with boxes); `printer_noodle_verdict` comes from the log and has no frame.
Nothing is sent to the printer except `camera.start_monitor`: vision never pauses or cancels.
Decide on the event (`3d printer status`, Fluidd), then pause yourself.

### The part-detached check (`vision_part_detached`)

Tier (a): OpenCV only, one frame, no history. The calibration projects each part footprint's prism
(bed to the current layer) into the picture; its near side wall is the band between the base edge on the plate
and the newest layer. The plate colour is learnt from the bed pixels outside the part in the same frame, and
`gap` is the share of the eroded wall band with that colour. Warning: `gap ≥ 0.25`, or `gap` ≥ 0.12 above the
lowest gap of this print, for 3 frames in a row; critical: the rise stays ≥ 0.25 for 12 frames. It abstains
(no event) on a grey plate (chroma < 25), without enough visible plate, below 1.5 mm, or without footprints.
Never pauses: [`printer guard`](#one-job-automatic-pause) acts on confirmed spaghetti only. The U1 incident it
was written for, the numbers on the real frame (gap 0.52), its limits and its unmeasured false-positive rate:
[printer-vision-detached-part.md](../research/printer-vision-detached-part.md). Tuning:

```yaml
watch:
  vision:
    detach: {gap_abs: 0.25, gap_rise: 0.12, gap_crit: 0.25, persist: 3, persist_crit: 12}  # or false
```

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

## Model and licence

The weights are **not** part of 3d-cli (MIT). On first use they are downloaded from
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
      camera: {kind: moonraker-monitor}            # the U1; K1 SE: {kind: mjpg-streamer, port: 8080}; see printer.md "Cameras"
      keepalive: true                              # default: on for kind moonraker-monitor only
      npu: true                                    # read the printer's own detector; default: on for moonraker-monitor only
      limits: {stale_s: 120, dark_luma: 20, npu_threshold: 0.83, npu_quiet_s: 300}   # defaults measured on the U1
      verify: true                                 # stage 2 (CLIP verifier); default on
      geometry: true                               # bed-volume mask from the camera calibration
      scene: false                                 # bed-change detectors; default off (noisy)
      ignore: [[[0.8, 0.0], [1.0, 0.0], [1.0, 0.3], [0.8, 0.3]]]  # polygons (0..1) to drop boxes in
```

## Status for Home Assistant

The live loop (not `--once`) keeps `~/.local/state/3d-cli/vision/NAME.json` current every cycle:
the printer state, the last frame's `p`, `q`, trend, luma and findings, frames scored, event
counts by kind, the last event and the camera error. 3d-api serves it as
`GET /v1/printers/NAME/vision` (scope `read`, plus `age_s`; 404 for an unknown printer or one whose
loop never ran), and the HA package generated by [`3d ha vision`](ha.md#camera-vision-for-every-fleet-printer-3d-ha-vision)
turns that into sensors for every fleet printer with a camera. The API reads the file from its own
state dir, so the vision loop and the API must share it (same user and host, or a mounted dir)
[deployment not verified]. A failed write only logs; it never stops the loop.

## Fleet parity

Nothing in `vision`, `guard`, `bed`, the dossier or the HA generator names a printer: the camera
comes from the printer's `watch.vision.camera` (else its model's default), the bed ROI from
`watch.bed.roi` or the model's calibration, thresholds from the device's `watch:` block.
`tests/test_fleet_vision_parity.py` iterates the fleet and fails on a U1 literal in these generic
modules (its allowlist names the few U1-specific ones) and on a camera printer without its
generated Home Assistant artifacts.

## Examples

```bash
3d printer vision u1 --once --save /tmp/u1.jpg   # one verdict + the frame
3d printer vision u1 --dry-run --verbose        # run the loop, print what would be sent
3d printer vision eval --pos ~/footage/spaghetti --neg ~/footage/healthy/*.mp4
```
