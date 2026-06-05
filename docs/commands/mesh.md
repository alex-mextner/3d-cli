# `3d mesh` — watertight / [manifold](GLOSSARY.md#manifold) / self-intersection / volume report

Runs a deep geometric check on a mesh file ([`.stl`](GLOSSARY.md#stl), [`.3mf`](GLOSSARY.md#3mf), `.scad`) and reports whether it is watertight, manifold, self-intersecting, and its volume / triangle count.

**Why it exists.** [OpenSCAD](GLOSSARY.md#openscad)’s warning text is not always enough — the modern manifold backend can produce non-watertight output without a warning line. The mesh check uses the actual mesh geometry to verify correctness.

## Usage

```
3d mesh <file.stl|.3mf|.scad> [-D k=v ...]
```

```bash
3d mesh part.stl
3d mesh model.scad -D 'width=80'
```

## Engine tiers

The tool tries progressively lighter stacks so it still works when heavy deps are missing:

1. [`trimesh`](GLOSSARY.md#trimesh) + open3d (full check)
2. `trimesh` + [`manifold3d`](GLOSSARY.md#manifold3d) (watertight + manifold)
3. [OpenSCAD](GLOSSARY.md#openscad) warning grep (degraded, grep-only)

## Exit codes

- `0` — `PASS` (no geometric defects)
- `1` — `FAIL` (non-manifold, self-intersecting, or degenerate)
- `2` — load / usage error
