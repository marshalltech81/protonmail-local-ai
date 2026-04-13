#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly IMAGE="${BRIDGE_IMAGE:-protonmail-local-ai/bridge}"

cd "$ROOT_DIR"

printf 'Building Proton Bridge image...\n'
docker compose build protonmail-bridge

printf 'Running Proton Bridge runtime smoke checks...\n'
docker run --rm --entrypoint /bin/sh "$IMAGE" -ceu '
bridge --help >/dev/null
gpg --version >/dev/null
pass --version >/dev/null
nc -h >/dev/null 2>&1 || true
getent passwd bridge | grep -F "bridge" | grep -F "/usr/sbin/nologin" >/dev/null
test "$(id -u bridge)" = "1000"
test "$(stat -c "%a" /data/gnupg)" = "700"
test -x /entrypoint.sh
'

printf 'Proton Bridge smoke checks passed.\n'
