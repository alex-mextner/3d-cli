# Fit-camera audit

Date: 2026-06-05

## Scope

Audited the current `fit-camera` implementation and command surface after the previous fit-camera/reference-alignment work. Files inspected:

- `lib/fit_camera.py`
- `lib/commands/fit_camera.py`
- `docs/commands/fit-camera.md`
- `tests/test_commands_misc.py`
- `tests/test_cli_smoke.py`
- the older `wave2-debug-overlays/lib/fit_camera.py` worktree copy for comparison only

## Findings

The current branch has no diff from `origin/main`, but `lib/fit_camera.py` already contains the main reference-alignment changes that were absent from the older `wave2-debug-overlays` worktree:

- Deterministic random search using a seed and ordered candidate reduction.
- Bbox-derived center, distance, pan bounds, and step sizes.
- Reference-aspect optimization and final render sizing.
- Degenerate candidate rejection when a rendered silhouette covers less than 8% or more than 92% of the frame.
- Configurable elevation bounds through `--el-range`, defaulting to `-45,85`.
- Overlay diagnostics, optional PCA axes, and JSON/console `ssim` reporting.

No small implementation bug was confirmed during this audit. The bounded issues found were documentation and test coverage:

- `docs/commands/fit-camera.md` did not document `--el-range`.
- The JSON output contract did not mention `ssim`, `fit_render`, or `overlay`.
- `tests/test_commands_misc.py::test_fit_camera_missing_ref` was a placeholder and did not assert the structured missing-reference error path.
- The command test did not assert that `--el-range` is forwarded to `lib/fit_camera.py`.

## Proof commands

Commands run from the `fit-camera-audit` worktree:

```bash
git status --short --branch
diff -u lib/fit_camera.py ../wave2-debug-overlays/lib/fit_camera.py
diff -u lib/commands/fit_camera.py ../wave2-debug-overlays/lib/commands/fit_camera.py
uv run --with pytest pytest tests/test_commands_misc.py -k fit_camera
uv run --with pytest pytest tests/test_cli_smoke.py
python3 bin/3d fit-camera --help
```

Results:

- Focused fit-camera command tests: 9 passed.
- CLI smoke harness: 41 passed.
- Direct `3d fit-camera --help`: exit 0 and includes `--el-range`.

Initial environment notes:

- `uv run pytest ...` failed because the new worktree virtualenv did not include `pytest`; rerun with `uv run --with pytest pytest ...`.
- `python bin/3d ...` failed because `python` is not on PATH in this environment; rerun with `python3`.

## Remaining risks

This audit did not run a real OpenSCAD fit-camera render because the requested scope emphasized safe smoke and robustly skipped tests. The algorithm remains image-mask based: raw, cluttered references can still produce polluted masks unless the caller passes a segmented mask or uses `3d compare`/`preprocess` first.
