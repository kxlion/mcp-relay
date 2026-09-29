#!/usr/bin/env bash
# Fail unless the CI aggregate gate (ci-required) succeeded on a commit. The
# release workflows publish only commits that passed CI. Needs GH_TOKEN with
# checks: read and GITHUB_REPOSITORY.
#
# Usage: require-ci.sh <commit-sha>

set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: %s <commit-sha>\n' "$0" >&2
    exit 2
fi
sha="$1"

# The most recently started ci-required run for the commit decides.
conclusion="$(gh api "repos/$GITHUB_REPOSITORY/commits/$sha/check-runs?check_name=ci-required" \
    --jq '[.check_runs[] | select(.app.slug == "github-actions")] | sort_by(.started_at) | last | .conclusion // "missing"')"
if [[ "$conclusion" != success ]]; then
    printf '::error::ci-required on %s is %s, expected success\n' "$sha" "$conclusion"
    exit 1
fi
printf 'ci-required succeeded on %s.\n' "$sha"
