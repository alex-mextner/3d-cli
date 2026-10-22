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
| `models` | model registry, checksums, offline-first `ensure`, explicit `fetch` |
| `calibrate` | honeycomb grid camera calibration, two-point scale, reference-object scale |
| `segment` | SAM2.1-tiny automatic masks + OWLv2 fill + CLIP background rejection; rembg single-object path |
| `detect` | OWLv2 open-vocabulary boxes |
| `recognise` | CLIP text prompts (class-mean calibrated) + bank retrieval |
| `bank` | thumbnails/silhouettes from meshes, embedding index, query |
| `fit_profile` | closed-form oriented rect + width profile (numpy, ms) |
| `fit_silhouette` | soft-rasteriser template fit (torch) + CMA-ES fallback |
| `view_check` | held-out-view IoU confidence |
| `register` | SIFT + RANSAC homography, relative pose, mask warp |

Adding a stage command: one module `commands/<name>.py` (`NAME`, `SUMMARY`, `add_arguments`, `run`) plus a
≈15-line `lib/commands/<name>.py` that calls `cli.vision_env.run_stage`.
