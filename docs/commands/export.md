# `3d export` — [STL](GLOSSARY.md#stl) / [3MF](GLOSSARY.md#3mf) / OFF / AMF export with geometry validation

Exports a `.scad` file to a mesh format and validates the result. Nonzero exit on bad geometry, so it can be used as a gate in CI scripts.

**Why it exists.** [OpenSCAD](GLOSSARY.md#openscad)’s built-in export can silently produce non-[manifold](GLOSSARY.md#manifold) or self-intersecting meshes. This command catches those defects and reports them with the exact remediation (`union()` for self-intersections, close solids for holes, etc.).

## Usage

```
3d export <file.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.stl` | Output path. Extension determines format: `.stl` `.3mf` `.off` `.amf` |
| `--ascii` | off | ASCII STL (default: binary STL) |
| `-D k=v` | — | Pass-through define (repeatable) |

```bash
3d export model.scad -o model.stl
3d export model.scad -o model.3mf -D 'width=80'
```

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
