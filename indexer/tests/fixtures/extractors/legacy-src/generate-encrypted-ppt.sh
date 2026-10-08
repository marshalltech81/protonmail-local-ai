#!/bin/bash
set -Eeuo pipefail

# Regenerate the password-protected legacy .ppt fixture
# (../legacy-encrypted.ppt, #983) with Apache POI: builds the indexer's
# ppt-builder stage (the JDK and the jars indexer/java/pom.xml pins),
# then compiles and runs EncryptedPpt.java in it, with no network. See
# ../README.md for what the deck holds.
#
# Usage: generate-encrypted-ppt.sh

src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="$(dirname "${src_dir}")"
indexer_dir="$(cd "${src_dir}/../../../.." && pwd)"
image="protonmail-local-ai-ppt-builder:fixtures"
password="synthetic-open-password" # pragma: allowlist secret

docker build --target ppt-builder --tag "${image}" "${indexer_dir}"
docker run --rm --network none \
  --volume "${src_dir}:/src:ro" --volume "${out_dir}:/out" \
  "${image}" sh -c '
    set -e
    javac --release 21 -cp "/opt/ppt/lib/*" -d /tmp/classes /src/EncryptedPpt.java
    java -cp "/opt/ppt/lib/*:/tmp/classes" EncryptedPpt /out/legacy-encrypted.ppt "$1"
  ' sh "${password}"
printf 'wrote %s\n' "${out_dir}/legacy-encrypted.ppt"
