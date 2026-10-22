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

## Three tiers (2026-10-22)

The U1 printed a large white honeycomb panel that peeled off the plate and no detector noticed.
Vision is therefore split in three tiers, each independent of the others:

| tier | what | cost | where |
|---|---|---|---|
| (a) cheap checks | stale/dark camera, Obico boxes + CLIP crop verifier, bed-change and detached-part geometry | CPU, every frame (10 s) | `vision_detect.py`, `vision_scene.py`, `vision_detach.py` |
| (b) mid-tier model | a whole-frame image model, zero-shot prompts or a fitted head | GPU if free (6 ms) else CPU (160 ms), every 30 s | `vision_model.py` |
| (c) LLM check | the temporary printer agent looks at the frame an event carries and decides | minutes, tokens | `printer_ops/printer_agent.py`, `subscriptions.py` |

Tier (b) did **not exist** before: stage 1 is Obico's YOLOv2 on 416 px crops of nothing but
spaghetti texture, stage 2 is CLIP ViT-B/32 on the crops of those boxes (both CPU onnxruntime,
`CPUExecutionProvider` hard-coded, ~0.14 s per frame, no GPU path), neither ever looks at the
whole frame, and the only class either covers is spaghetti: nothing for a detached part, an empty
plate, residue or a displaced part. `vision_model.py` adds the whole-frame model; it only ever
adds `Finding`s (warning, never an automatic pause) next to tier (a).

### Tier (b): candidates, measured (2026-10-22, RTX 4060 8 GB, desktop holding ~3.5 GB)

Every candidate was embedded once on the GPU (fp16, whole frame squashed to 224 px) over the same
11,381 frames and scored with the same prompt sets (`PROMPTS` in `vision_model.py`, not tuned on
any split) with `tools/vision_model_bench.py`. Frames: the public collection of
[printer-vision-tuning.md](printer-vision-tuning.md) (6,387 images; `test` split = 360
spaghetti, 982 healthy, 162 `detached` and 147 `warping`), 4,952 healthy U1 frames (six
timelapses and the speckled-wall recordings), 15 idle U1 frames, the 6 clean U1 spaghetti frames of
2026-10-14, the U1 panel frame and the K1 frame. AUROC; "TPR@1%" = recall at the threshold that
passes 1 % of the healthy frames of that task.

| candidate (licence, params) | spaghetti, public test AUROC / TPR@1% | clean U1 spaghetti (6) at the U1-healthy p99.9 threshold | empty plate (idle+K1 vs printing) | `detached` emptydeck | warping | any-failure head, leave-one-source-out | GPU b=1 ms / VRAM |
|---|---|---|---|---|---|---|---|
| CLIP ViT-B/32 (MIT, 151 M) | 0.843 / 0.29 | 1/6 | 0.91 | 0.50 | 0.62 | 0.725 | 5.4 / 589 MiB |
| **CLIP ViT-B/16 (MIT, 150 M)** | **0.888 / 0.53** | **5/6** | 0.98 | 0.45 | 0.52 | 0.797 | 3.7 / 693 MiB |
| LAION CLIP ViT-B/32 (MIT, 151 M) | 0.797 / 0.20 | 2/6 | 0.87 | 0.50 | 0.14 | 0.800 | 3.5 / 418 MiB |
| SigLIP base-224 (Apache-2.0, 203 M) | 0.868 / 0.53 | 0/6 | 0.99 | 0.53 | 0.80 | 0.843 | 4.2 / 933 MiB |
| SigLIP2 base-224 (Apache-2.0, 375 M) | 0.837 / 0.46 | 0/6 | 0.95 | 0.53 | 0.87 | 0.844 | 4.7 / 1137 MiB |
| DINOv2-small (Apache-2.0, 22 M), embedding + logistic head only | no zero-shot | | | head CV 0.68 | | 0.809 | 4.8 / 273 MiB |

Hugging Face revisions evaluated (first 10 hex of the commit): openai/clip-vit-base-patch32
`3d74acf9a2`, openai/clip-vit-base-patch16 `57c216476e`, laion/CLIP-ViT-B-32-laion2B-s34B-b79K
`1a25a44671`, google/siglip-base-patch16-224 `7fd15f0689`, google/siglip2-base-patch16-224 `75de2d55ec`,
facebook/dinov2-small `ed25f3a31f`. Licences are the ones the model cards state (MIT for the CLIP
family, Apache-2.0 for SigLIP/DINOv2).

Not evaluated, with the reason: YOLO11/Ultralytics (AGPL) and PrintGuard (GPL-2.0) stay out of the
shipped path; MobileCLIP (Apple's `apple-amlr` licence) is not permissive; OWLv2 (Apache-2.0, 155 M,
960 px) and RT-DETR/D-FINE would need box labels, of which we have 226 images (all spaghetti).
SigLIP/SigLIP2 score best on warping and on the fitted heads, but score the real U1 spaghetti
frames below their own healthy p99.9 (0/6), so they are not the shipped default; they remain
candidates for the fitted-head path once more labelled frames exist.

**Chosen: CLIP ViT-B/16.** Best spaghetti AUROC and the only candidate that sees the real U1
failure, MIT like the CLIP B/32 of the verifier (same preprocessing and prompt machinery), 150 M
parameters (86 M in the vision tower), 173 MB as an fp16 ONNX export
([Xenova/clip-vit-base-patch16](https://huggingface.co/Xenova/clip-vit-base-patch16) at revision
`342fdf2f67aded64d138ff074745fb4a5d2bba5f`, base model
[openai/clip-vit-base-patch16](https://huggingface.co/openai/clip-vit-base-patch16) at
`57c216476eefef5ab752ec549e440a49ae4ae5f3`, sha256 `3e261924…`). The ONNX export matches torch
(cosine ≥ 0.9999 on 128 frames); the int8 export does not (cosine 0.79) and is not used.

### Tier (b): what it does and does not do

Shipped thresholds are the ones measured on the ONNX path (fp16, CUDA) over the same frames:

| spaghetti score ≥ | public tune recall / healthy FP | public test recall / healthy FP | U1 healthy FP | clean U1 spaghetti |
|---:|---|---|---|---|
| 0.3 | 0.675 / 6.7 % | 0.617 / 2.5 % | 25 / 4952 | 6/6 |
| 0.4 | 0.612 / 4.5 % | 0.536 / 1.1 % | 3 / 4952 | 5/6 |
| **0.5** (default) | 0.556 / 3.1 % | 0.467 / 0.7 % | 0 / 4952 | 5/6 |
| 0.7 | 0.430 / 1.6 % | 0.306 / 0.3 % | 0 / 4952 | 4/6 |

- **It is a second opinion, not a replacement.** On the public test split tier (a) (verified score
  q ≥ 0.38) finds 303 of 360 spaghetti images with 0 false positives; tier (b) alone finds 168
  with 7 false positives. Of the 57 images tier (a) misses, tier (b) at 0.5 finds 12
  (union 315/360, still 7 false positives per 982 healthy single frames).
- **Cadence and debounce make it quiet.** One run every 30 s, and a Finding only after 3 runs in
  a row at or above the threshold (clears after 2 below). Replayed over 19.7 h of healthy U1
  footage (the speckled walls, 2.4 h at 10 s; the plain walls, 17.3 h) it raised 0 Findings
  (highest run score 0.34), also at threshold 0.4. The 6-frame real spaghetti clip is too sparse
  (20–107 s apart) for three consecutive runs at the default; with `confirm: 2` and threshold 0.4
  it raises one Finding at its last frame. Real spaghetti persists for minutes, so 90 s of
  confirmation is the price of silence.
- **No skill on a detached part.** Zero-shot `part_detached` has AUROC 0.45 on the
  bed-camera `detached` frames, 0.38 on the 8 U1 panel frames and 0.52 on warping: the whole frame
  of a panel lifting at one corner looks like the healthy print to a 224 px embedding. Even a head
  fitted on those bed-camera frames (sequence-grouped cross-validation AUROC 0.72) transfers to the
  U1 panel frames with AUROC 0.90 for CLIP B/16 but only 0.50–0.65 for the other candidates, on n = 8 frames from one
  event, of which 7 carry drawn detection boxes: that is not evidence. **The U1 panel case is
  tier (a)'s geometry detector's job (`vision_detach.py`); tier (b) ships with `part_detached` OFF**
  and a way to turn it on once there are labelled frames (below).
- **No skill on an empty plate either.** The score separates 15 near-identical idle frames
  (AUROC 0.98) but gives 0.32 and 0.27 on the two real U1 shutdown frames and 0.48 on the K1 frame
  (threshold 0.8 passes 0.55 % of healthy U1 frames and only 10 of 16 idle ones). `empty_bed` is
  OFF by default; the bed check of `3d printer bed` (tier (a)) is the empty-plate detector.
- The two U1 frames of the panel incident: `u1_monitor.jpg` scores spaghetti 0.01, part_detached
  0.27, empty_bed 0.52 and `u1-spag.jpg` (panel lifted, strings hanging) spaghetti 0.22: tier (b)
  would **not** have caught the panel, and says so.

### Tier (b): device and cost (measured through `vision_model.ModelScorer`, 150 frames)

| device | median per frame (resize + inference) | p95 | VRAM | notes |
|---|---|---|---|---|
| CPU (6 threads, load average 6–12 from other agents) | 164 ms | 177 ms | 0 | 0.45 s to load |
| RTX 4060 (onnxruntime-gpu 1.31 CUDA provider, cuDNN 9, fp16 export) | 6.1 ms | 9.6 ms | +379 MiB peak | arena capped at 3 GiB; 0.34 s to load |

For comparison, tier (a) on this machine today (onnxruntime session, 2 threads, random 416 px / 224 px
input): Obico YOLOv2 124 ms on the CPU, 6.6 ms on the CUDA provider (+674 MiB); CLIP B/32 verifier
crop 55 ms on the CPU, 2.1 ms on CUDA (+280 MiB). `ObicoScorer` and `ClipVerifier` hard-code
`CPUExecutionProvider`, so tier (a) has no GPU path today; the numbers say one would be cheap, but
it is not wired (the tier (a) cost is 0.14 s per 10 s frame on the CPU, ~1.4 % of a core).

The two paths agree to |Δp| ≤ 0.008 per class on the 150 frames. Embedding all 11,381 frames of
the data set took 52.5 s on the GPU (4.6 ms per frame). After `close()` the session's arena is
returned; the CUDA context (about 100–300 MiB) stays until the process exits. Device choice:
`auto` uses CUDA only if onnxruntime has the CUDA provider **and** `nvidia-smi` reports ≥ 2 GiB
free (the desktop shares the card); `THREED_VISION_DEVICE=cpu|cuda|auto` or `watch.vision.model.device`
override; `cuda` without the provider is a structured error, never a silent CPU run. The CPU build
of onnxruntime is what `uv sync --extra vision` installs; for the GPU path
`uv pip install onnxruntime-gpu nvidia-cudnn-cu13 nvidia-cublas nvidia-cuda-runtime nvidia-curand
nvidia-cufft` (onnxruntime-gpu replaces the CPU package; match the CUDA major version of the
wheel).

### Tier (b): labelled frames and the fitted head

`3d printer vision fit-head --class part_detached --pos DIR.. --neg DIR..` embeds the frames once,
fits a ridge-logistic head on the unit-length embeddings (numpy, seconds), reports block
cross-validation AUROC and the recall at the out-of-fold `--fpr` threshold, and writes
`~/.cache/3d-cli/vision/models/tier-b-head.json`. The head replaces the zero-shot probability of
its class and its threshold turns the class on. Frames come from `3d printer vision record NAME DIR`
(U1, K1 or any Moonraker camera) sorted into folders by the user. The honest state today: ONE
event with two raw frames of the lifted panel (`u1_monitor.jpg`, `u1-spag.jpg`) cannot validate a
detachment head; the data set needs more such frames per printer, and the head must be judged on
held-out prints, not on the frames it was fitted on.

## Labelled frames, server training and publishing (`tools/vision_train`)

Fine-tuning does not run on the workstation. `tools/vision_train/` (dev tools, outside `lib/`, not
part of the `3d` runtime) builds the labelled set, trains on the user's own GPU server and
publishes; large files never enter git.

| step | command | what it does |
|---|---|---|
| build | `dataset.py build OUT [--third-party]` | own frames from `sources.json` (provenance, labels, splits, per-source crop) → sanitised JPEGs named by sha256, `labels.jsonl`, `sanitation.json`; `--third-party` adds the public collection for server training only |
| check / review | `dataset.py check OUT`, `dataset.py sheets OUT` | repeats every sanitation check on the published part; contact sheets for the human review |
| look at the server | `remote.py preflight` | read-only `nvidia-smi`, `free -m`, `df -h`, `uptime`, python/uv/docker/rsync presence; starts nothing, writes nothing |
| train | `remote.py plan RUN --dataset OUT` then `remote.py run RUN --dataset OUT --yes [--mode finetune] [--max-hours H] -- TRAIN_ARGS` | rsync `lib/`, `tools/`, the data to `~/3d-cli-train/RUN/`, one ssh session under `timeout --kill-after`, a trap that kills the children, then pulls only small artifacts (`--max-size=20m`, no `*.pt`/`*.onnx`/`*.safetensors`), also after a failed run |
| artifacts | `remote.py pull RUN [--weights]`, `remote.py clean RUN --yes [--all]` | weights come to `~/.cache/3d-cli/train/runs/RUN/` only with `--weights`; `clean` frees the venv and copies but keeps `out/` (the server-side copy) unless `--all` |
| publish | `publish.py whoami`, `publish.py dataset OUT [--publish]`, `publish.py model RUN_DIR --dataset-revision SHA [--weights FILE] [--publish]` | dry run by default; reads every uploaded file back at the commit and compares sha256 |

Host and paths: `THREED_TRAIN_HOST` (default `65.21.40.51`), `THREED_TRAIN_USER`,
`THREED_TRAIN_DIR` (default `~/3d-cli-train`); user, key and port come from `~/.ssh/config`. No
secret is sent to the server: the Hugging Face token stays on the workstation and weights are pushed
from there after `pull --weights`. `train.py` modes: `head` (embeds every frame once with the base CLIP
B/16, fits one logistic head per class on the train split, reports on the test split next to the
zero-shot prompts, refits on everything), `finetune` (unfreezes the last N vision blocks, classifier
initialised from the prompts so training starts at the zero-shot answer, `--export-onnx` writes a tower
the runtime loads), `eval`. Its metrics are `vision_detect._table` and `vision_fit.auroc`, the same
code `3d printer vision eval --model` runs (imported, not copied), per provenance (own vs third-party).

**What is in the set today (2026-10-22, `DATASET_SHA256` 306bd93d…).** 436 published own frames
(Snapmaker U1 and one K1 frame): 421 healthy (358 train / 63 test, six prints), 7 spaghetti, 7
empty bed, 1 `part_detached`; plus 36 own frames kept private (the face/person detector fired, almost
certainly false hits on printer parts, but a refusal is a refusal), of which one is the second
frame of the panel event. Each published frame was cropped to the bed area, re-encoded (no EXIF/ICC),
checked for QR codes (none found inside the crop; the chamber back wall with its stickers is outside
it) and for faces/people on the published bytes; all 15 contact sheets were looked at by hand and show
only the inside of the printers. Third-party frames (the public collection, licences differ) are
never published. Honest size: 2 frames of one panel event cannot train or validate a detachment
detector, so `finetune` on this set is not warranted; the pipeline exists to be re-run when more
frames are recorded (`vision record`) and labelled.

Local smoke runs (RTX 4060, the desktop holding ~3.7 GB, `--smoke`: 28 frames): `head` 8.8 s on the
GPU and the same AUROCs on the CPU path; `finetune --batch 8` 3 steps in 6.0 s with a peak of +1.23 GiB
VRAM (cap 3 GiB); the exported ONNX tower (345 MB, fp32) loads in `vision_model.ModelScorer`. These
check the pipeline, not a result.

**Where the artifacts live.** (1) Public Hugging Face repos, linked both ways with the GitHub repo:
dataset `alex-mextner/3d-cli-print-failure-frames` (CC-BY-4.0, own frames only) and model
`alex-mextner/3d-cli-print-failure-tier-b` (MIT, the base licence), every card naming the 3d-cli commit,
the base model revision and the dataset revision. (2) The server copy under `~/3d-cli-train/RUN/out/`.
(3) Git holds only small text: this documentation, `sources.json`, the cards' generator, metrics.
The PrintOps backup (`lib/printops/backup.py`) snapshots the Odroid's SQLite databases from inside
a container and cannot take workstation-side model files; it was not changed. When a head is first
published its Hugging Face revision and sha256 must be pinned in `lib/vision_model.py` (like
`TIER_B`); nothing is trained yet, so there is no pin. **Publishing is paused:** the Hugging Face
token on this workstation belongs to the account `Alex-Ultra` (write scope present), not to
`alex-mextner`, so `publish.py dataset` was only dry-run (440 files, 28.4 MB, sanitation clean).

## Not done (follow-ups)

- Layer shift / detachment by comparing the part silhouette with the G-code footprint
  (needs a bed↔image homography for the fixed camera).
- Masks for the purge bin and prime tower.
