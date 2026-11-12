#!/usr/bin/env bash
# Turn on GitHub's auto-merge (squash) for an eligible pull request (ci-infrastructure runbook, "Codex review gate").
#
#   scripts/automerge.sh <pr-number> [--dry-run]
#
# Called by automerge.yml, which runs from the default branch. It only sets the flag: GitHub does the
# merge itself once every required check on `main`, codex-review included, has passed and every review
# thread is resolved. Nothing here bypasses those rules.
#
# This copy differs from gdrive-ownership-transfer's in one way: a pull request by a person is left alone
# (AGENTS.md: merge only on the owner's go-ahead). Auto-merge the owner turned on by hand stays on, unless the
# opt-out label `no-automerge` is set, which turns it off.
#
# Eligibility is decided here, not in the workflow, because this script runs trusted code:
#   - open, not a draft, from a branch of this repository (not a fork), and without the opt-out label
#     `no-automerge`;
#   - a pull request by dependabot[bot] additionally needs its head commit authored by dependabot[bot] (a
#     commit pushed on top by anyone else disqualifies it) and every `update-type:` in the head commit's
#     `updated-dependencies:` block (the metadata dependabot/fetch-metadata parses) to be semver-patch or
#     semver-minor. A major, or a dependency with no update type at all, is left for a human;
#   - a pull request by a person or by any other bot is not auto-merged.
#
# Exit status: 0 auto-merge enabled (or already on); 3 not eligible; anything else is an error. Prints
# `kind=dependabot` or `kind=human` on the first line of output so the workflow can choose its policy.
#
# Auto-merge is a GraphQL-only operation, so this uses curl against the GraphQL API with GITHUB_TOKEN
# (`contents: write` and `pull-requests: write`). If GitHub answers that the pull request is already in
# "clean status" (every requirement already met, so there is nothing to wait for), it merges directly
# through the REST API instead, with the same squash method, but only if the check `codex-review` has passed
# on the head commit (otherwise the branch may require no checks, and it exits 1 instead). Both merge requests
# are bound to the head commit that was checked. If the reply carries an error but the pull request
# shows auto-merge on, that counts as success. Needs GITHUB_REPOSITORY; GITHUB_API_URL (set by Actions) defaults
# to https://api.github.com.
set -euo pipefail
shopt -s inherit_errexit

pr="${1:-}"
mode="${2:-}"
if [[ ! "$pr" =~ ^[0-9]+$ ]] || [[ -n "$mode" && "$mode" != "--dry-run" ]]; then
  echo "usage: scripts/automerge.sh <pr-number> [--dry-run]" >&2
  exit 2
fi
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY (owner/name) is required}"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

optout_label="${OPTOUT_LABEL:-no-automerge}"
api="${GITHUB_API_URL:-https://api.github.com}"
hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
get() { curl -sS --fail-with-body "${hdr[@]}" "${api}/repos/${GITHUB_REPOSITORY}$1"; }

# --- eligibility -----------------------------------------------------------------------------------
pr_json="$(get "/pulls/${pr}")"
author="$(jq -r '.user.login' <<<"$pr_json")"
author_type="$(jq -r '.user.type' <<<"$pr_json")"
state="$(jq -r '.state' <<<"$pr_json")"
draft="$(jq -r '.draft' <<<"$pr_json")"
same_repo="$(jq -r '.head.repo.full_name == .base.repo.full_name' <<<"$pr_json")"
opted_out="$(jq -r --arg l "$optout_label" '[.labels[].name] | index($l) != null' <<<"$pr_json")"
head_sha="$(jq -r '.head.sha' <<<"$pr_json")"
node_id="$(jq -r '.node_id' <<<"$pr_json")"
already="$(jq -r '.auto_merge != null' <<<"$pr_json")"

if [[ "$author" == "dependabot[bot]" ]]; then kind=dependabot; elif [[ "$author_type" == "Bot" ]]; then kind=bot; else kind=human; fi
echo "kind=${kind}"

# A person's pull request merges on the owner's go-ahead, so leave its auto-merge as the owner set it. Only the
# opt-out label falls through, to the code below that turns an enabled auto-merge off.
if [[ "$kind" == "human" && "$opted_out" != "true" ]]; then
  echo "PR #${pr}: by ${author}; merging is left to the owner's go-ahead, so auto-merge is not touched."
  exit 3
fi

reason=""
if [[ "$draft" == "true" ]]; then reason="draft"
elif [[ "$same_repo" != "true" ]]; then reason="head is not a branch of this repository"
elif [[ "$opted_out" == "true" ]]; then reason="label ${optout_label} is set"
elif [[ "$kind" == "bot" ]]; then reason="author ${author} is a bot other than dependabot[bot]"
elif [[ "$kind" == "dependabot" ]]; then
  commit_json="$(get "/commits/${head_sha}")"
  commit_author="$(jq -r '.author.login // ""' <<<"$commit_json")"
  # One line per dependency, e.g. "version-update:semver-minor"; dependencies without a type count as unknown.
  mapfile -t types < <(jq -r '.commit.message' <<<"$commit_json" \
    | sed -n '/^updated-dependencies:/,/^\.\.\./p' | sed -n 's/^ *update-type: *//p')
  deps="$(jq -r '.commit.message' <<<"$commit_json" | sed -n '/^updated-dependencies:/,/^\.\.\./p' | grep -c '^- dependency-name:' || true)"
  if [[ "$commit_author" != "dependabot[bot]" ]]; then reason="head commit ${head_sha:0:7} is by ${commit_author:-unknown}, not dependabot[bot]"
  elif [[ "$deps" -eq 0 ]]; then reason="no updated-dependencies block in the head commit"
  elif [[ "${#types[@]}" -ne "$deps" ]]; then reason="${deps} dependencies but ${#types[@]} update types; treating the rest as unknown"
  else
    for t in "${types[@]}"; do
      case "$t" in
        version-update:semver-patch|version-update:semver-minor) ;;
        *) reason="update type ${t} is not patch or minor"; break ;;
      esac
    done
  fi
  [[ -z "$reason" ]] && echo "PR #${pr}: Dependabot update, ${deps} dependency update(s), all patch or minor, head ${head_sha:0:7} by dependabot[bot]."
fi

if [[ -n "$reason" ]]; then
  echo "PR #${pr}: not eligible for auto-merge: ${reason}."
  # Auto-merge enabled earlier stays on after a push by someone with write access (GitHub only switches it
  # off for pushes by people without it), so a pull request that has become ineligible must have it removed.
  if [[ "$state" == "open" && "$already" == "true" ]]; then
    if [[ "$mode" == "--dry-run" ]]; then echo "would disable the auto-merge enabled earlier."; exit 3; fi
    # shellcheck disable=SC2016  # $id is a GraphQL variable, not a shell one
    off_query='mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { pullRequest { number } } }'
    off_resp="$(curl -sS --fail-with-body "${hdr[@]}" -H "Content-Type: application/json" \
      -d "$(jq -n --arg q "$off_query" --arg id "$node_id" '{query: $q, variables: {id: $id}}')" "${api}/graphql")"
    off_errors="$(jq -r '[.errors[]?.message] | join(" | ")' <<<"$off_resp")"
    if [[ -n "$off_errors" ]]; then echo "PR #${pr}: could not disable the earlier auto-merge: ${off_errors}" >&2; exit 1; fi
    echo "PR #${pr}: disabled the auto-merge enabled earlier."
  fi
  exit 3
fi

if [[ "$state" != "open" ]]; then echo "PR #${pr} is ${state}; nothing to do."; exit 0; fi
if [[ "$already" == "true" ]]; then echo "PR #${pr}: auto-merge is already enabled."; exit 0; fi

# --- enable auto-merge -----------------------------------------------------------------------------
# shellcheck disable=SC2016  # $id is a GraphQL variable, not a shell one
query='mutation($id: ID!, $sha: GitObjectID!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH, expectedHeadOid: $sha}) {
    pullRequest { number autoMergeRequest { enabledAt } }
  }
}'
# Both merge requests below carry the head commit that was checked for eligibility, so a commit pushed
# after the check cannot be merged under it; GitHub refuses, and the push starts a new run.
body="$(jq -n --arg q "$query" --arg id "$node_id" --arg sha "$head_sha" '{query: $q, variables: {id: $id, sha: $sha}}')"

if [[ "$mode" == "--dry-run" ]]; then
  echo "would POST ${api}/graphql with:"; jq . <<<"$body"; exit 0
fi

resp="$(curl -sS --fail-with-body "${hdr[@]}" -H "Content-Type: application/json" -d "$body" "${api}/graphql")"
errors="$(jq -r '[.errors[]?.message] | join(" | ")' <<<"$resp")"

if [[ -z "$errors" ]]; then
  echo "PR #${pr}: auto-merge (squash) enabled at $(jq -r '.data.enablePullRequestAutoMerge.pullRequest.autoMergeRequest.enabledAt' <<<"$resp")."
  exit 0
fi

case "$errors" in
  *"clean status"*)
    # Every requirement is already satisfied, so auto-merge has nothing to wait for; merge now. But GitHub
    # also says "clean" when the branch requires no checks at all (protection removed or never applied), and
    # merging then would skip the review and the tests. So merge only if the base branch requires the Codex
    # review check, and every check it requires has passed on this head. Anything else: refuse, fail the run.
    review_check="${REVIEW_CHECK:-codex-review}"
    base_ref="$(jq -r '.base.ref' <<<"$pr_json")"
    # Required checks come from classic branch protection and from repository rulesets (this repository uses
    # rulesets), which the branch endpoint does not list.
    classic="$(get "/branches/${base_ref}" | jq -c '[.protection.required_status_checks.contexts // [] | .[]]')"
    from_rules="$(get "/rules/branches/${base_ref}" | jq -c '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]')"
    required="$(jq -cn --argjson a "$classic" --argjson b "$from_rules" '$a + $b | unique')"
    # GitHub counts a skipped or neutral conclusion as a pass for a required check (the Docker and security jobs
    # are skipped when a pull request does not touch their inputs); the Codex review check must be a success.
    unmet="$(get "/commits/${head_sha}/check-runs?per_page=100" | jq -r --argjson req "$required" --arg review "$review_check" --arg base "$base_ref" '
      . as $all
      | if ($req | index($review)) == null then "\($review) is not a required check of \($base)"
        else [$req[] | select(. as $c | any($all.check_runs[]; .name == $c and .status == "completed"
              and (.conclusion == "success" or ($c != $review and (.conclusion == "skipped" or .conclusion == "neutral")))) | not)]
          | if length == 0 then "" else "required check(s) not passed on the head: " + join(", ") end
        end')"
    if [[ -n "$unmet" ]]; then
      echo "PR #${pr}: GitHub reports it clean, but ${unmet}. Not merging directly; check the branch protection (docs)." >&2
      exit 1
    fi
    merge="$(curl -sS "${hdr[@]}" -H "Content-Type: application/json" -w '\n%{http_code}' \
      -X PUT -d "$(jq -n --arg sha "$head_sha" '{merge_method: "squash", sha: $sha}')" "${api}/repos/${GITHUB_REPOSITORY}/pulls/${pr}/merge")"
    code="${merge##*$'\n'}"
    if [[ "$code" == "200" ]]; then echo "PR #${pr}: already clean; merged directly (squash)."; exit 0; fi
    echo "PR #${pr}: already clean but the merge was refused (HTTP ${code}): $(head -n -1 <<<"$merge" | jq -r '.message // .')" >&2
    exit 0 ;;
  *"not allowed"*|*"not enabled"*)
    echo "PR #${pr}: ${errors}" >&2
    echo "Auto-merge is off for this repository. Operator step: gh api -X PATCH repos/${GITHUB_REPOSITORY} -F allow_auto_merge=true" >&2
    exit 1 ;;
  *)
    # GitHub can apply the mutation and still report an error for part of the reply (seen 2026-11-09 on
    # PR 83: "Resource not accessible by integration" while auto-merge was switched on). So ask the pull
    # request itself before calling this a failure.
    if [[ "$(get "/pulls/${pr}" 2>/dev/null | jq -r '.auto_merge != null' 2>/dev/null || true)" == "true" ]]; then
      echo "PR #${pr}: auto-merge (squash) is on, although GitHub's reply carried an error: ${errors}"
      exit 0
    fi
    now_head="$(get "/pulls/${pr}" 2>/dev/null | jq -r '.head.sha' 2>/dev/null || true)"
    if [[ -n "$now_head" && "$now_head" != "$head_sha" ]]; then
      echo "PR #${pr}: the head moved from ${head_sha:0:7} to ${now_head:0:7} during the check; the new push starts a new run that re-evaluates it."
      exit 0
    fi
    echo "PR #${pr}: enabling auto-merge failed: ${errors}" >&2
    exit 1 ;;
esac
