#!/bin/bash
set -Eeuo pipefail

# Maildir layout check (#275, #281) and UIDVALIDITY recovery check (#279),
# run against the shipped mbsync image's isync with synthetic Maildir
# stores; no IMAP server is contacted.
#
# The near side is mbsyncrc.template's own MaildirStore and Channel,
# unchanged. Only the far side is replaced: the template's IMAP store
# becomes a Maildir store at /far with SubFolders Legacy, which can hold
# every folder name used here as its own directory, standing in for
# Bridge. Each layout case (1-4) delivers synthetic messages, syncs,
# syncs again, then delivers more and syncs once more, and checks that
# every run succeeds and every message lands exactly once, in its own
# folder.
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
#   5. A spurious UIDVALIDITY change (new UIDVALIDITY, the same message at
#      every UID) is recovered by isync itself, with local state in place:
#      nothing is downloaded twice and no local file changes.
#   6. A genuine change (new UIDVALIDITY, an old UID now holding another
#      message) fails every sync and changes nothing locally. The
#      documented recovery (docs/troubleshooting.md, "mbsync reports a
#      UIDVALIDITY change") keeps the old Maildir whole and starts a
#      fresh one, which then holds each message once. The alternative of moving only the sync state aside is checked to
#      download every message again next to its old copy, with different
#      bytes, and to leave the old copy out of later deletions: why the
#      procedure does not use it.
#
# Needs Docker. Builds the mbsync image unless MBSYNC_IMAGE names one.
# Run: bash mbsync/tests/layout_check.sh  (or make test-mbsync-layout)

MBSYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MBSYNC_DIR
readonly RUN_ID="mbsync-layout-check-$$"
IMAGE="${MBSYNC_IMAGE:-}"
WORK="$(mktemp -d)"
readonly WORK
# The far and near stores the helpers below use; check 5 and 6 point
# them at stores of their own.
FAR="$WORK/far"
NEAR="$WORK/maildir"
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
    printf '%s/%s' "$FAR" "$(legacy_path "$1")"
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
        -v "$WORK/mbsyncrc:/work/mbsyncrc:ro" -v "$FAR:/far" -v "$NEAR:/maildir" \
        --entrypoint timeout "$IMAGE" 60 mbsync -c /work/mbsyncrc -a >"$WORK/sync-$1.log" 2>&1
}

# copies TAG: how many near-side message files hold a message.
copies() {
    grep -rlF --include='*' "Message-ID: <$1@example.invalid>" "$NEAR" 2>/dev/null \
        | grep -cE '/(cur|new)/[^/]+$' || true
}

# located NAME TAG: the message is in the near folder's own cur or new.
located() {
    local dir
    dir="$NEAR/$(legacy_path "$1")"
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

# 5 and 6: UIDVALIDITY changes (#279). Each case has stores of its own
# with populated local state: synced INBOX mail, a message deleted on the
# far side whose local copy is kept with the T flag (Expunge None), and a
# message that only ever existed locally.

# snapshot DIR: every message file under DIR by relative path, with its
# checksum and size, so any rename, rewrite or removal shows.
snapshot() {
    (cd "$1" && find . -type f \( -path '*/cur/*' -o -path '*/new/*' \) -exec cksum {} + | sort -k 3)
}

# far_file UID: the far INBOX file holding that UID.
far_file() {
    find "$FAR/INBOX/cur" -type f -name "*,U=$1:*" -print -quit
}

# near_copies TAG: the near-side message files holding the message.
near_copies() {
    grep -rlF "Message-ID: <$1@example.invalid>" "$NEAR" 2>/dev/null | grep -E '/(cur|new)/[^/]+$' || true
}

# trashed_copies TAG: how many near-side copies carry the T flag.
trashed_copies() {
    near_copies "$1" | grep -cE ':2,[A-Z]*T[A-Z]*$' || true
}

# set_far_uidvalidity: give the far INBOX a new UIDVALIDITY and keep its
# next-UID counter, as a Bridge that renumbered the folder would.
set_far_uidvalidity() {
    local next
    next="$(sed -n 2p "$FAR/INBOX/.uidvalidity")"
    printf '%s\n%s\n' 4000000001 "$next" >"$FAR/INBOX/.uidvalidity"
}

# uv_prepare LABEL: fresh stores with ten synced INBOX messages, the last
# then deleted on the far side, and one local-only message. Sets LIVE
# (the nine tags still on the far side), GONE and LOCAL; fails if the
# local state is not as described.
uv_prepare() {
    local entry
    FAR="$WORK/$1/far"
    NEAR="$WORK/$1/maildir"
    mkdir -p "$FAR" "$NEAR"
    chmod 700 "$FAR" "$NEAR"
    TAGS=()
    for entry in 1 2 3 4 5 6 7 8 9 10; do
        deliver INBOX
    done
    LIVE=()
    for entry in "${TAGS[@]}"; do
        LIVE+=("${entry#*|}")
    done
    GONE="${LIVE[9]}"
    unset 'LIVE[9]'
    sync_once "$1-setup" || return 1
    rm -f "$(grep -rlF "Message-ID: <$GONE@example.invalid>" "$FAR/INBOX/cur")"
    LOCAL="local-only-$1"
    printf 'From: sender@example.invalid\nMessage-ID: <%s@example.invalid>\n\nlocal only\n' \
        "$LOCAL" >"$NEAR/INBOX/cur/1700000000.${LOCAL}.synthetic:2,S"
    sync_once "$1-deleted" || return 1
    [[ "$(trashed_copies "$GONE")" == 1 && "$(copies "$LOCAL")" == 1 ]]
}

# each_once TAG...: every tag has exactly one near-side copy.
each_once() {
    local tag status=0
    for tag in "$@"; do
        if [[ "$(copies "$tag")" != 1 ]]; then
            printf '     %s copies of %s\n' "$(copies "$tag")" "$tag"
            status=1
        fi
    done
    return "$status"
}

# 5. Spurious: UIDVALIDITY changes, every UID still names its message.
ok=1
if uv_prepare spurious; then
    before="$(snapshot "$NEAR")"
    set_far_uidvalidity
    if ! sync_once spurious-change \
        || ! grep -qxF 'Notice: channel protonmail, far side box INBOX: Recovered from change of UIDVALIDITY.' \
            "$WORK/sync-spurious-change.log"; then
        ok=0
        sed 's/^/     /' "$WORK/sync-spurious-change.log"
    fi
    if [[ "$(snapshot "$NEAR")" != "$before" ]]; then
        ok=0
        printf '     local message files changed in the recovery\n'
    fi
    TAGS=()
    deliver INBOX
    if ! sync_once spurious-later; then
        ok=0
        sed 's/^/     /' "$WORK/sync-spurious-later.log"
    fi
    each_once "${LIVE[@]}" "${TAGS[0]#*|}" "$LOCAL" "$GONE" || ok=0
    [[ "$(trashed_copies "$GONE")" == 1 ]] || ok=0
else
    ok=0
    printf '     setup failed\n'
fi
if ((ok)); then
    pass "a spurious UIDVALIDITY change is recovered by isync, with no local change"
else
    fail "a spurious UIDVALIDITY change is recovered by isync, with no local change"
fi

# 6. Genuine: UIDVALIDITY changes and UIDs 1 and 2 swap messages.
ok=1
if uv_prepare genuine; then
    before="$(snapshot "$NEAR")"
    set_far_uidvalidity
    one="$(far_file 1)"
    two="$(far_file 2)"
    cp "$one" "$WORK/genuine/swap"
    cp "$two" "$one"
    cp "$WORK/genuine/swap" "$two"
    for run in first retry; do
        if sync_once "genuine-${run}"; then
            ok=0
            printf '     the %s sync after the change succeeded\n' "$run"
        fi
        if ! grep -qxF 'Error: channel protonmail, far side box INBOX: UIDVALIDITY genuinely changed (at UID 1).' \
            "$WORK/sync-genuine-${run}.log"; then
            ok=0
            sed 's/^/     /' "$WORK/sync-genuine-${run}.log"
        fi
        if [[ "$(snapshot "$NEAR")" != "$before" ]]; then
            ok=0
            printf '     the %s failed sync changed local message files\n' "$run"
        fi
    done
else
    ok=0
    printf '     setup failed\n'
fi
if ((ok)); then
    pass "a genuine UIDVALIDITY change fails every sync and changes nothing locally"
else
    fail "a genuine UIDVALIDITY change fails every sync and changes nothing locally"
fi

# The documented recovery: keep the old Maildir whole (left where it is
# here, standing in for the backup), start a fresh one and sync into it.
# The fresh one gets a path of its own: a bind mount of a path whose
# directory was just replaced can show the old one's files on Docker for
# Mac.
ok=1
backup="$NEAR"
NEAR="$WORK/genuine/fresh"
mkdir -m 700 "$NEAR"
if ! sync_once genuine-fresh; then
    ok=0
    sed 's/^/     /' "$WORK/sync-genuine-fresh.log"
fi
TAGS=()
deliver INBOX
if ! sync_once genuine-later; then
    ok=0
    sed 's/^/     /' "$WORK/sync-genuine-later.log"
fi
each_once "${LIVE[@]}" "${TAGS[0]#*|}" || ok=0
if [[ "$(copies "$GONE")" != 0 || "$(copies "$LOCAL")" != 0 ]]; then
    ok=0
    printf '     the fresh Maildir holds mail the far side no longer has\n'
fi
if [[ "$(snapshot "$backup")" != "$before" ]]; then
    ok=0
    printf '     the kept Maildir changed\n'
fi
if ((ok)); then
    pass "after a genuine change a fresh Maildir syncs each message once; the old one is kept whole"
else
    fail "after a genuine change a fresh Maildir syncs each message once; the old one is kept whole"
fi

# The rejected alternative: the old Maildir with only INBOX's sync state
# moved aside. isync then knows none of the local files and downloads
# every message again. The X-TUID header isync writes into each copy
# makes the new copy's bytes differ from the old one's, so the indexer
# keys them as two messages, and a later deletion in Proton reaches only
# the new copy.
ok=1
NEAR="$WORK/genuine/state-reset"
cp -Rp "$backup" "$NEAR"
mkdir "$WORK/genuine/state-aside"
mv "$NEAR"/INBOX/.mbsyncstate* "$WORK/genuine/state-aside/"
if ! sync_once genuine-state-reset; then
    ok=0
    sed 's/^/     /' "$WORK/sync-genuine-state-reset.log"
fi
for tag in "${LIVE[@]}"; do
    if [[ "$(copies "$tag")" != 2 ]]; then
        ok=0
        printf '     %s copies of %s\n' "$(copies "$tag")" "$tag"
    fi
done
first="$(near_copies "${LIVE[2]}" | sed -n 1p)"
second="$(near_copies "${LIVE[2]}" | sed -n 2p)"
if [[ -z "$second" ]] || cmp -s "$first" "$second"; then
    ok=0
    printf '     the two copies are not two different files\n'
fi
rm -f "$(grep -rlF "Message-ID: <${LIVE[2]}@example.invalid>" "$FAR/INBOX/cur")"
if ! sync_once genuine-state-reset-deleted; then
    ok=0
    sed 's/^/     /' "$WORK/sync-genuine-state-reset-deleted.log"
fi
if [[ "$(trashed_copies "${LIVE[2]}")" != 1 ]]; then
    ok=0
    printf '     %s of the two copies flagged deleted\n' "$(trashed_copies "${LIVE[2]}")"
fi
if ((ok)); then
    pass "moving only the sync state aside duplicates every message and leaves the old copies behind"
else
    fail "moving only the sync state aside duplicates every message and leaves the old copies behind"
fi

if ((FAILURES > 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all checks passed\n'
