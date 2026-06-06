# Mac-Friendly Image-to-3D Providers for Proxy Pipeline

Date: 2026-06-06

Machine tested:

- macOS 15.6.1 arm64
- Apple M4 Pro
- 24 GB unified memory
- Torch MPS available with `torch 2.12.0`
- MLX GPU available with `mlx`

Goal: identify practical image-to-3D providers that can feed `3d proxy-align` /
`fit-camera` with an approximate reference mesh. The provider output should be GLB, OBJ,
PLY, STL, or another mesh format that `trimesh` can load. USDZ is useful for Telegram /
Apple Quick Look proofs, but textured GLB is still the better intermediate for the proxy
pipeline.

## Recommendation Ranking

This ranking is provisional. It is based on import/build/API smoke tests, not on completed
local full inference, GLB quality inspection, `trimesh` loading, `3d proxy-align`, or
fit-camera proof. A provider must complete those gates before it becomes the default
implementation choice.

1. Hunyuan3D 2.1 MLX port (`dgrauet/Hunyuan3D-2.1-mlx`)

   First local Mac experiment to continue on this M4 Pro, not an accepted provider. The shape pipeline imports
   successfully with MLX GPU and Torch MPS on this machine. The MLX weight repo is not
   gated and publishes Apple-specific INT8/INT4 memory guidance. Full weights are about
   15.06 GB; shape-only starts with `dit.safetensors` at about 6.10 GB. The upstream notes
   claim FP16 shape peak around 10 GB, INT8 around 6 GB, INT4 around 4 GB, with Stage 2
   paint adding about 6 GB. This is the only candidate here that looks both Mac-native and
   realistic on 24 GB, but it is not promoted until it produces a GLB that passes the proxy
   validation gates. License caveat: Hunyuan3D 2.1 uses Tencent's community license; check
   territory and commercial-scale terms before product use.

2. Stable Fast 3D (`Stability-AI/stable-fast-3d`)

   Fast optional experiment to keep, but not an accepted provider or default local path on this machine. It has
   official MPS support, and the native `uv_unwrapper` / `texture_baker` packages compile
   on this M4 Pro after installing Torch first. `SF3D` imports successfully with pinned
   NumPy 1.26.4 and `rembg[cpu]`. The model is gated on Hugging Face and the weights are
   about 4.03 GB. Official docs recommend CPU if the system has less than 32 GB unified
   memory because MPS consumes more memory, so this 24 GB M4 Pro is a risk for MPS full
   inference. Still worth supporting as a gated experiment after snapshot/offline wiring.
   License caveat: SF3D is
   gated under Stability's community license; enterprise/commercial terms may require a
   separate license.

3. TRELLIS.2 Apple Silicon community ports (`shivampkumar/trellis-mac`, related forks)

   Best quality ambition, highest operational risk. The official Microsoft TRELLIS.2 is
   Linux + NVIDIA only, requiring at least 24 GB VRAM and CUDA/native packages. The Mac
   port `shivampkumar/trellis-mac` imports its `generate.py` successfully with Torch MPS
   on this machine, but its setup is heavier: it clones TRELLIS.2 and several Metal /
   Apple backend repos, and it requires Hugging Face auth plus gated access to DINOv3 and
   BRIA RMBG-2.0. The model checkpoints in `microsoft/TRELLIS.2-4B` total about 16.24 GB
   from HF file metadata. Treat as experimental until a full image-to-GLB run succeeds.
   License caveat: official TRELLIS.2 code is MIT, but the Mac port and all gated upstream
   model dependencies still need recorded license identifiers before product use.

4. Hosted Hugging Face ZeroGPU TRELLIS / SF3D

   Useful as an optional provider, not as a reliable default. Public Gradio APIs are
   discoverable without a token. In practice, TRELLIS ZeroGPU rejected the unauthenticated
   request with a quota error, and the SF3D public Space raised an upstream app exception.
   For automation, use a least-privilege read `HF_TOKEN`, PRO/team quota, or a first-party
   hosted Space that forwards `X-IP-Token` correctly. The provider contract should cache
   returned GLB artifacts and record queue/quota failures rather than blocking local tests.

## Tested Commands

Safety and reproducibility notes:

- The commands below record what was tested during this snapshot and are not copy/paste
  automation recipes. Before rerunning them, clone into a fresh owned directory from
  `mktemp -d`, pin every third-party repository to a reviewed full source commit SHA, pin
  every Hugging Face model to a snapshot commit SHA, verify the checked-out revisions, and
  record them in provider metadata. Tags, branches, and moving HF refs are not acceptable
  automation pins.
- These snippets execute third-party code and, for SF3D, build native extensions. Do not
  run them from mutable default branches in automation.
- Do not run public/provider code with a broad personal Hugging Face token. Use a
  least-privilege read token, an isolated `HF_HOME`, a `chmod 600` token file scoped to the
  test directory, and per-run cache permissions.
- Prefer prefetching gated weights into the isolated `HF_HOME`, then unsetting `HF_TOKEN`
  and running inference offline so mutable provider code does not receive the token.
- The fixed `/tmp/3d-*` paths below are historical artifact paths from the smoke run. New
  automation must use fresh per-run directories and must clean them up or preserve them as
  declared artifacts.
- Reviewed reference images must also have provenance. Use a version-controlled curated
  test image, a reproducibly generated synthetic render, or metadata that records the
  source URL/path, license, hash, and intended use. An unknown input image provenance keeps
  the provider result rejected.
- Token files, HF auth caches, and any directory containing credentials must never be
  preserved as artifacts.

Canonical provider metadata must be written before any generated mesh is considered for
`proxy-align` or `fit-camera`:

```json
{
  "schema_version": 1,
  "status_contract": {
    "generation_status_values": ["success", "warning", "failure", "diagnostic-only"],
    "proxy_align_status_values": ["not-run", "success", "warning", "failure", "diagnostic-only"],
    "unknown_versions_or_fields": "fail-closed",
    "warning_acceptance": "rejected_for_camera_priors_in_schema_v1"
  },
  "provider": "hunyuan-mlx|sf3d-local|trellis-mac|hf-space|manual",
  "generation_status": "success|warning|failure|diagnostic-only",
  "reject_reasons": ["not_evaluated"],
  "proxy_align": {
    "status": "not-run|success|warning|failure|diagnostic-only",
    "accepted_for_fit_camera": false,
    "reject_reasons": ["not_run"],
    "output_mesh": "<aligned/proxy mesh path or null>",
    "metrics_json": "<path or null>",
    "proof_artifacts": {
      "original_reference": "<path>",
      "reference_mask_segmentation": "<path or null>",
      "same_frame_render": "<path or null>",
      "overlay": "<path or null>",
      "metrics_json": "<path or null>"
    }
  },
  "source_repo": "https://example/repo.git",
  "source_revision": "<full immutable git commit sha or null>",
  "source_dirty": false,
  "reference_input": {
    "path": "<path>",
    "sha256": "sha256:...",
    "source": "repo fixture|synthetic render|local file|url",
    "license_status": "redistributable|local-only|unknown"
  },
  "model_repo": "org/model-or-null",
  "model_revision": "<full immutable HF snapshot commit sha or null>",
  "model_revision_verified": false,
  "artifact_manifest": [
    {
      "path": "<weight-or-native-generated-proof-or-metrics-artifact-path>",
      "sha256": "sha256:...",
      "size_bytes": 0,
      "source": "hf snapshot|native build|generated mesh|aligned proxy mesh|proof image|metrics json|manual"
    }
  ],
  "weight_hash_policy": "per-file sha256 and size are required for every consumed weight/native artifact",
  "code_license_identifier": "SPDX-or-explicit-code-license-name",
  "model_license_identifier": "SPDX-or-explicit-model-license-name",
  "license_status": "redistributable|local-only|unknown",
  "license_caveats": ["commercial threshold or unresolved dependency license"],
  "dependencies": [
    {
      "name": "dependency-name",
      "source_repo": "https://example/dependency.git",
      "source_revision": "<full immutable commit sha>",
      "model_repo": "org/dependency-model-or-null",
      "model_revision": "<full immutable HF snapshot sha or null>",
      "license_identifier": "SPDX-or-explicit-license-name"
    }
  ],
  "native_build": {"built": false, "inputs": []},
  "device_backend": "mlx|mps|cpu|cuda|hosted|manual",
  "cache_path": "<per-run cache path>",
  "token_mode": "none|prefetch-only|passed-to-provider",
  "output_mesh": "<per-run artifact path or null>",
  "mesh_validation": {
    "validation_status": "not-run|passed|failed",
    "loads_with_trimesh": false,
    "nonempty_geometry": false,
    "finite_vertices": false,
    "valid_faces": false,
    "sane_bounds_scale": false,
    "component_fragmentation_ok": false,
    "triangle_count_ok": false,
    "texture_material_ok": false,
    "manifold_policy": "pass|repairable|not-required|fail"
  },
  "metrics_json": "<path or null>",
  "proof_artifacts": {
    "original_reference": "<path>",
    "reference_mask_segmentation": "<path or null>",
    "same_frame_render": "<path or null>",
    "overlay": "<path or null>",
    "metrics_json": "<path or null>"
  }
}
```

Metadata defaults to rejected: `generation_status=diagnostic-only`,
`proxy_align.accepted_for_fit_camera=false`, and at least one `reject_reasons` entry.
Missing metadata, unknown status labels, missing proof artifacts, missing metrics, a failed
`trimesh` load, unverified source/model revisions, unresolved license identifiers,
threshold failures, or failed visual review are hard rejects. `fit-camera` may consume a
generated proxy only when `generation_status=success`, `proxy_align.status=success`, and
`proxy_align.accepted_for_fit_camera=true`.

The schema is closed for consumers. Unknown `schema_version`, unknown status enum values,
missing required fields, ambiguous `output_mesh` placement, missing artifact hashes, or
missing reference provenance must fail closed. Top-level `output_mesh` is the generated
provider mesh. Any aligned or transformed mesh produced by `proxy-align` must be recorded
under `proxy_align.output_mesh`; consumers reject when the expected field is missing.
`warning` remains a rejected status for camera-prior use in schema v1; it is useful only
for diagnostics that should be visible to humans. `artifact_manifest: []` is legal only
for `generation_status=diagnostic-only` metadata that includes
`artifact_manifest_missing` in `reject_reasons`. Any non-diagnostic output must record a
per-file manifest for consumed weights, native build artifacts, generated meshes, proof
images, and metrics. The metadata snippets below are diagnostic examples until every
required field is populated with immutable revisions, hashes, dependency licenses, proof
artifacts, and mesh validation results.
Mesh validation booleans are pass flags: `true` means the named check ran and passed;
`false` in diagnostic metadata means the gate is not passed and may be unrun, failed, or
unreported. Accepted outputs must set `mesh_validation.validation_status=passed` and
provide the corresponding validation metrics or logs.

Hardware and runtime:

```bash
uname -a
sysctl -n machdep.cpu.brand_string hw.memsize hw.optional.arm64
python3 - <<'PY'
import platform
print(platform.platform())
try:
    import torch
    print("torch", torch.__version__, "mps", torch.backends.mps.is_available())
except Exception as exc:
    print("torch unavailable", type(exc).__name__, exc)
try:
    import mlx.core as mx
    print("mlx", mx.default_device())
except Exception as exc:
    print("mlx unavailable", type(exc).__name__, exc)
PY
```

Torch MPS smoke:

```bash
uv run --with torch --with torchvision python - <<'PY'
import torch
print("torch", torch.__version__)
print("mps_available", torch.backends.mps.is_available())
print("mps_built", torch.backends.mps.is_built())
PY
```

Result: `torch 2.12.0`, `mps_available True`, `mps_built True`.

Hunyuan3D 2.1 MLX import smoke:

Historical command that was run for this snapshot:

```bash
git clone --depth 1 https://github.com/dgrauet/Hunyuan3D-2.1-mlx.git /tmp/3d-model-providers/Hunyuan3D-2.1-mlx
cd /tmp/3d-model-providers/Hunyuan3D-2.1-mlx
uv run \
  --with mlx \
  --with mlx-arsenal \
  --with torch \
  --with torchvision \
  --with transformers \
  --with diffusers \
  --with accelerate \
  --with safetensors \
  --with huggingface_hub \
  --with pillow \
  --with trimesh \
  --with scipy \
  --with scikit-image \
  --with pymcubes \
  --with einops \
  --with omegaconf \
  --with pyyaml \
  --with pymeshlab \
  --with opencv-python \
  python - <<'PY'
import torch
import mlx.core as mx
print("torch", torch.__version__, "mps", torch.backends.mps.is_available())
print("mlx", mx.default_device())
from hy3dshape.hy3dshape.pipeline_mlx import ShapePipeline
print("hunyuan ShapePipeline import ok")
PY
```

Result: `hunyuan ShapePipeline import ok`.

Pinned rerun shape for automation:

```bash
WORKDIR="$(mktemp -d)"
git clone https://github.com/dgrauet/Hunyuan3D-2.1-mlx.git "$WORKDIR/Hunyuan3D-2.1-mlx"
cd "$WORKDIR/Hunyuan3D-2.1-mlx"
export HUNYUAN_MLX_SOURCE_SHA="<reviewed-full-source-commit-sha>"
git checkout "$HUNYUAN_MLX_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$HUNYUAN_MLX_SOURCE_SHA"
# Run the same import smoke only after recording this revision in provider metadata.
```

Blocked automation shape for Hunyuan full shape smoke, after accepting disk/time cost and
choosing reviewed source/model revisions:

Blocker: this is not approved automation until the provider can prefetch the exact HF
snapshot into per-run `HF_HOME`, run from that local snapshot with `HF_TOKEN` unset and
`HF_HUB_OFFLINE=1`, and record the per-file artifact manifest. Until then the metadata
must keep `generation_status=diagnostic-only`.

```bash
WORKDIR="$(mktemp -d)"
git clone https://github.com/dgrauet/Hunyuan3D-2.1-mlx.git "$WORKDIR/Hunyuan3D-2.1-mlx"
cd "$WORKDIR/Hunyuan3D-2.1-mlx"
export HUNYUAN_MLX_SOURCE_SHA="<reviewed-full-source-commit-sha>"
export HUNYUAN_MLX_MODEL_REVISION="<reviewed-full-hf-snapshot-commit-sha>"
export HUNYUAN_MLX_REF="$WORKDIR/reference.jpg"
export HUNYUAN_MLX_OUTPUT="$WORKDIR/artifacts/hunyuan-shape.glb"
export HUNYUAN_MLX_PROVIDER_METADATA="$WORKDIR/provider-metadata.json"
export HF_HOME="$WORKDIR/hf-home"
export HF_HUB_OFFLINE=1
unset HF_TOKEN
git checkout "$HUNYUAN_MLX_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$HUNYUAN_MLX_SOURCE_SHA"
mkdir -p "$WORKDIR/artifacts"
cp <reviewed-reference-image> "$HUNYUAN_MLX_REF"
uv run <deps above> python - <<'PY'
import json
import os
import subprocess

import trimesh

from hy3dshape.hy3dshape.pipeline_mlx import ShapePipeline

source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
source_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
model_revision = os.environ["HUNYUAN_MLX_MODEL_REVISION"]
# This is illustrative only until the MLX provider accepts a local snapshot path instead
# of resolving a model repo online. Keep the generated metadata rejected until then.
pipe = ShapePipeline.from_pretrained(
    "dgrauet/hunyuan3d-2.1-mlx",
    revision=model_revision,
)
mesh = pipe(
    os.environ["HUNYUAN_MLX_REF"],
    num_inference_steps=20,
    guidance_scale=7.5,
    octree_resolution=128,
)
mesh.export(os.environ["HUNYUAN_MLX_OUTPUT"])
trimesh.load(os.environ["HUNYUAN_MLX_OUTPUT"])
with open(os.environ["HUNYUAN_MLX_PROVIDER_METADATA"], "w", encoding="utf-8") as fh:
    json.dump(
        {
            "schema_version": 1,
            "status_contract": {
                "generation_status_values": ["success", "warning", "failure", "diagnostic-only"],
                "proxy_align_status_values": ["not-run", "success", "warning", "failure", "diagnostic-only"],
                "unknown_versions_or_fields": "fail-closed",
                "warning_acceptance": "rejected_for_camera_priors_in_schema_v1",
            },
            "provider": "hunyuan-mlx",
            "generation_status": "diagnostic-only",
            "reject_reasons": [
                "artifact_manifest_missing",
                "dependency_provenance_incomplete",
                "proof_artifacts_missing",
                "license_identifier_unresolved",
                "model_revision_not_verified_by_snapshot_or_hash",
            ],
            "proxy_align": {
                "status": "not-run",
                "accepted_for_fit_camera": False,
                "reject_reasons": ["not_run"],
                "output_mesh": None,
                "metrics_json": None,
                "proof_artifacts": {
                    "original_reference": os.environ["HUNYUAN_MLX_REF"],
                    "reference_mask_segmentation": None,
                    "same_frame_render": None,
                    "overlay": None,
                    "metrics_json": None,
                },
            },
            "source_repo": "https://github.com/dgrauet/Hunyuan3D-2.1-mlx",
            "source_revision": source_revision,
            "source_dirty": source_dirty,
            "reference_input": {
                "path": os.environ["HUNYUAN_MLX_REF"],
                "sha256": "TODO",
                "source": "reviewed-reference-image",
                "license_status": "unknown",
            },
            "model_repo": "dgrauet/hunyuan3d-2.1-mlx",
            "model_revision": model_revision,
            "model_revision_verified": False,
            "artifact_manifest": [],
            "weight_hash_policy": "per-file sha256 and size required before verification",
            "code_license_identifier": "UNRESOLVED: dgrauet/Hunyuan3D-2.1-mlx license must be recorded",
            "model_license_identifier": "Tencent Hunyuan3D 2.1 Community License",
            "license_status": "unknown",
            "license_caveats": ["MLX port license unresolved"],
            "dependencies": [
                {
                    "name": "mlx/torch/huggingface_hub stack",
                    "source_repo": None,
                    "source_revision": None,
                    "model_repo": None,
                    "model_revision": None,
                    "license_identifier": "UNRESOLVED",
                }
            ],
            "native_build": {"built": False, "inputs": []},
            "device_backend": "mlx",
            "cache_path": os.environ.get("HF_HOME"),
            "token_mode": "none",
            "output_mesh": os.environ["HUNYUAN_MLX_OUTPUT"],
            "mesh_validation": {
                "validation_status": "not-run",
                "loads_with_trimesh": True,
                "nonempty_geometry": False,
                "finite_vertices": False,
                "valid_faces": False,
                "sane_bounds_scale": False,
                "component_fragmentation_ok": False,
                "triangle_count_ok": False,
                "texture_material_ok": False,
                "manifold_policy": "not-required",
            },
            "metrics_json": None,
            "proof_artifacts": {
                "original_reference": os.environ["HUNYUAN_MLX_REF"],
                "reference_mask_segmentation": None,
                "same_frame_render": None,
                "overlay": None,
                "metrics_json": None,
            },
        },
        fh,
        indent=2,
    )
PY
```

Stable Fast 3D import/build smoke.

Historical command that was run for this snapshot:

```bash
git clone --depth 1 https://github.com/Stability-AI/stable-fast-3d.git /tmp/3d-model-providers/stable-fast-3d
cd /tmp/3d-model-providers/stable-fast-3d
uv venv /tmp/3d-sf3d-venv --python 3.11
/tmp/3d-sf3d-venv/bin/python -m ensurepip --upgrade
/tmp/3d-sf3d-venv/bin/python -m pip install -U setuptools==69.5.1 wheel
/tmp/3d-sf3d-venv/bin/python -m pip install torch torchvision numpy==1.26.4
/tmp/3d-sf3d-venv/bin/python -m pip install --no-build-isolation ./uv_unwrapper ./texture_baker
/tmp/3d-sf3d-venv/bin/python -m pip install \
  einops==0.7.0 \
  jaxtyping==0.2.31 \
  omegaconf==2.3.0 \
  transformers==4.42.3 \
  open_clip_torch==2.24.0 \
  trimesh==4.4.1 \
  huggingface-hub==0.23.4 \
  'rembg[cpu]==2.0.57' \
  pynanoinstantmeshes==0.0.3 \
  gpytoolbox==0.2.0 \
  pillow
PYTORCH_ENABLE_MPS_FALLBACK=1 /tmp/3d-sf3d-venv/bin/python - <<'PY'
import torch
import numpy as np
print("torch", torch.__version__, "mps", torch.backends.mps.is_available(), "numpy", np.__version__)
from sf3d.system import SF3D
print("SF3D import ok")
PY
```

Result: native packages built as `macosx_11_0_arm64` wheels and `SF3D import ok`.

Pinned rerun shape for automation:

```bash
WORKDIR="$(mktemp -d)"
git clone https://github.com/Stability-AI/stable-fast-3d.git "$WORKDIR/stable-fast-3d"
cd "$WORKDIR/stable-fast-3d"
export SF3D_SOURCE_SHA="<reviewed-full-source-commit-sha>"
git checkout "$SF3D_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$SF3D_SOURCE_SHA"
# Build native extensions only after recording this revision in provider metadata.
```

Recommended automation command for SF3D full smoke, after HF gated access and reviewed
source/model revisions:

```bash
WORKDIR="$(mktemp -d)"
git clone https://github.com/Stability-AI/stable-fast-3d.git "$WORKDIR/stable-fast-3d"
cd "$WORKDIR/stable-fast-3d"
export SF3D_SOURCE_SHA="<reviewed-full-source-commit-sha>"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export HF_HOME="$WORKDIR/hf-home"
export HF_TOKEN_FILE="$WORKDIR/hf-read-token"
export SF3D_MODEL_REVISION="<reviewed-full-hf-snapshot-commit-sha>"
export SF3D_REF="$WORKDIR/reference.jpg"
export SF3D_OUTPUT_DIR="$WORKDIR/artifacts/sf3d-local"
export SF3D_PROVIDER_METADATA="$WORKDIR/provider-metadata.json"
git checkout "$SF3D_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$SF3D_SOURCE_SHA"
mkdir -p "$HF_HOME"
chmod 700 "$HF_HOME"
mkdir -p "$SF3D_OUTPUT_DIR"
cp <reviewed-reference-image> "$SF3D_REF"
uv venv "$WORKDIR/sf3d-venv" --python 3.11
source "$WORKDIR/sf3d-venv/bin/activate"
python -m pip install -U setuptools==69.5.1 wheel
python -m pip install torch torchvision numpy==1.26.4
python -m pip install --no-build-isolation ./uv_unwrapper ./texture_baker
python -m pip install huggingface-hub trimesh
mkdir -p "$WORKDIR/trusted-hf-prefetch"
cd "$WORKDIR/trusted-hf-prefetch"
umask 077
printf '%s' '<least-privilege-read-token>' > "$HF_TOKEN_FILE"
chmod 600 "$HF_TOKEN_FILE"
export SF3D_SNAPSHOT_PATH="$(python - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

token = Path(os.environ["HF_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
print(snapshot_download(
    "stabilityai/stable-fast-3d",
    revision=os.environ["SF3D_MODEL_REVISION"],
    token=token,
))
PY
)"
cd "$WORKDIR/stable-fast-3d"
rm -f "$HF_TOKEN_FILE"
export HF_HUB_OFFLINE=1
# Do not run inference until `run.py` is configured to consume `$SF3D_SNAPSHOT_PATH` or
# another local path that resolves to `$SF3D_MODEL_REVISION`. If the provider cannot force
# that path, record `model_revision_verified=false` and keep the proxy rejected.
# Example shape after wiring:
# python run.py "$SF3D_REF" \
#   --pretrained-model "$SF3D_SNAPSHOT_PATH" \
#   --device mps \
#   --texture-resolution 512 \
#   --output-dir "$SF3D_OUTPUT_DIR"
python - <<'PY'
import json
import os
import subprocess

source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
source_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
output_mesh = os.path.join(os.environ["SF3D_OUTPUT_DIR"], "mesh.glb")
with open(os.environ["SF3D_PROVIDER_METADATA"], "w", encoding="utf-8") as fh:
    json.dump(
        {
            "schema_version": 1,
            "status_contract": {
                "generation_status_values": ["success", "warning", "failure", "diagnostic-only"],
                "proxy_align_status_values": ["not-run", "success", "warning", "failure", "diagnostic-only"],
                "unknown_versions_or_fields": "fail-closed",
                "warning_acceptance": "rejected_for_camera_priors_in_schema_v1",
            },
            "provider": "sf3d-local",
            "generation_status": "diagnostic-only",
            "reject_reasons": [
                "artifact_manifest_missing",
                "dependency_provenance_incomplete",
                "inference_not_run_until_local_snapshot_is_wired",
            ],
            "proxy_align": {
                "status": "not-run",
                "accepted_for_fit_camera": False,
                "reject_reasons": ["not_run"],
                "output_mesh": None,
                "metrics_json": None,
                "proof_artifacts": {
                    "original_reference": os.environ["SF3D_REF"],
                    "reference_mask_segmentation": None,
                    "same_frame_render": None,
                    "overlay": None,
                    "metrics_json": None,
                },
            },
            "source_repo": "https://github.com/Stability-AI/stable-fast-3d",
            "source_revision": source_revision,
            "source_dirty": source_dirty,
            "reference_input": {
                "path": os.environ["SF3D_REF"],
                "sha256": "TODO",
                "source": "reviewed-reference-image",
                "license_status": "unknown",
            },
            "model_repo": "stabilityai/stable-fast-3d",
            "model_revision": os.environ["SF3D_MODEL_REVISION"],
            "model_revision_verified": False,
            "artifact_manifest": [],
            "weight_hash_policy": "per-file sha256 and size required before verification",
            "code_license_identifier": "Stability AI Community License",
            "model_license_identifier": "Stability AI Community License",
            "license_status": "unknown",
            "license_caveats": ["gated model terms and commercial thresholds must be recorded"],
            "dependencies": [
                {
                    "name": "torch/huggingface_hub/trimesh/native build stack",
                    "source_repo": None,
                    "source_revision": None,
                    "model_repo": None,
                    "model_revision": None,
                    "license_identifier": "UNRESOLVED",
                }
            ],
            "native_build": {
                "built": True,
                "inputs": [
                    {"path": "uv_unwrapper", "source_revision": source_revision},
                    {"path": "texture_baker", "source_revision": source_revision}
                ],
            },
            "device_backend": "mps",
            "cache_path": os.environ["HF_HOME"],
            "token_mode": "prefetch-only",
            "output_mesh": None,
            "mesh_validation": {
                "validation_status": "not-run",
                "loads_with_trimesh": False,
                "nonempty_geometry": False,
                "finite_vertices": False,
                "valid_faces": False,
                "sane_bounds_scale": False,
                "component_fragmentation_ok": False,
                "triangle_count_ok": False,
                "texture_material_ok": False,
                "manifold_policy": "not-required",
            },
            "metrics_json": None,
            "proof_artifacts": {
                "original_reference": os.environ["SF3D_REF"],
                "reference_mask_segmentation": None,
                "same_frame_render": None,
                "overlay": None,
                "metrics_json": None,
            },
        },
        fh,
        indent=2,
    )
PY
```

If MPS runs out of memory on this 24 GB machine, the CPU path is still illustrative until
the same immutable local snapshot wiring exists. Do not run it as an accepted provider
path without `HF_HUB_OFFLINE=1`, `$SF3D_SNAPSHOT_PATH`, manifest hashes, and the same
proxy-align proof gate:

```bash
export HF_HUB_OFFLINE=1
# SF3D_USE_CPU=1 python run.py "$SF3D_REF" \
#   --pretrained-model "$SF3D_SNAPSHOT_PATH" \
#   --texture-resolution 512 \
#   --output-dir "$WORKDIR/artifacts/sf3d-cpu"
```

TRELLIS.2 Mac import smoke.

Historical command that was run for this snapshot:

```bash
git clone --depth 1 https://github.com/shivampkumar/trellis-mac.git /tmp/3d-model-providers/trellis-mac
cd /tmp/3d-model-providers/trellis-mac
uv run \
  --with torch \
  --with torchvision \
  --with transformers \
  --with accelerate \
  --with huggingface_hub \
  --with safetensors \
  --with pillow \
  --with numpy \
  --with trimesh \
  --with scipy \
  python - <<'PY'
import torch
print("torch", torch.__version__, "mps", torch.backends.mps.is_available())
import generate
print("trellis-mac generate import ok")
PY
```

Result: `trellis-mac generate import ok`.

Pinned rerun shape for automation:

```bash
WORKDIR="$(mktemp -d)"
git clone https://github.com/shivampkumar/trellis-mac.git "$WORKDIR/trellis-mac"
cd "$WORKDIR/trellis-mac"
export TRELLIS_MAC_SOURCE_SHA="<reviewed-full-source-commit-sha>"
git checkout "$TRELLIS_MAC_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$TRELLIS_MAC_SOURCE_SHA"
# Run import/full smoke only after recording this revision in provider metadata.
```

ZeroGPU / public Gradio API smoke:

```bash
uv run --with gradio_client python - <<'PY'
from gradio_client import Client
for space in ["trellis-community/TRELLIS", "stabilityai/stable-fast-3d"]:
    print("---", space)
    client = Client(space)
    print(client.view_api(return_format="dict").keys())
PY
```

Result: both APIs are discoverable without an HF token.

TRELLIS ZeroGPU generation attempt:

```bash
uv run --with gradio_client python - <<'PY'
from gradio_client import Client, handle_file
c = Client("trellis-community/TRELLIS")
c.predict(
    handle_file("/tmp/3d-image3d-smoke/ref_block_rgb.jpg"),
    [],
    1,
    7.5,
    4,
    3.0,
    4,
    "stochastic",
    0.95,
    512,
    api_name="/generate_and_extract_glb",
)
PY
```

Result: failed with `AppError: You have exceeded your ZeroGPU quota ... Authenticate with
a Hugging Face token for more quota`.

Stable Fast 3D public Space generation attempt:

```bash
uv run --with gradio_client python - <<'PY'
from gradio_client import Client, handle_file
c = Client("stabilityai/stable-fast-3d")
c.predict(
    handle_file("/tmp/3d-image3d-smoke/ref_block_rgb.jpg"),
    0.85,
    "None",
    -1,
    512,
    api_name="/run_button",
)
PY
```

Result: failed with an upstream Gradio app exception and no verbose error.

## Output Formats

- Hunyuan MLX Stage 1 can export GLB via `mesh.export("output.glb")`.
- Hunyuan MLX Stage 2 writes textured OBJ and can save GLB.
- SF3D writes `mesh.glb`.
- TRELLIS Space and TRELLIS.2 write GLB; TRELLIS Space can also return gaussian PLY.
- `3d proxy-align` should consume GLB directly through `trimesh`.
- `3d usdz` currently accepts SCAD/STL, not GLB. For generated textured GLB proof in
  Telegram, either add a GLB-to-USDZ path or convert GLB to an intermediate STL and accept
  texture loss. The texture-preserving path should use USD tooling or Apple Reality
  Converter rather than the current STL-only converter.

## Artifacts

Local test image:

- `/tmp/3d-image3d-smoke/ref_block.png`
- `/tmp/3d-image3d-smoke/ref_block_rgb.jpg`

Provider API metadata:

- `/tmp/3d-image3d-smoke/stable-fast-3d/metadata.json`
- `/tmp/3d-image3d-smoke/stable-fast-3d-rgb/metadata.json`
- `/tmp/3d-image3d-smoke/trellis-zerogpu/metadata.json`

Temporary provider clones:

- `/tmp/3d-model-providers/stable-fast-3d`
- `/tmp/3d-model-providers/Hunyuan3D-2.1-mlx`
- `/tmp/3d-model-providers/trellis-mac`

No generated USDZ artifact exists yet because ZeroGPU did not return a GLB, and local full
inference was intentionally stopped before downloading gated/large weights.

## License and Provenance Gaps

These are not optional paperwork. A provider is blocked until every row it depends on has a
recorded license identifier, immutable source/model revision, and usage caveat in provider
metadata.

| Provider path | Code provenance | Model / dependency provenance | License status |
| --- | --- | --- | --- |
| `hunyuan-mlx` | `dgrauet/Hunyuan3D-2.1-mlx` at full commit SHA | `dgrauet/hunyuan3d-2.1-mlx` at HF snapshot SHA | Upstream Hunyuan3D 2.1 community license; MLX port license must be recorded before product use. |
| `sf3d-local` | `Stability-AI/stable-fast-3d` at full commit SHA plus local native build inputs | `stabilityai/stable-fast-3d` at HF snapshot SHA | Stability AI Community License; gated model terms and commercial thresholds must be recorded. |
| `trellis-mac` | `shivampkumar/trellis-mac` at full commit SHA plus every cloned backend repo SHA | `microsoft/TRELLIS.2-4B`, DINOv3, BRIA RMBG-2.0 at HF snapshot SHAs | Official TRELLIS.2 code is MIT; Mac port, DINOv3, BRIA RMBG-2.0, and backend repos still need explicit license identifiers. |
| `hf-space` | Space owner/revision and app source revision when available | Returned artifact metadata, model repo/revision when exposed | Hosted Spaces are not reproducible unless app/model revision and quota/auth status are captured. |
| `manual` | User-supplied path plus optional source URI | User-supplied mesh provenance | Must stay rejected until provenance, local-only distribution limits, mesh validation, and proof gates are recorded. Local-only does not bypass camera-prior validation. |

## Sources

- Stable Fast 3D model card: https://huggingface.co/stabilityai/stable-fast-3d
- Stable Fast 3D code: https://github.com/Stability-AI/stable-fast-3d
- Stable Fast 3D license: https://github.com/Stability-AI/stable-fast-3d/blob/main/LICENSE.md
- Hunyuan3D 2.1 MLX code: https://github.com/dgrauet/Hunyuan3D-2.1-mlx
- Hunyuan3D 2.1 MLX weights: https://huggingface.co/dgrauet/hunyuan3d-2.1-mlx
- Hunyuan3D 2.1 license: https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE
- Microsoft TRELLIS.2 code: https://github.com/microsoft/TRELLIS.2
- Microsoft TRELLIS.2 license: https://github.com/microsoft/TRELLIS.2/blob/main/LICENSE
- TRELLIS.2 Mac port: https://github.com/shivampkumar/trellis-mac
- TRELLIS community Space: https://huggingface.co/spaces/trellis-community/TRELLIS
- Hugging Face Spaces API docs: https://huggingface.co/docs/hub/en/spaces-api-endpoints
- Gradio ZeroGPU client notes: https://www.gradio.app/docs/python-client/using-zero-gpu-spaces
- Hugging Face ZeroGPU docs: https://huggingface.co/docs/hub/en/spaces-zerogpu

## Integration Plan

1. Add provider abstraction: `reference image -> proxy mesh path + provider metadata`.
2. Provider metadata must include repo URL, full immutable source commit SHA, full immutable
   Hugging Face snapshot SHA, revision verification state, per-file hash/size manifests
   for every consumed artifact even when the snapshot SHA is verified, direct and transitive license identifiers, native-extension build
   provenance, device/backend, cache path, token mode, reject reasons, proof artifacts, and
   the `accepted_for_fit_camera` decision.
3. Add `manual` provider first: user supplies GLB/OBJ/PLY/STL from any model.
4. Add `hunyuan-mlx` as the first local Mac candidate; promote it only after the full GLB
   and proof gates pass.
5. Add `sf3d-local` provider as a fast optional provider with explicit 24 GB memory warning.
6. Add `hf-space` provider with `HF_TOKEN` support and cache; never make ZeroGPU a required
   test dependency.
7. Add GLB-to-USDZ proof export. Preserve textures when possible; use STL fallback only for
   geometry-only proofs.
8. Feed returned GLB into `3d proxy-align`, save best transform/error JSON, then derive
   initial camera candidates for contour-based `fit-camera` only after the proxy is accepted.
9. Add a hard pre-prior reject gate for hallucinated or topology-mismatched image-to-3D
   outputs. Failed or diagnostic-only proxy results must not seed camera initialization.
   The required result schema must split generation and alignment stages:
   `generation_status`, `proxy_align.status`, and
   `proxy_align.accepted_for_fit_camera: true|false`. Only
   `generation_status=success`, `proxy_align.status=success`, and
   `proxy_align.accepted_for_fit_camera=true` may seed camera initialization. Threshold
   selection is a blocker before implementation: it must define minimum boundary F1,
   maximum symmetric contour Chamfer or SDF loss, maximum p95 miss, scale/bbox limits,
   crop/border limits, and render-against-original-reference visual review criteria.
   Pre-alignment mesh gates must also reject empty geometry, non-finite vertices, invalid
   faces, insane bounds/scale, extreme component fragmentation, unacceptable triangle
   counts, missing texture/materials when required, and meshes that fail the chosen
   manifold/watertight/repair policy.
10. Make `proxy-align` emit a mechanical consumer contract for generated proxies:
    `proxy_align.status`, `proxy_align.accepted_for_fit_camera`,
    `proxy_align.reject_reasons`, `proxy_align.metrics_json`,
    `proxy_align.proof_artifacts`, and `proxy_align.output_mesh`. The default is rejected.
    Missing or unknown status labels, missing metrics, failed `trimesh` load, proof
    artifact gaps, failed visual review, or any threshold failure must set
    `proxy_align.accepted_for_fit_camera=false` and add a reject reason. Top-level
    `output_mesh` is the raw provider output and must never be interpreted as an accepted
    aligned proxy or camera prior.
11. Gate every accepted provider output before using it as a camera prior: GLB must load with
    `trimesh`, `generation_status` must be `success`, `proxy_align.status` must be
    `success`, and `proxy_align.accepted_for_fit_camera` must be `true`. A render against
    the original
    reference must pass the proof package: original reference, fitted render in the same
    frame/camera, overlay or error map, reference mask/segmentation, metrics JSON, and
    explicit success/warning/failure/diagnostic-only label.
12. Manual/local-only meshes remain `proxy_align.accepted_for_fit_camera=false` until they
    pass the same `trimesh`, proxy-align, proof artifact, metrics, and provenance decision
    gates. Local-only provenance can limit distribution, but it cannot bypass camera-prior
    validation.
13. Add e2e `bin/3d` workflows for provider selection, auth/cache failure handling,
   proxy-align handoff, proof artifacts, metrics JSON, and durable result labels
   (`generation_status`, `proxy_align.status`, `proxy_align.accepted_for_fit_camera`, and
   reject reasons) before reporting any provider as complete.
