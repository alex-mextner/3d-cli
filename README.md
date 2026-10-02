# 3d-cli

Local workspace for reproducible CLI/MCP-driven 3D modeling and validation.

## Layout
- `projects/low-poly-cat/` — current cat reconstruction, editable sources, renders and exports.
- `scripts/blender/` — headless Blender scripts used by the pipeline.
- `vendor/` — pinned external MCP/skills/camera-matching references.
- `research/` — notes and comparisons of techniques.
- `share/` — small set of artifacts exposed through Tailscale Serve.

## Current cat artifact
`projects/low-poly-cat/exports/low_poly_cat_150mm_blender_v6.usdz`

Editable source:
`projects/low-poly-cat/source/cat_v6.blend`

Current goal: reference-locked reconstruction, not plausible freehand modeling. Export only after multi-view overlay gates pass.