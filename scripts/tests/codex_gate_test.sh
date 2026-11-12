#!/bin/bash
set -Eeuo pipefail

# Checks how scripts/codex-gate.sh classifies Codex's summary comment (codex-review check, #1836 review).
#
# The script is run with --query, which prints one state word and writes nothing, against a fake `curl`
# that serves canned GitHub responses, one scenario per marker shape. It asserts that an unfinished review
# of an older commit, or a summary with no code review started, is `stale` or `pending` (states the
# dependency-update policy can pass after its grace period) and not `running` (an active review of the
# head, which keeps the check pending): `running` for those shapes would leave an auto-merge waiting forever.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/scripts/codex-gate.sh"
HEAD=1111111111111111111111111111111111111111
OLD=2222222222222222222222222222222222222222

BIN="$(mktemp -d)"
trap 'rm -rf "$BIN"' EXIT
cat >"$BIN/curl" <<'FAKE'
#!/bin/bash
url="${*: -1}"
case "$url" in
  */pulls/7/comments*) echo '[]' ;;
  */pulls/7*) jq -cn --arg h "$FAKE_HEAD" '{head:{sha:$h},user:{login:"dependabot[bot]"},draft:false,labels:[]}' ;;
  */issues/7/comments*) printf '%s' "$FAKE_COMMENTS" ;;
  *) echo "unexpected request: $url" >&2; exit 22 ;;
esac
FAKE
chmod +x "$BIN/curl"

comment() { # marker-json -> the issue-comments array holding Codex's summary comment
  jq -cn --arg m "$1" '[{user:{login:"chatgpt-codex-connector[bot]"},
    body:("<!-- codex-pull-request-review-summary -->\nsummary\n<!-- codex-review-state:v1 " + $m + " -->")}]'
}
marker() { # code-status code-sha security-status security-sha; an empty argument is null
  jq -cn --arg a "$1" --arg b "$2" --arg c "$3" --arg d "$4" '
    def n($x): if $x == "" then null else $x end;
    {code_review: {status: n($a), commit_sha: n($b)}, security_review: {status: n($c), commit_sha: n($d)}}'
}

failed=0
expect() { # label expected-state comments-json
  local got
  got="$(FAKE_HEAD=$HEAD FAKE_COMMENTS="$3" PATH="$BIN:$PATH" GITHUB_TOKEN=x GITHUB_REPOSITORY=o/r "$SCRIPT" 7 --query 2>&1)"
  if [[ "$got" == "$2" ]]; then
    echo "ok   $1 -> $got"
  else
    echo "FAIL $1 -> '$got' (wanted $2)"
    failed=1
  fi
}

expect "no Codex comment"                                      pending   '[]'
expect "both reviews completed on the head"                    completed "$(comment "$(marker completed $HEAD completed $HEAD)")"
expect "head code review done, security review still running"  running   "$(comment "$(marker completed $HEAD running $HEAD)")"
expect "completed review of an older commit"                   stale     "$(comment "$(marker completed $OLD completed $OLD)")"
expect "unfinished review of an older commit"                  stale     "$(comment "$(marker running $OLD '' '')")"
expect "unfinished older review, security review done"         stale     "$(comment "$(marker running $OLD completed $OLD)")"
expect "unfinished review of the head"                         running   "$(comment "$(marker running $HEAD '' '')")"
expect "no code review started, only the security review done" pending   "$(comment "$(marker '' '' completed $HEAD)")"

exit "$failed"
