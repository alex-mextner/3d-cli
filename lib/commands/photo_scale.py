"""3d photo-scale — millimetres per pixel, plane depth and intrinsics for a photo.

WHAT: turns one photo into a physical scale by one of four routes: a reference object of known length (`--ref`),
  two clicked points a known distance apart (`--points`), the honeycomb grid on the wall (`--grid honeycomb`), or
  another photo of the same plane whose scale is known (`--homography-to`). Numpy + OpenCV only.

WHY: segmentation, closed-form profiles and silhouette fits all need mm/px; the plane depth (focal * mm/px) places
  the objects for the multi-view check. The algorithm lives in packages/threed-vision (`threed_vision.calibrate`);
  this command is a thin launcher.

Examples:
  3d photo-scale tools.jpg --ref 82=239.4 --instances seg/instances.json --json
  3d photo-scale tools.jpg --points 120,340,980,360 --distance 300 --f-px 937.5
  3d photo-scale wall.jpg --grid honeycomb --json
"""

from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d photo-scale <image> (--ref INSTANCE=MM | --points X1,Y1,X2,Y2 --distance MM | --grid honeycomb | --homography-to OTHER) [options]
  mm per pixel, plane depth and intrinsics of a photo (numpy + OpenCV, no model).
  Prints/--json: {"mm_per_px", "plane_depth_mm", "K", "f_px", "f_source", "source": "ref|points|grid|homography",
                  "quality": [...], "reg": {...}  (homography only)}

Sources (exactly one):
  --ref INSTANCE=MM    object of known longest dimension: INSTANCE is an instance id of --instances (instances.json from
                       `3d segment`) or a mask PNG path; MM its length. Pixel length = long side of the minimum-area rectangle.
  --points X1,Y1,X2,Y2 --distance MM    two pixel points a known distance apart
  --grid honeycomb     wall plane scale from the HSW grid in the photo (runs `calibrate-grid`'s detector)
  --homography-to OTHER.jpg   register this photo to a second photo of the same plane and transfer its scale;
                       needs --other-mm-per-px X (uniform) or --other-grid honeycomb (OTHER shows the panel)
Options:
  --instances FILE     instances.json for --ref INSTANCE=MM
  --calibration FILE   --grid: reuse a calibrate-grid calibration.json or an HSW refit/photo_calibration.json
  --at X,Y             --grid / --homography-to: pixel where the scale is wanted (default grid centroid / image centre)
  --f-px N | --f-ratio R   focal length in px, or as a fraction of the diagonal (default 0.375, the calibrated HSW phone)
  -o, --out FILE       also write the JSON here
  --json               print exactly one JSON object

--ref/--points/--homography-to assume the measured segment/plane is FRONTO-PARALLEL to the camera (a tilt of a degrees
under-measures lengths by 1-cos a: 3.4 % at 15, 13 % at 30 degrees) and a pinhole with centred principal point; the
`quality` list repeats this. The scale only holds for things lying in that plane.

Exit: 0 ok, 2 bad input, 3 grid not found / photos not registrable.

Examples:
  3d photo-scale tools.jpg --ref 82=239.4 --instances seg/instances.json --json
  3d photo-scale tools.jpg --points 120,340,980,360 --distance 300 --f-px 937.5
  3d photo-scale wall.jpg --grid honeycomb --json
  3d photo-scale a.jpg --homography-to b.jpg --other-grid honeycomb"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("photo-scale", argv)


COMMAND = Command(
    name="photo-scale",
    group="REFERENCE-MATCH PIPELINE",
    summary="photo -> mm per pixel / plane depth / K from a reference object, two points, the honeycomb grid or another photo",
    usage=USAGE,
    run=run,
)
