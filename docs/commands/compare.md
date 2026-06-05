# `3d compare` — segmented model/reference comparison

Compares a model or already-rendered PNG to a reference photo after first segmenting the
reference subject. It writes comparison artifacts and prints machine-parseable metric
lines.

## Usage

```
3d compare <model.scad|render.png> <reference.jpg> [-o outdir] [options]
```

| Option | Default | What |
|---|---|---|
| `-o, --out DIR` | `/tmp/3dcompare` | Directory for `mask.png`, `matched_render.png`, `diff.png`, and `collage.png`. |
| `--rand N` | `80` | Random-search samples for camera fitting. |
| `--refine N` | `40` | Coordinate-descent refine steps for camera fitting. |

```bash
3d compare model.scad photo.jpg -o match/
3d compare render.png photo.jpg
3d compare model.scad photo.jpg --rand 8 --refine 3
```

## Notes

- Requires ImageMagick for metrics, diffs, and collage output.
- A `.scad` input runs the fit/compare pipeline; a PNG input compares an existing render.
- Output includes [`IoU`](GLOSSARY.md#iou), [`SSIM`](GLOSSARY.md#ssim), `DSSIM`, artifact paths, and fallback status.
