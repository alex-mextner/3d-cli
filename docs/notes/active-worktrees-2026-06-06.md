# Active Worktrees And Remote Branch Status - 2026-06-06

Last verified: 2026-06-06 in the `roadmap/proof-requirements` worktree. Regenerate or
recheck this file before acting on any merge, branch deletion, or worktree deletion.

This note records why the current long-running worktrees and remote-only roadmap branches
still exist and what must happen next. It is intentionally more specific than `git worktree
list`.

This is a point-in-time snapshot, not a durable source of branch truth. Commit IDs,
ahead/behind status, test counts, and dirty-file lists must be rechecked before merging or
deleting any branch. Worktree paths below use the default project worktree root template
`<worktree-root>/3d-cli/<branch-slug>`, not a portable absolute path. After this note lands
on `main`, the affected worktrees should be rebased promptly so their dirty `AGENTS.md` and
`ROADMAP.md` edits are either preserved in small commits or deliberately discarded after
review.

## `roadmap/e2e-fit-camera-readable`

Path: `<worktree-root>/3d-cli/e2e-fit-camera-readable`

Status: committed and pushed on branch `roadmap/e2e-fit-camera-readable`.

What it did:

- added a human-readable e2e workflow around `fit-camera`, shell redirection, pipes,
  `score | tee | awk >`, camera reuse, and a generated quality report;
- checks real artifacts and metrics, not only exit codes;
- includes helper changes in `tests/e2e/workflow_helper.py`, `lib/cli/pyrun.py`, and
  `doctor` tests.

Review reported by the agent:

- multi-model review completed;

Verification reported by the agent:

- focused checks passed;
- full pre-commit reportedly completed but left one existing warning.

Merge readiness: not merge-ready under the current zero-warning policy until the warning is
reproduced, classified, and fixed or explicitly waived by policy.

Why it is not merged yet:

- it was written before the latest `main` proof/proxy changes and needs a rebase/conflict
  review;
- it mainly proves readable rejection workflow, not a positive `fit-camera` success;
- integration should verify it still aligns with the stronger proof requirement: reference,
  render, overlay, metrics, and explicit failure/success label.

Next action: rebase onto current `main`, review the helper/core changes carefully, run `3d
test`, then merge if the e2e story remains accurate.

## `roadmap/fit-camera-proof`

Path: `<worktree-root>/3d-cli/fit-camera-proof`

Status: branch has three commits ahead of old `origin/main`, is behind current `main`, and
has an uncommitted `ROADMAP.md` change.

What it did:

- added `--search-mode proof`, `--view-prior`, and `--az-range`;
- added proof-mode pose diagnostics and broader search;
- made Pantheon diagnostic/failure instead of success;
- added synthetic oracle and directional/equivalence diagnostics.

Review reported by the agent:

- Codex review found no remaining blockers;
- a second available reviewer was used when Gemini/DeepSeek were unavailable.

Verification reported by the agent:

- focused tests passed;
- full commit hook reportedly completed but left one existing warning.

Merge readiness: not merge-ready under the current zero-warning policy until the warning is
reproduced, classified, and fixed or explicitly waived by policy.

Why it is not merged yet:

- it is stale relative to current `main`;
- it has dirty roadmap edits that must be inspected rather than blindly overwritten;
- the reported Pantheon result is still failed/diagnostic, not a useful real proof;
- it overlaps conceptually with `roadmap/spatial-fit-experiments` and should be integrated
  in a deliberate order.

Next action: inspect and preserve the useful dirty `ROADMAP.md` changes, rebase, then either
merge the proof-mode CLI pieces or supersede them with the newer spatial experiment branch.

## Remote-only branch: `roadmap/spatial-fit-experiments`

Path: `<worktree-root>/3d-cli/spatial-fit-experiments`

Status: remote branch `origin/roadmap/spatial-fit-experiments` exists, but no active local
worktree is currently attached to it. Recheck the remote SHA before recreating the worktree.

What it did:

- added experimental `fit-camera --objective contour`;
- added `--spatial-report`, `--trace`, `--mask-polarity`, and `--backplate`;
- added `lib/spatial_fit_metrics.py`;
- added synthetic oracle, candidate-evolution video, finite-difference pose sensitivity,
  view-bank retrieval, and symmetry/equivalence diagnostics.

Important result:

- synthetic view bank recovered the hidden azimuth/elevation top-1;
- local finite-difference monotonicity looked good in synthetic cases;
- the local optimizer still did not exactly recover camera by itself;
- real Pantheon remained a warning/failure, which is the correct label.

Review reported by the agent:

- Codex found no actionable regressions in the final uncommitted review;
- Gemini via opencode was unavailable in that run;
- the fallback OpenCode/DeepSeek reviewer findings were reported as addressed.

Verification reported by the agent:

- worktree doctor passed;
- `bin/3d test` reportedly completed with 2865 tests, one skipped test, and one existing
  warning;
- the commit hook repeated the same gate.

Merge readiness: not merge-ready under the current zero-warning policy until the warning is
reproduced, classified, and fixed or explicitly waived by policy.

Why it is not merged yet:

- no active local worktree is currently attached, so it must be recreated from
  `origin/roadmap/spatial-fit-experiments` before integration;
- it is experimental and needs review against the stricter proof rule;
- it should not be presented as solved real-photo fit-camera.

Next action: create a local worktree from `origin/roadmap/spatial-fit-experiments`, rebase,
inspect artifacts, and integrate the metrics/reporting pieces before enabling any behavior
by default.

## `roadmap/mac-image3d-providers`

Path: `<worktree-root>/3d-cli/mac-image3d-providers`

Status: committed and pushed on branch `roadmap/mac-image3d-providers`.

What it did:

- evaluated Apple Silicon image-to-3D provider candidates;
- identified Hunyuan3D 2.1 MLX as the best current M4 Pro candidate;
- found Stable Fast 3D promising but gated and memory-risky;
- noted ZeroGPU/TRELLIS can work, but quota/auth/provider failures are real.

Review reported by the agent:

- Codex review was run on the uncommitted research diff and reported no remarks.

Verification reported by the agent:

- commit hook passed with 2948 tests, four skipped tests, and no mypy failures.

Why it is not merged yet:

- it is research documentation, not a working provider command;
- it must be reconciled with `3d auth hf` and the proxy-align/provider plan;
- no final USDZ/proof was produced from a visually good image-to-3D result.

Next action: merge the research doc after review, then implement a provider command only with
auth, cache, quota metadata, and a render-against-reference rejection gate.

## `roadmap/review-discipline`

Path: `<worktree-root>/3d-cli/roadmap-review-discipline`

Status: has a committed process-doc change plus uncommitted changes in `AGENTS.md`,
`ROADMAP.md`, `docs/commands/README.md`, and `docs/rules/development.md`.

What it did:

- expanded multi-model review discipline and API fallbacks;
- appears partially superseded by the live `review` CLI and by the proof/reporting rules now
  recorded in `AGENTS.md`.

Review status:

- the committed part may have been reviewed earlier, but the current dirty diff has not
  been reviewed in this audit.

Verification status:

- not run for the current dirty diff.

Why it is not merged yet:

- uncommitted changes need careful diff review;
- it touches shared process docs and may conflict with current `AGENTS.md` updates;
- it should not be merged blindly just because it is "process" work.

Next action: inspect diff, keep only the still-current review rules, reconcile with the
Telegram/proof requirements, then either merge a small process commit or delete the stale
branch.
