# `3d usdz` — export to Apple AR Quick Look

Exports a `.scad` or [`.stl`](GLOSSARY.md#stl) model to a colored USDZ file for Apple AR Quick Look. A `.scad`
input is first exported to STL through `3d export`, so geometry validation runs before the
USDZ conversion.

## Usage

```
3d usdz <file.scad|file.stl> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.usdz` | Output USDZ path. |
| `--color r,g,b` | `0.78,0.74,0.66` | Diffuse color components, each in `0..1`. |

```bash
3d usdz part.scad -o part.usdz
3d usdz part.stl --color 0.30,0.55,0.85
```

The converter fixes common AR export details: Z-up to Y-up orientation, millimeter units,
and a basic preview material.
