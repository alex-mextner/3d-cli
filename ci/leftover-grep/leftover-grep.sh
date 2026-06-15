#!/usr/bin/env bash
# Leftover-marker gate: fail if the CODE introduces debugging/forbidden leftovers.
#
# Catches the classic "oops, left it in" mistakes before they merge:
#   • focused tests        — .only(  / fdescribe / fit(   (silently skip the rest of a suite)
#   • debugger statements  — `debugger;`
#   • stray console logs    — console.log/debug (configurable; warn vs block)
#   • untracked TODOs       — TODO/FIXME WITHOUT an issue reference (TODO(ABC-123) is ok)
#   • merge conflict markers — <<<<<<< / ======= / >>>>>>>
#
# By default it scans only the lines ADDED in the PR diff (so it doesn't punish you for
# pre-existing debt), falling back to a full-tree scan when no base ref is available.
#
# Knobs (env):
#   LEFTOVER_BASE        diff base. Default origin/main -> main -> full-tree scan.
#   LEFTOVER_INCLUDE     ERE of file paths to scan. Default: source-ish extensions.
#   LEFTOVER_EXCLUDE     ERE of paths to skip. Default: vendored/build/lock dirs.
#   TICKET_REGEX         what makes a TODO "tracked". Default: TODO/FIXME followed by
#                        (ABC-123) or (#123) or a URL. Customize for your tracker.
#   ALLOW_CONSOLE        "1" = console.log is a WARNING, not a failure (default: block).
#   LEFTOVER_FULLTREE    "1" = always scan the whole tree, ignore the diff.
#   LEFTOVER_HEAD        head ref/SHA to diff against the base. Default HEAD. Under a
#                        tamper-resistant pull_request_target setup this is the PR head SHA,
#                        fetched as DATA — `git diff` + grep only READ those lines, they
#                        never execute PR code — so the trusted base script still gates.
#
# Usage: sh ci/leftover-grep/leftover-grep.sh
set -euo pipefail

LEFTOVER_BASE="${LEFTOVER_BASE:-origin/main}"
LEFTOVER_HEAD="${LEFTOVER_HEAD:-HEAD}"
LEFTOVER_INCLUDE="${LEFTOVER_INCLUDE:-\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|rb|java|kt|c|h|cpp|hpp|cs|php|swift|sh)$}"
LEFTOVER_EXCLUDE="${LEFTOVER_EXCLUDE:-(^|/)(node_modules|dist|build|out|vendor|\.git|coverage|__snapshots__)/|\.min\.(js|css)$|lock$}"
TICKET_REGEX="${TICKET_REGEX:-[A-Z]+-[0-9]+|#[0-9]+|https?://}"
ALLOW_CONSOLE="${ALLOW_CONSOLE:-0}"
LEFTOVER_FULLTREE="${LEFTOVER_FULLTREE:-0}"

# Resolve a base ref or empty (-> full-tree scan).
base=""
if [ "$LEFTOVER_FULLTREE" != "1" ]; then
  if git rev-parse --verify --quiet "$LEFTOVER_BASE" >/dev/null 2>&1; then base="$LEFTOVER_BASE"
  elif git rev-parse --verify --quiet main >/dev/null 2>&1; then base="main"; fi
fi

# Collect (file, lineno, line) tuples for ADDED lines (diff) or all lines (full tree).
# Output format: <file>\t<lineno>\t<text>
emit_lines() {
  if [ -n "$base" ]; then
    # Parse `git diff` unified output, tracking the new-file line number, emitting only '+'
    # lines (added). Robust enough for a gate without extra deps.
    git diff --no-color --unified=0 "$base...$LEFTOVER_HEAD" -- . \
      | awk '
        /^\+\+\+ /      { f=$2; sub(/^b\//,"",f); next }
        /^@@ /          { match($0, /\+[0-9]+/); ln=substr($0,RSTART+1,RLENGTH-1)+0; next }
        /^\+/ && f!=""  { t=substr($0,2); printf "%s\t%d\t%s\n", f, ln, t; ln++; next }
      '
  else
    # Full-tree scan of tracked files.
    git ls-files | while IFS= read -r f; do
      [ -f "$f" ] || continue
      grep -nH '' "$f" 2>/dev/null | sed 's/:/\t/; s/:/\t/' || true
    done
  fi
}

violations=0
warnings=0
report() { # <severity> <file> <lineno> <rule> <text>
  if [ "$1" = "WARN" ]; then warnings=$((warnings+1)); echo "  warn  [$4] $2:$3  $5" >&2
  else violations=$((violations+1)); echo "::error file=$2,line=$3::[$4] $5"; echo "  BLOCK [$4] $2:$3  $5" >&2; fi
}

if [ -n "$base" ]; then echo "[leftover] scanning diff vs ${base} ..." >&2; else echo "[leftover] scanning full tree ..." >&2; fi

while IFS=$'\t' read -r file ln text; do
  [ -n "${file:-}" ] || continue
  printf '%s' "$file" | grep -qE "$LEFTOVER_INCLUDE" || continue
  printf '%s' "$file" | grep -qE "$LEFTOVER_EXCLUDE" && continue

  # focused tests
  printf '%s' "$text" | grep -qE '\.only\(|(^|[^a-zA-Z])f(describe|it|test)\(' && report BLOCK "$file" "$ln" "focused-test" "$text"
  # debugger
  printf '%s' "$text" | grep -qE '(^|[^a-zA-Z])debugger;?\s*$' && report BLOCK "$file" "$ln" "debugger" "$text"
  # merge conflict markers
  printf '%s' "$text" | grep -qE '^(<{7}|={7}|>{7})( |$)' && report BLOCK "$file" "$ln" "merge-marker" "$text"
  # console.log/debug
  if printf '%s' "$text" | grep -qE 'console\.(log|debug)\('; then
    [ "$ALLOW_CONSOLE" = "1" ] && report WARN "$file" "$ln" "console" "$text" || report BLOCK "$file" "$ln" "console" "$text"
  fi
  # TODO/FIXME without a tracker reference
  if printf '%s' "$text" | grep -qE '(TODO|FIXME)'; then
    printf '%s' "$text" | grep -qE "($TICKET_REGEX)" || report BLOCK "$file" "$ln" "untracked-todo" "$text"
  fi
done < <(emit_lines)

echo "[leftover] $violations blocking, $warnings warning(s)." >&2
[ "$violations" = "0" ] || { echo "[leftover] FAIL — remove the leftovers above (or reference a ticket on the TODO)." >&2; exit 1; }
echo "[leftover] PASS."
