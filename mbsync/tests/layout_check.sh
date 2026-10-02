#!/bin/bash
set -Eeuo pipefail

# Maildir layout check (#275, #281), run against the shipped mbsync image's
# isync with synthetic Maildir stores; no IMAP server is contacted.
#
# The near side is mbsyncrc.template's own MaildirStore and Channel,
# unchanged. Only the far side is replaced: the template's IMAP store
# becomes a Maildir store at /far with SubFolders Legacy, which can hold
# every folder name used here as its own directory, standing in for
# Bridge. Each case delivers synthetic messages, syncs, syncs again, then
# delivers more and syncs once more, and checks that every run succeeds
# and every message lands exactly once, in its own folder.
#
# Checks:
#   1. Folders/A/B and Folders/A!B, whose names once flattened to one
#      sync state file (#275), each keep their own state at the box.
#   2. Child folders named cur, new and tmp, which SubFolders Verbatim
#      put in their parent's own message directories (#281), sync next
#      to their parent, an ordinary child and a sibling.
#   3. Other names Proton allows (dots, a leading dot, "!", spaces, a
#      modified UTF-7 name as Bridge sends it, INBOX below the top level,
#      deep nesting) each get their own directory.
#   4. Child names that would land on isync's own files in their parent's
#      directory (.mbsyncstate*, .uidvalidity, .isyncuidmap.db) are left
#      out by the channel's Patterns; their parent still syncs.
#
# Needs Docker. Builds the mbsync image unless MBSYNC_IMAGE names one.
# Run: bash mbsync/tests/layout_check.sh  (or make test-mbsync-layout)

MBSYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MBSYNC_DIR
readonly RUN_ID="mbsync-layout-check-$$"
IMAGE="${MBSYNC_IMAGE:-}"
WORK="$(mktemp -d)"
readonly WORK
FAILURES=0
SEQ=0
TAGS=()

cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() {
    printf 'FAIL %s\n' "$1"
    FAILURES=$((FAILURES + 1))
}

# The template with its IMAP account dropped and its far store replaced
# by a Maildir store. Everything else, the near store and the Channel
# included, is copied as it is.
render_config() {
    awk '
        /^IMAPAccount / { skip = 1 }
        /^IMAPStore protonmail-remote$/ {
            print "MaildirStore protonmail-remote"
            print "Path /far/"
            print "Inbox /far/INBOX"
            print "SubFolders Legacy"
            skip = 1
            next
        }
        skip && /^$/ { skip = 0 }
        !skip { print }
    ' "$MBSYNC_DIR/mbsyncrc.template" >"$WORK/mbsyncrc"
    chmod 644 "$WORK/mbsyncrc"
}

# legacy_path NAME: a folder's directory in a SubFolders Legacy store,
# relative to its Path: every component after the first gets a leading
# dot. The far store uses it, and so does the near store (#281).
legacy_path() {
    local name="$1" path rest component
    path="${name%%/*}"
    rest="${name#"$path"}"
    while [[ -n "$rest" ]]; do
        rest="${rest#/}"
        component="${rest%%/*}"
        rest="${rest#"$component"}"
        path="${path}/.${component}"
    done
    printf '%s' "$path"
}

far_path() {
    printf '%s/far/%s' "$WORK" "$(legacy_path "$1")"
}

# deliver NAME: put one synthetic message in a far folder and record
# NAME|TAG in TAGS. Runs in this shell, so the sequence number advances.
deliver() {
    local dir tag
    dir="$(far_path "$1")"
    mkdir -p "$dir/cur" "$dir/new" "$dir/tmp"
    SEQ=$((SEQ + 1))
    tag="layout-check-${SEQ}"
    printf 'From: sender@example.invalid\nTo: recipient@example.invalid\nSubject: synthetic %s\nMessage-ID: <%s@example.invalid>\n\nsynthetic body %s\n' \
        "$tag" "$tag" "$tag" >"$dir/cur/1700000000.${tag}.synthetic:2,S"
    TAGS+=("${1}|${tag}")
}

# sync_once LABEL: run isync over both stores; returns its status.
sync_once() {
    docker run --rm --read-only --user "$(id -u):$(id -g)" --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$WORK/mbsyncrc:/work/mbsyncrc:ro" -v "$WORK/far:/far" -v "$WORK/maildir:/maildir" \
        --entrypoint timeout "$IMAGE" 60 mbsync -c /work/mbsyncrc -a >"$WORK/sync-$1.log" 2>&1
}

# copies TAG: how many near-side message files hold a message.
copies() {
    grep -rlF --include='*' "Message-ID: <$1@example.invalid>" "$WORK/maildir" 2>/dev/null \
        | grep -cE '/(cur|new)/[^/]+$' || true
}

# located NAME TAG: the message is in the near folder's own cur or new.
located() {
    local dir
    dir="$WORK/maildir/$(legacy_path "$1")"
    grep -qlF "Message-ID: <$2@example.invalid>" "$dir"/cur/* "$dir"/new/* 2>/dev/null
}

# run_case LABEL NAME... : deliver to every folder, sync twice, deliver
# again, sync once more; every run must exit 0 and every message must
# arrive exactly once, in its folder's own near directory. Leaves the tags
# in TAGS (NAME|TAG lines).
run_case() {
    local label="$1" name tag run entry ok=1
    shift
    TAGS=()
    for name in "$@"; do
        deliver "$name"
    done
    for run in first second; do
        if ! sync_once "${label}-${run}"; then
            ok=0
            printf '     %s sync exited non-zero; log follows\n' "$run"
            sed 's/^/     /' "$WORK/sync-${label}-${run}.log"
        fi
    done
    for name in "$@"; do
        deliver "$name"
    done
    if ! sync_once "${label}-later"; then
        ok=0
        printf '     sync after later arrivals exited non-zero; log follows\n'
        sed 's/^/     /' "$WORK/sync-${label}-later.log"
    fi
    for entry in "${TAGS[@]}"; do
        tag="${entry#*|}"
        if [[ "$(copies "$tag")" != "1" ]]; then
            ok=0
            printf '     %s: %s copies of %s\n' "${entry%%|*}" "$(copies "$tag")" "$tag"
        elif ! located "${entry%%|*}" "$tag"; then
            ok=0
            printf '     %s: %s is not in its own folder\n' "${entry%%|*}" "$tag"
        fi
    done
    ((ok))
}

if [[ -z "$IMAGE" ]]; then
    IMAGE="$RUN_ID"
    docker build -q -t "$IMAGE" "$MBSYNC_DIR" >/dev/null
fi
render_config
mkdir -p "$WORK/far" "$WORK/maildir"
chmod 700 "$WORK/far" "$WORK/maildir"
deliver INBOX

# The near side is the template's: pull-only, never expunging.
if grep -qx 'Sync Pull' "$WORK/mbsyncrc" && grep -qx 'Expunge None' "$WORK/mbsyncrc" \
    && grep -qx 'Near :protonmail-local:' "$WORK/mbsyncrc"; then
    pass "the check runs the template's own near store and channel"
else
    fail "the check runs the template's own near store and channel"
fi

# 1. #275: two names that flatten to one state file name.
if run_case state "Folders/A/B" "Folders/A!B"; then
    pass "Folders/A/B and Folders/A!B sync independently across runs"
else
    fail "Folders/A/B and Folders/A!B sync independently across runs"
fi
if [[ -s "$WORK/maildir/INBOX/.mbsyncstate" ]] \
    && [[ -s "$WORK/maildir/Folders/.A/.B/.mbsyncstate" ]] \
    && [[ -s "$WORK/maildir/Folders/.A!B/.mbsyncstate" ]] \
    && [[ -z "$(find "$WORK/maildir" -maxdepth 1 -name '.mbsyncstate*' -print -quit)" ]]; then
    pass "each folder keeps its sync state in its own directory"
else
    fail "each folder keeps its sync state in its own directory"
    find "$WORK/maildir" -name '.mbsyncstate*' | sed "s|^$WORK|     |"
fi

# 2. #281: Maildir's own directory names as folder names.
if run_case reserved-dirs "Folders/Parent" "Folders/Parent/cur" "Folders/Parent/new" \
    "Folders/Parent/tmp" "Folders/Parent/Child" "Folders/Sibling"; then
    pass "children named cur, new and tmp sync next to their parent and siblings"
else
    fail "children named cur, new and tmp sync next to their parent and siblings"
fi

# 3. Other names.
if run_case names "Folders/a.b" "Folders/.dot" "Folders/x!y" "Folders/with space" \
    "Folders/Caf&AOk-" "Folders/INBOX" "Folders/Deep/Er/Est" "Folders/Deep/Er"; then
    pass "dots, a leading dot, !, spaces, UTF-7, INBOX and nesting each map to their own folder"
else
    fail "dots, a leading dot, !, spaces, UTF-7, INBOX and nesting each map to their own folder"
fi

# 4. Names of isync's own files are left out; the template lists each one,
# and its descendants.
missing=0
for reserved in uidvalidity isyncuidmap.db mbsyncstate mbsyncstate.journal mbsyncstate.new \
    mbsyncstate.lock; do
    for pattern in "!\"*/${reserved}\"" "!\"*/${reserved}/*\""; do
        if ! grep -E '^Patterns ' "$WORK/mbsyncrc" | tr ' ' '\n' | grep -qxF -- "$pattern"; then
            printf '     Patterns lacks %s\n' "$pattern"
            missing=1
        fi
    done
done
if ((missing == 0)); then
    pass "Patterns leaves out every child name isync uses for its own files"
else
    fail "Patterns leaves out every child name isync uses for its own files"
fi
TAGS=()
for name in "Folders/Kept" "Folders/Kept/mbsyncstate" "Folders/Kept/mbsyncstate.journal" \
    "Folders/Kept/mbsyncstate.new" "Folders/Kept/mbsyncstate.lock" \
    "Folders/Kept/isyncuidmap.db" "Folders/Kept/mbsyncstate/Below"; do
    deliver "$name"
done
ok=1
for run in first second; do
    if ! sync_once "excluded-${run}"; then
        ok=0
        sed 's/^/     /' "$WORK/sync-excluded-${run}.log"
    fi
done
for entry in "${TAGS[@]}"; do
    name="${entry%%|*}"
    tag="${entry#*|}"
    if [[ "$name" == "Folders/Kept" ]]; then
        expected=1
    else
        expected=0
    fi
    if [[ "$(copies "$tag")" != "$expected" ]]; then
        ok=0
        printf '     %s: %s copies of %s\n' "$name" "$(copies "$tag")" "$tag"
    fi
done
if ((ok)) && located "Folders/Kept" "${TAGS[0]#*|}" \
    && [[ -f "$WORK/maildir/Folders/.Kept/.mbsyncstate" ]]; then
    pass "folders named after isync's own files are skipped; their parent syncs"
else
    fail "folders named after isync's own files are skipped; their parent syncs"
fi

if ((FAILURES > 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all checks passed\n'
