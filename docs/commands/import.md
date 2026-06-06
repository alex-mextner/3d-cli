# `3d import` - OpenSCAD wrappers and import plans

Imports direct OpenSCAD-readable mesh formats by generating a `.scad` wrapper, or
prints a conversion plan for formats that need an external converter first.

## Usage

```bash
3d import <model> [options]
```

Direct wrapper formats are `.stl`, `.off`, `.amf`, and `.3mf`.

Planning-only formats are `.obj`, `.ply`, `.gltf`, `.glb`, `.dae`, `.step`, `.stp`,
`.iges`, `.igs`, `.brep`, `.fcstd`, `.usd`, `.usdc`, and `.usdz`.

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<model>.import.scad` for direct formats | Output `.scad` wrapper path |
| `--format FMT` | inferred from extension | Override format detection |
| `--mode MODE` | `auto` | `auto`, `wrapper`, or `plan` |
| `--scale N` | `1` | Uniform scale in the generated wrapper |
| `--convexity N` | `10` | OpenSCAD import convexity |

```bash
3d import part.stl
3d import part.stl -o wrappers/part.scad --scale 25.4 --convexity 12
3d import scan.obj --mode plan
3d import asset.mesh --format obj --mode plan
```

## Wrapper Output

For direct formats, the wrapper calls OpenSCAD `import()` with a relative path when
possible. The source model is never overwritten.
