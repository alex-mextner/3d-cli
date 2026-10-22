"""3d calibrate-grid — camera + wall-plane calibration from a photo of the HSW honeycomb panel.

WHAT: finds the honeycomb cell centres in a wall photo, labels them on the 20.4382 x 23.6 mm lattice and solves the
  pinhole camera (focal length, rotation, translation; square pixels, central principal point) plus the photo-px ->
  wall-mm homography. Numpy + OpenCV only: no GPU, no model.

WHY: it gives every later photo stage a physical scale (mm per px at the wall) and a camera for the fit/check steps.
  (`3d calibrate` is the PRINTER calibration command, hence the name.) The algorithm lives in packages/threed-vision
  (`threed_vision.calibrate.detect_grid`); this command is a thin launcher.

Examples:
  3d calibrate-grid wall.jpg --json
  3d calibrate-grid wall.jpg -o calib/ --f-px 720.8
"""

from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d calibrate-grid <image> [-o DIR] [options]
  Detect the HSW honeycomb grid in a wall photo and solve camera + plane (numpy + OpenCV, no model).
  Needs a python with numpy/opencv/pillow/scipy (the vision venv, or `uv` for an ad-hoc one).

Options:
  -o, --out DIR        write DIR/calibration.json and DIR/overlay.jpg (green = detected cells, red = reprojected lattice)
  --grid-pitch CxR     column x row pitch in mm (default 20.4382x23.6)
  --f-px N             known focal length in px: only R and t are solved (EXIF / earlier calibration)
  --f-ratio R          known focal length / image diagonal (the HSW phone is 0.375)
  --json               print exactly one JSON object: ok, K, f_px, R, t_mm, homography_grid_mm_to_px,
                       homography_px_to_grid_mm (photo px -> wall mm), reproj_median_px, reproj_p95_px,
                       n_cells, n_inliers, inlier_cells_px, inlier_cells, f_source, quality, ...

Grid frame (same as hsw-editor grid.ts): mm, x right, y UP, z out of the wall; cell (col,row) is at
((col+0.5)*20.4382, (row+0.5+0.5*(col&1))*23.6). The labelling origin is arbitrary (lowest-left detected cell = 0,0).
Without --f-px/--f-ratio the focal length is estimated from the perspective of the grid; it is weakly determined when
the wall is seen nearly head-on (`quality` has f_weak / f_unobservable_prior_used, then 0.375*diagonal is used).

Exit: 0 ok, 2 bad input, 3 no grid found / poor fit.

Examples:
  3d calibrate-grid wall.jpg --json
  3d calibrate-grid wall.heic -o calib/ --f-px 720.8"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("calibrate-grid", argv)


COMMAND = Command(
    name="calibrate-grid",
    group="REFERENCE-MATCH PIPELINE",
    summary="honeycomb grid photo -> focal length, camera pose, px->wall-mm homography (threed-vision, no model)",
    usage=USAGE,
    run=run,
)
