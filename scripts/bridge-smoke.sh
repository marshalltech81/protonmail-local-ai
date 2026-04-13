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

# Pre-created state directories and the entrypoint should keep their expected
# permissions after image build changes.
test "$(stat -c "%a" /data/config)" = "700"
test "$(stat -c "%a" /data/local)" = "700"
test "$(stat -c "%a" /data/cache)" = "700"
test "$(stat -c "%a" /data/gnupg)" = "700"
test "$(stat -c "%a" /data/pass)" = "700"
test -x /entrypoint.sh
'

printf 'Running Proton Bridge compose runtime checks...\n'
docker compose run --rm --no-deps --entrypoint /bin/sh protonmail-bridge -ceu '
# The service user should still be able to write to the declared scratch and
# state paths even when the Compose service uses a read-only root filesystem.
touch /tmp/bridge-smoke
touch /home/bridge/bridge-smoke
touch /data/bridge-smoke
rm -f /data/bridge-smoke
'

docker compose run --rm --no-deps --entrypoint /bin/sh --user root protonmail-bridge -ceu '
# Root is used here only to distinguish a read-only rootfs from normal
# non-root permission failures elsewhere in the image.
if touch /bridge-smoke-rootfs 2>/dev/null; then
    echo "Bridge root filesystem is unexpectedly writable." >&2
    exit 1
fi
'

printf 'Proton Bridge smoke checks passed.\n'
