# `3d export` — format-aware model export with geometry validation

Exports a `.scad` file to STL, 3MF, OFF, AMF, or color-capable USDZ and validates the
result. It can also list and plan future formats before their converters land. Nonzero
exit on bad geometry, so it can be used as a gate in CI scripts.

**Why it exists.** [OpenSCAD](GLOSSARY.md#openscad)’s built-in export can silently produce non-[manifold](GLOSSARY.md#manifold) or self-intersecting meshes. This command catches those defects and reports them with the exact remediation (`union()` for self-intersections, close solids for holes, etc.).

## Usage

```
3d export <file.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.<format extension>` | Output path. Extension determines format when `--format` is omitted |
| `--format FORMAT` | inferred from output extension or `stl` | `stl`, `3mf`, `off`, `amf`, `usdz`, `obj`, `ply`, `glb`, `gltf`, `step`, `brep`, or `svg` |
| `--stl` / `--3mf` / `--off` / `--amf` / `--usdz` | none | Selector shortcuts; planned selectors include `--glb`, `--step`, and related flags |
| `--list-formats` | off | List supported and planned export formats |
| `--plan` | off | Print the export plan without running OpenSCAD or converters |
| `--ascii` | off | ASCII STL (default: binary STL) |
| `--color r,g,b` | `0.78,0.74,0.66` | Diffuse color for USDZ export, each component `0..1` |
| `-D k=v` | — | Pass-through define (repeatable) |

```bash
3d export model.scad -o model.stl
3d export model.scad -o model.3mf -D 'width=80'
3d export model.scad --usdz --color 0.30,0.55,0.85
3d export --list-formats
3d export model.scad --plan --format glb
```

## Formats

Supported formats are `stl`, `3mf`, `off`, `amf`, and `usdz`. Planned formats are
`obj`, `ply`, `glb`, `gltf`, `step`, `brep`, and `svg`; use `--plan` to inspect the
intended path without running a converter.

USDZ is handled inside `3d export`: OpenSCAD writes the validated intermediate STL and
`lib/usdz.py` converts that mesh with the requested color. The command does not delegate
back through `3d usdz`, so `3d export model.scad --usdz` cannot recurse through the legacy
`.scad` compatibility path.

## Validation

After export, the command checks for:

- `non-manifold` → holes in the mesh
- `self-intersect` → `union()` needed
- `degenerate` → zero-area triangles
- OpenSCAD `ERROR:` lines

If the output is a binary STL, the authoritative mesh check (`lib/mesh_check.py`) is also run. If the mesh stack is absent, the result is a grep-only pass with a note to run `3d mesh <out>` for the full check.

## Exit codes

- `0` — export succeeded, geometry clean
- `1` — export failed or geometry defect detected
- `2` — invalid arguments, unsupported planned execution, or mismatched format/output extension
