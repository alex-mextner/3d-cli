# Development process

Ported and Python-adapted from the hyper-canvas-draft workspace. These are the mandatory
working rules for any agent or human touching `3d`.

## Branch & worktree discipline
- Independent work runs in **separate git worktrees** with distinct file ownership (clean merge).
- Do NOT do feature work directly in a shared checkout another agent is mutating. Tiny doc/config
  edits also go in a worktree if a concurrent writer holds the main tree.
- Branch names: `<area>-<short-description>` (e.g. `core-object-model`, `docs-reframe-readme`).

## The cycle
1. **Diagnose first (root cause).** Reproduce; read the error (it often contains the fix); check
   recent changes (`git log`, `git diff`). Collect evidence before proposing a fix.
2. **TDD** (see `testing.md`): failing test → confirm it fails for the right reason → minimal code →
   green → refactor.
3. **Before every commit (3-stage, mandatory even if asked to "just commit"):**
   1. Dead-code / unused scan (see `code-style.md`); fix or justify.
   2. Self-review your own diff.
   3. `timeout 1200 codex exec review --uncommitted` — read findings (`tail -120`), fix real issues.
   4. If a slice/spec asks for an additional reviewer or a narrower review command, run that too;
      stricter task instructions override this baseline.
4. **Commit** atomically: message `<area>: <what changed>` (what, not "update"/"fix"). Formatter-only
   churn goes in a separate `style:` commit BEFORE logic changes.
5. **Push** to origin regularly — don't accumulate a long unpushed tail.

## Hard rules
- **Never** `git commit --no-verify`; never skip/disable a pre-commit hook — fix the cause.
- **Zero-warnings**: lint + mypy warnings are errors. No blanket ignores.
- Only `Edit`/`Write` for code; no `sed`/`perl`/`awk` rewrites.
- **Timeouts are a smell** — fix the root cause, don't paper over with a longer wait.
- Wrap every hang-prone shell command (network, render, install, codex) in `timeout`.
- Deferred review findings → record them (issue/TODO with rationale); never silently drop a known
  issue.

## Repo docs are English-only
`AGENTS.md`, any `CLAUDE.md`, and `docs/**` contain NO Cyrillic — they are read by all agents.
