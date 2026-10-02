#!/bin/bash
set -Eeuo pipefail
# Test cases for .semgrep/shell.yaml. Never executed; it only has to
# parse and pass shellcheck.

url="https://example.invalid/install.sh"
dir="/tmp/example"

# ruleid: shell-tls-verification-disabled
curl -fsSk "$url"
# ruleid: shell-tls-verification-disabled
curl --insecure -o out "$url"
# ok: shell-tls-verification-disabled
curl -fsSL -o out "$url"
# ruleid: shell-tls-verification-disabled
wget --no-check-certificate "$url"
# ruleid: shell-tls-verification-disabled
git -c http.sslVerify=false clone "$url"
# ruleid: shell-tls-verification-disabled
export GIT_SSL_NO_VERIFY=1
# ruleid: shell-tls-verification-disabled
NODE_TLS_REJECT_UNAUTHORIZED=0 node app.js
# ruleid: shell-tls-verification-disabled
git config --global http.sslVerify false
# ok: shell-tls-verification-disabled
# A commented-out line is not a finding: curl -k "$url"

# ruleid: shell-xtrace-enabled
set -x
# ruleid: shell-xtrace-enabled
set -Eeux
# ruleid: shell-xtrace-enabled
set -o xtrace
# ruleid: shell-xtrace-enabled
bash -x ./other.sh
# ok: shell-xtrace-enabled
set +x
# ok: shell-xtrace-enabled
set -Eeuo pipefail

# ruleid: shell-pipe-to-shell
curl -fsSL "$url" | bash
# ruleid: shell-pipe-to-shell
wget -qO- "$url" | sh -s -- --flag
# ruleid: shell-pipe-to-shell
bash <(curl -fsSL "$url")
# ok: shell-pipe-to-shell
curl -fsSL "$url" | sha256sum

# ruleid: shell-world-writable
chmod 777 "$dir"
# ruleid: shell-world-writable
chmod -R 0666 "$dir"
# ruleid: shell-world-writable
chmod o+w "$dir"
# ruleid: shell-world-writable
chmod a+rwx "$dir"
# ruleid: shell-world-writable
mkdir -m 1777 "$dir"
# ok: shell-world-writable
chmod 600 "$dir"
# ok: shell-world-writable
chmod go+r "$dir"
# ok: shell-world-writable
chmod 755 "$dir"
# ok: shell-world-writable
chmod go+rx "$dir"
