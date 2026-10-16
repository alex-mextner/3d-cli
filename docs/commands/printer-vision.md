# `3d printer vision` — watch the printer camera for failed prints

Issue: [#150](https://github.com/alex-mextner/3d-cli/issues/150). Events go through the same
dispatcher and sinks as [`3d printer watch`](printer.md) (stdout JSONL, `tg`, `steer`, `exec`
from the device's `watch:` block in `printer-devices.yaml`).

## Usage

```text
3d printer vision NAME [--interval S] [--once] [--save OUT.jpg] [--dry-run] [--min-severity LEVEL]
                       [--snapshot URL|PATH] [--no-keepalive] [--no-npu] [--evidence DIR] [--verbose]
                       [--no-verify] [--no-geometry] [--scene]
3d printer vision eval --pos PATH... --neg PATH... [--every N] [--warmup N] [--interval S]
                       [--lead-in N] [--printer NAME] [--no-verify] [--scene] [--json]
3d printer vision record NAME DIR [--interval S] [--max N] [--snapshot URL|PATH] [--no-keepalive]
3d printer vision fetch-model
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
- `eval` — replay footage: each `--pos`/`--neg` path is a directory of frames, one image, or a
  video (a U1 timelapse `.mp4`); each path counts as one print. Prints per-frame
  precision/recall/false-positive rate at several score thresholds, and whether the live
  tracker would have raised an alert on each sequence.
- `record` — while the printer prints, save every frame with the print context (Z, objects)
  into `DIR` (`meta.jsonl`), for `eval --printer NAME` later.
- `fetch-model` — download the detector and verifier weights now (otherwise the first run does it).

## What it detects

| kind | severity | when |
|---|---|---|
| `vision_spaghetti` | warning, then critical | the trend of the verified score q crosses the alert / the escalated threshold while printing |
| `vision_spaghetti_vetoed` | info | the raw detector's trend would warn, but the verifier confirms less than 0.3 of it (texture, not filament) |
| `vision_foreign_object`, `vision_part_moved` | warning | only with `--scene`: something new on the bed outside the parts / a part's silhouette changed |
| `vision_no_growth` | info | only with `--scene`: the newest layers of a part look empty (under- or no extrusion) |
| `printer_noodle_verdict` | warning | the U1's own NPU detector logs a defect verdict (`noodle`, `residue`, …) at or above 0.83 (once per episode, 5 min quiet ends it). The `nozzle` label is the firmware's nozzle check (high p = nozzle found) and raises nothing ([#306](https://github.com/alex-mextner/3d-cli/issues/306)) |
| `vision_camera_stale` | warning | printing, but the picture has not changed (or could not be fetched) for 120 s |
| `vision_camera_dark` | warning | printing with a mean brightness below 20/255 (light off, lens covered) |

Each event fires once per episode. Camera events (`vision_*`) carry the saved frame path in
`data.frame` (with boxes); `printer_noodle_verdict` comes from the log and has no frame.
Nothing is sent to the printer except `camera.start_monitor`: vision never pauses or cancels.
Decide on the event (`3d printer status`, Fluidd), then pause yourself.

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
      snapshot: /server/files/camera/monitor.jpg   # default for Moonraker printers
      keepalive: true                              # default: on for the U1 monitor.jpg only
      verify: true                                 # stage 2 (CLIP verifier); default on
      geometry: true                               # bed-volume mask from the camera calibration
      scene: false                                 # bed-change detectors; default off (noisy)
      ignore: [[[0.8, 0.0], [1.0, 0.0], [1.0, 0.3], [0.8, 0.3]]]  # polygons (0..1) to drop boxes in
```

## Examples

```bash
3d printer vision u1 --once --save /tmp/u1.jpg   # one verdict + the frame
3d printer vision u1 --dry-run --verbose        # run the loop, print what would be sent
3d printer vision eval --pos ~/footage/spaghetti --neg ~/footage/healthy/*.mp4
```
