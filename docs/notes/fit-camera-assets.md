# Fit-Camera Asset Inventory

This note records existing local assets that are useful for testing `3d fit-camera`.
The files were found under `/Users/ultra/xp`; they are not copied into this repository.

## Best fixture candidates

### `/Users/ultra/xp/3d-tests/*`

The `3d-tests` directory contains several Pantheon test projects with the same basic
shape:

- `pantheon.scad`
- `references/front.jpg`
- `references/oblique.jpg`
- `3d.yaml` with `project.reference: references/front.jpg`
- generated fit/compare artifacts such as `fit_0.json`, `fit_0_fit.png`,
  `fit_0_overlay.png`, `judge_front/camera_fit.png`, `judge_front/camera_overlay.png`,
  masks, diffs, and collages.

Most useful concrete path:

```bash
/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad
/Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg
/Users/ultra/xp/3d-tests/gflash-3dcli/references/oblique.jpg
/Users/ultra/xp/3d-tests/gflash-3dcli/fit_0.json
/Users/ultra/xp/3d-tests/gflash-3dcli/fit_1.json
```

Why it is useful:

- It is a real paired model/reference fixture, not a synthetic unit-only asset.
- It has both front and oblique references.
- It already has expected diagnostic artifacts for visual regression checks.
- It exercises the camera fitter on a recognizable asymmetric building with columns,
  a portico, and a dome instead of the symmetric `examples/cube.scad`.

Verified locally:

```bash
./bin/3d render /Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad \
  --view front --size 240x240 -o /tmp/3d-fit-assets-pantheon-front.png

./bin/3d fit-camera /Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad \
  /Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg \
  --out /tmp/3d-fit-assets-pantheon-front.json \
  --rand 4 --refine 1 --opt-size 120x --final-size 240x --seed 7

./bin/3d fit-camera /Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad \
  /Users/ultra/xp/3d-tests/gflash-3dcli/references/oblique.jpg \
  --out /tmp/3d-fit-assets-pantheon-oblique.json \
  --rand 4 --refine 1 --opt-size 120x --final-size 240x --seed 7
```

Smoke results:

- Front smoke fit completed, wrote JSON/fit/overlay artifacts in `/tmp`, and reported
  IoU `0.2544` with the intentionally tiny `--rand 4 --refine 1` search.
- Oblique smoke fit completed, wrote JSON/fit/overlay artifacts in `/tmp`, and reported
  IoU `0.2028` with the same tiny search.
- Existing full-run artifacts in `gflash-3dcli/fit_0.json` and `fit_1.json` report IoU
  `0.7097` and `0.68`.

Recommended fixture usage:

- Use this as an integration fixture only when `/Users/ultra/xp/3d-tests` exists.
- Gate it with `pytest.skip` or an env opt-in if the external path is missing.
- Assert command completion, JSON schema fields (`camera_arg`, `camera`, `iou`,
  `fit_render`, `overlay`), and artifact existence. Avoid strict IoU thresholds on the
  quick smoke path unless the random/refine budget is fixed and the OpenSCAD version is
  pinned.

### `/Users/ultra/xp/garage-band/projects/lego-loco`

Useful paths:

```bash
/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad
/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_orient_express_loco_only.jpg
/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_orient_express_side.jpg
/Users/ultra/xp/garage-band/projects/lego-loco/preprocess/mask.png
/Users/ultra/xp/garage-band/projects/lego-loco/match/camera.json
/Users/ultra/xp/garage-band/projects/lego-loco/match/camera_fit.png
```

Why it is useful:

- It appears to be the original domain source for `fit_camera.py`; `match/camera.json`
  has `camera_arg`, search params, and IoU `0.7671`.
- The assembly is asymmetric and closer to the reference-matching workflow than a cube.
- It includes real references plus a preprocessed mask.

Verified locally:

```bash
./bin/3d fit-camera /Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad \
  /Users/ultra/xp/garage-band/projects/lego-loco/references/ref_orient_express_side.jpg \
  --out /tmp/3d-fit-assets-loco-side.json \
  --rand 2 --refine 0 --opt-size 120x --final-size 240x --seed 7
```

Smoke result: the command completed and wrote artifacts, but the intentionally tiny
search reported IoU `0.0000`. Treat this as a regression/debug corpus, not as the first
CI fixture. The stored `match/camera.json` is the more useful evidence for this project.

### `/Users/ultra/xp/garage-band/projects/cell-sensor-adapter`

Useful paths:

```bash
/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/assembly.scad
/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/docs/photos/loadcell_bracket_clear.png
/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/docs/photos/1.png
/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/docs/bracket/bonly_workface.png
/Users/ultra/xp/garage-band/projects/cell-sensor-adapter/previews/loadcell/brk_recess_match.png
```

Why it is useful:

- It is a real mechanical project with a renderable OpenSCAD assembly and reference-like
  photos/previews.
- It is better suited for manual visual checks than for immediate automated fit-camera
  tests because no existing camera JSON was found.

Verified locally:

```bash
./bin/3d render /Users/ultra/xp/garage-band/projects/cell-sensor-adapter/assembly.scad \
  --view 3-4 --size 240x180 -o /tmp/3d-fit-assets-cell-assembly.png
```

## Repository-local assets

The current repository has very limited fit-camera assets:

- `examples/cube.scad` renders, but it is symmetric and a poor camera-fit target.
- `docs/img/section.jpg` is an illustrative documentation image, not a model reference.
- No repository-local paired `model.scad` plus reference image plus expected camera JSON
  was found.

## Dependency notes

Available on this machine during the inventory:

- `/opt/homebrew/bin/openscad`
- `/opt/homebrew/bin/magick`
- `/opt/homebrew/bin/uv`
- `/opt/homebrew/bin/codex`
- `/opt/homebrew/bin/opencode`
- `/opt/homebrew/bin/gemini`
- `/opt/homebrew/bin/jq`

One local dependency issue was observed: after `3d render` created `.venv`, `3d
fit-camera` initially failed because `.venv` lacked Pillow and old `cli.pyrun`
preferred `.venv` over `uv run --with ...`. Current `cli.pyrun` probes the requested
imports first and falls back to `uv --with` or an importable system Python when the
local `.venv` is incomplete.

## Risks

- The best assets live outside this repository, so CI cannot rely on them unless they
  are copied into a controlled fixture package or guarded as local-only tests.
- Some references are real photos or generated artifacts with unclear licensing; do not
  commit them without checking provenance.
- Fast `--rand`/`--refine` smoke runs prove command viability but not fit quality.
- Strict pixel/IoU assertions may vary with OpenSCAD, fonts, and render settings.
