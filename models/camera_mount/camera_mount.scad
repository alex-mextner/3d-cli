// Top-down Logitech C920 mount for the Creality K1 SE and the Snapmaker U1 (issue #338).
//
// WHAT: an all-printed bridge, no metal: a 20x20 printed beam (four segments joined by square
//   tenons and snap pins) laid diagonally across the printer top. Four feet are clamped on the
//   beam once by printed wedges and never move; a lift-off shoe under each
//   foot indexes it to the printer (K1 SE: lips that drop into an inner top corner; U1: a peg
//   that drops into a Top Cover silicone pad). The centre hub joins the beam halves around an
//   open window; the C920 looks down through it from a post with 10 mm snap-pin detents and a
//   tilt plate that hangs by gravity on three printed hangers set by wedges (no springs). The
//   camera's own clip slides into a printed cradle — no 1/4" screw. Optional: Pi Zero W tray,
//   cable clips, a 4-LED diffuse ring.
// WHY this shape: the bridge rests on two kinematic points (two opposite corners or two pads),
//   so it lands in the same place every time without tools; all four feet stay on the beam,
//   so moving the rig between printers changes no setting — only which pair of shoes is on.
//   Everything is plastic because the load is one webcam (README.ru.md §1 has the sag sum).
// Every dimension marked MEASURE is an estimate until the user measures it (README.ru.md, §2).
//
// Usage: openscad -D 'part="hub"' -o hub.stl camera_mount.scad   (see PARTS below)

part = "assembly";   // PARTS: assembly | exploded | anim | pov | detail | detail_joint | detail_wedge
                     //   | detail_tilt | glb_printer | glb_mount | glb_c920 | hub | beam_a | beam_b | foot
                     //   | shoe_k1 | shoe_u1 | post | arm | cam_plate | wedge | hanger
                     //   | pin_long | pin_short | pi_tray | cable_clip | led_ring | dowel
                     //   | plate_hub | plate_beam | plate_small | plate_extras | plate_led | fit_*
printer = "k1se";    // assembly view: k1se | u1
pin_hole = 5;        // post hole the height pin goes through, counted from the post bottom:
                     // 2 (camera highest) .. 5 (camera lowest)
anim_t = 1;          // part="anim": installation progress 0..1 (1 = installed)

$fn = 64;
eps = 0.01;
fit = 0.2;           // clearance per side for printed sliding fits

// ---------- printed beam ----------
tube = 20;           // beam section 20x20 (the name stays from the metal-tube version)
tube_gap = 0.4;      // beam clearance per socket (total)
tube_half = 300;     // one beam half, hub socket floor to the outer end
tg = tube + tube_gap;
seg_joint = 180;     // segment joint, from the hub centre (clear of every foot and of the hub)
tenon = 12;          // square tenon on segment A (flush with the underside), 25 mm into segment B
tenon_len = 25;
tenon_fit = 0.12;    // per side: tight, so the joint has no play under the camera's weight

// ---------- printed fasteners ----------
// one wedge for every clamp: 1:7.5 taper (7.6 degrees, under PLA's ~17-degree friction angle),
// so a pushed-in wedge stays put; truncated ends, no knife edge
wedge_len = 45; wedge_w = 6.6; wedge_h0 = 2.5; wedge_h1 = 8.5;
wslot_w = wedge_w + 0.4;   // slot / window width for the wedge
pin_s = 5.5;         // snap pin section (square, printed flat)
pin_hole_s = pin_s + 0.6;
hanger = [16, 8];    // tilt hanger bar section (across, thick)
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
top_wall = 11;       // sleeve wall over the beam (the clamp wedge slot runs under it)
foot_len = 30;       // sleeve length along the tube
foot_base = 2;       // extra height under the sleeve
key = 12;            // square key on the foot bottom
key_h = 9;
// beam channel: 45-degree chamfers in all four corners keep every bridge over it at 14.4 mm
// (the K1 SE bridge limit is 15), whichever way up the part prints; the beam sits on the two
// lower chamfers (a V seat that centres it) and the clamp wedge presses it down into them
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
arm_t = 12;
boss = [post[0] + 2 * fit + 10, post[1] + 2 * fit + 10];
boss_h = 25;
sock_d = 22;         // post depth in the arm boss
tilt_r = 26;         // tilt screw circle
tilt_ang = [135, 15, 255];
tilt_gap = 10;       // arm underside to plate top, wedges at mid travel
pad_r = 36;
plate_t = 6;
cam_yaw = -45;       // tab direction (hub frame); image x then runs along the printer X
cam_axis_off = 29;   // MEASURE C-2: C920 lens axis to the clip-foot face (spec: 43.3 with clip, body 29)
clip_w = 31;         // MEASURE C-3: width of the C920 clip foot (the part with the 1/4" socket)
clip_l = 40;         // MEASURE C-3: length of the clip foot along its slide
clip_t = 4;          // MEASURE C-3: thickness of the clip foot at its side edges
cam_lens_below_floor = 5; // MEASURE C-1: lens face below the clip foot's lower edge
tab_t = 5;
rail = 3;            // cradle rail wall
lip = 2.5;           // cradle lip over the foot edges
tab_w = clip_w + 2 * (fit + rail + 1);
tab_len = clip_l + 4;
// post placement for the assembly view
post_bottom_z = hub_z + pc_pin_z - post_pitch * pin_hole;
// usable holes: the post stays 30+ mm in the channel and never reaches below the printer top
assert(pin_hole == floor(pin_hole) && post_bottom_z >= 0 && post_bottom_z <= hub_z + pc_h - 30,
       str("pin_hole ", pin_hole, " is out of range: a whole hole number 2..5"));
assert(printer == "k1se" || printer == "u1", str("printer \"", printer, "\" is not k1se or u1"));
arm_bot_z = post_bottom_z + post_len - sock_d + boss_h;
plate_top_z = arm_bot_z - tilt_gap;
slot_floor_z = plate_top_z - plate_t - tab_len;   // the clip foot's lower edge rests here
lens_z = slot_floor_z - cam_lens_below_floor;

// ---------- design invariants (3d check --consistency) ----------
k1_pin_d = (k1_outer / 2 - k1_rim) * sqrt(2) + k1_s;
u1_pin_d = sqrt(u1_pad_dx * u1_pad_dx + u1_pad_dy * u1_pad_dy) / 2;
assert(sock_x0 + tube_half >= u1_pin_d + foot_len / 2 + 10, "tube half too short for the U1 pads");
assert(sock_x0 + tube_half >= k1_pin_d + foot_len / 2 + 10, "tube half too short for the K1 SE corners");
assert(2 * hub_half <= 210, "hub does not fit the K1 SE bed");
assert(peg_d < u1_hole_d && peg_len > 1, "U1 peg must enter the pad hole");
// the joint pin head must not block a foot sliding on from the outer end, and segment A must
// leave the hub socket before the joint
assert(seg_joint + tenon_len + 10 < min(k1_pin_d, u1_pin_d) - foot_len / 2, "beam joint under a foot");
assert(seg_joint > hub_half + 20, "beam joint inside the hub");
assert(sock_x0 + tube_half - seg_joint <= 200 && seg_joint + tenon_len - sock_x0 <= 200, "beam segment longer than the K1 SE bed");
// sag of the printed beam at the hub, simply supported on the U1 pads (the longer span):
// centre load = C920 162 g + post, arm, plate, screws ~150 g + LED ring ~60 g = 3.7 N;
// section = 4 perimeters (1.8 mm shell) + 25 % infill; printed PLA E ~ 2500 MPa (low end)
beam_shell = 1.8;
beam_I = (pow(tube, 4) - pow(tube - 2 * beam_shell, 4)) / 12 + 0.25 * pow(tube - 2 * beam_shell, 4) / 12;
beam_w = 1.24e-3 * 9.81e-3 * (tube * tube - pow(tube - 2 * beam_shell, 2) * 0.75);   // N/mm
span = 2 * u1_pin_d;
sag = 3.7 * pow(span, 3) / (48 * 2500 * beam_I) + 5 * beam_w * pow(span, 4) / (384 * 2500 * beam_I);
assert(sag < 2, str("beam sags ", sag, " mm"));
echo(str("beam: I = ", round(beam_I), " mm4, sag at the hub on the U1 span ", round(span), " mm = ",
         round(sag * 100) / 100, " mm, bending stress ", round(3.7 * span / 4 * (tube / 2) / beam_I * 100) / 100, " MPa"));
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

// clamp slot for a wedge across the beam (y), its floor on the beam's top face (z_beam_top):
// the wedge pushed in along y presses the beam down into the V seat
module wedge_slot(z_beam_top) {
    translate([-wslot_w / 2, -50, z_beam_top - 0.5]) cube([wslot_w, 100, wedge_h1 - 1]);
}

// wedge, printed on its side: length along x, taper in y, width = extrusion height
module wedge() {
    linear_extrude(wedge_w) polygon([[0, 0], [wedge_len, 0], [wedge_len, wedge_h1], [0, wedge_h0]]);
    // grip: a tab on the thick end
    translate([wedge_len - eps, -2, 0]) cube([6, wedge_h1 + 4, wedge_w]);
}

// tilt hanger, printed flat: a T head that carries the plate from below, a bar up through the
// plate and the arm, a window above the arm for the wedge. In use the bar's long axis is z.
hanger_len = plate_t + tilt_gap + arm_t + wedge_h1 + 8;
hanger_win_z = plate_t + tilt_gap + arm_t - 3;   // window bottom, from the head's top face
module hanger_part() {
    linear_extrude(hanger[1]) difference() {
        union() {
            translate([-4, -13]) square([4 + eps, 26]);                 // head
            translate([0, -hanger[0] / 2]) square([hanger_len, hanger[0]]);
        }
        translate([hanger_win_z, -wslot_w / 2]) square([wedge_h1 + 3 + 2, wslot_w]);
    }
}

// snap pin, printed flat: head, square shank, the last 10 mm split into two barbed prongs
module snap_pin(L) {
    linear_extrude(pin_s) difference() {
        union() {
            translate([-8, -6]) square([8, 12]);
            translate([-eps, -pin_s / 2]) square([L - 3, pin_s]);
            for (s = [-1, 1]) polygon([[L - 7, s * pin_s / 2], [L - 3, s * (pin_s / 2 + 0.8)],
                                       [L, s * (pin_s / 2 - 0.6)], [L, 0], [L - 7, 0]]);
        }
        translate([L - 11, -0.75]) square([12, 1.5]);
    }
}

// hole for a tilt hanger bar (bar across = tangential), through a plate of thickness h
module hanger_hole(h) {
    translate([-hanger[1] / 2 - fit, -hanger[0] / 2 - fit, -1]) cube([hanger[1] + 2 * fit, hanger[0] + 2 * fit, h + 2]);
}

// square hole for a snap pin along x, centred on (y, z)
module pin_hole_x(x0, len, y, z) {
    translate([x0, y - pin_hole_s / 2, z - pin_hole_s / 2]) cube([len, pin_hole_s, pin_hole_s]);
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
            translate([sx * (sock_x0 + sock_len / 2), 0, 0]) wedge_slot(bot_wall + tube_seat + tube);
        }
        // post channel and the height-pin hole along x
        translate([-post[0] / 2 - fit, pc_y0, -1]) cube([post[0] + 2 * fit, post[1] + 2 * fit, pc_h + 2]);
        pin_hole_x(-pc_w / 2 - 1, pc_w + 2, post_y, pc_pin_z);
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
        wedge_slot(foot_base + bot_wall + tube_seat + tube);
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
            pin_hole_x(-post[0] / 2 - 1, post[0] + 2, 0, z);
        for (z = [post_pitch * 5 : post_pitch * 5 : post_len - 1])   // depth marks every 50 mm
            translate([-post[0] / 2 - 1, -post[1] / 2 - eps, z - 0.5]) cube([post[0] + 2, 0.6, 1]);
    }
}

// arm frame (hub frame shifted so the arm underside is z = 0): pad over the lens axis at the
// origin, boss for the post at y = post_y, three square holes for the tilt hangers
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
        pin_hole_x(-boss[0] / 2 - 1, boss[0] + 2, post_y, -boss_h + sock_d - post_pitch);
        for (a = tilt_ang) rotate(a) translate([tilt_r, 0, 0]) hanger_hole(arm_t);
        // the lens sits under this pad: mark its axis for alignment
        translate([0, 0, arm_t - 0.6]) cylinder(d = 3, h = 1, $fn = 16);
    }
}

// camera plate frame: plate top at z = 0, lens axis at the origin. The plate hangs on the
// three tilt-screw heads (countersunk seats underneath); the tab below carries a cradle the
// C920 clip foot slides down into: two rails with lips over the foot's side edges and two
// 45-degree stops at the bottom (no bridge across the slot in either print or use).
module cam_plate() {
    slot_x = cam_axis_off;   // the foot's back face lies on the tab face here
    difference() {
        union() {
            hull() {
                translate([0, 0, -plate_t]) cylinder(r = pad_r, h = plate_t);
                rotate(cam_yaw) translate([slot_x - clip_t - fit - lip, -tab_w / 2, -plate_t]) cube([clip_t + fit + lip + tab_t + 11, tab_w, plate_t]);
            }
            rotate(cam_yaw) {
                translate([slot_x, -tab_w / 2, -plate_t - tab_len + eps]) cube([tab_t, tab_w, tab_len]);
                for (s = [-1, 1]) {
                    // rail + lip, along the whole tab
                    translate([slot_x - clip_t - fit - lip / 2, s * (tab_w / 2 - rail / 2 - 0.5), -plate_t - tab_len / 2 + eps])
                        cube([clip_t + fit + lip + eps, rail + 1, tab_len], center = true);
                    translate([slot_x - clip_t - fit - lip, s > 0 ? clip_w / 2 + fit - lip : -tab_w / 2, -plate_t - tab_len + eps])
                        cube([lip, tab_w / 2 - clip_w / 2 - fit + lip, tab_len]);
                    // bottom stop: a 45-degree ledge from each rail
                    translate([slot_x - clip_t - fit, s * (clip_w / 2 + fit), -plate_t - tab_len]) rotate([90, 0, 0])
                        linear_extrude(4, center = true) polygon([[0, 0], [clip_t + fit + eps, 0], [clip_t + fit + eps, clip_t + fit + 1], [0, 1]]);
                }
            }
            // gusset between tab and plate
            rotate(cam_yaw) translate([slot_x + tab_t - eps, 0, -plate_t]) rotate([90, 0, 0])
                linear_extrude(4, center = true) polygon([[0, 0], [10, 0], [0, -14]]);
        }
        // the slot itself: the clip foot slides down between the rails, under the lips
        rotate(cam_yaw) translate([slot_x - clip_t - fit, -clip_w / 2 - fit, -plate_t - tab_len + 4])
            cube([clip_t + fit + eps, clip_w + 2 * fit, tab_len]);
        for (a = tilt_ang) rotate(a) translate([tilt_r, 0, -plate_t]) hanger_hole(plate_t);
        // axis mark on the plate: line it up with the arm mark
        translate([0, 0, -0.6]) cylinder(d = 3, h = 1, $fn = 16);
    }
}

// ================= beam segments =================
// segment frame = assembly frame of the +x half: beam underside at z = 0, centred on y = 0
module beam_a() {   // hub socket floor .. joint, plus the tenon into segment B
    translate([sock_x0, -tube / 2, 0]) cube([seg_joint - sock_x0, tube, tube]);
    // the tenon is flush with the beam underside: it prints on the bed, and under sag it sits
    // on the tension side, where the pin and the pocket walls hold it best
    translate([seg_joint - eps, -tenon / 2, 0]) difference() {
        cube([tenon_len + eps, tenon, tenon]);
        translate([tenon_len / 2 - pin_hole_s / 2, (tenon - pin_hole_s) / 2, -1]) cube([pin_hole_s, pin_hole_s, tenon + 2]);
    }
}
module beam_b() {   // joint .. outer end, with the tenon pocket and the vertical pin hole
    difference() {
        translate([seg_joint, -tube / 2, 0]) cube([sock_x0 + tube_half - seg_joint, tube, tube]);
        // pocket open at the bottom, matching the flush tenon
        translate([seg_joint - 1, -tenon / 2 - tenon_fit, -1])
            cube([tenon_len + 1.5, tenon + 2 * tenon_fit, tenon + tenon_fit + 1]);
        translate([seg_joint + tenon_len / 2 - pin_hole_s / 2, -pin_hole_s / 2, -1]) cube([pin_hole_s, pin_hole_s, tube + 2]);
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

// ================= scene: printer and camera models (views, animation, GLB; never printed) ==========
// Simplified, at the real outer sizes: K1 SE 355 x 355 x 482 (Creality spec), U1 body 480 x 450
// under the top plane (the U1-1 pad estimate + 25 mm; spec footprint 580 x 540 x 500 with the side
// spool bays), bed at Z max. z = 0 is the printer top surface the shoes stand on.
k1_h = 482;
u1_h = 500;
c_frame = "#2b2d31"; c_bed = "#3a3a3a"; c_pei = "#c9a227"; c_steel = "#b8bcc2";

module k1se_model() {
    o = k1_outer; pw = 28; base = 95;
    color(c_frame) {
        difference() {   // die-cast top frame
            translate([-o / 2, -o / 2, -30]) cube([o, o, 30]);
            translate([-o / 2 + k1_rim, -o / 2 + k1_rim, -31]) cube([o - 2 * k1_rim, o - 2 * k1_rim, 32]);
        }
        for (x = [-1, 1], y = [-1, 1]) translate([x * (o / 2 - pw / 2) - pw / 2, y * (o / 2 - pw / 2) - pw / 2, -k1_h]) cube([pw, pw, k1_h]);
        translate([-o / 2, -o / 2, -k1_h]) cube([o, o, base]);                 // electronics base
        for (x = [-1, 1]) translate([x * (o / 2 - pw / 2) - pw / 2, -o / 2, -k1_h + base + 150]) cube([pw, o, 22]);   // side crossbeams
        translate([-o / 2, -o / 2, -k1_h + base]) cube([o, pw, 18]);           // front sill
    }
    color("#1b1b1b") translate([-60, -o / 2 - 2, -k1_h + 20]) cube([120, 3, 60]);   // screen
    color(c_steel) for (dz = [0, 24]) translate([-o / 2 + pw, o / 2 - 70, -58 - dz]) rotate([0, 90, 0]) cylinder(d = 8, h = o - 2 * pw);
    color("#222") translate([-28, o / 2 - 100, -120]) cube([56, 60, 80]);    // toolhead, parked at the back
    color(c_bed) translate([-117.5, -117.5, -k1_rim_to_bed - 8]) cube([235, 235, 8]);
    color(c_pei) translate([-110, -110, -k1_rim_to_bed]) cube([220, 220, 0.6]);
}

module u1_model() {
    W = u1_outer[0]; D = u1_outer[1]; r = 24; open_m = 55;
    module rbox(w, d, h) { hull() for (x = [-1, 1], y = [-1, 1]) translate([x * (w / 2 - r), y * (d / 2 - r), 0]) cylinder(r = r, h = h, $fn = 32); }
    color("#e8e8e6") difference() {
        translate([0, 0, -u1_h]) rbox(W, D, u1_h);
        // chamber, open top; the cut stops 1 mm above the top so the camera-view render (eye just
        // above the top) is never inside a subtracted volume, which OpenCSG previews as solid
        translate([0, 0, -u1_h + 60]) rbox(W - 2 * open_m, D - 2 * open_m, u1_h - 59);
        translate([-(W - 2 * open_m) / 2, -D / 2 - 1, -u1_h + 70]) cube([W - 2 * open_m, open_m + 2, u1_h - 110]);   // door opening
    }
    color("#2a2c30") translate([0, 0, -0.4]) difference() {   // dark top plane
        rbox(W - 4, D - 4, 0.5);
        translate([0, 0, -1]) rbox(W - 2 * open_m, D - 2 * open_m, 3);
    }
    color("#111") for (x = [-1, 1], y = [-1, 1]) translate([x * u1_pad_dx / 2, y * u1_pad_dy / 2, -0.2]) difference() {   // silicone pads
        cylinder(d = u1_hole_d + 8, h = 0.4, $fn = 32);
        translate([0, 0, -1]) cylinder(d = u1_hole_d, h = 2, $fn = 32);
    }
    color("#9ec9e8", 0.25) translate([-(W - 2 * open_m) / 2, -D / 2, -u1_h + 70]) cube([W - 2 * open_m, 3, u1_h - 110]);   // glass door
    for (i = [0 : 3]) translate([-120 + i * 80, D / 2 - open_m - 35, -95]) {   // four toolheads in their docks
        color("white") cube([60, 34, 70]);
        color("#f2b705") translate([30, -0.5, 45]) rotate([90, 0, 0]) cylinder(d = 20, h = 1);
    }
    for (sy = [-1, 1]) color("#c9c9c9") translate([W / 2, sy * 95 - 40, -u1_h + 140]) cube([50, 80, 220]);   // side spool bays
    color(c_bed) translate([-140, -140, -u1_rim_to_bed - 6.5]) cube([280, 280, 6.5]);
    color(c_pei) translate([-135, -135, -u1_rim_to_bed]) cube([270, 270, 0.6]);
}

module printer_model(pr) { if (pr == "u1") u1_model(); else k1se_model(); }

// Logitech C920 in the plate frame (plate top at z = 0): body 94 x 29 x 24 hanging lens-down,
// the clip foot standing in the cradle, the hinge link between them
module c920() {
    lz = lens_z - plate_top_z;
    rotate(cam_yaw) {
        color("#1d1d1f") hull() for (s = [-1, 1]) translate([0, s * 32.5, lz]) scale([14.5 / 12, 1, 1]) cylinder(r = 12, h = 24);
        color("#3b3b3d") translate([cam_axis_off - clip_t, -clip_w / 2, slot_floor_z - plate_top_z]) cube([clip_t, clip_w, clip_l]);
        color("#3b3b3d") hull() {
            translate([cam_axis_off - clip_t - 1, -12, lz + 18]) cube([1, 24, 10]);
            translate([10, -12, lz + 22]) cube([1, 24, 2]);
        }
        color("#9a9a9a") translate([0, 0, lz - 0.4]) cylinder(d = 16, h = 0.6);
        color("#05070a") translate([0, 0, lz - 0.6]) cylinder(d = 9, h = 0.4);
    }
}

module beam_half(e = 0, slide = 0, pin_drop = 0, show_pin = true) {   // the +x half
    color("orange") translate([e, 0, 0]) beam_a();
    color("gold") translate([2 * e + slide, 0, 0]) beam_b();
    // the short pin standing in its vertical hole, head on top
    if (show_pin) color("darkorange") translate([seg_joint + tenon_len / 2 + 2 * e + slide, -pin_s / 2, tube + e + pin_drop]) rotate([0, 90, 0])
        rotate([0, 0, 180]) snap_pin(pin_short);
}

// smooth 0..1 progress of a phase that runs from t = a to t = b
function ph(t, a, b) = let(x = min(1, max(0, (t - a) / (b - a)))) x * x * (3 - 2 * x);

// The mount on printer `pr`. t < 1 plays the installation (anim_t): the beam is joined from its
// segments, the hub and the feet go on, the bridge is lowered onto the printer, then the camera
// head. explode > 0 pulls the parts apart (the exploded view). led: show the optional LED ring.
module mount_scene(pr, t = 1, explode = 0, led = true, camera = true) {
    pin_d = pr == "u1" ? u1_pin_d : k1_pin_d;
    other_d = pr == "u1" ? k1_pin_d : u1_pin_d;
    ang = pr == "u1" ? -atan2(u1_pad_dy, u1_pad_dx) : -45;
    e = explode;
    p_seg = ph(t, 0.00, 0.10); p_pin = ph(t, 0.08, 0.14); p_hub = ph(t, 0.14, 0.24); p_hw = ph(t, 0.22, 0.28);
    p_feet = ph(t, 0.28, 0.38); p_fw = ph(t, 0.36, 0.41); p_shoe = ph(t, 0.40, 0.46); p_low = ph(t, 0.46, 0.62);
    p_head = ph(t, 0.62, 0.71); p_cam = ph(t, 0.72, 0.80);
    rotate(ang) translate([0, 0, (1 - p_low) * 260]) {
        color("orange") translate([0, 0, hub_z + 2 * e]) hub();
        for (sx = [-1, 1]) mirror([sx > 0 ? 0 : 1, 0, 0]) translate([(1 - p_hub) * 70, 0, tube_bot + 2 * e])
            beam_half(e, (1 - p_seg) * 90, (1 - p_pin) * 45, t >= 0.06);
        for (sx = [-1, 1]) {
            slide_in = (1 - p_feet) * (sock_x0 + tube_half + 30 - min(pin_d, other_d));
            if (t >= 0.27) {
                translate([sx * (pin_d + slide_in), 0, foot_z + 2 * e]) color("orange") foot();
                translate([sx * (other_d + slide_in), 0, foot_z + 2 * e]) color("orange") foot();   // the other pair, shoes off
            }
            if (t >= 0.39) translate([sx * pin_d, 0, -(1 - p_shoe) * 50]) rotate(sx > 0 ? 180 : 0) color("darkorange")
                if (pr == "u1") shoe_u1(); else shoe_k1();
            if (t >= 0.21) color("gold") translate([sx * (sock_x0 + sock_len / 2) - wedge_w / 2, -wedge_len / 2 - (1 - p_hw) * 60, tube_bot + tube + 3 * e]) wedge_along_y();
            if (t >= 0.35) for (x = [pin_d, other_d]) color("gold")
                translate([sx * x - wedge_w / 2, -wedge_len / 2 - (1 - p_fw) * 60, tube_bot + tube + 3 * e]) wedge_along_y();
        }
        if (t >= 0.61) translate([0, 0, (1 - p_head) * 170]) {
            color("orange") translate([0, post_y, post_bottom_z + 3 * e]) post_part();
            color("orange") translate([0, 0, arm_bot_z + 4 * e]) arm();
            translate([0, 0, plate_top_z + 4 * e - (e > 0 ? e : 0)]) {
                color("orange") cam_plate();
                for (a = tilt_ang) rotate(a) translate([tilt_r, 0, -plate_t - e]) {
                    color("gold") translate([hanger[1] / 2, 0, 0]) rotate([0, -90, 0]) hanger_part();
                    color("goldenrod") translate([-wedge_len / 2, -wedge_w / 2, plate_t + tilt_gap + arm_t + e]) wedge_along_x();
                }
                if (camera && t >= 0.71) translate([0, 0, (1 - p_cam) * 70]) c920();
            }
        }
        if (led) color("orange", 0.9) translate([0, 0, hub_z + e]) led_ring();
        if (t >= 0.36) {
            tray_x = -165;   // between the hub and the K1 SE foot: the same spot on both printers
            color("orange") translate([tray_x, 0, tube_bot - snap_base + e - (1 - p_fw) * 40]) pi_tray();
            color("#2e7d32") translate([tray_x - 32.5, tg / 2 + snap_wall + 3, tube_bot - snap_base + 6 + e - (1 - p_fw) * 40]) cube([65, 30, 1.6]);   // Pi Zero W
            for (x = [-115, 115]) color("orange") translate([x, 0, tube_bot - snap_base + e - (1 - p_fw) * 40]) cable_clip();
        }
    }
}

// the frame shown in the camera's own picture: the scene seen from the lens, with the C920's
// field of view (2304 x 1536: 70.4 x 50.4 degrees). OpenSCAD's export camera has a fixed
// 22.5-degree vertical field, so the scene is squeezed in x and y about the lens axis by
// tan(11.25) / tan(25.2): the same picture a 50.4-degree camera at the lens takes.
pov_k = tan(22.5 / 2) / tan(50.4 / 2);
module camera_pov(pr) {
    scale([pov_k, pov_k, 1]) { printer_model(pr); mount_scene(pr, camera = false); }
}

// close-ups for the guide and the animation stills
module detail_joint() {   // segment A, B and the pin, pulled apart along the beam
    color("orange") beam_a();
    color("gold") translate([30, 0, 0]) beam_b();
    color("darkorange") translate([seg_joint + tenon_len / 2 + 30, -pin_s / 2, tube + 25]) rotate([0, 90, 0]) rotate([0, 0, 180]) snap_pin(pin_short);
}
module detail_wedge() {   // a foot on a beam stub, its wedge half pushed in
    color("gold") translate([-60, -tube / 2, tube_bot - foot_z]) cube([120, tube, tube]);
    color("orange") foot();
    color("goldenrod") translate([-wedge_w / 2, -wedge_len / 2 - 22, tube_bot - foot_z + tube]) wedge_along_y();
}
module detail_tilt() {   // arm, plate, hangers, wedges and the camera, nothing else
    translate([0, 0, -plate_top_z]) {
        color("orange") translate([0, 0, arm_bot_z]) arm();
        translate([0, 0, plate_top_z]) {
            color("orange") cam_plate();
            for (a = tilt_ang) rotate(a) translate([tilt_r, 0, -plate_t]) {
                color("gold") translate([hanger[1] / 2, 0, 0]) rotate([0, -90, 0]) hanger_part();
                color("goldenrod") translate([-wedge_len / 2, -wedge_w / 2, plate_t + tilt_gap + arm_t]) wedge_along_x();
            }
            c920();
        }
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
// beam segments lie as installed (pin holes vertical), centred on the origin
module print_beam_a() { translate([-(sock_x0 + seg_joint + tenon_len) / 2, 0, 0]) beam_a(); }
module print_beam_b() { translate([-(seg_joint + sock_x0 + tube_half) / 2, 0, 0]) beam_b(); }
// wedge as installed, flat side down: length along y (beam clamps) or x (tilt hangers)
module wedge_along_y() { multmatrix([[0, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 0]]) wedge(); }
module wedge_along_x() { multmatrix([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0]]) wedge(); }
pin_long = pc_w + 8;          // hub post channel and arm boss
pin_short = tube + 8;         // beam joints

// ================= print plates (K1 SE bed 220 x 220; everything inside 210 x 210) =================
module plate_hub() {
    hub();
    translate([0, -80, 0]) print_post();
    for (i = [0 : 1]) translate([-95, 70 + i * 16, 0]) snap_pin(pin_long);   // height pin + arm pin
    for (i = [0 : 3]) translate([84 + (i % 2) * 14, 82 + floor(i / 2) * 14, 0]) dowel();
    for (y = [70, 86]) translate([18, y, 0]) wedge();   // the hub's two clamps
}

module plate_beam() {
    for (i = [0 : 1]) translate([-15 + i * 30, 0, 0]) rotate(90) print_beam_b();
    for (i = [0 : 1]) translate([-75 + i * 30, -25, 0]) rotate(90) print_beam_a();
    for (i = [0 : 1]) translate([50, -60 + i * 16, 0]) snap_pin(pin_short);
    for (y = [30, 58, 86]) translate([34, y, 0]) hanger_part();   // tilt hangers
    for (y = [-30, -15, 0]) translate([35, y, 0]) wedge();         // their wedges
}

module plate_small() {
    for (i = [0 : 3]) translate([-90 + i * 40, -50, 0]) print_foot();
    for (x = [-70, 10]) translate([x, 50, 0]) print_shoe_k1();
    for (y = [60, 0]) translate([82, y, 0]) print_shoe_u1();
    for (i = [0 : 3]) translate([-100 + (i % 2) * 60, -98 + floor(i / 2) * 16, 0]) wedge();   // feet clamps
}

module plate_extras() {
    translate([-60, 20, 0]) print_arm();
    translate([20, 55, 0]) print_cam_plate();
    translate([40, -40, 0]) pi_tray();
    for (y = [60, 10, -40]) translate([92, y, 0]) cable_clip();

}

// ================= dispatch =================
if (part == "assembly") { printer_model(printer); mount_scene(printer); }
else if (part == "exploded") mount_scene(printer, explode = 40);
else if (part == "anim") { printer_model(printer); mount_scene(printer, anim_t, led = false); }
else if (part == "pov") camera_pov(printer);
else if (part == "detail_joint") detail_joint();
else if (part == "detail_wedge") detail_wedge();
else if (part == "detail_tilt") detail_tilt();
// GLB groups: one mesh per material, assembled in the same frame
else if (part == "glb_printer") printer_model(printer);
else if (part == "glb_mount") mount_scene(printer, camera = false);
else if (part == "glb_c920") rotate(printer == "u1" ? -atan2(u1_pad_dy, u1_pad_dx) : -45) translate([0, 0, plate_top_z]) c920();
// close-ups for the guide: the rear-left corner (K1 SE) or pad (U1) moved to the origin
else if (part == "detail") {
    c = printer == "u1" ? [u1_pad_dx / 2, -u1_pad_dy / 2, 0] : [k1_outer / 2 - k1_rim, -(k1_outer / 2 - k1_rim), 0];
    pin_d = printer == "u1" ? u1_pin_d : k1_pin_d;
    ang = printer == "u1" ? -atan2(u1_pad_dy, u1_pad_dx) : -45;
    translate(c) {
        printer_model(printer);
        rotate(ang) translate([-pin_d, 0, 0]) {   // rear-left foot, its shoe and a beam stub
            color("orange") translate([0, 0, foot_z]) foot();
            color("darkorange") if (printer == "u1") shoe_u1(); else shoe_k1();
            color("gold") translate([-40, -tube / 2, tube_bot]) cube([140, tube, tube]);
        }
    }
}
else if (part == "hub") hub();
else if (part == "beam_a") print_beam_a();
else if (part == "beam_b") print_beam_b();
else if (part == "foot") print_foot();
else if (part == "shoe_k1") print_shoe_k1();
else if (part == "shoe_u1") print_shoe_u1();
else if (part == "post") print_post();
else if (part == "arm") print_arm();
else if (part == "cam_plate") print_cam_plate();
else if (part == "wedge") wedge();
else if (part == "hanger") hanger_part();
else if (part == "pin_long") snap_pin(pin_long);
else if (part == "pin_short") snap_pin(pin_short);
else if (part == "pi_tray") pi_tray();
else if (part == "cable_clip") cable_clip();
else if (part == "led_ring") print_led_ring();
else if (part == "dowel") dowel();
else if (part == "plate_hub") plate_hub();
else if (part == "plate_beam") plate_beam();
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
