# `3d proxy-align` - align image-to-3D proxy meshes to CAD meshes

`proxy-align` takes a CAD mesh and a generated proxy mesh, estimates a transform from the
proxy into the CAD frame, and writes shape/topology scores, a render-like projection
quality gate, and a visual proof image.

This is the local, reproducible half of the TRELLIS workflow: an external generator can
turn a reference image into `.glb`, `.ply`, `.obj`, or `.stl`; `3d proxy-align` then
normalizes that proxy, searches coarse pose candidates, refines with nearest-neighbor ICP,
and stores the best transform/error for downstream `fit-camera`. A generated mesh is not
trusted automatically: it must pass the `quality_gate` before it can be used as a camera
prior.

## Usage

```bash
3d proxy-align <cad-mesh> <proxy-mesh> [options]
```

## Options

| Option | Default | Meaning |
|---|---:|---|
| `--out DIR` | `./proxy-align` | Output directory for `result.json` and `alignment_proof.png` or SVG fallback |
| `--samples N` | `2500` | Surface samples per mesh |
| `--yaw-step DEG` | `45` | Coarse yaw grid step in degrees, from 1 to 360 |
| `--pitch VALUES` | `0` | Comma-separated pitch candidates |
| `--roll VALUES` | `0` | Comma-separated roll candidates |
| `--icp-steps N` | `10` | Nearest-neighbor refinement steps per candidate |
| `--json` | off | Print only the `result.json` path |

## Examples

```bash
3d proxy-align cad.stl trellis.glb --out match/proxy
3d proxy-align cad.stl proxy.ply --yaw-step 30 --pitch -20,0,20
3d proxy-align cad.stl proxy.stl --out work/proxy | jq -r .best.error.chamfer_mean
3d proxy-align cad.stl proxy.stl --out work/proxy --json | xargs cat | jq .quality_gate.status
```

## Output

`result.json` contains:

- input mesh descriptors: face/vertex counts, components, watertight status, Euler number,
  bounding-box extents;
- the best normalized transform from proxy to CAD: row-vector `matrix_3x3`, scale,
  internal rotation matrix, and translation;
- an original-space proxy-to-CAD transform that can be applied as
  `cad_point = proxy_point @ matrix_3x3 + translation`;
- contour-independent spatial errors: bidirectional Chamfer mean, p95, Hausdorff max,
  radial shape-histogram distance, and a topology penalty;
- `quality_gate.status` (`ok`, `warning`, or `reject`) with rejection reasons;
- orthographic CAD-vs-proxy projection contour checks: minimum edge F1@3px, maximum
  edge Chamfer distance in pixels, projection coverage drift, and candidate ambiguity;
- top ranked candidates for debugging ambiguous or symmetric objects.

`alignment_proof.png` overlays CAD samples in red and aligned proxy samples in blue from
XY, XZ, and YZ projections. If Pillow is unavailable in the active `.venv`, the command
writes an SVG fallback instead. The artifact is intentionally human-readable: a good proof
should make gross backside, scale, and orientation mistakes visible before any 2D
fit-camera stage.

## Quality Gate

The proxy quality gate exists to reject failed image-to-3D generations before they poison
`fit-camera`. It checks two things:

- 3D agreement after alignment: normalized Chamfer p95, topology penalty, and whether the
  best candidate is clearly better than nearby rotations;
- render-like 2D agreement: CAD and proxy are projected into shared XY/XZ/YZ frames and
  their silhouette boundaries are compared with edge F1 and contour Chamfer.

Treat `quality_gate.status == "reject"` as a hard stop. The command still writes
`result.json` and proof artifacts for debugging, but exits `1` so shell workflows stop
unless they explicitly choose to inspect the failed proxy. `warning` means the transform
can be inspected manually, but it should not be promoted into an automated camera seed
without the proof image and contour numbers.

## TRELLIS / ZeroGPU Role

TRELLIS or another image-to-3D model should be treated as a provider that produces a proxy
mesh. The CLI should not depend on that provider being available during tests. A robust
workflow is:

```bash
# provider step: HF ZeroGPU/TRELLIS or manual download produces reference.glb
3d proxy-align cad.stl reference.glb --out match/proxy
jq '.quality_gate' match/proxy/result.json
jq -e '.quality_gate.status == "ok"' match/proxy/result.json >/dev/null
CAMERA_PRIOR="$(jq -r '.best.transform_proxy_to_cad_original.matrix_3x3' match/proxy/result.json)"
```

The current command does not yet call Hugging Face directly. That provider layer should
cache every generated mesh and record the Space/model/version/quota status so failed
queues do not make local geometry tests flaky.
