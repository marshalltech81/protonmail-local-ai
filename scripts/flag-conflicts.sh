#!/usr/bin/env bash
# Label open pull requests that conflict with their base branch (ci-infrastructure runbook, "Pull requests with merge conflicts").
#
#   scripts/flag-conflicts.sh [--dry-run]
#
# GitHub does not start a pull request's workflows while it has merge conflicts, so the required checks never
# appear, `codex-review` stays pending and auto-merge waits for ever, with only a small notice on the pull
# request to show for it. This script makes that visible: for every open pull request it asks GitHub whether
# the branch merges cleanly, adds the label `has-conflicts` (and one comment, when it adds the label) to a
# pull request that does not, and removes the label from one that now does. It resolves nothing itself.
#
# It runs (conflicts.yml) when the default branch moves, when the checks start on a pull request (which clears
# the label once a conflict is resolved) and every few hours (a conflict a pull request update introduces
# starts no checks), or by hand. GitHub works out mergeability in the background, so a pull request whose answer is still pending is asked
# again a few times and otherwise left for the next run.
#
# Needs GITHUB_TOKEN (issues: write, pull-requests: write) and GITHUB_REPOSITORY; GITHUB_API_URL (set by
# Actions) defaults to https://api.github.com. Uses only curl and jq. Environment: CONFLICT_LABEL (default
# has-conflicts), MERGEABLE_TRIES (default 5) and MERGEABLE_WAIT seconds between tries (default 4).
set -euo pipefail
shopt -s inherit_errexit

mode="${1:-}"
if [[ -n "$mode" && "$mode" != "--dry-run" ]]; then echo "usage: scripts/flag-conflicts.sh [--dry-run]" >&2; exit 2; fi
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY (owner/name) is required}"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

label="${CONFLICT_LABEL:-has-conflicts}"
tries="${MERGEABLE_TRIES:-5}"
wait_s="${MERGEABLE_WAIT:-4}"
api="${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}"
hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
get() { curl -sS --fail-with-body "${hdr[@]}" "${api}$1"; }
send() { # method path json-body
  curl -sS --fail-with-body "${hdr[@]}" -H "Content-Type: application/json" -X "$1" -d "$3" "${api}$2" >/dev/null
}

# Every open pull request number, over all pages.
numbers=""; page=1
while :; do
  chunk="$(get "/pulls?state=open&per_page=100&page=${page}")"
  numbers+="$(jq -r '.[].number' <<<"$chunk")"$'\n'
  [[ "$(jq 'length' <<<"$chunk")" -lt 100 ]] && break
  page=$((page + 1))
done

flagged=0; cleared=0; pending=0
for pr in $numbers; do
  for ((attempt = 1; attempt <= tries; attempt++)); do
    pr_json="$(get "/pulls/${pr}")"
    [[ "$(jq -r '.mergeable' <<<"$pr_json")" != "null" ]] && break
    [[ "$attempt" -lt "$tries" ]] && sleep "$wait_s"
  done
  mergeable="$(jq -r '.mergeable' <<<"$pr_json")"
  base="$(jq -r '.base.ref' <<<"$pr_json")"
  has_label="$(jq -r --arg l "$label" '[.labels[].name] | index($l) != null' <<<"$pr_json")"

  if [[ "$mergeable" == "null" ]]; then
    echo "PR #${pr}: GitHub has not worked out mergeability yet; left for the next run."; pending=$((pending + 1))
  elif [[ "$mergeable" == "false" && "$has_label" == "false" ]]; then
    echo "PR #${pr}: conflicts with ${base}; adding the label ${label} and a comment."
    flagged=$((flagged + 1))
    [[ "$mode" == "--dry-run" ]] && continue
    # Create the label once if the repository does not have it yet (422 when it already exists).
    send POST /labels "$(jq -n --arg n "$label" '{name: $n, color: "D93F0B", description: "Merge conflicts with the base branch"}')" 2>/dev/null || true
    send POST "/issues/${pr}/labels" "$(jq -n --arg n "$label" '{labels: [$n]}')"
    send POST "/issues/${pr}/comments" "$(jq -n --arg b "This pull request has merge conflicts with \`${base}\`. GitHub does not start its checks while it conflicts, so the required checks never appear and auto-merge waits. Update the branch (the button on this page works when it is only behind), or merge \`${base}\` into it and resolve the conflicts. The label \`${label}\` goes away by itself once it merges cleanly. Added by \`scripts/flag-conflicts.sh\`." '{body: $b}')"
  elif [[ "$mergeable" == "true" && "$has_label" == "true" ]]; then
    echo "PR #${pr}: merges cleanly with ${base} again; removing the label ${label}."
    cleared=$((cleared + 1))
    [[ "$mode" == "--dry-run" ]] && continue
    # The label may be removed by hand between the read and this call; that is the outcome we wanted.
    curl -sS "${hdr[@]}" -X DELETE "${api}/issues/${pr}/labels/${label}" >/dev/null || true
  fi
done
echo "Checked $(grep -c . <<<"$numbers" || true) open pull request(s): ${flagged} flagged, ${cleared} cleared, ${pending} pending."
