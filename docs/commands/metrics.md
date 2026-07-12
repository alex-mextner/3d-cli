# `3d metrics` — inspect persisted metrics + compute metric batteries

Reads command metrics from the JSONL store under `~/.local/share/3d-cli/metrics/`, or
`$XDG_DATA_HOME/3d-cli/metrics/` when `XDG_DATA_HOME` is set, and computes the standard
geometry (mesh↔mesh) and perceptual (image↔image) metric batteries (APPLY-RESEARCH
P1.1 / P1.2).

## Usage

```
3d metrics <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | Show metric JSONL files, record counts, and latest timestamp. |
| `show [--limit N] [--command NAME]` | Print metric records as deterministic JSON lines. |
| `geometry A B [options]` | 3D shape battery between two meshes/STLs (B = target). |
| `perceptual A B [options]` | Perceptual/semantic image battery between two images. |

```bash
3d metrics list
3d metrics show --limit 20
3d metrics show --command render --limit 5
```

## `geometry` — 3D shape metrics between two meshes

Samples both meshes by surface area and reports the pinned-convention battery. `B` is the
target: F-score `tau` is 1% of `B`'s bbox diagonal and F-score precision is "fraction of
`A` points near `B`". Inputs are assumed to share a coordinate frame (no ICP is run;
alignment is recorded as `none`).

| Metric | Range | Best | Sense |
|---|---|---|---|
| F-score@tau (**primary**, Tatarchenko CVPR 2019) | 0..1 | 1 | higher |
| Chamfer L1 / L2 (bidirectional, mean) | ≥0 | 0 | lower |
| Hausdorff (directed + symmetric) | ≥0 | 0 | lower |
| Normal consistency (abs-dot) | 0..1 | 1 | higher |
| Volumetric IoU (shared voxel grid) | 0..1 | 1 | higher |

| Option | Default | What |
|---|---|---|
| `--samples N` | 50000 | Area-weighted surface samples per mesh. |
| `--tau-frac F` | 0.01 | F-score tau as a fraction of the target bbox diagonal. |
| `--voxel-res R` | 48 | Voxel grid resolution for volumetric IoU. |
| `--seed S` | 0 | Sampling seed (determinism). |
| `--json` | off | Print the full JSON report (senses + convention). |
| `--no-store` | off | Do not append a record to the metrics store. |

```bash
3d metrics geometry candidate.stl target.stl
3d metrics geometry candidate.stl target.stl --samples 100000 --json
3d metrics geometry candidate.stl target.stl --tau-frac 0.02 --voxel-res 64 --no-store
```

Prints machine-parseable `KEY=VALUE` lines: `F_SCORE`, `F_SCORE_TAU`, `PRECISION`,
`RECALL`, `CHAMFER_L1`, `CHAMFER_L2`, `HAUSDORFF`, `NORMAL_CONSISTENCY`, `VOLUMETRIC_IOU`.
Requires the `trimesh`/`scipy`/`numpy` runtime (resolved via `.venv`/`uv`).

## `perceptual` — image metrics with explicit senses

Reports PSNR always; LPIPS and CLIP-similarity when their (heavy) wheels are installed.
A missing wheel is reported as `unavailable` with the exact install command — never a
fabricated score.

| Metric | Range | Best | Sense | Needs |
|---|---|---|---|---|
| PSNR | dB | high | higher | ImageMagick / numpy |
| LPIPS (Zhang CVPR 2018) | ≥0 | 0 | lower | `pip install lpips torch pillow` |
| CLIP-sim (Hessel 2021) | 0..100 | 100 | higher | `pip install open_clip_torch torch pillow` |

| Option | Default | What |
|---|---|---|
| `--metrics LIST` | `psnr,lpips,clip` | Comma-separated subset to compute. |
| `--json` | off | Print the full JSON report (senses + convention). |
| `--no-store` | off | Do not append a record to the metrics store. |

```bash
3d metrics perceptual render.png photo.jpg
3d metrics perceptual render.png photo.jpg --metrics psnr,lpips
3d metrics perceptual render.png photo.jpg --json --no-store
```

Prints `PSNR`/`PSNR_SENSE` and, per channel, either `LPIPS=<value>`/`LPIPS_SENSE=...` or
`LPIPS=unavailable`/`LPIPS_INSTALL=...` (same for `CLIP`). Exit code is `127` only when
every requested channel was unavailable (nothing could be measured).
