#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly IMAGE="${BRIDGE_IMAGE:-protonmail-local-ai/bridge}"

cd "$ROOT_DIR"

printf 'Building Proton Bridge image...\n'
docker compose build protonmail-bridge

# Keep the smoke test intentionally small and local: verify the runtime image
# has the expected binary, supporting tools, service account shape, and locked
# down filesystem bits without requiring a live Proton login.
printf 'Running Proton Bridge runtime smoke checks...\n'
docker run --rm --entrypoint /bin/sh "$IMAGE" -ceu '
# Core runtime binaries the entrypoint depends on.
bridge --help >/dev/null
gpg --version >/dev/null
pass --version >/dev/null

# The service user should remain non-root and non-login.
getent passwd bridge | grep -F "bridge" | grep -F "/usr/sbin/nologin" >/dev/null
test "$(id -u bridge)" = "1000"

# Sensitive state directories and the entrypoint should keep their expected
# permissions after image build changes.
test "$(stat -c "%a" /data/gnupg)" = "700"
test -x /entrypoint.sh
'

printf 'Proton Bridge smoke checks passed.\n'
