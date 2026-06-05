# `3d preview` — fast throwntogether preview

Runs OpenSCAD's fast preview path and writes a PNG. This is useful for quick visual
iteration when a full CGAL render is unnecessary.

## Usage

```
3d preview <file.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.png` | Output PNG path. |
| `--cam C` | `0,0,0,55,0,25,0` | Camera, either 7-param gimbal or 6-param vector. |
| `--size WxH` | `800x600` | Image size. |
| `-D k=v` | repeatable | OpenSCAD define passed through. |

```bash
3d preview model.scad
3d preview model.scad -o look.png --size 1024x768
```

Unlike `render`, `preview` intentionally accepts the 7-param gimbal camera form because
OpenSCAD preview commonly uses it.
