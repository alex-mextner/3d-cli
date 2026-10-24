# `3d scad section-color` — coloured cross-sections of [OpenSCAD](GLOSSARY.md#openscad) assemblies

Produces a **true cross-section that keeps per-part colours** (each body keeps its own colour, including on the freshly-cut face). For a single part where mono is fine, use `3d scad section` instead.

## The method (why this is the only way that works)

A coloured section needs all three of these together:

1. **`--render`** (CGAL/manifold). throwntogether (no `--render`) keeps colours but applies **no cut at all** — you just see the closed solid.
2. **Per-part cut** — each part is differenced individually, not the whole union.
3. **`color()` OUTSIDE the `difference()`** — if `color()` is *inside* the difference, `--render` paints the new cut faces the default tan colour and the section looks monochrome. Outside, the cut face inherits the part colour.

So: `--render` + per-part-cut + colour-outside = the only coloured section.

## Contract: how an assembly opts in

The assembly must honour `-D cut=true` and cut each part with a `section_cut()` module (define it in a small helper such as the one below), with `color()` on the outside:

```openscad
// section_lib.scad — a minimal example; 3d does not ship it. Put your own copy next to
// the assembly (include resolves against the file's own directory).
section_plane = "YZ";
section_keep  = "neg";
cut = false;
module section_cut(plane = section_plane, keep = section_keep) {
    if (!cut) children();
    else difference() {
        children();
        // half-space for the chosen plane/keep, e.g. YZ/neg removes +X:
        translate([0, -500, -500]) cube([1000, 1000, 1000]);
    }
}
```

```openscad
include <section_lib.scad>

color(c_shaft) section_cut() shaft();   // colour OUTSIDE section_cut
color(c_head)  section_cut() head_body();
```

- When `cut == false` (default) `section_cut()` is a pass-through → full solid.
- `3d scad section-color` auto-passes `-D cut=true`, `-D 'section_plane="YZ"'` and `-D 'section_keep="neg"'` before your own `-D` values, so you can override them.
- An assembly with its own equivalent `CUT()` module and a `cut` parameter satisfies the contract without the lib.

## Camera convention (do not "fix" it)

6-param **VECTOR** camera: `eye_x,eye_y,eye_z,center_x,center_y,center_z`. A 7th param is gimbal mode with `dist=0` → empty frame. The eye sits on the side where material was **removed**, looking back at the exposed cut face:

| plane | keep=neg removes | eye on |
|-------|------------------|--------|
| YZ (split X) | +X half | +X |
| XZ (split Y) | +Y half | +Y |
| XY (split Z) | +Z half | +Z |

`--keep=pos` flips the dominant-axis sign. The command computes `eye = center + dist × direction` with offsets of 0.29·dist and 0.50·dist on the two secondary axes for a 3D (not flat) read.

## Usage

```
3d scad section-color <assembly.scad> <out.png> [opts]
  --plane=YZ|XZ|XY     cut plane            (default YZ: split across X)
  --keep=neg|pos       half to keep         (default neg: removes +axis half)
  --center=X,Y,Z       camera look-at point (default 0,0,0)
  --dist=N             camera distance      (default 250)
  --size=WxH           image size           (default 1200x900)
  -D 'var=val'         pass-through define  (repeatable)
```

```bash
3d scad section-color firing_assembly.scad cut.png \
  --plane=YZ --center=0,0,118 --dist=210 --size=850x950
```

## Sanity guard

The command prints the camera and the PNG byte size, and warns if the PNG is under 20 KB (likely an empty frame — check `--center`/`--dist`/`--plane`). For tiny demo geometry a small PNG is normal. A failed `assert()` or any OpenSCAD `ERROR:` exits 1.

## Notes

- `include`/`use` resolve against the **input file's own directory**: keep `section_lib.scad` reachable from there.
- Pass extra model params with repeated `-D`, e.g. `-D 'cock=0'`.
