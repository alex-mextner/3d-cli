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

## Snapmaker U1 default

The camera sits low at the front-right corner (about X 294, Y -24 mm, ~13 mm above the nozzle
plane), looking diagonally across the bed towards the back-left; +Y runs to the right of the
image, the prime tower is at the right, the toolhead docks top-right. Lens: wide, fitted as an
equidistant fisheye (f ~1190 px).

The first model was fitted to refined image edges (plate right edge, the GFT wall's top and
base lines) and prime-tower corners in four frames of `u1_walls_main_v3` at gcode Z 3.62-12.38
(features in `lib/data/vision/u1-camera-features.yaml`), rms 9.2 px; the physical plate edge
came out at X ~275-277 and Y ~-13 (the spring-steel sheet is 276 x 293 mm, larger than the
printable 270 x 270). Above Z ~12 it was an extrapolation: at Z 80 the current-layer outlines
fell 30-60 px off the walls nearest the camera.

The bundled model (issue #262) is refit over the whole height of the same job by region
matching, not hand-picked points: for 12 frames from gcode Z 10.6 to 89.4 the job's G-code
walls up to the frame's layer are painted far-to-near as vertical quads, the surfaces of the
green tool (T2) are compared with the frame's green pixels (HSV hue 55-95, saturation >= 90),
and Powell maximises the mean IoU over pose, focal length, principal point and k1, starting
from the edge fit. A last pose-only pass adds the distance from the current layer's visible top
outline to the green parts' upper boundary — the line a "printing now" overlay draws. Mean
green IoU went from 0.39 to 0.58 (the ceiling is well below 1: lit faces lose saturation, the
toolhead hides parts); the median distance of that top outline to the real top edge went from
7-25 px to 2-4 px at Z 80-90 and to 5-13 px at Z 10-45. Frames, layers and both metrics per
frame are listed under `fit:` in `u1-camera.yaml`.

Known limits:

- Residuals left: ~5-13 px at mid heights and on the walls nearest the camera, where a mm is
  10+ px. Pad masks accordingly (`pad_mm` >= 3).
- Focal length vs camera distance is still weakly constrained along the viewing diagonal; the
  region fit trades them against k1 (it came out slightly positive). Frames of a job with parts
  spread along X would pin it down.
- One job, one lighting setup; the green segmentation needs a green part in view.
- Walls frames are mapped to Z via `virtual_sdcard` progress, not checked for tool-change Z-hop.
- The toolhead occludes the back half of the bed in many frames; masks do not model it.
