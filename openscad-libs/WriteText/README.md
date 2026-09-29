# WriteText

Raised or engraved lettering wrapped around cylinder and cone walls in OpenSCAD, drawn
with OpenSCAD's native `text()` — so any installed font works, including Cyrillic,
Greek and other non-Latin scripts — and rendering without warnings on current OpenSCAD.

WriteText is the successor of **[Write.scad by HarlanDMii](https://www.thingiverse.com/thing:16193)**
(CC BY 3.0), whose `writecylinder()` wrapped text around a cylinder using bundled
Latin-only DXF fonts and undeclared module parameters that current OpenSCAD warns about.

## Install

```bash
3d openscad libs WriteText install     # into OpenSCAD's user library folder
```

or copy this folder to your OpenSCAD library folder as `WriteText/`
(macOS `~/Documents/OpenSCAD/libraries`, Linux `~/.local/share/OpenSCAD/libraries`,
Windows `Documents\OpenSCAD\libraries`).

## Use

```openscad
use <WriteText/WriteText.scad>

// raised letters on a cone wall: same r1/r2/h as the cylinder() they sit on
cylinder(r1 = 82.5, r2 = 70, h = 50);
cone_text("Дефолт", r1 = 82.5, r2 = 70, h = 50, size = 20, relief = 1.2);

// engraved letters on a cylinder
difference() {
  cylinder(r = 30, h = 40);
  cylinder_text_cut("FIDO", r = 30, h = 40, size = 12, depth = 1);
}
```

`WriteText_example.scad` renders both.

| Module / function | What |
|---|---|
| `cone_text(txt, r1, r2, h, size, relief=1, font, spacing=1, azimuth=-90, z, baseline)` | Raised letters standing `relief` mm off the wall (along its normal). They touch the wall but never overlap it, so they can be a separate part/colour for multi-material printing. |
| `cone_text_cut(txt, r1, r2, h, size, depth=1, …)` | Volume to `difference()` from the cone to engrave the letters `depth` mm deep. |
| `cylinder_text(txt, r, h, size, …)` / `cylinder_text_cut(…)` | The same on a straight cylinder. |
| `text_width(txt, size, spacing=1)` | Width of the word along the wall, in mm. |
| `text_advance(c)` | Advance width of one character at size 1. |

Parameters:

- `size` — capital letter height, measured along the sloped wall.
- `font` — any OpenSCAD font name (default `"Arial:style=Bold"`); it must contain the
  glyphs of `txt`.
- `azimuth` — direction the middle of the word faces, degrees around Z (`-90` = front).
- `z` — height of the text centre line (default: middle of the wall).
- `spacing` — extra letter spacing factor (1 = the font's own spacing).
- `baseline` — baseline offset at size 1 that centres the glyphs on `z` (default
  `-0.35`, right for Arial).

Letters are placed one by one along the arc, each flat on the plane tangent to the wall
at its centre, tilted with the wall and cut to the wall's shell, so they follow the
curved surface. Spacing is proportional, from an Arial Bold advance-width table covering
Cyrillic, Latin, digits and common punctuation (OpenSCAD's glyph measuring,
`textmetrics()`, is still experimental); other fonts are spaced approximately — tune with
`spacing`.

## License and attribution

CC BY 3.0 — see [LICENSE](LICENSE). Based on the idea and interface of Write.scad by
HarlanDMii (Harlan Martin), https://www.thingiverse.com/thing:16193, licensed CC BY 3.0.
Keep this attribution when you share or adapt WriteText.
