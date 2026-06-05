// examples/cube.scad — a trivial test part for the `3d` CLI.
// A rounded-ish hollow box with a couple of Customizer parameters, so
// `3d params`, `render`, `export`, `mesh`, `check`, `printability` all have
// something real to chew on.

width  = 20;   // [10:40] outer width (mm)
depth  = 20;   // [10:40] outer depth (mm)
height = 16;   // [8:30]  outer height (mm)
wall   = 2;    // [1.2:4] wall thickness (mm)

module hollow_box(w, d, h, t) {
    difference() {
        cube([w, d, h], center = true);
        // inner cavity, open-topped, leaves a floor of thickness t
        translate([0, 0, t])
            cube([w - 2 * t, d - 2 * t, h], center = true);
    }
}

hollow_box(width, depth, height, wall);
