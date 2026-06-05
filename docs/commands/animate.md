# `3d animate` - deterministic render frame plans

Generates a deterministic PNG frame plan over the existing `3d render` workflow.
With `--plan`, it is dependency-free and does not call OpenSCAD.

## Usage

```bash
3d animate <file.scad> [options]
```

| Option | Default | What |
|---|---|---|
| `--plan` | off | Print the frame plan as JSON and do not render |
| `--frames N` | `24` | Number of frames |
| `--outdir DIR` | `animations/frames` | Frame output directory |
| `--view NAME` | `iso` | View passed to `3d render --view` |
| `--size WxH` | `800x600` | Render size passed to `3d render --size` |
| `-D k=v` | none | Pass-through define for every frame; repeatable |
| `-D k=start:end` | none | Linearly interpolate a numeric define across frames |

```bash
3d animate model.scad --plan --frames 12 -D angle=0:90
3d animate model.scad --plan --size 1024x768
3d animate model.scad --frames 24 --view front --outdir anim/frames
```

## Rendering

Without `--plan`, the command creates the frame directory and calls `3d render` once
per planned frame. A nonzero render exits the animation run.
