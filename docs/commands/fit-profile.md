# `3d fit-profile` — closed-form length / width profile of a mask

Measures one silhouette without any optimiser: principal axis, oriented rectangle, per-station lateral extents, a shape `kind`, and how well that
straight-axis profile re-draws the mask. About 1–3 ms per mask (numpy + OpenCV), so it can run on every segmented object.

**Why it exists.** A photo → 3-D pipeline needs a size and a rough shape for *every* object in a scene, instantly. The closed form answers
that (median recon IoU 0.97 on 97 real carpet masks) and decides whether the soft-silhouette fit ([fit-silhouette](fit-silhouette.md)) is needed at all.
It is the first stage of `fit_auto` and the template chooser (box → p=8, cylinder/lathe/ring → p=2, flat loft → p=4).

## Usage

```
3d fit-profile <mask.png|instances.json#ID> --mm-per-px X [--k 12] [--nbins 96] [--axis-deg A] [--json]
```

| Argument | Default | What |
|---|---|---|
| `mask` | — | PNG, any non-zero pixel is the object; or `instances.json#ID` (an instance of `3d segment`, its `mask` path is relative to the JSON) |
| `--mm-per-px` | `1.0` | scale; every `*_mm` field is in mm (1.0 = pixels) |
| `--k` | `12` | stations reported in `stations` |
| `--nbins` | `96` | bins along the axis the profile is measured in |
| `--axis-deg` | PCA | force the axis angle (image frame); PCA is ill-defined for near-square masks |
| `--json` | off | print exactly one JSON object (otherwise one summary line) |

```bash
3d fit-profile masks/046.png --mm-per-px 1.263 --json | jq '.kind, .length_mm, .iou_recon'
3d fit-profile instances.json#70 --mm-per-px 1.263
```

## Output (`--json`)

```json
{"kind": "box|cylinder_capsule|lathe_profile|loft_flat|ring_lathe",
 "length_mm": 255.3, "max_width_mm": 38.4, "rect_mm": [255.0, 36.1], "rect_fill": 0.81, "solidity": 0.93, "elongation": 7.1,
 "center_px": [x, y], "extent_center_px": [x, y], "axis_unit": [ax, ay], "axis_deg": -83.2, "lateral_offset_mm": 0.4,
 "stations": [{"t": 0.0, "lo_mm": -17.0, "hi_mm": 19.4}, ...],
 "iou_recon": 0.981, "pixels": 3789, "ring_mm": {"r_outer": 40.0, "r_inner": 22.5}, "time_ms": 2.3}
```

`stations[k]` are lateral extents (`lo`/`hi` on either side of the axis line through `center_px`) at `t = k/(K-1)` along the length;
`center_px` / `extent_center_px` are in the input mask's pixel frame; the axis is oriented so the wider half comes first ("handle first").
`ring_mm` exists only for `ring_lathe` (tape rolls, washers). The `kind` classifier is crude by design (a thin capsule can read as `box`); the
decisive number is `iou_recon`.

## Limits (measured, not hoped)

* A straight-axis profile cannot follow bent or open shapes: the recorded open red pliers reads `iou_recon` 0.77, tin snips 0.85. Low IoU is the signal to refine or ask for a better mask.
* It says nothing about thickness or 3-D pose.
* Empty masks, masks below 8 pixels (a 1-pixel mask is noise), a non-positive/non-finite `--mm-per-px` exit 2 (`BadInput`); no output ever contains NaN.

Exit codes: 0 ok, 2 bad input (missing file, empty/tiny mask, bad scale).
Python: `from threed_vision.fit_profile import fit_profile; fit_profile(mask_bool, mm_per_px, K=12)`.
