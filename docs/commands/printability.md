# `3d printability` — wall / feature / overhang / orientation gate ([FDM](GLOSSARY.md#fdm))

Checks whether a model is printable on a standard FDM printer (Bambu A1 + PLA / PETG). Enforces hard thresholds: wall ≥ 1.2 mm, floor ≥ 0.8 mm, feature ≥ 1.0 mm, overhang ≤ 45°.

**Why it exists.** A watertight [manifold](GLOSSARY.md#manifold) model can still be unprintable — walls too thin, overhangs too steep, or features too small. This gate catches those issues before you waste filament and time.

## Usage

```
3d printability <file.scad|.stl> [more parts...] [-D k=v ...]
```

```bash
3d printability part.scad
3d printability a.scad b.scad
3d printability part.stl
```

## Exit codes

- `0` — all parts cleared the HARD rules
- `1` — at least one HARD rule failed

## Implementation notes

`.scad` inputs are exported to STL first via [OpenSCAD](GLOSSARY.md#openscad). The mesh analysis is run by `lib/printability_mesh.py` ([trimesh](GLOSSARY.md#trimesh) + rtree + scipy). If the mesh stack is absent, the gate degrades to `SKIP` inside `3d check` rather than failing the whole run.
