# `3d vision-models` — vision model weights

`3d vision-models status|pull` manages the weights the photo->3D vision stages need. Implemented by `threed_vision.models`
(`packages/threed-vision/src/threed_vision/models.py`).

**Why it exists.** The vision stages are offline-first: **nothing is ever downloaded at request/inference time.** A missing or corrupt
model raises `MissingDependency` (exit 127) whose fix line is the exact `3d vision-models pull <name>`. Fetching is a separate, explicit,
verified step.

## Usage

```
3d vision-models status [--verify] [--json]
3d vision-models pull [name ...] [--json]
```

```bash
3d vision-models status
3d vision-models status --verify --json
3d vision-models pull                        # everything that is missing
3d vision-models pull sam2-tiny owlv2-base --json
```

| model | files (in the cache dir) | source | licence | size |
|---|---|---|---|---|
| `sam2-tiny` | `sam2.1_hiera_tiny.pt` | `dl.fbaipublicfiles.com/segment_anything_2/092824/` | Apache-2.0 | 149 MB |
| `owlv2-base` | `owlv2-base-patch16-ensemble/` (HF snapshot, 8 files, loaded with `local_files_only=True`) | `google/owlv2-base-patch16-ensemble` @ `cfd3195b` | Apache-2.0 | 593 MB |
| `clip-b32` | `clipb32_vision.onnx`, `clip_text.onnx`, `clip_tokenizer.json` | `Xenova/clip-vit-base-patch32` @ `d15189d7` (`onnx/vision_model.onnx`, `onnx/text_model.onnx`, `tokenizer.json`) | MIT | 580 MB |
| `dinov2-s` | `dinov2s.onnx` | `Xenova/dinov2-small` @ `c2bb04a5` (`onnx/model.onnx`) | Apache-2.0 | 84 MB |
| `u2net` | `u2net.onnx`, `u2netp.onnx` | rembg release `v0.0.0` | Apache-2.0 / MIT | 172 MB |

Every file is pinned with size + sha256 in the registry (`models.MODELS`); Hugging Face URLs are pinned to a commit. The ONNX encoders
are the public Xenova exports: the sha256 of the files that were already in the 3d-cli research cache equals the Hugging Face LFS oid, so
no local export is needed. `clipb32_vision_fp16.onnx` is deliberately not registered (it does not load with default ORT graph
optimisation, and the fp32 tower runs at ~4 ms per crop).

## Behaviour

- **Cache dir:** `$THREED_VISION_MODELS_DIR`, default `~/.cache/3d-cli/vision/models`.
- **`pull` is idempotent:** verified files are skipped (`present`).
- **`pull` imports before downloading:** if a registered file (matching size AND sha256) already exists in a legacy location it is hard-linked
  (copied when the filesystem differs) instead of downloaded: `~/.cache/3d-cli/vision/models/research/`, `~/.cache/hsw-editor/spikes/vision/weights/`,
  `~/.cache/hsw-editor/u2net/`, the `~/.cache/hsw-editor/hf` OWLv2 snapshot, plus any directory in `$THREED_VISION_IMPORT_DIRS` (os.pathsep separated).
- **`pull` is atomic:** downloads go to `<file>.part<pid>`, size and sha256 are checked, then `os.replace`. A failed or mismatching download leaves nothing behind.
  A file lock serialises concurrent pulls.
- **Verification is cached:** the first lookup hashes the file; the result is stored in `<cache>/.verified.json` keyed by size + mtime, so later `model_path()` calls are a `stat()`.
- **`status`** reports per file `ok` (verified), `unverified` (size right, not hashed yet; `--verify` hashes), `missing`, `corrupt`.

## Python API

```python
from threed_vision import models

models.status()            # {"models_dir": str, "models": {name: {"installed","verified","path","size_bytes","licence","description",
                           #                                         "files":[{"rel","size","sha256","state"}],"pull"}}}
models.fetch(["sam2-tiny"], progress=lambda model, file, done, total: ...)   # {"models_dir", "results": {name: {rel: "present"|"imported-linked"|"imported-copied"|"downloaded"}}}
models.model_path("sam2-tiny")                      # verified Path or MissingDependency (exit 127, names `3d vision-models pull sam2-tiny`)
models.model_path("clip-b32", "clip_text.onnx")     # one file of a multi-file model
```

Exit codes: 0 ok, 2 unknown model name, 127 network failure while pulling (fix line explains `THREED_VISION_IMPORT_DIRS`).
