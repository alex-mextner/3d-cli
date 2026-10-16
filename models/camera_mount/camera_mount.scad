// Top-down Logitech C920 mount for the Creality K1 SE and the Snapmaker U1 (issue #338).
//
// WHAT: a 20x20 aluminium square tube (two halves) laid diagonally across the printer top.
//   Four printed feet clamp on the tube once and never move; a lift-off shoe under each foot
//   indexes it to the printer (K1 SE: a V-lip that drops into an inner top corner; U1: a peg
//   that drops into a Top Cover silicone pad). The centre hub joins the halves around an open
//   window; the C920 looks down through it from a post with 10 mm pin detents and a
//   three-screw spring tilt plate. Optional: Pi Zero W tray, cable clips, a 4-LED diffuse ring.
// WHY this shape: the bridge rests on two kinematic points (two opposite corners or two pads),
//   so it lands in the same place every time without tools; all four feet stay on the tube,
//   so moving the rig between printers changes no setting — only which pair of shoes is on.
// Every dimension marked MEASURE is an estimate until the user measures it (README.ru.md, §2).
//
// Usage: openscad -D 'part="hub"' -o hub.stl camera_mount.scad   (see PARTS below)

part = "assembly";   // PARTS: assembly | exploded | hub | foot | shoe_k1 | shoe_u1 | post | arm
                     //   | cam_plate | knob_m5 | knob_m4 | pin_knob | pi_tray | cable_clip
                     //   | led_ring | dowel | plate_hub | plate_small | plate_extras | plate_led | fit_*
printer = "k1se";    // assembly view: k1se | u1
pin_hole = 5;        // post hole the height pin goes through, counted from the post bottom:
                     // 2 (camera highest) .. 5 (camera lowest)

$fn = 64;
eps = 0.01;
fit = 0.2;           // clearance per side for printed sliding fits

// ---------- bought parts ----------
tube = 20;           // aluminium square tube 20x20x1.5 (two halves)
tube_gap = 0.4;      // tube clearance per socket (total)
tube_half = 300;     // length of one tube half
tg = tube + tube_gap;
m5_d = 5.6;   m5_nut_af = 8.3; m5_nut_h = 4.4; m5_head_af = 8.3; m5_head_h = 3.8;
m4_d = 4.5;   m4_nut_af = 7.2; m4_nut_h = 3.4; m4_head_af = 7.2; m4_head_h = 3.0;
tripod_d = 6.8;      // 1/4"-20 camera screw clearance
spring_d = 9;        // bed-levelling spring seat diameter
spring_gap = 12;     // arm-to-plate gap with the springs half compressed

// ---------- printer interfaces (MEASURE) ----------
k1_outer = 355;      // K1 SE top frame outer size (spec 355x355x482)
k1_rim = 25;         // MEASURE K1-1: top rail width, outer edge to inner edge
k1_corner_r = 6;     // MEASURE K1-2: inner corner fillet radius of the top opening
k1_lip_depth = 6;    // MEASURE K1-3: free depth of the inner wall under the rail top
k1_rim_to_bed = 340; // MEASURE K1-4: rail top to bed at Z max (estimate)
u1_pad_dx = 430;     // MEASURE U1-1: silicone pad centres, left-right
u1_pad_dy = 400;     // MEASURE U1-1: silicone pad centres, front-back
u1_hole_d = 8;       // MEASURE U1-2: silicone pad hole diameter
u1_hole_depth = 6;   // MEASURE U1-2: silicone pad hole depth
u1_outer = [480, 450]; // U1 top outline for the render only (estimate)
u1_rim_to_bed = 330; // MEASURE U1-4: rim top to bed at Z max (estimate)

// ---------- feet and shoes ----------
wall = 5;            // sleeve side walls
bot_wall = 4;        // sleeve wall under the tube
top_wall = 11;       // sleeve wall over the tube (holds the M5 nut under a peaked roof)
foot_len = 30;       // sleeve length along the tube
foot_base = 2;       // extra height under the sleeve
key = 12;            // square key on the foot bottom
key_h = 9;
// tube channel: 45-degree chamfers in all four corners keep every bridge over it at 14.4 mm
// (the K1 SE bridge limit is 15), whichever way up the part prints; the tube sits on the two
// lower chamfers (a V seat that centres it) and the M5 clamp presses it down into them
ch = 3;
chan_h = 26;
tube_seat = ch - (tg - tube) / 2;   // tube underside above the channel floor
blk_w = tg + 2 * wall;
sleeve_h = bot_wall + chan_h + top_wall;
shoe_t = key_h + 5;  // shoe thickness on the rim (holds the key pocket; printed top-down)
k1_s = 20;           // key centre to the K1 inner corner, along the tube
k1_a = 30;           // K1 pad size behind the corner
k1_lip_len = 32;
k1_lip_t = 3;
u1_pad = [30, 50];   // U1 shoe pad (along, across the tube)
peg_d = u1_hole_d - 0.4;
peg_len = min(5, u1_hole_depth - 0.5);

// heights above the printer top surface (z = 0)
foot_z = shoe_t;                          // foot bottom
tube_bot = foot_z + foot_base + bot_wall + tube_seat; // tube underside
tube_c = tube_bot + tg / 2;

// ---------- hub ----------
win_r = 50;          // viewing window radius
ring_w = 12;
R_o = win_r + ring_w;
ring_h = 20;         // window ring height (sockets and post block stay full height)
sock_len = 40;
sock_x0 = R_o + 2;   // socket floor (tube end stop)
hub_half = sock_x0 + sock_len;
hub_z = tube_bot - tube_seat - bot_wall;  // hub underside
hub_h = sleeve_h;
post = [20, 12];     // post section (along, across the tube)
post_len = 120;
post_pitch = 10;
pc_w = post[0] + 2 * fit + 12;            // post channel block
pc_y0 = R_o + 2;                          // post channel inner face (hub frame)
pc_h = 50;
pc_pin_z = pc_h - 10;                     // pin height in the channel block
post_y = pc_y0 + fit + post[1] / 2;       // post centre line
led_r = win_r + ring_w / 2;               // LED ring dowel circle
led_ang = [45, 135, 225, 315];

// ---------- arm, tilt plate, camera ----------
arm_t = 10;
boss = [post[0] + 2 * fit + 10, post[1] + 2 * fit + 10];
boss_h = 25;
sock_d = 22;         // post depth in the arm boss
tilt_r = 26;         // tilt screw circle
tilt_ang = [135, 15, 255];
pad_r = 36;
plate_t = 6;
cam_yaw = -45;       // tab direction (hub frame); image x then runs along the printer X
cam_axis_off = 29;   // MEASURE C-2: C920 lens axis to the clip-base face (spec: 43.3 with clip, body 29)
tab_w = 30; tab_t = 5;
tab_screw_z = 35;    // tripod screw below the plate
tab_len = tab_screw_z + 10;
cam_lens_below_screw = 15; // MEASURE C-1: lens face below the tripod screw axis

// post placement for the assembly view
post_bottom_z = hub_z + pc_pin_z - post_pitch * pin_hole;
// usable holes: the post stays 30+ mm in the channel and never reaches below the printer top
assert(pin_hole == floor(pin_hole) && post_bottom_z >= 0 && post_bottom_z <= hub_z + pc_h - 30,
       str("pin_hole ", pin_hole, " is out of range: a whole hole number 2..5"));
assert(printer == "k1se" || printer == "u1", str("printer \"", printer, "\" is not k1se or u1"));
arm_bot_z = post_bottom_z + post_len - sock_d + boss_h;
plate_top_z = arm_bot_z - spring_gap;
lens_z = plate_top_z - plate_t - tab_screw_z - cam_lens_below_screw;

// ---------- design invariants (3d check --consistency) ----------
k1_pin_d = (k1_outer / 2 - k1_rim) * sqrt(2) + k1_s;
u1_pin_d = sqrt(u1_pad_dx * u1_pad_dx + u1_pad_dy * u1_pad_dy) / 2;
assert(sock_x0 + tube_half >= u1_pin_d + foot_len / 2 + 10, "tube half too short for the U1 pads");
assert(sock_x0 + tube_half >= k1_pin_d + foot_len / 2 + 10, "tube half too short for the K1 SE corners");
assert(2 * hub_half <= 210, "hub does not fit the K1 SE bed");
assert(peg_d < u1_hole_d && peg_len > 1, "U1 peg must enter the pad hole");
// every ray from the lens to the far bed corner (U1 270 mm bed + 10 mm) clears the hub window
// at the hub's underside and the LED ring's bore at the ring's underside (the rays spread
// downward, so the lowest plane of each opening is the binding one)
function ray_r(z) = (lens_z - z) * (sqrt(2) * 140) / (lens_z + u1_rim_to_bed);
assert(ray_r(hub_z) < win_r, "the hub window cuts the view at this lens height");
assert(ray_r(hub_z - led_t) < led_id / 2, "the LED ring cuts the view at this lens height");
echo(str("lens above printer top = ", lens_z, " mm; lens to bed: K1 SE ",
         lens_z + k1_rim_to_bed, " mm, U1 ", lens_z + u1_rim_to_bed, " mm"));

// ================= helpers =================
module hex(af, h) { cylinder(d = af / cos(30), h = h, $fn = 6); }

// the tube channel along +x from x = 0, floor at z = 0
module tube_channel(len) {
    rotate([90, 0, 90]) linear_extrude(len) polygon([
        [-tg / 2 + ch, 0], [tg / 2 - ch, 0], [tg / 2, ch], [tg / 2, chan_h - ch],
        [tg / 2 - ch, chan_h], [-tg / 2 + ch, chan_h], [-tg / 2, chan_h - ch], [-tg / 2, ch]]);
}

module knob(d, h, af, pocket, hole) {
    difference() {
        cylinder(d = d, h = h);
        for (i = [0 : 7]) rotate(i * 45) translate([d / 2 + 1, 0, -1]) cylinder(d = 5, h = h + 2, $fn = 24);
        translate([0, 0, -eps]) hex(af, pocket);
        translate([0, 0, -1]) cylinder(d = hole, h = h + 2, $fn = 24);
        translate([0, 0, h - 0.8]) difference() {   // soften the top edge
            cylinder(d = d + 2, h = 1);
            cylinder(d1 = d, d2 = d - 1.6, h = 0.8 + eps);
        }
    }
}

// vertical M5 clamp: a bolt hole from the top into the tube channel, and a nut slot that opens
// into the channel roof and out of the -y side, so the nut drops in before the tube goes in.
// The slot has a 45-degree peaked roof: no bridge over the nut when the part prints upright,
// and no ceiling at all when it prints upside down (the feet).
module m5_clamp(z_cav_top, h_total) {
    w = m5_nut_af + 0.2;
    translate([0, 0, z_cav_top - 1]) cylinder(d = m5_d, h = h_total, $fn = 24);
    // the slot runs to the far chamfer, so no stub of channel roof is left hanging past it
    translate([0, tg / 2 - ch, z_cav_top - eps]) rotate([90, 0, 0]) linear_extrude(50 + tg / 2 - ch)
        polygon([[-w / 2, 0], [w / 2, 0], [w / 2, m5_nut_h], [0, m5_nut_h + w / 2], [-w / 2, m5_nut_h]]);
}

// ================= hub =================
module hub() {
    difference() {
        union() {
            cylinder(r = R_o, h = ring_h, $fn = 128);   // the window ring ties the two sockets
            for (sx = [-1, 1]) {
                x0 = sx > 0 ? R_o - 12 : -hub_half;
                translate([x0, -blk_w / 2, 0]) cube([hub_half - R_o + 12, blk_w, hub_h]);
            }
            translate([-pc_w / 2, R_o - 8, 0]) cube([pc_w, pc_y0 - R_o + 8 + post[1] + 2 * fit + 6, pc_h]);
        }
        translate([0, 0, -1]) cylinder(r = win_r, h = pc_h + 2, $fn = 128);
        for (sx = [-1, 1]) {
            x0 = sx > 0 ? sock_x0 : -hub_half - 1;
            translate([x0, 0, bot_wall]) tube_channel(sock_len + 1);
            translate([sx * (sock_x0 + sock_len / 2), 0, 0]) m5_clamp(bot_wall + chan_h, top_wall + 2);
        }
        // post channel and the height-pin hole along x
        translate([-post[0] / 2 - fit, pc_y0, -1]) cube([post[0] + 2 * fit, post[1] + 2 * fit, pc_h + 2]);
        translate([-pc_w / 2 - 1, post_y, pc_pin_z]) rotate([0, 90, 0]) cylinder(d = m4_d + 0.1, h = pc_w + 2, $fn = 24);
        // LED ring dowels (snug)
        for (a = led_ang) rotate(a) translate([led_r, 0, -eps]) cylinder(d = 5.5, h = 8, $fn = 24);
    }
}

// ================= feet and shoes =================
// foot frame: tube along x, foot bottom at z = 0, key down from z = 0
module foot() {
    difference() {
        union() {
            translate([-foot_len / 2, -blk_w / 2, 0]) cube([foot_len, blk_w, foot_base + sleeve_h]);
            // square key: indexes the shoe in x, y and yaw (no chamfer: the 0.2 mm gap is the
            // whole fit, and `3d fit-check` holds it over the full key length)
            translate([-key / 2, -key / 2, -key_h]) cube([key, key, key_h + eps]);
        }
        translate([-foot_len / 2 - 1, 0, foot_base + bot_wall]) tube_channel(foot_len + 2);
        m5_clamp(foot_base + bot_wall + chan_h, top_wall + 2);
    }
}

module key_pocket() {
    translate([-key / 2 - fit, -key / 2 - fit, shoe_t - key_h - 0.5]) cube([key + 2 * fit, key + 2 * fit, key_h + 1]);
}

// K1 SE shoe, foot frame (key at the origin). The inner top corner of the frame is at
// x = +k1_s on the tube line; the opening is the 90-degree sector facing +x. Pad on the rail,
// two lips hang inside the opening against the two inner walls and stop short of the fillet.
module shoe_k1() {
    difference() {
    translate([k1_s, 0, 0]) rotate(-45) {
        difference() {
            union() {
                translate([-k1_a, -k1_a, 0]) cube([k1_a, k1_a, shoe_t]);
                translate([-k1_a, 0, 0]) cube([k1_a, k1_lip_len, shoe_t]);
                translate([0, -k1_a, 0]) cube([k1_lip_len, k1_a, shoe_t]);
                // lips: against the wall x = 0 (inside, x > 0) and the wall y = 0
                translate([0, k1_corner_r + 1, -k1_lip_depth]) cube([k1_lip_t, k1_lip_len - k1_corner_r - 1, k1_lip_depth + shoe_t]);
                translate([k1_corner_r + 1, 0, -k1_lip_depth]) cube([k1_lip_len - k1_corner_r - 1, k1_lip_t, k1_lip_depth + shoe_t]);
            }
            // keep the pad off the opening, except where the lips are
            translate([k1_lip_t, k1_lip_t, -k1_lip_depth - 1]) cube([k1_lip_len + 1, k1_lip_len + 1, shoe_t + k1_lip_depth + 2]);
        }
    }
    key_pocket();
    }
}

// U1 shoe: a pad on the top surface and a peg that drops into the silicone pad hole
module shoe_u1() {
    difference() {
        translate([-u1_pad[0] / 2, -u1_pad[1] / 2, 0]) cube([u1_pad[0], u1_pad[1], shoe_t]);
        key_pocket();
    }
    // peg with a 1 mm lead-in chamfer
    translate([0, 0, -peg_len + 1]) cylinder(d = peg_d, h = peg_len - 1 + eps, $fn = 32);
    translate([0, 0, -peg_len]) cylinder(d1 = peg_d - 2, d2 = peg_d, h = 1 + eps, $fn = 32);
}

// ================= post, arm, tilt plate =================
// post frame: bottom at z = 0, holes along x at z = 10, 20, ... post_len - 10
module post_part() {
    difference() {
        translate([-post[0] / 2, -post[1] / 2, 0]) cube([post[0], post[1], post_len]);
        for (z = [post_pitch : post_pitch : post_len - post_pitch / 2])
            translate([-post[0] / 2 - 1, 0, z]) rotate([0, 90, 0]) cylinder(d = m4_d, h = post[0] + 2, $fn = 24);
        for (z = [post_pitch * 5 : post_pitch * 5 : post_len - 1])   // depth marks every 50 mm
            translate([-post[0] / 2 - 1, -post[1] / 2 - eps, z - 0.5]) cube([post[0] + 2, 0.6, 1]);
    }
}

// arm frame (hub frame shifted so the arm underside is z = 0): pad over the lens axis at the
// origin, boss for the post at y = post_y, three M4 screws pointing down (heads on top)
module arm() {
    difference() {
        union() {
            hull() {
                cylinder(r = pad_r, h = arm_t);
                translate([-boss[0] / 2, post_y - boss[1] / 2, 0]) cube([boss[0], boss[1], arm_t]);
            }
            translate([-boss[0] / 2, post_y - boss[1] / 2, -boss_h]) cube([boss[0], boss[1], boss_h + eps]);
        }
        translate([-post[0] / 2 - fit, post_y - post[1] / 2 - fit, -boss_h - 1]) cube([post[0] + 2 * fit, post[1] + 2 * fit, sock_d + 1]);
        translate([-boss[0] / 2 - 1, post_y, -boss_h + sock_d - post_pitch]) rotate([0, 90, 0]) cylinder(d = m4_d + 0.1, h = boss[0] + 2, $fn = 24);
        for (a = tilt_ang) rotate(a) translate([tilt_r, 0, 0]) {
            translate([0, 0, -1]) cylinder(d = m4_d, h = arm_t + 2, $fn = 24);
            translate([0, 0, arm_t - m4_head_h - 0.3]) hex(m4_head_af + 0.2, m4_head_h + 1);
            translate([0, 0, -eps]) cylinder(d = spring_d + 0.6, h = 1);
        }
        // the lens sits under this pad: mark its axis for alignment
        translate([0, 0, arm_t - 0.6]) cylinder(d = 3, h = 1, $fn = 16);
    }
}

// camera plate frame: plate top at z = 0, tab hangs below, lens axis at the origin
module cam_plate() {
    difference() {
        union() {
            hull() {
                translate([0, 0, -plate_t]) cylinder(r = pad_r, h = plate_t);
                // reaches past the tab under the gusset, so the gusset prints on the plate
                rotate(cam_yaw) translate([cam_axis_off, -tab_w / 2, -plate_t]) cube([tab_t + 11, tab_w, plate_t]);
            }
            rotate(cam_yaw) translate([cam_axis_off, -tab_w / 2, -plate_t - tab_len + eps]) cube([tab_t, tab_w, tab_len]);
            // gusset between tab and plate
            rotate(cam_yaw) translate([cam_axis_off + tab_t - eps, 0, -plate_t]) rotate([90, 0, 0])
                linear_extrude(4, center = true) polygon([[0, 0], [10, 0], [0, -14]]);
        }
        for (a = tilt_ang) rotate(a) translate([tilt_r, 0, 0]) {
            translate([0, 0, -plate_t - 1]) cylinder(d = m4_d + 0.6, h = plate_t + 2, $fn = 24);
            translate([0, 0, -1 + eps]) cylinder(d = spring_d + 0.6, h = 1);
        }
        rotate(cam_yaw) translate([cam_axis_off - 1, 0, -plate_t - tab_screw_z]) rotate([0, 90, 0]) cylinder(d = tripod_d, h = tab_t + 2, $fn = 32);
        // axis mark on the plate: line it up with the arm mark
        translate([0, 0, -0.6]) cylinder(d = 3, h = 1, $fn = 16);
    }
}

// ================= Pi Zero W tray, cable clip =================
// snap U that clips onto the tube from below: base under the tube, walls up its sides, lips
// over its top corners. Print frame = use frame (base on the bed, opening up).
snap_base = 2.5;
snap_wall = 2.4;
module tube_snap(len) {
    lip = 1.2;
    z0 = snap_base + tg + 0.2 + lip;    // the tube's top corner meets the lip's 45-degree underside
    difference() {
        translate([-len / 2, -tg / 2 - snap_wall, 0]) cube([len, tg + 2 * snap_wall, z0 + 2]);
        translate([-len / 2 - 1, -tg / 2, snap_base]) cube([len + 2, tg, tg + 10]);
    }
    // lip section: a 45-degree underside (prints without support) catches the tube's top
    // corner, the slope above lets it snap in; 0.5 mm into the wall
    for (s = [-1, 1]) translate([-len / 2, s * tg / 2, z0 - lip]) rotate([90, 0, 90]) linear_extrude(len)
        polygon([[s * 0.5, 0], [0, 0], [-s * lip, lip], [-s * lip, lip + 0.9], [0, lip + 1.8], [s * 0.5, lip + 1.8]]);
}

// Pi Zero W tray (board 65 x 30, M2.5 holes 58 x 23): hangs under the tube on a snap, the
// board lies beside the tube on four pins; an elastic band in the grooves keeps it down
module pi_tray() {
    tray = [72, 36, 3];
    y0 = tg / 2 + snap_wall - eps;
    tube_snap(30);
    translate([-tray[0] / 2, y0, 0]) difference() {
        cube(tray);
        translate([tray[0] / 2 - 18, tray[1] / 2 - 6, -1]) cube([36, 12, 5]);   // air under the SoC
        for (x = [10, tray[0] - 14]) translate([x, -1, 1.5]) cube([4, tray[1] + 2, 2]);  // band grooves
    }
    for (x = [-29, 29], y = [-11.5, 11.5]) translate([x, y0 + tray[1] / 2 + y, tray[2] - eps]) {
        cylinder(d = 5, h = 3, $fn = 24);
        cylinder(d = 2.3, h = 5.5, $fn = 20);
        translate([0, 0, 5.5]) sphere(d = 2.3, $fn = 20);
    }
}

// cable clip: snap under the tube, hook for a USB cable up to 5 mm beside it
module cable_clip() {
    tube_snap(14);
    translate([-7, tg / 2 + snap_wall + 4, 0]) rotate([90, 0, 90]) linear_extrude(14) difference() {
        hull() {
            translate([0, 5.5]) circle(d = 11, $fn = 32);
            translate([-5.5, 0]) square([11, 1]);
            translate([-4.5, 0]) square([1, 11]);   // fill the notch against the snap wall
        }
        translate([0, 5.5]) circle(d = 5.4, $fn = 32);
        translate([-1.7, 5.5]) square([3.4, 8]);
        translate([-6, 10]) square([12, 5]);   // blunt the horn tips beside the slot
    }
}

// ================= LED ring (optional) =================
// ring frame: ring top at z = 0 (against the hub underside), pods below, aligned with the
// printer X/Y axes; a notch lets the post pass at its lowest setting
led_id = 130; led_od = 190; led_t = 4;
pod = [46, 20, 12];   // tangential, radial, height
pod_r = led_od / 2 - pod[1] / 2 - 2;
module led_ring() {
    difference() {
        union() {
            translate([0, 0, -led_t]) difference() {
                cylinder(d = led_od, h = led_t, $fn = 160);
                translate([0, 0, -1]) cylinder(d = led_id, h = led_t + 2, $fn = 160);
            }
            for (a = led_ang) rotate(a) translate([pod_r, 0, -led_t - pod[2] / 2 + eps])
                cube([pod[1], pod[0], pod[2]], center = true);
        }
        for (a = led_ang) rotate(a) {
            translate([led_r, 0, -led_t - 1]) cylinder(d = 6.0, h = led_t + 2, $fn = 24);
            translate([pod_r, 0, 0]) {
                // strip cavity (LED strip up to 12 mm wide, 3 LEDs), open downward
                translate([-7, -21, -led_t - pod[2] - 1]) cube([14, 42, pod[2] - 1]);
                // rebate at the cavity mouth for a 1.5 mm diffuser strip 17 x 44 (glue or tape it)
                translate([-8.5, -22, -led_t - pod[2] - 1]) cube([17, 44, 2.6]);
                // wire hole up through the ring
                translate([0, 18, -led_t - 2]) cylinder(d = 4, h = led_t + 4, $fn = 20);
            }
        }
        translate([-post[0] / 2 - 2, pc_y0 - 4, -led_t - 1]) cube([post[0] + 4, led_od, led_t + 2]);
    }
}

// dowel: head under the ring, shank through the ring into the hub underside
module dowel() {
    cylinder(d = 9, h = 2);
    cylinder(d = 5.6, h = 2 + led_t + 7.5, $fn = 24);
}

// ================= assembly views =================
module c920() {   // dummy C920: clip base against the tab, body, lens
    rotate(cam_yaw) {
        color("dimgray") translate([cam_axis_off - 4, -16, -plate_t - tab_screw_z - 20]) cube([4, 32, 40]);
        color("black") hull() for (s = [-1, 1]) translate([0, s * 32.5, -plate_t - tab_screw_z - cam_lens_below_screw])
            scale([14.5 / 12, 1, 1]) cylinder(r = 12, h = 24);
        color("silver") translate([0, 0, lens_z - plate_top_z - 0.5]) cylinder(d = 14, h = 1);
    }
}

// explode = 0 for the installed view; > 0 lifts parts apart along z for the exploded view
module mount_on(pr, explode = 0) {
    pin_d = pr == "u1" ? u1_pin_d : k1_pin_d;
    other_d = pr == "u1" ? k1_pin_d : u1_pin_d;
    ang = pr == "u1" ? -atan2(u1_pad_dy, u1_pad_dx) : -45;
    e = explode;
    rotate(ang) {
        color("orange") translate([0, 0, hub_z + 2 * e]) hub();
        color("silver") for (sx = [-1, 1]) translate([sx > 0 ? sock_x0 + e : -sock_x0 - tube_half - e, -tube / 2, tube_bot + 2 * e])
            difference() {
                cube([tube_half, tube, tube]);
                translate([-1, 1.5, 1.5]) cube([tube_half + 2, tube - 3, tube - 3]);
            }
        for (sx = [-1, 1]) {
            translate([sx * pin_d, 0, foot_z + 2 * e]) color("orange") foot();
            translate([sx * pin_d, 0, 0]) rotate(sx > 0 ? 180 : 0) color("darkorange")
                if (pr == "u1") shoe_u1(); else shoe_k1();
            translate([sx * other_d, 0, foot_z + 2 * e]) color("orange") foot();   // the other pair, shoes off
        }
        color("orange") translate([0, post_y, post_bottom_z + 3 * e]) post_part();
        color("orange") translate([0, 0, arm_bot_z + 4 * e]) arm();
        translate([0, 0, plate_top_z + 4 * e - (e > 0 ? e : 0)]) {
            color("orange") cam_plate();
            c920();
        }
        color("orange", 0.9) translate([0, 0, hub_z + e]) led_ring();
        tray_x = -165;   // between the hub and the K1 SE foot: the same spot on both printers
        color("orange") translate([tray_x, 0, tube_bot - snap_base + e]) pi_tray();
        for (x = [-115, 115]) color("orange") translate([x, 0, tube_bot - snap_base + e]) cable_clip();
    }
}

module ghost(pr) {
    color("slategray", 0.45) if (pr == "u1") {
        difference() {
            translate([-u1_outer[0] / 2, -u1_outer[1] / 2, -40]) cube([u1_outer[0], u1_outer[1], 40]);
            translate([-u1_pad_dx / 2 + 20, -u1_pad_dy / 2 + 20, -41]) cube([u1_pad_dx - 40, u1_pad_dy - 40, 42]);
            for (x = [-1, 1], y = [-1, 1]) translate([x * u1_pad_dx / 2, y * u1_pad_dy / 2, -u1_hole_depth]) cylinder(d = u1_hole_d, h = u1_hole_depth + 1);
        }
        translate([-135, -135, -u1_rim_to_bed - 6]) cube([270, 270, 6]);
    } else {
        difference() {
            translate([-k1_outer / 2, -k1_outer / 2, -30]) cube([k1_outer, k1_outer, 30]);
            translate([-k1_outer / 2 + k1_rim, -k1_outer / 2 + k1_rim, -31]) cube([k1_outer - 2 * k1_rim, k1_outer - 2 * k1_rim, 32]);
        }
        translate([-110, -110, -k1_rim_to_bed - 6]) cube([220, 220, 6]);
    }
}

// ================= print orientation (each part flat on z = 0, no supports) =================
module print_foot() { translate([0, 0, foot_base + sleeve_h]) rotate([180, 0, 0]) foot(); }
module print_shoe_k1() { translate([0, 0, shoe_t]) rotate([180, 0, 0]) shoe_k1(); }
module print_shoe_u1() { translate([0, 0, shoe_t]) rotate([180, 0, 0]) shoe_u1(); }
module print_arm() { translate([0, 0, arm_t]) rotate([180, 0, 0]) arm(); }
module print_cam_plate() { rotate([180, 0, 0]) cam_plate(); }
module print_led_ring() { rotate([180, 0, 0]) led_ring(); }
// the post lies on its 12 mm side, so the pin holes are vertical
module print_post() { translate([-post_len / 2, 0, post[0] / 2]) rotate([0, 90, 0]) post_part(); }
module knob_m5() { knob(24, 10, m5_head_af, m5_head_h, m5_d); }
module knob_m4() { knob(18, 9, m4_nut_af, m4_nut_h, m4_d); }
module pin_knob() { knob(18, 9, m4_head_af, m4_head_h, m4_d); }

// ================= print plates (K1 SE bed 220 x 220; everything inside 210 x 210) =================
module plate_hub() {
    hub();
    translate([0, -80, 0]) print_post();
    for (x = [-88, -62]) translate([x, 90, 0]) pin_knob();   // height pin + arm pin
    for (i = [0 : 3]) translate([70 + (i % 2) * 14, 82 + floor(i / 2) * 14, 0]) dowel();
}

module plate_small() {
    for (i = [0 : 3]) translate([-90 + i * 40, -50, 0]) print_foot();
    for (x = [-70, 10]) translate([x, 50, 0]) print_shoe_k1();
    for (y = [60, 0]) translate([82, y, 0]) print_shoe_u1();
    for (i = [0 : 5]) translate([-90 + i * 27, -90, 0]) knob_m5();
    for (i = [0 : 2]) translate([82, -40 - i * 22, 0]) knob_m4();
    translate([60, -40, 0]) knob_m4();   // the nut knob that keeps the height pin in
}

module plate_extras() {
    translate([-60, 20, 0]) print_arm();
    translate([20, 55, 0]) print_cam_plate();
    translate([40, -40, 0]) pi_tray();
    for (y = [60, 10, -40]) translate([92, y, 0]) cable_clip();
}

// ================= dispatch =================
if (part == "assembly") { ghost(printer); mount_on(printer); }
else if (part == "exploded") mount_on(printer, 40);
// close-ups for the guide: the rear-left corner (K1 SE) or pad (U1) moved to the origin
else if (part == "detail") {
    c = printer == "u1" ? [u1_pad_dx / 2, -u1_pad_dy / 2, 0] : [k1_outer / 2 - k1_rim, -(k1_outer / 2 - k1_rim), 0];
    pin_d = printer == "u1" ? u1_pin_d : k1_pin_d;
    ang = printer == "u1" ? -atan2(u1_pad_dy, u1_pad_dx) : -45;
    translate(c) {
        ghost(printer);
        rotate(ang) translate([-pin_d, 0, 0]) {   // rear-left foot, its shoe and a tube stub
            color("orange") translate([0, 0, foot_z]) foot();
            color("darkorange") if (printer == "u1") shoe_u1(); else shoe_k1();
            color("silver") translate([-40, -tube / 2, tube_bot]) cube([140, tube, tube]);
        }
    }
}
else if (part == "hub") hub();
else if (part == "foot") print_foot();
else if (part == "shoe_k1") print_shoe_k1();
else if (part == "shoe_u1") print_shoe_u1();
else if (part == "post") print_post();
else if (part == "arm") print_arm();
else if (part == "cam_plate") print_cam_plate();
else if (part == "knob_m5") knob_m5();
else if (part == "knob_m4") knob_m4();
else if (part == "pin_knob") pin_knob();
else if (part == "pi_tray") pi_tray();
else if (part == "cable_clip") cable_clip();
else if (part == "led_ring") print_led_ring();
else if (part == "dowel") dowel();
else if (part == "plate_hub") plate_hub();
else if (part == "plate_small") plate_small();
else if (part == "plate_extras") plate_extras();
else if (part == "plate_led") print_led_ring();
// assembly-frame pairs for `3d fit-check --spec fits.yaml` (insert pushed home along z)
else if (part == "fit_foot") translate([0, 0, shoe_t]) foot();
else if (part == "fit_shoe_k1") shoe_k1();
else if (part == "fit_shoe_u1") shoe_u1();
else if (part == "fit_hub") hub();
else if (part == "fit_post") translate([0, post_y, 0]) post_part();
else if (part == "fit_arm") translate([0, 0, post_len - sock_d + boss_h]) arm();
else assert(false, str("unknown part: ", part));
