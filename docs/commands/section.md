# `3d section` — engineering cross-section render

Alias for `3d render <file> --section`. Cuts the model with a plane and draws it the way an engineering drawing does: the **cut faces are violet with 45-degree hatch lines**, every uncut surface keeps the colour of its **part**, and a legend under the picture names the parts and the "cut face". Issue: [#586](https://github.com/alex-mextner/3d-cli/issues/586).

**Why it exists.** A shorthand for the section mode so you can type `3d section` instead of `3d render --section`, and a section that reads at a glance: the hatch tells cut material from the surface behind it, the colours tell the parts of an assembly apart (same parts, palette, roles and legend as [`3d dimension`](dimension.md)).

## Usage

```
3d section <file.(scad|3mf|obj|stl)> -o out.png [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out PATH` | `<file>.png` | Output PNG |
| `--plane YZ\|XZ\|XY` | `YZ` | Cut plane (perpendicular to X, Y or Z) |
| `--at MM` | middle of the model | Position of the plane along its axis, in model mm. A plane outside the model is an error naming the range |
| `--keep neg\|pos` | `neg` | Which half to keep; the eye looks at the cut from the removed side |
| `--size WxH` | `1200x900` | Exact image size (160..8192 px a side) |
| `--part SEL=NAME[:ROLE]` | — | Name a part and/or set its role (`printed`, `vitamin`, `reference`, `stand_in`); SEL is a part name or `#RRGGBB`. A `stand_in` is drawn muted (repeatable) |
| `-D k=v` | — | OpenSCAD define for a `.scad` input (repeatable) |
| `--color` | — | Accepted for old scripts; a section is always per-part coloured |

```bash
3d section model.scad -o sec.png --plane YZ
3d section assembly.scad -D standin=true -o sec.png --plane XZ --at -7.3 --keep pos --part "#2a62c9=PCB:stand_in"
3d section box.3mf -o sec.png --plane XY --at 12 --size 1600x1200
```

## How it is drawn

- Parts and colours: read from `color()` (a `.scad` is exported to 3MF), 3MF materials, OBJ+MTL, else the deterministic part palette — the rules of [`3d dimension`](dimension.md#parts-colours-and-the-legend). The cut-face violet (`#B48CDC`) is reserved: no part is given a colour close to it.
- The cut: every triangle is clipped against the plane; the section outline of each closed solid is rebuilt and filled with even-odd rule, so a **cavity stays a hole** (no violet inside it). A solid whose outline does not close (a non-watertight mesh) gives a warning and an incomplete cut face, never a silent full one.
- The cut face is painted last in violet with the hatch over it, plus a dark violet outline; the legend has a hatched `cut face` swatch.

`--module` (the old fallback cut of one OpenSCAD module) was removed: the section now cuts the model mesh itself, so pick what to show with `--part` and `--at`.

See [`render.md`](render.md) for the other render modes.
