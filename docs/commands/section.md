# `3d section` — true cross-section render

Alias for `3d render <file> --section`. Produces a cross-section PNG by cutting the model ([STL](GLOSSARY.md#stl)-cut or per-part coloured assembly mode).

**Why it exists.** A shorthand for the section mode so you can type `3d section` instead of `3d render --section`.

## Usage

```
3d section <file.scad> -o out.png [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | — | Output PNG (required) |
| `--plane YZ\|XZ\|XY` | `YZ` | Cut plane |
| `--keep neg\|pos` | `neg` | Which half to keep |
| `--color` | off | Coloured per-part assembly mode |
| `--module 'name();'` | — | Module to cut (fallback only) |
| `--size WxH` | `1200x900` | Image size |
| `-D k=v` | — | Pass-through define (repeatable) |

```bash
3d section model.scad -o sec.png --plane YZ
3d section assembly.scad -o sec.png --color --plane YZ
```

See [`render.md`](render.md) for the full option list and implementation notes.
