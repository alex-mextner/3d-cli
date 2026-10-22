# Printer camera failure detection — research and evaluation (2026-10-14)

Background for [`3d printer vision`](../commands/printer-vision.md), issue
[#150](https://github.com/alex-mextner/3d-cli/issues/150). Printers: Snapmaker U1 (camera) and
Creality K1 SE (no camera at the time of this research; since 2026-10-15 it is rooted, runs
Moonraker and has a USB webcam on mjpg-streamer `:8080`).

## Snapmaker U1 camera facts

- **No stream on stock firmware.** nginx `/webcam/` answers 502: there is no mjpg/crowsnest
  streamer; the camera belongs to Snapmaker's `unisrv` service.
- **Frames.** `unisrv` rewrites `/server/files/camera/monitor.jpg` (Moonraker file root) at about
  2 fps while a client keeps the monitor awake. The file is rewritten in place, so a read
  occasionally returns a Moonraker 500 or a truncated JPEG (missing `FF D9`); retry.
- **Keepalive.** The monitor stays awake only while someone sends the JSON-RPC
  `camera.start_monitor` over Moonraker's websocket (what Snapmaker Orca does); it gets no reply.
  `3d printer vision` sends it once a minute. This is the only call vision makes to the printer.
- **The printer's own detector.** The U1 runs an RKNN "noodle" model on its NPU while printing
  and logs each verdict to `klippy.log`; it is enabled by `NOODLE_ENABLE=1` /
  `defect_detection.json` `enable: true`. Moonraker does not expose it (`camera.detect_capture` →
  -32601 method not found), so vision tails `klippy.log` with HTTP Range requests.
- **Fluidd / Home Assistant ingress path.** Fluidd's "U1 Camera" entry points at
  `/api/ingress/u1_printer/u1-camera/u1.mjpeg` and `.../u1.jpg`: the Home Assistant ingress of the
  `u1-camera-proxy` container, which polls `monitor.jpg` and serves MJPEG. It works only when
  Fluidd is opened through Home Assistant; opened directly the same path hits Moonraker (404).
- **Odroid proxy font fix.** After Home Assistant moved to the Odroid host, `u1-camera-proxy`
  (`/home/ultra/odroid-overlay/compose.odroid.yaml`) failed on every frame with PIL's
  "cannot open resource" font error while drawing its overlay: the font path it expects does not
  exist on the new host. Fix: point the proxy's font setting at a font present in the container
  (then `docker compose -f /home/ultra/homeassistant/ha.docker-compose.yaml -f
  /home/ultra/odroid-overlay/compose.odroid.yaml up -d u1-camera-proxy`), or install
  `fonts-dejavu-core` on the host. vision itself does not depend on the proxy: it reads
  `monitor.jpg` directly.

## Detectors considered

| Detector | Licence | Result on U1 frames | Used |
|---|---|---|---|
| [Obico](https://github.com/TheSpaghettiDetective/obico-server) `ml_api` failure model (YOLOv2, ONNX, 416×416) | AGPL-3.0 repo, no separate weights licence | 22/22 spaghetti frames above p 0.3; healthy max 0.10 on a 235-frame sample | yes, downloaded at runtime, sha256-pinned |
| [PrintGuard](https://github.com/oliverbravery/PrintGuard) (ShuffleNetV2 + prototypes) | GPL-2.0-only | 0/22 at its 0.75 threshold, with or without a bed crop | no |
| HF YOLO11s / MobileViT / PatchCore / YOLOv5 models | AGPL (Ultralytics) or unclear | not evaluated end to end | no |
| OctoPrint-PrintWatch | AGPL, cloud-only model | — | no |
| U1 NPU `noodle` model | closed | caught the 04:33 and 13:46 episodes itself | read from klippy.log |

Obico's decision scheme ([backend/lib/prediction.py](https://github.com/TheSpaghettiDetective/obico-server/blob/6f938964c63892df2b6d5b68d99ea783fc1a7c78/backend/lib/prediction.py),
constants in [settings.py](https://github.com/TheSpaghettiDetective/obico-server/blob/6f938964c63892df2b6d5b68d99ea783fc1a7c78/backend/config/settings.py#L473-L487)):
frame score `p` = sum of box confidences (> 0.08, NMS 0.45); EWM (span 12) minus a long-run
mean (7200 frames); not failing below 0.38, failing above 0.78 or above 3.8× the short-run excess;
critical when the test still passes after dividing by 1.75. 3d-cli re-implements this in its own
code with two U1 changes: warm-up 6 frames instead of 30, and a long-run mean seeded with 100
clean frames, so a monitor started during a failure does not learn the spaghetti as background.

Upstream quirks found: the ONNX post-processing scales box height by the frame width
([lib/onnx.py#L96](https://github.com/TheSpaghettiDetective/obico-server/blob/6f938964c63892df2b6d5b68d99ea783fc1a7c78/ml_api/lib/onnx.py#L96));
OpenCV DNN cannot load the ONNX (int64/float `NaryEltwise`), hence onnxruntime. CPU cost
~0.1–0.14 s per frame with 2 threads.

## Evaluation on real U1 footage

Data (local only, `~/.cache/3d-cli/vision/data/`): 4,095 healthy frames — six U1 timelapses
downloaded read-only from the printer plus idle frames — and the 6 frames of the
13:47:58–13:51:05 UTC spaghetti episode. One failure episode only, so the false-alarm numbers
are preliminary. Reproduce with:

```bash
D=~/.cache/3d-cli/vision/data
3d printer vision eval --pos $D/eval/pos_spaghetti --neg $D/mp4/*.mp4 $D/eval/neg_*
```

Per frame (`3d printer vision eval`, 2026-10-14, 4,101 frames, 0.136 s/frame on the CPU):

| p threshold | precision | recall | false-positive rate (fp) |
|---|---|---|---|
| 0.1 | 0.094 | 1.0 | 0.0142 (58) |
| 0.2 | 0.375 | 1.0 | 0.0024 (10) |
| 0.3 | 0.75 | 1.0 | 0.0005 (2) |
| 0.5 | 1.0 | 1.0 | 0 |

Whole sequences through the live tracker: 0 false alarms on 8 healthy sequences (worst healthy
p 0.335 stays below the 0.38 alert floor, so this holds for any warm-up). The spaghetti clip has
only 6 frames, so with the default warm-up of 6 `eval` (which treats each path as a print start)
never alerts on it; with `--warmup 0` or `--warmup 2` it warns at the 3rd frame (13:50:05 UTC) and is
critical by the 5th. Live, the warm-up also applies when the monitor starts mid-print: the first
alert can come after 6 frames (~60 s at the default interval).

The worst healthy frame is the dark tip of a tree support in `quant_1`. The U1's own detector
flagged the same episode earlier (13:46). The 04:38 episode has no frames (no timelapse was
recording); `klippy.log` shows NPU scores 0.84–0.89 from 04:33 and a pause with code 2.

## Not done (follow-ups)

- Layer shift by comparing the part silhouette with the G-code footprint. Detachment from the plate is
  covered since 2026-10-22 by `vision_part_detached`; the incident that motivated it is written up in
  [printer-vision-detached-part.md](printer-vision-detached-part.md).
- Masks for the purge bin and prime tower.
