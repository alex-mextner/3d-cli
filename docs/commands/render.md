# `3d render` — single view / multi-angle / cross-section renders

The unified render command. It produces camera-locked PNGs from an `.scad` file using OpenSCAD’s CGAL backend. The camera is computed from the model’s bounding box (exact fit, no drift), so the same model renders identically across machines.

**Why it exists.** The old bash pipeline had separate `view`, `multi`, and `section` scripts. Unifying them removes duplicate argument parsing and guarantees every mode uses the same bbox-exact camera math.

## Usage

```
3d render <file.scad> [mode + options]
```

### Modes (mutually exclusive)

| Mode | What |
|---|---|
| *(default)* | Single view render |
| `--multi [OUTDIR]` | front / back / left / right / top / iso into `OUTDIR` (default `previews/`), async |
| `--section` | True cross-section (generic STL-cut, or `--color` assembly mode) |

### Single-view options

| Option | Default | What |
|---|---|---|
| `--view NAME` | `iso` | `front` `back` `left` `right` `top` `bottom` `iso` `3-4` `front-left` `front-right` `rear-left` `rear-right` |
| `--cam ex,ey,ez,cx,cy,cz` | bbox-derived | Manual 6-param **vector** camera (wins over `--view`) |
| `--ortho` | off | Orthographic projection |
| `--colorscheme NAME` | `Tomorrow Night` | OpenSCAD colorscheme |

### Section options

| Option | Default | What |
|---|---|---|
| `--plane YZ\|XZ\|XY` | `YZ` | Cut plane |
| `--keep neg\|pos` | `neg` | Which half to keep |
| `--color` | off | Coloured per-part **assembly** mode (assembly must honour `-D cut=true`) |
| `--module 'name();'` | — | Module to cut (fallback when no mesh stack) |

### Common

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.png` | Output PNG (single / section) |
| `--size WxH` | `1200x900` | Image size (single / section); multi uses `800x600` |
| `-D k=v` | — | Pass-through OpenSCAD define (repeatable) |
| `--render` | off | Force CGAL render mode (slower but exact; use when preview mode is not enough) |

```bash
3d render model.scad --view left -o left.png
3d render model.scad --view 3-4 --ortho
3d render model.scad --multi previews/ --render
3d render model.scad --section --plane YZ -o sec.png
3d render assembly.scad --section --color --plane YZ -o sec.png
```

## Implementation notes

The heavy work (bbox-exact cameras, async multi, STL-cut section) lives in `lib/render.py` and runs through `pyrun`. Single and multi need **no Python mesh deps** — they degrade to `--autocenter --viewall` if the mesh stack is absent. The generic section genuinely needs `trimesh` to read the STL bounding box for the cut.
