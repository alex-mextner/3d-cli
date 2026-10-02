# Warning-only outgoing-push anti-slop

## Scope

Add one bounded pre-push advisory stage for this Python repository. It runs from the repo's existing pre-push path, preserves the global dispatcher and all blocking security/test gates, and returns zero for its own findings or analyzer failures.

The stage reads the ref-update records supplied by Git and analyzes only file blobs reachable from the outgoing **local object**. It never uses the index or working tree as the source of truth. For a new remote ref it compares against the local remote-tracking default branch when available; without one it uses the new commit's first parent (or the empty tree for a root commit) and warns rather than scanning the whole tree. Ref deletions and non-commit objects are skipped with an explicit warning. Rename destinations are analyzed; deleted paths have no outgoing blob to inspect. NUL-delimited Git output is used so spaces and other legal path characters remain intact. Multiple ref updates are deduplicated without broad repository scans.

## Analyzer policy

The exact installed reusable anti-slop source was inspected before implementation:

- live `rig`: `/Users/ultra/.local/bin/rig` -> `/Users/ultra/xp/rig-cli/bin/rig`;
- live Rig version/source: `0.56.1`, checkout `bcae5fe0a6f4dfac8adc0f7d605da5baa65062b`;
- agent-tools checkout: `/Users/ultra/.local/share/agent-tools/release-bootstrap-791-ship-gpu-ffd7599-cc831-a5c0139-825`, source checkout `0794222c91b929d8f2eb88f9836f67d66978b609`;
- anti-slop submodule gitlink: `e00cea0` (`alex-mextner/anti-slop`), package version `0.1.0`.

That reusable anti-slop implementation is an Oxlint TypeScript/JavaScript plugin and ships no outgoing-push fragment or Python carrier. The policy is explicit:

- `.js`/`.ts` outgoing blobs are eligible only for a locally provisioned Oxlint anti-slop tool/plugin. This worktree has neither `oxlint` nor `tools/oxlint/anti-slop`, so the hook emits an explicit advisory-unavailable warning rather than pretending those blobs were scanned.
- Python coverage comes from the hook's bounded Ruff/stdlib checks for this push and the existing CI `ci/leftover-grep/leftover-grep.sh` rule set for added-line leftover markers. The hook does not claim that CI's full rule set ran locally.

The bounded adapter does not download rules or invoke a model from a push hook. Missing Ruff, missing Oxlint/plugin, malformed blobs, and analyzer exceptions produce visible `WARN anti-slop` diagnostics and do not block the push.
Diagnostics identify the repository-relative file, rule, line when available, and a next action. Findings are filtered to added/modified lines in the outgoing diff so historical occurrences in an otherwise changed file do not become noise. The installed shim also requires the existing global dispatcher to prove it ran by touching `GLOBAL_HOOKS_DISPATCH_MARKER`; a missing/unlaunchable dispatcher or failed preserved gate remains blocking, while anti-slop findings and analyzer failures remain advisory.

The shim's recursion guard assumes the global dispatcher executes its global gates without recursively invoking this repository shim. A nested invocation fails closed with status 125 rather than bypassing the dispatcher or security gates.

## Acceptance

- Real pushes use outgoing committed blobs, not staged or unstaged edits.
- Existing updates, new branches, ref deletions, multi-ref pushes, renames/deletions, filenames with spaces, non-HEAD refs, and non-commit objects are safe and explicit.
- Only relevant changed files are materialized/analyzed; no whole-tree scan or network/model call occurs.
- Anti-slop findings and analyzer failures are warnings with exit zero; other hook failures retain their original non-zero status.
- The supported setup command installs the tracked Python pre-push shim into the current worktree's Git hooks directory and, when distinct, the common Git hooks directory without replacing either dispatcher chain. Run it once from each linked worktree. `rig apply` remains the supported machine dispatcher installation/configuration command.

## Supported setup

From the checkout that should run the advisory stage:

```text
python scripts/install_pre_push_hook.py
```

On a machine whose global dispatcher is not yet provisioned, reconcile that
dispatcher separately with the existing Rig command:

```text
rig apply commit --only git_hooks --yes
```

The Rig command does not copy this repository's analyzer; the Python installer
does. Neither command was run against the production/global hook state during
this implementation.
