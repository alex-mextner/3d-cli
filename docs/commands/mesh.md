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
