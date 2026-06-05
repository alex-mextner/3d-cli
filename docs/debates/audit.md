# Debate Notes Audit

Date: 2026-06-05

## Scope

This audit searched for Claude-organized debate, advisor, critique, and discussion
results related to this repo, with special attention to `fit-camera`, reference masks,
and render/backplate comparison workflow.

Searched:

- Primary user-supplied source: `/Users/ultra/.moshi/uploads/ideas.pdf`
- PDF provenance path named on page 9:
  `/Users/ultra/xp/3d-tests/debate/debate.md`
- This worktree: `/Users/ultra/.config/superpowers/worktrees/3d-cli/debate-notes-audit`
- Sibling worktrees under `/Users/ultra/.config/superpowers/worktrees/3d-cli`
- Tracked repo docs and specs, including `docs/critic-prompts.md`, `docs/research/`,
  `docs/specs/`, `ROADMAP.md`, `RESEARCH.md`, and `docs/benchmarks/`
- Hidden repo metadata: `/Users/ultra/xp/3d-cli/.git/opencode`
- Claude transcripts referenced by tracked docs under
  `~/.claude/projects/-Users-ultra-xp-3d-cli/...`

## Consolidated Verdict

The strongest debate result is the user-supplied PDF at
`/Users/ultra/.moshi/uploads/ideas.pdf`. It converges with the repo's later benchmark
lessons: the comparison workflow fails when it trusts raw silhouette IoU from a bad mask
or degenerate camera. The practical fix order is:

1. Reject degenerate camera candidates during search, before coordinate descent can refine
   them.
2. Segment the reference first, with multi-attempt GrabCut as the low-dependency path and
   SAM 2 as the better optional path.
3. Show users an inspectable render/diff/reference collage or backplate artifact.
4. Report SSIM/DSSIM alongside IoU, with SSIM as the prominent visual-quality signal and
   IoU as a locked-pose silhouette tiebreaker.
5. Add hard camera search bounds (`az_range`, `el_range`) and keep the fitted pose frozen
   for downstream shape edits.

Longer-term ideas from the debate are useful but lower priority: landmark reprojection,
LPIPS/perceptual loss, and hierarchical or learned optimizers.

## Concrete Results Found

### Render/reference comparison debate swarm

Primary source:

- `/Users/ultra/.moshi/uploads/ideas.pdf`

Corroborating source named by the PDF:

- `/Users/ultra/xp/3d-tests/debate/debate.md`

Conclusion:

- The PDF is a synthesized result from an AI debate swarm: `deepseek-coder-v2:16b`,
  `qwen2.5:14b`, `gemini-2.5-flash`, and `gemini-3.1-pro`, using 12 turns of
  round-robin polling on 2026-06-05.
- The debate focused on degenerate `3d compare` / `fit-camera` / segmentation behavior
  for front-facing symmetric views. It cites the observed failure mode as Pantheon front
  IoU around `0.006`, with oblique around `0.38-0.44`.
- The group converged on a practical priority order:
  1. Add a degenerate pose penalty during camera search, not only after refinement.
  2. Use multi-attempt GrabCut or SAM 2 for reference segmentation.
  3. Show SSIM as the primary user-facing quality metric, with IoU as a tiebreaker.
  4. Add geometric bounds on camera search space.
  5. Add landmark reprojection as a future camera-fit objective.
  6. Add LPIPS/perceptual loss as a future camera-fit objective.
  7. Consider a hierarchical three-stage optimizer later.

Actionable fit-camera/reference/backplate recommendations:

- Move degenerate candidate rejection into the optimization loop. The PDF specifically
  recommends rejecting camera candidates during `eval_losses_async` when the rendered
  silhouette occupies less than 8% or more than 92% of the frame, so coordinate descent
  does not spend steps refining bad candidates.
- Run multiple GrabCut attempts with different border insets and choose the mask whose
  area fraction is plausible. The PDF proposes `(2%/4%, 6%/12%, 12%/18%)` inset configs
  scored toward a `30-70%` subject area target.
- Add hard pose bounds to random search, especially `az_range` and `el_range`, to prevent
  implausible camera candidates such as below-ground views and extreme rotations.
- Report both IoU and SSIM in `3d compare`. The PDF recommends making SSIM the primary
  quality signal shown to users because it captures luminance, contrast, and structure
  better than binary silhouette IoU on symmetric subjects.
- Prefer SAM 2 as the long-term segmentation path, falling back to multi-attempt GrabCut
  with a structured `MissingDependency` when SAM 2 is unavailable.
- Keep render/reference/backplate comparison visually inspectable. The PDF reinforces the
  need for a diagnostic artifact that makes degeneracy obvious; in repo terms this aligns
  with `3d compare` collage/backplate output.

Status notes:

- The PDF says degenerate pose rejection during search and multi-attempt GrabCut were
  implemented in a `worktree-compare-fix` worktree. This audit did not find that worktree
  under `/Users/ultra/.config/superpowers/worktrees/3d-cli`, but current repo code already
  contains related `3d compare` SSIM/DSSIM/collage behavior and multi-attempt GrabCut
  references.
- The PDF's "Implementation Plan (Immediate)" lists: degenerate pose rejection during
  search, multi-attempt GrabCut, SSIM reporting, geometric bounds, then SAM 2 and LPIPS in
  a next worktree.

### Pantheon benchmark arm lessons

Tracked artifact:

- `docs/benchmarks/3d-cli-arm-lessons.md`

Corroborating untracked Claude artifacts:

- `~/.claude/projects/-Users-ultra-xp-3d-cli/bc61f43a-c443-4937-ba2a-3816d648bf66/subagents/workflows/wf_1c39bdc2-6b1/agent-aa12922bb6de23c7d.jsonl`
- `~/.claude/projects/-Users-ultra-xp-3d-cli/bc61f43a-c443-4937-ba2a-3816d648bf66/subagents/workflows/wf_1c39bdc2-6b1/journal.jsonl`

Conclusion:

- Claude ran a Pantheon photo-match benchmark arm using the `3d` CLI and consulted an
  advisor during the modeling loop.
- The main modeling conclusions were: use shallow spherical caps for exterior domes,
  build joined assemblies with intentional interpenetration plus `union()`, and judge
  massing from clean multi-view/orthographic renders rather than chasing a single
  suspect score.
- The main CLI/tooling conclusions were: `fit-camera` can produce a degenerate zoomed
  solution that games IoU; raw photo IoU is polluted when the reference is unsegmented;
  `check` should separate manifold/watertight failure from printability failure for
  display models; and users need a single render/diff/reference comparison artifact.

Actionable fit-camera/reference/backplate recommendations:

- Warn or reject when `fit-camera` finds a degenerate close-up/fragment camera.
- Segment references before scoring. Prefer a `preprocess` subject mask over raw
  thresholding against sky, ground, or neighboring objects.
- Add or keep a render/diff/reference collage/backplate artifact so the render,
  reference, and pixel difference can be inspected together.
- Treat silhouette IoU as a tiebreaker unless the pose is sane and the reference mask is
  clean.

### Benchmark lesson integration

Tracked artifacts:

- `assets/templates/AGENTS.project.md`
- `docs/benchmarks/3d-cli-arm-lessons.md`

Corroborating untracked Claude artifacts:

- `~/.claude/projects/-Users-ultra-xp-3d-cli/bc61f43a-c443-4937-ba2a-3816d648bf66/subagents/workflows/wf_7355d1da-141/journal.jsonl`
- `~/.claude/projects/-Users-ultra-xp-3d-cli/bc61f43a-c443-4937-ba2a-3816d648bf66/workflows/wf_7355d1da-141.json`

Conclusion:

- A Claude workflow named `pantheon-bench-prep` organized parallel agents and explicitly
  assigned one agent to analyze the prior `3d-cli` benchmark arm and bake the lessons into
  the init template.
- The structured result says the agent extracted Bash commands, assistant reasoning, and
  tool failures from the Pantheon transcript, then wrote the benchmark lessons doc and
  updated the project template.
- The recorded lessons include: optimize geometry rather than camera, segment references
  before comparison, reject degenerate fit-camera outputs, cross-check with multi-view and
  collage artifacts, and iterate toward a tool-measured similarity target rather than
  stopping at "looks ok."

Actionable fit-camera/reference/backplate recommendations:

- Make reference preprocessing part of the natural fit/score path, not an optional fact
  users must remember.
- Prefer comparison artifacts that are visually auditable by humans and agents:
  multi-view renders plus render/diff/reference collage.
- Add guardrails so high IoU from a zoomed or cropped camera is flagged as unreliable.

### Research-derived critic and match-loop recommendations

Tracked artifacts:

- `docs/research/report.md`
- `docs/APPLY-RESEARCH.md`
- `docs/critic-prompts.md`
- `ROADMAP.md`

Conclusion:

- These are not a single Claude "debate result", but they are the canonical research
  conclusions that the Claude-organized work folded into the roadmap.
- The repo consistently recommends a forced-monotonic loop: a vision critic proposes one
  numeric parameter edit, deterministic tools score it, and the orchestrator accepts only
  strict metric improvements that keep required gates passing.
- The repo also recommends a locked camera pose, a changelog to prevent oscillation,
  overlay/edge-overlay inputs for the critic, and optional depth/normal channels as critic
  context rather than generated meshes as deliverables.

Actionable fit-camera/reference/backplate recommendations:

- Freeze the fitted camera before shape optimization; a drifting pose invalidates score
  comparisons.
- Use IoU plus AE and overlay diagnostics as deterministic arbiters, not the critic's own
  judgment.
- Hand the critic an overlay/backplate-style diagnostic, not just separate render and
  reference images.
- Add per-feature masks or scores when global IoU drives edits to the wrong region.

### Project-agnostic architecture debate/result

Tracked artifact:

- `docs/specs/2026-06-05-3d-cli-architecture.md`

Related sibling worktree:

- `/Users/ultra/.config/superpowers/worktrees/3d-cli/arch-structure`

Conclusion:

- The architecture result is that `3d` should be project-agnostic, with subject-specific
  knowledge living in project data and feature lists rather than core modules.
- This affects critic and backplate workflows directly: prompts and preprocessing must talk
  about "the subject" and caller-supplied features, not hardcoded locomotive parts.

Actionable fit-camera/reference/backplate recommendations:

- Keep feature vocabularies caller-supplied in critic prompts.
- Keep reference/backplate preprocessing generic: mask the subject, not a specific domain
  object.

### Fit-camera sibling audit

Sibling worktree artifact:

- `/Users/ultra/.config/superpowers/worktrees/3d-cli/fit-camera-audit/docs/fit-camera-audit.md`

Conclusion:

- The current branch already contains several fit-camera improvements: deterministic random
  search, bbox-derived bounds, reference-aspect optimization, degenerate candidate rejection
  for extreme frame coverage, configurable elevation bounds, overlay diagnostics, optional
  PCA axes, and `ssim` reporting.
- The remaining confirmed issues in that sibling audit were documentation and test coverage:
  document `--el-range`, document JSON output fields, assert missing-reference structured
  errors, and assert `--el-range` forwarding.

Actionable fit-camera/reference/backplate recommendations:

- Treat the sibling audit as implementation status, not a broader debate result.
- If integrating it, keep the tests and docs updates with the command surface change.

## Locations With No Concrete Debate Results

- `/Users/ultra/xp/3d-cli/.git/opencode` exists, but it is an empty file, not a readable
  transcript directory.
- No tracked `docs/debates/` directory existed before this audit.
- The sibling worktrees mostly contain the same tracked research and benchmark documents;
  the only directly relevant uncommitted artifact found was
  `fit-camera-audit/docs/fit-camera-audit.md`.

## Remaining Unknowns

- Some Claude transcripts are untracked and outside the repo. They corroborate the tracked
  docs, but they are not durable repo artifacts unless explicitly copied or summarized.
- The audit did not exhaustively parse every large image-containing JSONL line; it targeted
  structured workflow results, advisor/debate terms, and paths referenced by tracked docs.
- If more debate results exist, likely locations are additional Claude workflow journals under
  `~/.claude/projects/-Users-ultra-xp-3d-cli/.../subagents/workflows/` or future
  `docs/benchmarks/` notes.
