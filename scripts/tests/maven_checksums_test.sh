#!/bin/bash
set -Eeuo pipefail

# Keeps the .ppt reader's Maven runs verifying every artifact against
# the committed SHA-256 summary file, indexer/java/checksums/
# checksums.sha256 (#1117). Maven Resolver's trusted-checksums check
# runs on every resolution, so a jar or POM that reached the BuildKit
# cache mount (indexer/Dockerfile) or the restored ~/.m2 cache
# (.github/workflows/security.yml) by any route other than a verified
# download fails the run.
#
# The checks:
# - the Dockerfile's ppt-builder command and the workflow's resolve
#   carry exactly the required -Daether.* flags (SHA-256, failIfMissing,
#   recording off, the summary-file source, not origin-aware) and
#   --strict-checksums; the summary file's directory is the committed
#   one (the Dockerfile copies it there before the run), never the
#   default inside the local repository, which is the cache;
# - the ppt-checksums stage that `make ppt-checksums` builds records
#   with the same algorithm and source flags, from an empty file, with
#   no cache mount, running both goals the two commands run;
# - the committed file is well formed and has a jar and a POM line for
#   every dependency and the dependency plugin pom.xml pins, read from
#   pom.xml itself, so a Dependabot bump without a regenerated file
#   fails here as well as in the build.
# Copies of the files with synthetic edits show each check fails. Needs
# no Docker, Maven or network.
#
# Run: bash scripts/tests/maven_checksums_test.sh  (or make test-maven-checksums)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly DOCKERFILE=indexer/Dockerfile
readonly WORKFLOW=.github/workflows/security.yml
readonly MAKEFILE=Makefile
readonly POM=indexer/java/pom.xml
readonly SUMMARY=indexer/java/checksums/checksums.sha256
readonly FILES=("$DOCKERFILE" "$WORKFLOW" "$MAKEFILE" "$POM" "$SUMMARY")

readonly PP=aether.artifactResolver.postProcessor.trustedChecksums
readonly SRC=aether.trustedChecksumsSource.summaryFile
# The flags every verifying run carries, apart from the source's basedir.
readonly VERIFY_FLAGS=(
    "-D$PP=true"
    "-D$PP.checksumAlgorithms=SHA-256"
    "-D$PP.failIfMissing=true"
    "-D$PP.record=false"
    "-D$SRC=true"
    "-D$SRC.originAware=false"
)
# The flags the recording run carries, apart from the source's basedir.
readonly RECORD_FLAGS=(
    "-D$PP=true"
    "-D$PP.checksumAlgorithms=SHA-256"
    "-D$PP.record=true"
    "-D$SRC=true"
    "-D$SRC.originAware=false"
)
readonly DOCKER_BASEDIR=/build/checksums
# shellcheck disable=SC2016 # expanded by the runner's shell, not here
readonly WORKFLOW_BASEDIR='$GITHUB_WORKSPACE/indexer/java/checksums'

# The Dockerfile's instructions under $1, one per line, continuation
# lines joined.
dockerfile_instructions() {
    awk '{ if (sub(/\\$/, "")) { buf = buf $0 " "; next } print buf $0; buf = "" }' "$1/$DOCKERFILE"
}

# The instructions of stage $2 (`FROM ... AS $2` up to the next FROM).
dockerfile_stage() {
    dockerfile_instructions "$1" | awk -v stage="$2" '
        /^FROM / { found = ($NF == stage) }
        found'
}

# The workflow's `run: >-` blocks under $1, one per line.
workflow_runs() {
    awk '
        BEGIN { indent = -1 }
        function flush() { if (buf != "") print buf; buf = ""; indent = -1 }
        indent >= 0 {
            match($0, /^ */)
            if ($0 ~ /^ *$/) next
            if (RLENGTH > indent) { sub(/^ */, ""); buf = buf " " $0; next }
            flush()
        }
        /^ *run: >-$/ { match($0, /^ */); indent = RLENGTH; buf = "run:" }
        END { flush() }
    ' "$1/$WORKFLOW"
}

# Exactly one line of $2 matching $3, or a message naming $1.
one_line() {
    local what="$1" lines="$2" pattern="$3" found
    found="$(grep -E -- "$pattern" <<<"$lines" || true)"
    if [[ -z "$found" ]]; then
        printf '%s: no command matching %s\n' "$what" "$pattern"
        return 1
    fi
    if (($(grep -c '' <<<"$found") != 1)); then
        printf '%s: several commands matching %s\n' "$what" "$pattern"
        return 1
    fi
    printf '%s\n' "$found"
}

# Fails unless command $2 (named $1) carries exactly the -Daether.* flags
# $4... plus the summary-file basedir $3, and --strict-checksums.
flags_are() {
    local what="$1" command="$2" basedir="$3" expected actual
    shift 3
    expected="$(printf '%s\n' "$@" "-D$SRC.basedir=$basedir" | sort)"
    actual="$(grep -oE -- '-Daether\.[^[:space:]"]+' <<<"$command" | sort)"
    if [[ "$actual" != "$expected" ]]; then
        printf '%s: the Maven Resolver flags differ from the required set\n' "$what"
        diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") | sed 's/^/  /' || true
        return 1
    fi
    if [[ "$command" != *" --strict-checksums "* ]]; then
        printf '%s: no --strict-checksums\n' "$what"
        return 1
    fi
}

# Fails unless the verifying commands under $1 carry the required flags.
verification_is_pinned() {
    local root="$1" builder command runs
    builder="$(dockerfile_stage "$root" ppt-builder)"
    command="$(one_line "$DOCKERFILE ppt-builder" "$builder" '^RUN .*mvn .*dependency:copy-dependencies')" || { printf "%s\n" "$command"; return 1; }
    flags_are "$DOCKERFILE ppt-builder" "$command" "$DOCKER_BASEDIR" "${VERIFY_FLAGS[@]}" || return 1
    # The summary file is copied to that directory before the run.
    if ! awk -v cmd="$command" -v dir="$DOCKER_BASEDIR/" '
            $0 == "COPY java/checksums/checksums.sha256 " dir { copied = 1 }
            $0 == cmd { exit !copied }
            END { if (!copied) exit 1 }' <<<"$builder"; then
        printf '%s ppt-builder: no COPY java/checksums/checksums.sha256 %s/ before the Maven run\n' \
            "$DOCKERFILE" "$DOCKER_BASEDIR"
        return 1
    fi
    runs="$(workflow_runs "$root")"
    command="$(one_line "$WORKFLOW" "$runs" 'mvn .*dependency:resolve')" || { printf "%s\n" "$command"; return 1; }
    flags_are "$WORKFLOW" "$command" "$WORKFLOW_BASEDIR" "${VERIFY_FLAGS[@]}"
}

# Fails unless the recording stage and make target under $1 record a
# fresh file with the image's Maven and no cache.
recording_is_pinned() {
    local root="$1" stage command
    stage="$(dockerfile_stage "$root" ppt-checksums-record)"
    command="$(one_line "$DOCKERFILE ppt-checksums-record" "$stage" '^RUN .*mvn ')" || { printf "%s\n" "$command"; return 1; }
    flags_are "$DOCKERFILE ppt-checksums-record" "$command" /out "${RECORD_FLAGS[@]}" || return 1
    if [[ "$command" == *--mount=* ]]; then
        printf '%s ppt-checksums-record: the recording run mounts a cache\n' "$DOCKERFILE"
        return 1
    fi
    if [[ "$command" != *" dependency:copy-dependencies "* || "$command" != *" dependency:resolve"* ]]; then
        printf '%s ppt-checksums-record: does not run both goals\n' "$DOCKERFILE"
        return 1
    fi
    if grep -q '^COPY .*checksums' <<<"$stage"; then
        printf '%s ppt-checksums-record: starts from a copied summary file\n' "$DOCKERFILE"
        return 1
    fi
    if ! grep -qx 'FROM ppt-tools AS ppt-checksums-record' <<<"$stage"; then
        printf '%s ppt-checksums-record: not built on ppt-tools\n' "$DOCKERFILE"
        return 1
    fi
    if ! grep -qx 'COPY --from=ppt-checksums-record /out/checksums.sha256 /' \
        <<<"$(dockerfile_stage "$root" ppt-checksums)"; then
        printf '%s ppt-checksums: does not export the recorded file\n' "$DOCKERFILE"
        return 1
    fi
    if ! grep -E -- '--target ppt-checksums .*--output type=local,dest=indexer/java/checksums ' \
        "$root/$MAKEFILE" >/dev/null; then
        printf '%s: no ppt-checksums target exporting the stage to indexer/java/checksums\n' "$MAKEFILE"
        return 1
    fi
}

# Prints the repository paths of a jar and a POM for every dependency and
# the dependency plugin pinned in pom.xml under $1.
pinned_paths() {
    awk '
        /<(dependency|plugin)>/ { g = ""; a = ""; v = "" }
        /<groupId>/ { g = $0; gsub(/.*<groupId>|<\/groupId>.*/, "", g) }
        /<artifactId>/ { a = $0; gsub(/.*<artifactId>|<\/artifactId>.*/, "", a) }
        /<version>/ { v = $0; gsub(/.*<version>|<\/version>.*/, "", v) }
        /<\/(dependency|plugin)>/ {
            gsub(/\./, "/", g)
            print g "/" a "/" v "/" a "-" v ".jar"
            print g "/" a "/" v "/" a "-" v ".pom"
        }
    ' "$1/$POM"
}

# Fails unless the summary file under $1 is well formed and covers pom.xml.
summary_covers_pom() {
    local root="$1" file="$1/$SUMMARY" bad paths path missing=0
    if [[ ! -s "$file" ]]; then
        printf '%s: missing or empty\n' "$SUMMARY"
        return 1
    fi
    bad="$(grep -cvE '^[0-9a-f]{64}  [A-Za-z0-9._/-]+$' "$file" || true)"
    if ((bad > 0)); then
        printf '%s: %d malformed line(s)\n' "$SUMMARY" "$bad"
        return 1
    fi
    if [[ -n "$(awk '{print $2}' "$file" | sort | uniq -d)" ]]; then
        printf '%s: a path is listed twice\n' "$SUMMARY"
        return 1
    fi
    paths="$(pinned_paths "$root")"
    if (($(grep -c '' <<<"$paths") < 20)); then
        printf '%s: read only %d pinned paths\n' "$POM" "$(grep -c '' <<<"$paths")"
        return 1
    fi
    while IFS= read -r path; do
        if ! awk -v p="$path" '$2 == p { found = 1 } END { exit !found }' "$file"; then
            printf '%s: no checksum for %s (run make ppt-checksums)\n' "$SUMMARY" "$path"
            missing=$((missing + 1))
        fi
    done <<<"$paths"
    ((missing == 0))
}

# Copies the checked files into $WORK/$1 with the repository's layout and
# prints that directory.
fixture() {
    local dir="$WORK/$1" file
    for file in "${FILES[@]}"; do
        mkdir -p "$dir/$(dirname "$file")"
        cp "$ROOT_DIR/$file" "$dir/$file"
    done
    printf '%s\n' "$dir"
}

# Runs the check $1 on $2 expecting failure, with $3 in its output.
expect_failure() {
    local check="$1" root="$2" message="$3" out
    if out="$("$check" "$root" 2>&1)"; then
        printf 'passed, expected failure\n'
        return 1
    fi
    if ! grep -F -- "$message" <<<"$out" >/dev/null; then
        printf 'expected %q in:\n%s\n' "$message" "$out"
        return 1
    fi
}

# Applies sed expression $3 to file $2 of fixture $1, failing when it
# changes nothing, and prints the fixture's directory.
edited_fixture() {
    local root
    root="$(fixture "$1")"
    sed -i.bak -E "$3" "$root/$2"
    if cmp -s "$root/$2" "$root/$2.bak"; then
        printf 'the edit %s changed nothing in %s\n' "$3" "$2" >&2
        return 1
    fi
    printf '%s\n' "$root"
}

the_tree_verifies_every_resolution() { verification_is_pinned "$ROOT_DIR"; }
the_tree_records_from_scratch() { recording_is_pinned "$ROOT_DIR"; }
the_summary_covers_the_pom() { summary_covers_pom "$ROOT_DIR"; }

a_workflow_without_fail_if_missing_is_detected() {
    local root
    root="$(edited_fixture no-fail "$WORKFLOW" "/-D$PP\\.failIfMissing=true$/d")"
    expect_failure verification_is_pinned "$root" "$WORKFLOW: the Maven Resolver flags differ"
}

a_dockerfile_that_records_is_detected() {
    local root
    root="$(edited_fixture records "$DOCKERFILE" "s/(-D$PP\\.record=)false/\\1true/")"
    expect_failure verification_is_pinned "$root" "$DOCKERFILE ppt-builder: the Maven Resolver flags differ"
}

a_weaker_algorithm_is_detected() {
    local root
    root="$(edited_fixture sha1 "$DOCKERFILE" "s/(checksumAlgorithms=)SHA-256/\\1SHA-1/")"
    expect_failure verification_is_pinned "$root" "$DOCKERFILE ppt-builder: the Maven Resolver flags differ"
}

an_extra_flag_is_detected() {
    local root
    root="$(edited_fixture extra "$WORKFLOW" "s/(-D$SRC=true)/\\1 -D$PP.snapshots=true/")"
    expect_failure verification_is_pinned "$root" "$WORKFLOW: the Maven Resolver flags differ"
}

a_basedir_in_the_cache_is_detected() {
    local root
    root="$(edited_fixture basedir "$WORKFLOW" "s#(-D$SRC\\.basedir=)[^ ]+#\\1.checksums#")"
    expect_failure verification_is_pinned "$root" "$WORKFLOW: the Maven Resolver flags differ"
}

a_missing_copy_is_detected() {
    local root
    root="$(edited_fixture no-copy "$DOCKERFILE" "/^COPY java\\/checksums\\//d")"
    expect_failure verification_is_pinned "$root" "no COPY java/checksums/checksums.sha256"
}

a_dropped_strict_checksums_is_detected() {
    local root
    root="$(edited_fixture no-strict "$WORKFLOW" "s/ --strict-checksums//")"
    expect_failure verification_is_pinned "$root" "$WORKFLOW: no --strict-checksums"
}

a_cached_recording_is_detected() {
    local root
    root="$(edited_fixture record-cache "$DOCKERFILE" \
        "/^FROM ppt-tools AS ppt-checksums-record/,/^FROM/s/^RUN mvn/RUN --mount=type=cache,target=\\/root\\/.m2 mvn/")"
    expect_failure recording_is_pinned "$root" "the recording run mounts a cache"
}

a_recording_without_record_is_detected() {
    local root
    root="$(edited_fixture record-off "$DOCKERFILE" "s/(-D$PP\\.record=)true/\\1false/")"
    expect_failure recording_is_pinned "$root" "ppt-checksums-record: the Maven Resolver flags differ"
}

a_bumped_dependency_is_detected() {
    local root
    root="$(edited_fixture dep-bump "$POM" "s#<version>2\\.22\\.0</version>#<version>2.99.0</version>#")"
    expect_failure summary_covers_pom "$root" \
        "no checksum for commons-io/commons-io/2.99.0/commons-io-2.99.0.jar"
}

a_bumped_plugin_is_detected() {
    local root
    root="$(edited_fixture plugin-bump "$POM" "s#<version>3\\.11\\.0</version>#<version>3.99.0</version>#")"
    expect_failure summary_covers_pom "$root" \
        "no checksum for org/apache/maven/plugins/maven-dependency-plugin/3.99.0/maven-dependency-plugin-3.99.0.jar"
}

a_malformed_line_is_detected() {
    local root
    root="$(edited_fixture malformed "$SUMMARY" "1s/^[0-9a-f]{8}/zzzzzzzz/")"
    expect_failure summary_covers_pom "$root" "1 malformed line(s)"
}

# Runs each case in a subshell with errexit on, outside any condition.
check() {
    local description="$1" status
    shift
    set +e
    (
        set -e
        "$@"
    ) >"$WORK/case-output" 2>&1
    status=$?
    set -e
    if ((status == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/case-output"
        FAILURES=$((FAILURES + 1))
    fi
}

check "the Dockerfile and the workflow verify every resolution" the_tree_verifies_every_resolution
check "make ppt-checksums records from an empty file, uncached" the_tree_records_from_scratch
check "the summary file covers what pom.xml pins" the_summary_covers_the_pom
check "a workflow without failIfMissing is detected" a_workflow_without_fail_if_missing_is_detected
check "a Dockerfile run that records is detected" a_dockerfile_that_records_is_detected
check "a weaker checksum algorithm is detected" a_weaker_algorithm_is_detected
check "an extra resolver flag is detected" an_extra_flag_is_detected
check "a summary file read from the cache is detected" a_basedir_in_the_cache_is_detected
check "a summary file not copied into the build is detected" a_missing_copy_is_detected
check "a dropped --strict-checksums is detected" a_dropped_strict_checksums_is_detected
check "a recording run with a cache mount is detected" a_cached_recording_is_detected
check "a recording run that does not record is detected" a_recording_without_record_is_detected
check "a dependency bumped without new checksums is detected" a_bumped_dependency_is_detected
check "a plugin bumped without new checksums is detected" a_bumped_plugin_is_detected
check "a malformed summary line is detected" a_malformed_line_is_detected

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
