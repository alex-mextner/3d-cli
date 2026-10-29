# `3d mesh` — watertight / [manifold](GLOSSARY.md#manifold) / self-intersection / volume report

Runs a deep geometric check on a mesh file ([`.stl`](GLOSSARY.md#stl), [`.3mf`](GLOSSARY.md#3mf), `.scad`) and reports whether it is watertight, manifold, self-intersecting, and its volume / triangle count.

**Why it exists.** [OpenSCAD](GLOSSARY.md#openscad)’s warning text is not always enough — the modern manifold backend can produce non-watertight output without a warning line. The mesh check uses the actual mesh geometry to verify correctness.

## Usage

```
3d mesh <file.stl|.3mf|.scad> [-D k=v ...] [--repair [-o OUT]]
```

```bash
3d mesh part.stl
3d mesh model.scad -D 'width=80'
3d mesh purchased.stl --repair                 # writes purchased.fixed.stl, then gates it
3d mesh purchased.stl --repair -o fixed.stl
```

## `--repair` — fix the mesh, then gate it

`--repair` runs [meshfix](https://github.com/alex-mextner/meshfix) (a standalone C++17 library with a stable C ABI,
also used by the Snapmaker Orca fork) on the input before the check. It welds duplicate and near-duplicate
vertices, drops degenerate and duplicate faces, stitches T-junctions, makes the winding consistent and outward,
splits non-manifold edges and vertices, fills holes, and removes floating islands. The repaired mesh is written to
`<input>.fixed.stl` (never over the input) or to `-o OUT` (`.stl`/`.obj` natively, `.3mf`/`.ply`/`.off` through
trimesh), a summary is printed, and the gate then judges the **repaired** mesh, so the exit code tells you whether
the fix worked:

```
repair (meshfix 0.1.1): faces 411213 -> 411110, watertight no -> yes, volume 78151.4941 -> 78151.4941 mm3
  holes filled 19 (+37 faces), non-manifold edges 87 -> 0, faces flipped 16, duplicate faces 100, ...
```

The first use clones meshfix at the pinned tag into `libs/meshfix` and builds it once into the cache (git, cmake and
a C++17 compiler needed); an installed package works too (`pip install "meshfix @ git+https://github.com/alex-mextner/meshfix@v0.1.1"`).
Without either, `--repair` exits 127 with that install command. Python callers (the `3d prep` pipeline, ticket #431) use
`lib/mesh_repair.py`: `repair_mesh(vertices, faces, **opts)` and `repair_file(src, dst)`, whose report keeps the keys
`watertight_before/after`, `volume_before/after`, `faces_before/after`, `holes_filled`.

Holes are closed from their own boundary (no fairing), and overlapping shells are reported, not merged; see the
meshfix README for the full list of limits.

## Engine tiers

The tool tries progressively lighter stacks so it still works when heavy deps are missing:

1. [`trimesh`](GLOSSARY.md#trimesh) + open3d (full check)
2. `trimesh` + [`manifold3d`](GLOSSARY.md#manifold3d) (watertight + manifold)
3. [OpenSCAD](GLOSSARY.md#openscad) warning grep (degraded, grep-only)

## Exit codes

- `0` — `PASS` (no geometric defects; with `--repair`: of the repaired mesh)
- `1` — `FAIL` (non-manifold, self-intersecting, or degenerate)
- `2` — load / usage error

---

# `3d mesh deform` — deterministic, GUI-free sculpting from a spec

```
3d mesh deform <mesh.stl|.3mf> <spec.yaml|.json> [-o OUT] [options]
3d mesh fit-silhouette <mesh.stl|.3mf> <spec.yaml|.json> [-o OUT] [options]
```

Applies an ordered, seeded list of sculpt-like operations to a mesh — what a person does in
[Blender](GLOSSARY.md#blender--bpy) sculpt mode — with **no GUI and no Blender**: named regions with smooth falloff,
**keep-out zones that never move**, handle deformation ([ARAP](GLOSSARY.md#arap) / biharmonic), smooth, inflate,
crease, pinch, fur relief, remesh, decimate, repair, symmetrize. Same input + same spec + same seed gives the same
SHA-256. Research, the chosen stack and benchmarks: [docs/research/mesh-fit-2026-10-20.md](../research/mesh-fit-2026-10-20.md).

`fit-silhouette` is the same engine for a spec that contains a `fit_silhouette` op — a **heuristic** that nudges
region handles so the silhouette matches reference masks — and prints the per-iteration IoU table.

```bash
3d mesh deform figure.stl examples/mesh-fit/cat-fur.yaml -o figure_fur.stl --collage fur.png --verify-determinism
3d mesh deform part.stl spec.yaml --report fit.json --json
3d mesh fit-silhouette part.stl fit.yaml -o part_fit.stl
```

## Options

| Option | Meaning | Example |
|---|---|---|
| `-o`, `--out FILE` | result `.stl` or `.3mf` (byte-reproducible); `.obj`/`.ply` go through trimesh (no byte guarantee). Default `<mesh>.deformed.stl`. | `-o figure_fur.stl` |
| `--report FILE` | JSON report (default `<out>.report.json`). | `--report fur.json` |
| `--collage FILE.png` | before / after / displacement heat-map in front, left and top views, drawn by the numpy rasteriser (needs Pillow). | `--collage fur.png` |
| `--verify-determinism` | runs the spec twice and exits 1 unless both meshes are identical. | `--verify-determinism` |
| `--seed N` | overrides the spec seed. | `--seed 7` |
| `--labels FILE.npz` | per-face labels (an array `face_label`, one int per face of the mesh as loaded) to carry to the result: faces split by a `remesh` `refine` op give their label to every child. Refused up front when the spec has a `decimate`, `repair` or non-`refine` `remesh` op (no input-to-output face map). Any other array with one row per face (a face group, a patch id) is carried the same way; the rest (e.g. `names`) is copied unchanged. | `--labels cat.labels.npz` |
| `--out-labels FILE.npz` | where to write the carried labels (default `<out>.labels.npz`); needs `--labels`. | `--out-labels cat_fur.labels.npz` |
| `--no-self-intersections` | skips the self-intersection count (pymeshlab when it imports, else a built-in KD-tree edge/triangle test: ~3 s on 400k faces; counts transversal piercings, not coplanar overlaps). | `--no-self-intersections` |
| `--json` | prints the whole report instead of the summary. | `--json` |

Exit codes: `0` PASS, `1` gate FAIL (a keep-out zone moved, or two runs differ — nothing is written), `2` usage / input
error (unknown spec key, missing file, unpinned handle patch).

## Spec

```yaml
seed: 1                        # every random-looking thing (fur noise) is a pure function of this
landmarks: {file: landmarks.json, prefix: lm_}   # boxes of a garage-band style landmarks file -> regions lm_<name>
regions:
  eyes:  {union: [{box: lm_eye_L, margin: 0.6, falloff: 1.9}, {box: lm_eye_R, margin: 0.6, falloff: 1.9}]}
  crown: {zrange: [60, 90], falloff: 8}
keepout: [eyes]                # never moves; the falloff band fades the op to 0
ops:
  - {op: smooth, region: crown, iterations: 5}
  - {op: fur, region: crown, amplitude: 0.35, overhang_safe: true}
```

**Regions** are per-vertex weights in `[0, 1]`; weight 1 is inside, the `falloff` (mm, Hermite ramp) fades to 0.
One of: `box [x0,x1,y0,y1,z0,z1]` (or `box: lm_<landmark>`), `sphere {center, radius}`, `ellipsoid {center, radii}`,
`capsule {a, b, radius}`, `halfspace {point, normal}`, `zrange [z0, z1]`, `points {file|list, radius}` (distance to a
point cloud, e.g. the "quiet points" of a self-intersection pass), `ramp {quantity: nx|ny|nz|x|y|z|curvature, from, to}`,
`normal_cone {axis, angle_deg, soft_deg}`, `gap {min, max, rays, cone_deg, within, backend}` (weight 0 where another
surface is near in front of the vertex; ray cast, ~100x faster with `embreex` installed, `backend: rtree` is float64 and
identical on every machine), and combinators `union`, `intersect`, `product`, `difference` over region names.
Modifiers: `falloff`, `margin`, `invert`, `scale`, `blur` (mask smoothing passes). A reference is a name, an inline
definition, or a list (union).

**Keep-out zones** (`keepout: [regions]`): every op's displacement is multiplied by `1 - weight`, and vertices at
weight 1 are pinned to their exact bytes. At the end the gate compares the **output against the input**: each
protected input vertex must still be a vertex of the result, bit-identical — max displacement `0.0` mm, else exit 1.
An op that cannot keep vertices (`decimate`, `remesh` with the pymeshlab engine, `repair`) is refused next to a keep-out
zone unless the spec sets `keepout_tolerance_mm: <mm>`, which switches the gate to the surface distance of the zone.
`remesh` with the built-in `refine` engine keeps every original vertex and is always allowed.

## Ops

| Op | What it does | Parameters |
|---|---|---|
| `smooth` | Taubin (non-shrinking) or Laplacian smoothing of the region | `iterations` (10), `lambda` (0.5), `method` taubin\|laplacian |
| `inflate` | move along the smoothed vertex normal | `amount` (mm, signed), `normal_smoothing` (2) |
| `draw` | move along the region's average normal (one direction) | `amount`, `normal_smoothing` |
| `displace` | move along a fixed direction, whatever the local normal does (fill a socket forward, shave a ridge back) | `direction: [x,y,z]`, `amount` (mm, signed) |
| `pinch` | pull toward a point or line, in the tangent plane | `strength` (0.5), `toward: {point}\|{line: [a, b]}`, `keep_normal` |
| `crease` | pinch + a groove | `strength`, `depth` (mm), `sharpness` (2), `toward` |
| `scale_local` | scale about a pivot, weighted by the region | `factor` (scalar or `[x,y,z]`), `pivot` |
| `symmetrize` | mirror (average, or copy one side) | `axis` x\|y\|z, `at`, `mode` average\|mirror_positive\|mirror_negative |
| `grab` (alias `move`) | move / rotate / scale handle regions; the surface follows | `handles: [{region, translate, rotate: {axis, deg}, scale, pivot}]`, `anchors`, `solver` none\|harmonic\|biharmonic\|arap (arap), `iterations` (10), `influence` (mm; solve only this patch), `handle_min` |
| `fur` (alias `relief`) | seeded strand relief along the normal, outward only | `amplitude`, `amplitude_regions`, `flow`, `pitch`, `length`, `fine`, `warp`, `clumps`, `normal_smoothing`, `overhang_safe: {max_angle_deg}` |
| `remesh` | `refine` (default): conforming midpoint splits to `target_edge` (mean edge) or `max_edge` (longest edge: fails when `max_rounds` is not enough), vertex-preserving, no quality guarantee; with a `region` only faces touching it are split (a neighbour sharing a split edge is split too, so the mesh stays closed) and the report gives the longest edge left in the zone; `botsch`: isotropic remeshing via `gpytoolbox` (best triangle quality, moves vertices); `pymeshlab`: isotropic explicit remeshing (GPL, needs libGL) | `engine`, `target_edge` (mm) or `max_edge` (mm, refine), `max_rounds` (10, refine), `max_faces` (10 000 000, refine: a round that would exceed it is refused before it allocates), `iterations`, `feature_deg` |
| `decimate` | `manifold3d` (default): tolerance simplify; `quadric` (default when `target_faces` is set): `fast-simplification` to a face count. Both leave sliver triangles: remesh before a further `grab`. | `engine`, `tolerance` (mm), `target_faces` |
| `repair` | `builtin` (trimesh: merge, drop degenerate/duplicate faces, fix winding, fill holes) or `meshfix` (the `3d mesh --repair` adapter) | `engine` |
| `fit_silhouette` | see below | `views`, `handles`, `iterations`, `step_mm`, `min_step_mm`, `min_gain`, `anchors`, `influence` |

`grab` solvers: `harmonic` (fast, C0 at the handle), `biharmonic` (smooth thin-plate pull), `arap` (as-rigid-as-possible,
local/global iterations seeded by biharmonic: surface detail stays rigid, strongest for twists and bends; it does not
preserve volume — compare `volume_before_mm3` / `volume_after_mm3` in the report). `influence` restricts the solve to a
patch around the handles, which is what keeps a 400k-face mesh at ~1.4 s for the ARAP step. `solver: none` is the
brush-style kernel (each handle region's weight scales a translate/rotate/scale, no solve).

### Fur relief and the overhang-safe mode

`fur` displaces vertices along their (ring-smoothed) normal by a ridged strand field stretched along `flow` (default
`[0,0,1]`: strands run down the surface, so the across-flow direction is horizontal and cannot create an overhang),
domain-warped across the flow, with a fine octave and slow height modulation. `amplitude_regions` blend other
amplitudes in (head 0.25, tail 0.45), `clumps` add a rounder, shorter layer where a region and a normal-z ramp allow.
Displacement is `>= 0`, so walls never get thinner. With `overhang_safe: {max_angle_deg: 45}` a face that was printable
may never end up steeper than the limit (repo convention: normal z below `-sin(angle)`, bed faces exempt): violating
faces halve, then zero, the displacement of their vertices until none is left, so there are no undercuts. The noise is
the one of the Chekhov `fur.py`; with the same seed, flow `[0,0,1]` and the Chekhov constants it reproduces that field
to `1e-9` (pinned by a golden test). [`examples/mesh-fit/cat-fur.yaml`](../../examples/mesh-fit/cat-fur.yaml) is the whole
Chekhov fur pipeline as a spec (landmark boxes, face/ear/cable masks, bib clumps, tail fullness); its
`landmarks.json` is a synthetic stand-in with the layout of `landmarks_fur.json`. Not carried by the example: the
phone-contact corridor and the self-intersection "quiet point" loop of `build_fur.py` (add them as `points` regions).

### `fit_silhouette` — heuristic, read the table

```yaml
ops:
  - op: fit_silhouette
    views:
      - {name: front, mask: front_mask.png, polarity: light, ortho: {direction: [0,1,0], up: [0,0,1], center: [0,0,45], mm_per_px: 0.25}}
      - {name: top,   mask: top_mask.png, view: top, align: mask_bbox}
    handles:
      - {region: chest, direction: [0,-1,0], max_move: 4.0}
    influence: 25
```

Each handle is a region (vertices at weight 1), a direction and a travel limit. The surface follows through a
biharmonic deformation that is linear in the handle travels, so one factorisation serves every candidate; a greedy
coordinate search (first improvement, fixed handle order, step halving) maximises the mean silhouette IoU over the
views. Keep-out zones are honoured inside the loop. **What it is not:** a general non-rigid registration. It can stall
in local optima, silhouettes do not see depth, a view needs a calibrated camera (`ortho: {mm_per_px, center}`,
`perspective: {eye, center, up, fov_deg}`, `camera_json:` from `3d fit-camera` — the perspective conversion assumes a
vertical field of view and is approximate — or `align: mask_bbox`, which assumes the reference is framed like the model),
and masks are read with Pillow (`polarity: light` = object white, as `3d silhouette` writes them). The IoU table printed by
`fit-silhouette` (and stored in the report) is the evidence; a rising IoU with a wrong-looking collage is a failed fit.

## Report

`--report` writes JSON: `status`, `seed`, `spec_sha256`, `mesh_sha256`, `input` / `output` (vertices, faces, watertight,
volume, bbox, sha256), `watertight_before|after`, `volume_before_mm3|after`, per-op records (`vertices_moved`,
`max_displacement_mm`, `mean_displacement_mm`, `seconds`, op details such as ARAP iterations or the fur overhang-safe
rounds), `keepout` (`locked_vertices`, `fade_vertices`, `max_displacement_mm`, `tolerance_mm`, `gate`),
`self_intersections` (`before` / `after`), `determinism`, `timing_s`.

## Optional dependencies

Core needs only what `3d` already ships (numpy, scipy, trimesh, manifold3d, pyyaml). Optional, each with a structured
error naming the install line when missing: `pillow` (reference masks, `--collage`), `gpytoolbox` (remesh `botsch`),
`fast-simplification` (decimate `quadric`), `pymeshlab` (remesh `pymeshlab`, faster self-intersection count; GPL-3,
needs libGL), `embreex` (~100x faster `gap` ray casts). Why these and not libigl / OpenVDB / bpy, with benchmarks:
[research note](../research/mesh-fit-2026-10-20.md).

## Reproducibility, honestly

Byte-identical output needs the same input, spec, seed **and** library versions. Inside one environment it holds for
any thread count (SuperLU and the stacked 3x3 SVDs are single-thread deterministic; reductions use ordered
`np.bincount`; the tool pins threaded BLAS to one thread because the research found it changes the bits of large dense products) — `tests/e2e/test_mesh_deform_workflows.py` runs `OMP_NUM_THREADS=1` against `8` and compares SHA-256.
Across machines, `gap` regions with the embree backend (float32) can differ from `rtree` (float64) in the last bits;
pin `backend: rtree` when a hash must travel. `.obj`/`.ply` are written by trimesh and are not byte-guaranteed.
The STL/3MF writers use a fixed header, fixed zip timestamps and float32 coordinates (the input is canonicalised to
float32 on load, so untouched vertices round-trip exactly).

## Speed

Measured on the 16-core remote box (see the research note): a 6-op spec (smooth, inflate, fur with overhang-safe, ARAP
grab on a 25 mm patch, crease, symmetrize) on a 401,868-face mesh takes ~4 s; every brush is under 0.4 s.
`MESHFIT_PERF=1 pytest tests/test_meshfit_perf.py` re-runs the 400k-face budget test (run it on the remote box).
