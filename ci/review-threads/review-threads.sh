#!/usr/bin/env bash
# Merge gate: FAIL if a PR has unresolved review threads.
#
# GitHub's "require conversation resolution" branch-protection toggle is the native way to
# do this, but it's admin-only and invisible from inside the repo. This script gives you
# the same gate as a portable CI check (and a pre-merge preflight in a ship script): it
# counts unresolved review threads via the GraphQL API and exits non-zero if any remain.
#
# Requires: gh (authenticated). Reads the PR number from $1 or $PR_NUMBER.
#
# Knobs (env):
#   PR_NUMBER   PR number (or pass as $1).
#   GH_REPO     owner/repo (default: gh's current repo; honored by gh automatically).
#
# Usage:
#   sh ci/review-threads/review-threads.sh 123
#   PR_NUMBER=123 sh ci/review-threads/review-threads.sh
set -euo pipefail

PR="${1:-${PR_NUMBER:-}}"
[ -n "$PR" ] || { echo "Usage: $0 <pr-number>  (or set PR_NUMBER)" >&2; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 2; }

# Paginate reviewThreads and count the unresolved ones. Using {owner}/{repo} placeholders
# lets gh fill the current repo; override with GH_REPO / --repo if needed.
QUERY='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$pr){
      reviewThreads(first:100,after:$endCursor){
        pageInfo{hasNextPage endCursor}
        nodes{isResolved isOutdated}
      }
    }
  }
}'

# Count unresolved threads (sum across pages). Outdated-but-unresolved still count by
# default — set IGNORE_OUTDATED=1 to skip threads GitHub marked outdated.
IGNORE_OUTDATED="${IGNORE_OUTDATED:-0}"
if [ "$IGNORE_OUTDATED" = "1" ]; then
  JQ='[.data.repository.pullRequest.reviewThreads.nodes[] | select((.isResolved|not) and (.isOutdated|not))] | length'
else
  JQ='[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved|not)] | length'
fi

# Fail-CLOSED on an API error: a silent 0 would let an unresolved PR merge. Check gh's
# exit status, not just the parsed count.
if RAW=$(gh api graphql --paginate \
     -F owner='{owner}' -F name='{repo}' -F pr="$PR" \
     -f query="$QUERY" --jq "$JQ" 2>/dev/null); then
  UNRESOLVED=$(printf '%s' "$RAW" | awk '{s+=$1} END{print s+0}')
else
  echo "::error::could not query review threads for PR #$PR (gh api failed) — failing closed." >&2
  echo "FAIL: review-thread query failed for PR #$PR." >&2
  exit 1
fi

if [ "${UNRESOLVED:-0}" != "0" ]; then
  echo "::error::PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve every thread before merging." >&2
  echo "FAIL: $UNRESOLVED unresolved review thread(s) on PR #$PR." >&2
  exit 1
fi
echo "PASS: PR #$PR has no unresolved review threads."
