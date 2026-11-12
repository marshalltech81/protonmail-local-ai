#!/usr/bin/env bash
# Report Codex's review of a pull request as the `codex-review` check run (ci-infrastructure runbook, "Codex review gate").
#
#   scripts/codex-gate.sh <pr-number> [--query | --proceed | --dry-run]
#
# Codex (chatgpt-codex-connector[bot]) does not post a GitHub check, so `main` cannot require it directly.
# This script reads the summary comment Codex leaves on every pull request it reviews, which carries a
# machine-readable marker (`<!-- codex-review-state:v1 {...} -->`), and writes a check run named
# `codex-review` on the pull request's head commit:
#
#   success      Codex completed its code review and its security review of the pull request's current head
#                commit, or the pull request carries the bypass label. Findings
#                themselves are not counted here: branch protection already requires every review thread to
#                be resolved before a merge.
#   in_progress  Codex has not finished reviewing the head commit: no marker yet, a review still running, or
#                a completed review of an earlier commit (Codex reviews every push in this repository, so
#                the check completes when the review of the new commit lands). A summary with no
#                `security_review` block yet counts as an unfinished security review, not a finished one.
#
# Modes:
#   (default)   evaluate and write the check run.
#   --query     print one word (completed | running | stale | pending | skipped | draft) and write nothing.
#   --proceed   like the default, but a pull request whose head Codex has not reviewed gets `success` with a
#               title that says so. Used by automerge.yml after its grace period (dependency-update policy).
#               If EXPECT_HEAD_SHA is set and the pull request's head is a different commit, nothing is
#               written: the grace period was granted to the commit that was eligible then, not to a later
#               one that someone pushed while it ran.
#   --dry-run   evaluate and print the check run payload without writing it.
#
# Needs GITHUB_TOKEN with `checks: write` (plus read on pull requests and issues) and GITHUB_REPOSITORY.
# Uses only curl and jq, so it runs on any runner.
set -euo pipefail
shopt -s inherit_errexit # a failed API call inside $(...) must stop the script, not read as an empty result

pr="${1:-}"
mode="${2:-}"
if [[ ! "$pr" =~ ^[0-9]+$ ]] || [[ -n "$mode" && ! "$mode" =~ ^--(query|proceed|dry-run)$ ]]; then
  echo "usage: scripts/codex-gate.sh <pr-number> [--query | --proceed | --dry-run]" >&2
  exit 2
fi
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY (owner/name) is required}"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

codex_bot="${CODEX_BOT:-chatgpt-codex-connector[bot]}"
check_name="${CHECK_NAME:-codex-review}"
skip_label="${SKIP_LABEL:-codex-gate:skip}"
actions_app_id=15368 # GitHub Actions; check runs written with GITHUB_TOKEN belong to it
api="https://api.github.com/repos/${GITHUB_REPOSITORY}"

gh_api() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  local -a args=(-sS -X "$method" -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "${api}${path}")
  if [[ -n "$body" ]]; then args+=(-H "Content-Type: application/json" -d "$body"); fi
  curl --fail-with-body "${args[@]}"
}
gh_api_all() { # GET every page of an endpoint that returns a JSON array; any failed page fails the script
  local path="$1" sep='?' page=1 all='[]' chunk
  [[ "$path" == *\?* ]] && sep='&'
  while :; do
    chunk="$(gh_api GET "${path}${sep}per_page=100&page=${page}")" || return $?
    all="$(jq -c --argjson a "$all" '$a + .' <<<"$chunk")" || return $?
    [[ "$(jq 'length' <<<"$chunk")" -lt 100 ]] && break
    page=$((page + 1))
  done
  printf '%s' "$all"
}

# --- pull request ----------------------------------------------------------------------------------
pr_json="$(gh_api GET "/pulls/${pr}")"
head_sha="$(jq -r '.head.sha' <<<"$pr_json")"
author="$(jq -r '.user.login' <<<"$pr_json")"
draft="$(jq -r '.draft' <<<"$pr_json")"
has_skip_label="$(jq -r --arg l "$skip_label" '[.labels[].name] | index($l) != null' <<<"$pr_json")"

if [[ "$mode" == "--proceed" && -n "${EXPECT_HEAD_SHA:-}" && "$EXPECT_HEAD_SHA" != "$head_sha" ]]; then
  echo "PR #${pr}: the head is ${head_sha:0:7}, not ${EXPECT_HEAD_SHA:0:7}, which the grace period was granted to; not applying the policy."
  exit 0
fi

# --- Codex's summary comment -----------------------------------------------------------------------
# One issue comment per pull request, edited in place as the review progresses. The marker is JSON.
# The fetch is separate from the parse so that an API failure stops the script (and so cannot read as
# "no review yet"), while a pull request without the comment simply yields an empty marker.
comments="$(gh_api_all "/issues/${pr}/comments")"
marker="$(jq -r --arg bot "$codex_bot" '
      [.[] | select(.user.login == $bot and (.body | contains("codex-pull-request-review-summary")))]
      | last // empty | .body
      | capture("codex-review-state:v1 (?<json>\\{.*?\\}) -->") | .json' <<<"$comments")"
review_status=""; reviewed_sha=""; has_findings=""; sec_status=""; sec_sha=""
if [[ -n "$marker" ]]; then
  review_status="$(jq -r '.code_review.status // ""' <<<"$marker")"
  reviewed_sha="$(jq -r '.code_review.commit_sha // ""' <<<"$marker")"
  has_findings="$(jq -r '.code_review.has_any_findings // ""' <<<"$marker")"
  sec_status="$(jq -r '.security_review.status // ""' <<<"$marker")"
  sec_sha="$(jq -r '.security_review.commit_sha // ""' <<<"$marker")"
fi
# Inline comments Codex left (for the summary only; threads are enforced by branch protection).
findings="$(gh_api_all "/pulls/${pr}/comments" \
  | jq -r --arg bot "$codex_bot" '[.[] | select(.user.login == $bot)] | length')"

# --- decision --------------------------------------------------------------------------------------
waiting_on_security=false
if [[ "$draft" == "true" ]]; then
  state=draft
elif [[ "$has_skip_label" == "true" ]]; then
  state=skipped
elif [[ "$review_status" == "completed" && "$reviewed_sha" == "$head_sha" ]]; then
  state=completed
  # The security review runs beside the code review and finishes on its own schedule, and it is required: a
  # summary with no security block yet, or one still running or of an earlier commit, keeps the check pending.
  # If Codex never produces one, comment `@codex review` or use the bypass label.
  if [[ "$sec_status" != "completed" || "$sec_sha" != "$head_sha" ]]; then
    state=running; waiting_on_security=true
  fi
elif [[ "$review_status" == "completed" ]]; then
  state=stale # reviewed an earlier commit; the review of the head has not started yet
elif [[ -n "$marker" ]]; then
  state=running
else
  state=pending
fi

if [[ "$mode" == "--query" ]]; then echo "$state"; exit 0; fi
if [[ "$state" == "draft" ]]; then
  echo "PR #${pr} is a draft; Codex reviews when it is marked ready. Nothing written."; exit 0
fi

short() { printf '%s' "${1:0:7}"; }
rerun="To re-run this gate: gh workflow run codex-gate.yml -f pr=${pr}."
case "$state" in
  skipped)
    status=completed; conclusion=success
    title="Skipped by label ${skip_label}"
    summary="An administrator bypassed the Codex review gate for this pull request. Remove the label to re-evaluate." ;;
  completed)
    status=completed; conclusion=success
    if [[ "$has_findings" == "true" ]]; then title="Codex review complete: ${findings} finding(s)"; else title="Codex review complete: no findings"; fi
    summary="Codex reviewed the head commit $(short "$head_sha"), code and security. Review threads must still be resolved before merge (branch protection)." ;;
  running)
    status=in_progress; conclusion=""
    if [[ "$waiting_on_security" == "true" ]]; then
      title="Codex security review in progress"
      summary="Codex finished its code review of $(short "$head_sha"); its security review (${sec_status:-not started}${sec_sha:+, commit $(short "$sec_sha")}) has not finished. This check updates when it does. If it never does, comment \`@codex review\`, or an administrator can add the label \`${skip_label}\`. ${rerun}"
    else
      title="Codex review in progress"
      summary="Codex has started on $(short "$reviewed_sha") and not finished. This check updates when it does. ${rerun}"
    fi ;;
  stale|pending)
    if [[ "$mode" == "--proceed" ]]; then
      status=completed; conclusion=success
      title="Codex did not review; proceeding (dependency-update policy)"
      summary="No Codex review of $(short "$head_sha") arrived within the grace period for this dependency update, so the gate is not holding it. The CI checks remain required. See the runbook to change this policy."
    elif [[ "$state" == "stale" ]]; then
      status=in_progress; conclusion=""
      title="Codex review of $(short "$head_sha") pending"
      summary="Codex reviewed $(short "$reviewed_sha"); the head is now $(short "$head_sha"). Codex reviews every push, so this check completes when that review lands. If it does not, comment \`@codex review\`. ${rerun}"
    else
      status=in_progress; conclusion=""
      title="Waiting for Codex review"
      summary="No Codex review yet for pull request #${pr}. Codex reviews automatically when a pull request is opened, marked ready, or pushed to. If it does not arrive, comment \`@codex review\`, or an administrator can add the label \`${skip_label}\`. ${rerun}"
    fi ;;
esac

text="$(printf 'author: %s\nhead: %s\ncodex status: %s\nreviewed commit: %s\nsecurity review: %s (%s)\ninline comments by Codex: %s\nmode: %s\nevaluated: %s' \
  "$author" "$head_sha" "${review_status:-none}" "${reviewed_sha:-none}" "${sec_status:-none}" "${sec_sha:+$(short "$sec_sha")}" "$findings" "${mode:-default}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"

payload="$(jq -n --arg name "$check_name" --arg sha "$head_sha" --arg status "$status" --arg conclusion "$conclusion" \
  --arg title "$title" --arg summary "$summary" --arg text "$text" '
  {name: $name, head_sha: $sha, status: $status, output: {title: $title, summary: $summary, text: $text}}
  + (if $conclusion != "" then {conclusion: $conclusion} else {} end)')"

if [[ "$mode" == "--dry-run" ]]; then
  echo "state: ${state}"; jq . <<<"$payload"; exit 0
fi

# --- write the check run (update the existing one for this commit if there is one) ---------------------
existing_run="$(gh_api GET "/commits/${head_sha}/check-runs?check_name=${check_name}&per_page=100" \
  | jq -c --argjson app "$actions_app_id" '[.check_runs[] | select(.app.id == $app)] | sort_by(.id) | last // {}')"
existing="$(jq -r '.id // empty' <<<"$existing_run")"
# Once the dependency-update policy has let a pull request through, a later re-evaluation of the same
# commit (a label change, a manual run) must not put it back to "waiting": the grace period has already run.
if [[ ( "$state" == "pending" || "$state" == "stale" ) && "$mode" != "--proceed" \
      && "$(jq -r '.conclusion // ""' <<<"$existing_run")" == "success" \
      && "$(jq -r '.output.title // ""' <<<"$existing_run")" == "Codex did not review; proceeding"* ]]; then
  echo "PR #${pr} ${head_sha:0:7}: ${check_name} already passed under the dependency-update policy; leaving it."
  exit 0
fi
# GitHub will not move a completed check run back to "in progress" through PATCH (the output changes, the
# status and conclusion stay), and branch protection evaluates the newest run of a name on a commit. So an
# unfinished run is updated in place, a finished one is left alone when nothing changed, and otherwise a new
# run is created.
existing_status="$(jq -r '.status // ""' <<<"$existing_run")"
existing_conclusion="$(jq -r '.conclusion // ""' <<<"$existing_run")"
existing_title="$(jq -r '.output.title // ""' <<<"$existing_run")"
if [[ -n "$existing" && "$existing_status" != "completed" ]]; then
  gh_api PATCH "/check-runs/${existing}" "$(jq 'del(.name, .head_sha)' <<<"$payload")" >/dev/null
  echo "PR #${pr} ${head_sha:0:7}: ${check_name} -> ${status}${conclusion:+/$conclusion} (updated ${existing}): ${title}"
elif [[ -n "$existing" && "$existing_status" == "$status" && "$existing_conclusion" == "$conclusion" && "$existing_title" == "$title" ]]; then
  echo "PR #${pr} ${head_sha:0:7}: ${check_name} already ${status}${conclusion:+/$conclusion} (${existing}): ${title}"
else
  id="$(gh_api POST "/check-runs" "$payload" | jq -r '.id')"
  echo "PR #${pr} ${head_sha:0:7}: ${check_name} -> ${status}${conclusion:+/$conclusion} (created ${id}): ${title}"
fi
