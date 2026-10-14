# `3d halves-check` — do the two halves of a split part still fit?

A part too big or too overhanging for one print is often cut in two along a plane, each half printed lying on its cut face, and the halves glued back with dowels. If the generator built the halves independently, the cut faces may no longer be the same shape — one half sticks out past the other, dowel holes miss each other, a crumb floats on one face. A per-part [mesh](mesh.md) gate cannot see that. `halves-check` brings both halves into their common **assembly frame**, takes the cut face of each (a section 0.05 mm inside the material), rasterises both on one grid and compares them.

## Usage

```
3d halves-check <file.3mf|dir> [...] [options]
3d halves-check --pair A.stl[,A2.stl] B.stl[,B2.stl] [--plane auto|y=V|'nx ny nz d'] [options]
```

```bash
3d halves-check print/u1_trees.3mf
3d halves-check print/ --png out/halves/
3d halves-check --pair out/assembly/TA1__TREE_L.stl,out/assembly/TA1__TRUNK.stl out/assembly/TA2__TREE_L.stl,out/assembly/TA2__TRUNK.stl --plane y=146.3
3d halves-check plate.3mf --json | jq '.pairs[] | {pair, status, max_dev_mm}'
```

## What is checked (per pair)

| Check | FAIL when |
|---|---|
| outline | the faces differ by more than `--tol` anywhere (max distance from a pixel of one face to the other face — how far one half sticks out), or IoU < `--min-iou` |
| dowels | hole counts differ (or differ from `halves_dowels`), a hole centre misses its partner by more than `--tol`, or diameters differ by more than `--tol` |
| size | face width or height differs by more than `--tol` (a shortened tip or base) |
| islands | a piece of one face has no partner on the other face |
| side | a half reaches more than `--tol` across the cut plane onto the other half's side |

The whole-half silhouettes along the plane axes are reported for information (an off-centre cut differs legitimately).

## Marking pairs in a 3MF

Per object, as Orca/Bambu `Metadata/model_settings.config` `<object>` metadata (`<metadata key="…" value="…"/>`), or as `<metadata name="…">` inside the `<object>` of `3D/3dmodel.model`:

| Key | Value |
|---|---|
| `halves_pair` | pair id shared by both halves (`TA`); `none` = not a half (silences the name heuristic) |
| `halves_side` | `1` (material on n·p ≥ d) or `2` (n·p ≤ d) |
| `halves_to_assembly` | 16 numbers, row-major 4×4: the object's **local** coordinates (components assembled, before the build-item transform — moving/rotating the object on the plate keeps it valid) → assembly frame, mm |
| `halves_local_min` | `x y z`: min corner of the local bbox when the matrix was written; a slicer that re-centres the object shifts the local frame, the difference is undone first |
| `halves_cut` | `nx ny nz d`: the cut plane in the assembly frame (same value on both halves) |
| `halves_dowels` | optional: number of dowel holes each face must have (a whole number; if both halves carry it, the values must agree) |

Both halves of a pair must be in the same 3MF. Objects named like `X1` + `X2` (first word of the name) without any `halves_pair` key are reported as unmarked halves: `WARN` by default, `FAIL` with `--strict-names`.

## STL pairs

`--pair A B` takes two STL sets that already share the assembly frame (comma-join the colour parts of one half). The cut plane is the largest flat face both halves share, or `--plane x=V`, `y=V`, `z=V`, `'nx ny nz d'`.

## Options

- `--tol MM` — outline deviation, dowel offset/diameter and size tolerance (default 0.2)
- `--res MM` — raster pixel of the face comparison (default 0.05); a face whose raster would exceed 40 million pixels is an error (exit 2) — use a coarser `--res`
- `--min-iou F` — minimum cut-face IoU (default 0.9)
- `--png DIR` — `<file>__<pair>.png` overlay per pair: grey both, red half 1 only, blue half 2 only, assembly up (world z) at the top
- `--json` — JSON report on stdout (`status`, `pairs[]` with `iou`, `max_dev_mm`, `hole_pairs`, `face_size_a/b`, `failures`, …)
- `--strict-names` — unmarked `X1`/`X2` objects FAIL instead of WARN
- `--require-pairs` — FAIL when no marked pair is found

## Exit codes

- `0` — `PASS`
- `1` — `FAIL` (a pair does not match; unmarked halves with `--strict-names`; no pairs with `--require-pairs`)
- `2` — usage / load / metadata error (missing partner, unparsable matrix, missing file)
