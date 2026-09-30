// WriteText — raised or engraved lettering on cylinders and cones, using
// OpenSCAD's native text() so any system font works, including Cyrillic,
// Greek and other non-Latin scripts.
//
// Successor in spirit to Write.scad by HarlanDMii (writecylinder() and
// friends): https://www.thingiverse.com/thing:16193 — licensed CC BY 3.0.
// Write.scad draws letters from bundled Latin-only DXF fonts and passes
// undeclared module parameters, which current OpenSCAD reports as warnings;
// WriteText is a rewrite on top of text() that renders warning-free.
// License: CC BY 3.0 (see LICENSE next to this file).
//
// Usage:
//   use <WriteText/WriteText.scad>
//
//   // raised letters on a cone wall (bottom radius 82.5, top 70, height 50)
//   cylinder(r1 = 82.5, r2 = 70, h = 50);
//   cone_text("Дефолт", r1 = 82.5, r2 = 70, h = 50, size = 20, relief = 1.2);
//
//   // engraved letters on a cylinder
//   difference() {
//     cylinder(r = 30, h = 40);
//     cylinder_text_cut("FIDO", r = 30, h = 40, size = 12, depth = 1);
//   }
//
// How it works: each letter is drawn flat, stood on the plane tangent to the
// wall at the letter's centre, tilted with the wall, and extruded through the
// wall; intersecting that with a shell of the wall gives letters that follow
// the curved surface. Letters are spaced along the arc by their advance
// widths, so the word is centred on `azimuth` with proportional spacing.

// Advance widths of Arial Bold at size 1 (measured with OpenSCAD's
// textmetrics()). OpenSCAD has no stable way to measure a glyph without the
// experimental textmetrics feature, so proportional spacing uses this table;
// characters missing from it get wt_default_advance. Other fonts are spaced
// approximately; tune with `spacing`.
wt_glyph_chars = str(
  "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
  "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
  "0123456789 -!?.,'\"&+");
wt_glyph_advance = [
  1.003, 0.998, 1.003, 0.787, 0.989, 0.926, 0.929, 1.255, 0.870, 0.998, 0.998, 0.848, 0.975, 1.157, 1.003, 1.080,
  0.998, 0.926, 1.003, 0.848, 0.864, 1.185, 0.926, 1.015, 0.976, 1.396, 1.415, 1.208, 1.360, 0.998, 0.988, 1.432,
  0.998, 0.772, 0.858, 0.854, 0.578, 0.882, 0.772, 0.772, 0.985, 0.690, 0.854, 0.854, 0.695, 0.882, 1.027, 0.839,
  0.848, 0.839, 0.848, 0.772, 0.680, 0.772, 1.215, 0.772, 0.854, 0.806, 1.158, 1.172, 1.013, 1.186, 0.854, 0.767,
  1.186, 0.810, 1.003, 1.003, 1.003, 1.003, 0.926, 0.848, 1.080, 1.003, 0.386, 0.772, 1.003, 0.848, 1.157, 1.003,
  1.080, 0.926, 1.080, 1.003, 0.926, 0.848, 1.003, 0.926, 1.311, 0.926, 0.926, 0.848, 0.772, 0.848, 0.772, 0.848,
  0.772, 0.463, 0.848, 0.848, 0.386, 0.386, 0.772, 0.386, 1.235, 0.848, 0.848, 0.848, 0.848, 0.541, 0.772, 0.463,
  0.848, 0.772, 1.080, 0.772, 0.772, 0.694, 0.772, 0.772, 0.772, 0.772, 0.772, 0.772, 0.772, 0.772, 0.772, 0.772,
  0.386, 0.463, 0.463, 0.848, 0.386, 0.386, 0.330, 0.659, 1.003, 0.811];
assert(len(wt_glyph_chars) == len(wt_glyph_advance), "WriteText: glyph table out of sync");
wt_default_advance = 0.85;

// Baseline offset (size 1) that centres Arial's band from descender (-0.29)
// to cap height (0.99) on the text centre line.
wt_default_baseline = -0.35;

wt_default_font = "Arial:style=Bold";

// Advance width of character `c` at size 1.
function text_advance(c) =
  let(hits = [for (i = [0:1:len(wt_glyph_chars) - 1]) if (wt_glyph_chars[i] == c) wt_glyph_advance[i]])
  len(hits) > 0 ? hits[0] : wt_default_advance;

// Width of `txt` along the wall, in mm, at letter height `size`.
function text_width(txt, size, spacing = 1) =
  wt_sum([for (c = txt) text_advance(c) * size * spacing]);

function wt_sum(v, n = undef) =
  let(k = is_undef(n) ? len(v) : n) k <= 0 ? 0 : wt_sum(v, k - 1) + v[k - 1];

// Raised letters on the outside of a cone wall (r1 at z=0, r2 at z=h, the
// same arguments as cylinder()). The letters stand `relief` mm proud of the
// wall, measured along the wall normal, and touch the wall without
// overlapping it, so they can be a separate part/colour.
//   size     capital letter height along the sloped wall
//   z        height of the text centre line (default: middle of the wall)
//   azimuth  direction the middle of the word faces, degrees around Z
//   spacing  extra letter spacing factor (1 = the font's own spacing)
//   baseline baseline offset at size 1 that centres the glyphs on z
module cone_text(txt, r1, r2, h, size, relief = 1, font = wt_default_font,
                 spacing = 1, azimuth = -90, z = undef, baseline = wt_default_baseline) {
  angle = atan((r1 - r2) / h);
  difference() {
    intersection() {
      wt_letter_blanks(txt, r1, r2, h, size, relief, font, spacing, azimuth, z, baseline);
      wt_cone(r1, r2, h, relief / cos(angle));
    }
    wt_cone(r1, r2, h);
  }
}

// Volume to subtract from a cone to engrave `txt` `depth` mm into its wall
// (measured along the wall normal). Same arguments as cone_text().
module cone_text_cut(txt, r1, r2, h, size, depth = 1, font = wt_default_font,
                     spacing = 1, azimuth = -90, z = undef, baseline = wt_default_baseline) {
  angle = atan((r1 - r2) / h);
  difference() {
    wt_letter_blanks(txt, r1, r2, h, size, depth, font, spacing, azimuth, z, baseline);
    wt_cone(r1, r2, h, -depth / cos(angle));
  }
}

// cone_text() on a straight cylinder of radius r.
module cylinder_text(txt, r, h, size, relief = 1, font = wt_default_font,
                     spacing = 1, azimuth = -90, z = undef, baseline = wt_default_baseline) {
  cone_text(txt, r, r, h, size, relief, font, spacing, azimuth, z, baseline);
}

// cone_text_cut() on a straight cylinder of radius r.
module cylinder_text_cut(txt, r, h, size, depth = 1, font = wt_default_font,
                         spacing = 1, azimuth = -90, z = undef, baseline = wt_default_baseline) {
  cone_text_cut(txt, r, r, h, size, depth, font, spacing, azimuth, z, baseline);
}

// The cone surface pushed out along the horizontal by dr (dr < 0 pulls it in).
module wt_cone(r1, r2, h, dr = 0) {
  cylinder(r1 = r1 + dr, r2 = r2 + dr, h = h);
}

// Flat letters standing on the plane tangent to the wall at the text centre
// line, extruded along the wall normal far enough to pass through the whole
// shell `reach` mm outside and the wall's curvature inside.
module wt_letter_blanks(txt, r1, r2, h, size, reach, font, spacing, azimuth, z, baseline) {
  zc = is_undef(z) ? h / 2 : z;
  angle = atan((r1 - r2) / h);
  rc = r1 + (r2 - r1) * zc / h;              // wall radius at the centre line
  n = len(txt);
  adv = [for (c = txt) text_advance(c) * size * spacing];
  total = wt_sum(adv);
  // How far the wall curves away from the tangent plane under one letter.
  // Letters are at most ~1.45 * size wide, so `size` bounds the half-width,
  // and the wall radius under the letter is at least rc - size.
  rl = rc - size;
  sag = rl > size ? rl - sqrt(rl * rl - size * size) : rc;
  thick = 2 * (reach + sag + 1);
  for (i = [0:1:n - 1]) if (txt[i] != " ") {
    arc = wt_sum(adv, i) + adv[i] / 2 - total / 2;
    rotate([0, 0, azimuth + arc / rc * 180 / PI])
      translate([rc, 0, zc])
        rotate([0, -angle, 0])       // lean back with the wall
          rotate([90, 0, 90])        // stand up, facing +X
            linear_extrude(height = thick, center = true)
              translate([-adv[i] / 2, baseline * size])
                text(txt[i], size = size, font = font, halign = "left", valign = "baseline");
  }
}
