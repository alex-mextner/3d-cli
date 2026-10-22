# threed-vision

Photo -> 3D primitives. **Single source of truth** for the vision algorithms used by
the `3d` CLI (`lib/commands/*` are thin launchers via `lib/cli/vision_env.py`) and by the
hsw-editor vision service (`alex-mextner/hsw-editor`, `services/vision`, which depends on this
package and adds only orchestration, HTTP and caching).

Stages are pure modules with JSON-serialisable results; model-backed stages are classes with an
explicit `load()` / `unload()` lifecycle so a long-lived service keeps weights resident and a CLI
call pays the load once. Models are **never downloaded implicitly**: a missing file raises
`MissingDependency` (exit 127) naming the pull command; `THREED_VISION_MODELS_DIR` overrides the cache
(default `~/.cache/3d-cli/vision/models`).

Run any stage: `python -m threed_vision <stage> --help` (or `3d <stage>`). Exit codes: 0 ok,
2 bad input, 3 low confidence, 127 missing dependency/model. `--json` prints exactly one JSON object to stdout.

Install (venv outside the repo): `uv venv ~/.cache/3d-cli/vision/venv --python 3.12 &&
uv pip install --python ~/.cache/3d-cli/vision/venv/bin/python -e 'packages/threed-vision[gpu,seg,fit]'`
(torch/torchvision MUST come from the cu128 index: `uv pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128 --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match`; a default/cu130 torch leaves `onnxruntime-gpu` (pinned 1.23.x, CUDA 12) on CPU. `3d doctor`/`python -m threed_vision info` name the missing `.so`).

## Module map

| module | role |
|---|---|
| `io` | `load_rgb` (HEIC/EXIF), masks, polygons, overlays, `write_json` |
| `device` | `resolve_device`, `free_vram_mb`, `setup_ort_cuda`, `ort_diagnose` |
| `clip` | `ClipEncoder` (CLIP ViT-B/32 image + text), `DinoEncoder` (DINOv2-small), letterbox/prep/`crop_masked`; ONNX, CUDA provider with CPU fallback (`.providers` reports what is really active) |
| `models` | model registry (pinned files, sha256, licence), offline-first `model_path`, `status`, explicit atomic `fetch` that imports existing local copies |
| `calibrate` | honeycomb grid camera calibration, two-point scale, reference-object scale |
| `segment` | SAM2.1-tiny automatic masks + OWLv2 fill + CLIP background rejection; click-to-add/merge/delete helpers; u2net single-object path (own onnxruntime session, no rembg) |
| `detect` | OWLv2 open-vocabulary boxes |
| `recognise` | CLIP text prompts (class-mean calibrated) + bank retrieval |
| `bank` | thumbnails/silhouettes from meshes, embedding index, query |
| `fit_profile` | closed-form oriented rect + width profile (numpy, ms) |
| `loft` | the lathe / flat / box loft template (numpy) and the `fit` dict schema shared by everything below |
| `render` | independent hard cv2 rasteriser (`hard_mask`, `view_iou`), `mesh_from_fit`, `overlay` |
| `fit_init` | closed-form 3-D start + ranked pose candidates (numpy) |
| `soft_raster` | differentiable convex-hull-union soft rasteriser + Adam `LoftFit` (torch, imported lazily) |
| `fit_silhouette` | `fit_object` (soft-silhouette fit, GPU->CPU) and `fit_cma` (CMA-ES second attempt) |
| `fit` | `fit_auto`: closed form first, soft refine only if needed, CMA only for low confidence |
| `view_check` | `check(fit, views)`: IoU of the fit in extra views, `needs_views`, single-photo sanity mode |
| `mesh_export` | `emit_mesh(fit, 'glb'\|'stl'\|'obj'\|'scad')`: watertight mesh in mm |
| `register` | SIFT + RANSAC homography, relative pose, mask warp |

Adding a stage command: one module `commands/<name>.py` (`NAME`, `SUMMARY`, `add_arguments`, `run`) plus a
≈15-line `lib/commands/<name>.py` that calls `cli.vision_env.run_stage`.

## Using the stages from a service (hsw-editor `services/vision`)

Weights live in `THREED_VISION_MODELS_DIR` (default `~/.cache/3d-cli/vision/models`); nothing downloads at request time. One-time setup is
`models.fetch()` (or `3d vision-models pull`). A long-lived process keeps engines resident and hands the SAME `ClipEncoder` / `OwlDetector` to every
stage that needs one; each engine has `load()`, `unload()`, `loaded`, `device`, `vram_estimate_mb`.

```python
from threed_vision import io, models
from threed_vision.clip import ClipEncoder
from threed_vision.detect import OwlDetector
from threed_vision.segment import Segmenter

models.status()                                # {"models_dir", "models": {name: {"installed", "verified", "files": [...], "pull"}}}
models.fetch(["sam2-tiny", "owlv2-base", "clip-b32"], progress=lambda model, file, done, total: None)   # explicit, idempotent, atomic

clip, owl = ClipEncoder("auto"), OwlDetector("auto")                   # device: "auto" (cuda only with enough free VRAM) | "cuda" | "cpu"
seg = Segmenter(device="auto", clip=clip, detector=owl)                # method="sam2"|"sam2-dense"|"u2net", fill="owlv2"|"none", reject_bg=True
rgb = io.load_rgb("tools.heic")
res = seg.run(rgb, progress=lambda stage, frac: None)                  # {"instances": [{"id","source","bbox_xywh","area_px","bg_prob","stability","mask"}], "method","device","timing_s","vram_mb"}
boxes = owl.detect(rgb, ["a hand tool", "pliers"], threshold=0.15, nms=0.5, max_area_frac=0.05)   # {"boxes": [{"xyxy","score","label"}], "timing_s"}
boxed = seg.masks_for_boxes(rgb, boxes["boxes"])                    # detector boxes -> SAM2 box-prompted masks (+ label/score/box_xyxy) = `3d detect --masks`
insts = seg.add_by_point(rgb, res["instances"], [[640, 410]])         # UI click-to-add; add_by_box(rgb, insts, [x0, y0, x1, y1])
insts = Segmenter.merge(insts, [3, 7]); insts = Segmenter.delete(insts, [5]); Segmenter.pick(insts, 640, 410)   # click-to-merge / delete / which instance is under (x, y)
seg.unload(); owl.unload(); clip.unload()                              # really frees VRAM
```

Errors are `threed_vision.errors.VisionError` subclasses with `exit_code`: `BadInput` (2), `LowConfidence` (3), `MissingDependency` (127: package,
GPU runtime or model missing; `.render()` includes the fix such as `3d vision-models pull sam2-tiny`). GPU out-of-memory on `device="auto"` falls back
to CPU transparently (`res["device"]` says what ran); an explicit `device="cuda"` raises instead. See `docs/commands/segment.md` for measurements.
