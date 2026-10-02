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
curl -fsSL \
    --insecure -o out "$url"
# ruleid: shell-tls-verification-disabled
wget -q \
    --no-check-certificate "$url"
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
# ruleid: shell-tls-verification-disabled
curl "https://example.invalid/tool#v1" --insecure -o tool
# ruleid: shell-tls-verification-disabled
curl -H "X-Note: # release" --insecure -o out "$url"
# ruleid: shell-tls-verification-disabled
wget --header "X-Note: # release" --no-check-certificate "$url"
# ruleid: shell-tls-verification-disabled
printf '%s\n' "# fetch"; curl -k "$url"
# ok: shell-tls-verification-disabled
# A commented-out line is not a finding: curl -k "$url"
# ok: shell-tls-verification-disabled
    # Nor is an indented one: wget --no-check-certificate "$url"

# ruleid: shell-xtrace-enabled
set -x
# ruleid: shell-xtrace-enabled
set -Eeux
# ruleid: shell-xtrace-enabled
set -o xtrace
# ruleid: shell-xtrace-enabled
bash -o xtrace ./other.sh
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
# ruleid: shell-pipe-to-shell
curl -fsSL "$url" | /bin/bash
# ruleid: shell-pipe-to-shell
curl -fsSL "$url" | /usr/bin/env bash
# ruleid: shell-pipe-to-shell
wget -qO- "$url" \
    | sh
# ruleid: shell-pipe-to-shell
curl -H "X-Note: # release" -fsSL "$url" | bash
# ruleid: shell-pipe-to-shell
printf '%s\n' "# install"; bash <(curl -fsSL "$url")
# ok: shell-pipe-to-shell
curl -fsSL "$url" | sha256sum
# ok: shell-pipe-to-shell
# curl -fsSL "$url" | bash

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
# ruleid: shell-world-writable
mkdir --mode=777 "$dir"
# ruleid: shell-world-writable
install --mode 666 src "$dir/dst"
# ruleid: shell-world-writable
chmod --recursive 777 "$dir"
# ruleid: shell-world-writable
chmod --verbose -R o+w "$dir"
# ruleid: shell-world-writable
chmod -- 0666 "$dir"
# ok: shell-world-writable
chmod --recursive 755 "$dir"
# ok: shell-world-writable
chmod 600 "$dir"
# ok: shell-world-writable
chmod go+r "$dir"
# ok: shell-world-writable
chmod 755 "$dir"
# ok: shell-world-writable
chmod go+rx "$dir"
