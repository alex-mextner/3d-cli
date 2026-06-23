#!/bin/bash
# =============================================================================
# install-dev-hooks.sh — wire this repo's TRACKED dev pre-commit gate into a
# fresh clone (or another machine). Idempotent: safe to run repeatedly.
#
# Installs the tracked repo-dev gate (scripts/hooks/pre-commit: ruff -> pytest ->
# mypy via `3d test`) into <common-git-dir>/hooks/pre-commit.
#
# Dispatcher coexistence: if the EXISTING hook already calls a global-git-hooks
# dispatcher inline (the agent-tools secret-scan line), that prefix is PRESERVED
# and the tracked dev body is spliced in below it. This matters when a repo sets a
# LOCAL `core.hooksPath = .git/hooks` (which bypasses the global composer, so the
# only way the dispatcher runs is that inline line). On a machine with NO local
# override but a global composer, the composer runs this repo-local hook as its
# first stage, so the pure dev body is all that's needed.
#
# This is the REPO-DEV gate, distinct from the `3d init` user-facing .scad
# template (assets/templates/pre-commit) — see scripts/hooks/pre-commit.
#
# Usage:  scripts/install-dev-hooks.sh
# =============================================================================
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

SRC="$ROOT/scripts/hooks/pre-commit"
if [ ! -f "$SRC" ]; then
    echo "[install-dev-hooks] tracked source missing: $SRC" >&2
    exit 1
fi

# Resolve the hooks dir via the git COMMON dir. git always runs hooks from the
# common dir's hooks/ (shared across all linked worktrees), so a linked worktree's
# per-worktree git dir (.git/worktrees/<name>) is the WRONG target — a hook placed
# there never fires. `--git-common-dir` returns the shared dir for a normal repo, a
# linked worktree, AND a submodule, and (unlike `--git-path hooks`) is NOT shadowed
# by a global core.hooksPath. This matches where the agent-tools global composer
# looks for the repo-local hook.
COMMON_DIR="$(git rev-parse --git-common-dir)"
# --git-common-dir may be relative to CWD; make it absolute against the repo root.
case "$COMMON_DIR" in
    /*) : ;;
    *) COMMON_DIR="$ROOT/$COMMON_DIR" ;;
esac
HOOKS_DIR="$COMMON_DIR/hooks"
DEST="$HOOKS_DIR/pre-commit"

mkdir -p "$HOOKS_DIR"

# If the existing hook calls a global-git-hooks dispatcher inline, extract that
# contiguous block (the marker comment through its `... || exit $?` guard). We
# splice it back in below so a repo that relies on the inline dispatcher (a LOCAL
# core.hooksPath=.git/hooks that bypasses the global composer) keeps its secret
# scan. Emits nothing if there's no marker OR the block doesn't terminate on a
# `... || exit $?` line — we splice ONLY a well-formed block, never a guessed tail.
extract_dispatcher_block() {
    [ -f "$DEST" ] || return 0
    grep -q "global-git-hooks-dispatcher" "$DEST" || return 0
    awk '
        /global-git-hooks-dispatcher/ { inblk = 1 }
        inblk { buf = buf $0 "\n" }
        inblk && /\|\| exit \$\?/ { printf "%s", buf; found = 1; exit }
        END { if (!found) exit 1 }   # marker but no terminator -> emit nothing
    ' "$DEST" || return 0
}

# Build the intended hook content into a temp file.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
DISPATCHER_BLOCK="$(extract_dispatcher_block)"
if [ -n "$DISPATCHER_BLOCK" ]; then
    # shebang from the tracked source, then the preserved dispatcher block, then
    # the tracked dev body (without its own shebang line).
    head -n 1 "$SRC" > "$TMP"
    printf '%s\n\n' "$DISPATCHER_BLOCK" >> "$TMP"
    tail -n +2 "$SRC" >> "$TMP"
else
    cp "$SRC" "$TMP"
fi

# Idempotent: only rewrite when the intended content differs from what's on disk.
if [ -f "$DEST" ] && cmp -s "$TMP" "$DEST"; then
    echo "[install-dev-hooks] $DEST already up to date."
else
    # Back up an existing, DIFFERENT hook so a hand-placed / third-party gate is
    # never silently discarded. Don't clobber a prior backup (it may hold the real
    # original from an earlier run).
    if [ -f "$DEST" ]; then
        if [ -e "$DEST.bak" ]; then
            echo "[install-dev-hooks] existing pre-commit differed; $DEST.bak already exists — left it untouched."
        else
            cp "$DEST" "$DEST.bak"
            echo "[install-dev-hooks] existing pre-commit differed — backed up to $DEST.bak"
        fi
    fi
    cp "$TMP" "$DEST"
    [ -n "$DISPATCHER_BLOCK" ] && echo "[install-dev-hooks] preserved the existing global-hooks-dispatcher prefix."
    echo "[install-dev-hooks] installed $DEST"
fi
# Always assert the exec bit — git won't run a non-executable hook, and the
# up-to-date branch above must still repair a cleared bit.
chmod +x "$DEST"

# Warn (don't fail) if a core.hooksPath is configured that ISN'T this repo's own
# hooks dir. git then runs hooks ONLY from that path; .git/hooks/pre-commit fires
# only if it points at a composing dispatcher that re-invokes the repo-local hook
# (the agent-tools pattern). A husky/lefthook/pre-commit-framework setup points it
# elsewhere and would silently skip this gate — say so instead of claiming success.
HOOKS_PATH="$(git config --get core.hooksPath || true)"
if [ -n "$HOOKS_PATH" ]; then
    case "$HOOKS_PATH" in
        ".git/hooks"|"$HOOKS_DIR") : ;;   # points at us — fine
        *)
            echo "[install-dev-hooks] WARNING: core.hooksPath=$HOOKS_PATH is set." >&2
            echo "  git runs hooks from there, so $DEST fires ONLY if that path is a" >&2
            echo "  composing dispatcher that re-invokes the repo-local hook. If you use" >&2
            echo "  husky/lefthook/pre-commit, wire '3d test' into that tool instead." >&2
            ;;
    esac
fi

echo "[install-dev-hooks] done. The repo-dev gate (ruff/pytest/mypy) runs on commit (unless core.hooksPath redirects it — see any warning above)."
