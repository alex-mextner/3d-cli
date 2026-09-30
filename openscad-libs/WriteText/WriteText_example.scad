// WriteText examples: raised Cyrillic on a cone, engraved Latin on a cylinder.
// Render: openscad -o example.stl WriteText_example.scad
use <WriteText/WriteText.scad>

$fn = 60;

// Raised letters on a tapered cup (r1 = 40 at the bottom, r2 = 32 at the top).
color("gold") cylinder(r1 = 40, r2 = 32, h = 30);
color("royalblue") cone_text("Привет", r1 = 40, r2 = 32, h = 30, size = 10, relief = 1);

// Engraved letters on a straight cylinder next to it.
translate([100, 0, 0])
  difference() {
    cylinder(r = 30, h = 30);
    cylinder_text_cut("Hello", r = 30, h = 30, size = 10, depth = 1);
  }
