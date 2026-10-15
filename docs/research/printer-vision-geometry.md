# Printer camera geometry (`lib/vision_geometry.py`)

Issue #150. A fixed chamber camera sees the bed and the print from one pose. A `CameraModel`
(pinhole + OpenCV distortion, or an equidistant fisheye in the cv2.fisheye layout) maps bed
millimetres into the image, so `3d printer vision` can build per-frame masks:

| function | what it marks |
|---|---|
| `bed_mask(cam, z, shape)` | the printable bed area (`cam.bed`, default 0..270 mm) at gcode Z |
| `volume_mask(..., objects, heights)` | bed + each object's padded footprint prism up to min(z, height) + `above_mm`: where print-related pixels may legitimately be (ROI) |
| `silhouette_mask(...)` | expected part silhouette: footprint prisms from the bed to min(z, height) |
| `top_band_mask(...)` | the newest `band_mm` of every object still growing (no-extrusion check) |
| `overlay(bgr, ...)` | review drawing: bed outline, footprints at the bed and at the current top |

Frames: a bed point `(X, Y, h)` (h = height above the bed) photographed at gcode Z = z sits at
world `(X, Y, h - z)` on a bed-moves-Z printer (`bed_moves_z: true`, Snapmaker U1) and at
`(X, Y, h)` otherwise. Pixel outputs are in the model's resolution; masks rescale to the
requested shape (same aspect and crop). Footprints are the `exclude_object` convex hulls, so
`silhouette_mask` over-covers thin or concave parts (walls with fins become solid blocks).

The masks need only numpy + scipy (core deps); OpenCV is imported lazily by `overlay()` and the
calibration CLI.

## Calibration data

`load_calibration(printer)` reads `<config dir>/vision/<printer>-camera.yaml` (`{camera: {...}}`),
else the bundled file for the device's `model:` from `printer-devices.yaml`
(`lib/data/vision/u1-camera.yaml` for "Snapmaker U1"), else returns `None`.

Refit from pixel <-> bed-point correspondences (frames at several Z; `h` = height above bed):

```bash
python lib/vision_geometry.py fit points.yaml out.yaml --model fisheye --overlay frame.jpg Z check.jpg
```

`points.yaml`: `{width, height, points: [{u, v, x, y, h, z, label}]}`. Without an initial model
the fit starts from a linear DLT, then fits pose + focal, then distortion; it needs >= 8 points
that are not all at one world height.

## Snapmaker U1 default (preliminary)

The camera sits low at the front-right corner (about X 305, Y -43 mm, ~11 mm above the nozzle
plane), looking diagonally across the bed towards the back-left; +Y runs to the right of the
image, the prime tower is at the right, the toolhead docks top-right. Lens: wide, fitted as an
equidistant fisheye (f ~1270 px, k1 -0.12).

The bundled model was fitted to refined image edges (plate right edge, the GFT wall's top and
base lines) and prime-tower corners in four frames of `u1_walls_main_v3` at gcode Z 3.62-12.38
(features in `lib/data/vision/u1-camera-features.yaml`), rms 9.2 px. The physical plate edge
came out at X ~275-277 and Y ~-13 (the spring-steel sheet is 276 x 293 mm, larger than the
printable 270 x 270).

Known limits:

- Accuracy is uneven: 5-10 px at the prime tower and the far walls, 20-30 px at the part nearest
  the camera (front-right), where a mm is 10+ px. Pad masks accordingly (`pad_mm` >= 5).
- The features used are mostly parallel to the Y axis, so camera distance vs focal length is
  weakly constrained (a flat residual valley along the viewing diagonal). A better fit needs
  correspondences along X (back wall, plate front edge) and frames at high Z.
- Gcode Z range in the fit is 3.6-12.4 mm; above that the model is an extrapolation.
- Walls frames are mapped to Z via `virtual_sdcard` progress, not checked for tool-change Z-hop.
- The toolhead occludes the back half of the bed in many frames; masks do not model it.

## Ghost of the rest of the print (`lib/vision_ghost.py`)

Issue #261. The same camera model draws what is still to be printed over the live frame — a
semi-transparent 3D ghost of every wall above the current layer, for a camera feed such as the
U1 camera proxy of the Home Assistant integration.

- `ShellBuilder` collects a job's walls (outer/overhang walls, prime tower) while a caller streams
  the G-code past it once (`comment()` per comment, `move()` per G0-G3 with layer, layer Z, tool,
  feature, X/Y, whether it extrudes and, for G2/G3, `arc=(I, J, clockwise)`, sampled into
  chords; a closed arc is a full circle); points closer than 0.5 mm merge; at 3 M points it
  stops, also mid-path, and marks the `Shell` truncated (no ghost then). A 135 MB four-tool job
  keeps ~0.93 M points (~8 MB).
- `render(shell, cam, layer, shape)` paints vertical wall quads (the layers left every 2nd
  layer, bands tiling the height) far-to-near into one id buffer with Pillow — a painter's
  z-buffer; quads are ~1 mm wide, so per-quad depth order is enough. Printed walls are painted
  only as occluders and only when the camera is below the current top: from above it (the U1
  camera sits ~14 mm over the nozzle plane) every sight line to the ghost passes above the
  printed part, so just the band printed now is painted, for its glow. Ghost walls take the
  filament colour (dark ones lifted) washed 30 % towards pale cyan so they never pass for the
  real part, shaded by how squarely the wall faces the camera, opacity 80 % near to 28 % far,
  a 2 px light line at silhouettes and depth steps, a light diagonal hatch; the band printed
  now glows amber.
- The result is a `Ghost`: a sparse blend table (pixel index, inverse alpha, premultiplied
  colour). `Ghost.blend(rgb)` applies it in place, so a camera feed renders once per layer
  and blends every frame.

Measured on `u1_walls_main_v3`, layer 729 of 1193, 1920 x 1080: `render` 0.55 s on the
workstation and 1.9 s on the ODROID-N2+ (HA container, 2 CPUs); `blend` 5 ms / 20 ms.
Collecting the shell inside a Python G-code pass adds ~22 s for 135 MB on the workstation (the
camera proxy shares its index pass).
Limits: walls only (no top-surface caps, so a ghost seen from above shows no lid), no toolhead
model (the ghost is drawn over it), and alignment is only as good as the calibration.
