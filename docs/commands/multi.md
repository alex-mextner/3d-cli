# `3d multi` — render all standard angles

Alias for `3d render <file> --multi [outdir]`. Renders front, back, left, right, top, and iso views into a single directory.

**Why it exists.** A shorthand so you do not have to type `--multi` every time you want the full thumbnail set.

## Usage

```
3d multi <file.scad> [outdir] [--render] [--size WxH] [-D k=v]...
```

```bash
3d multi model.scad previews/
3d multi model.scad --size 800x600
```

See [`render.md`](render.md) for the full option list and implementation notes.
