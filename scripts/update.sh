#!/bin/bash
# =============================================================================
# update.sh
# Update ProtonBridge to a new version.
# Usage: BRIDGE_VERSION=v3.22.0 ./scripts/update.sh
#    or: bump BRIDGE_VERSION in .env, then run: make update
# =============================================================================
set -Eeuo pipefail

VERSION="${BRIDGE_VERSION:-$(grep BRIDGE_VERSION .env | cut -d= -f2)}"

if [ -z "$VERSION" ]; then
    echo "ERROR: BRIDGE_VERSION not set in .env or environment."
    exit 1
fi

echo "Updating ProtonBridge to ${VERSION}..."
docker compose build \
    --build-arg BRIDGE_VERSION="${VERSION}" \
    protonmail-bridge

echo "Restarting Bridge..."
docker compose up -d protonmail-bridge

echo ""
echo "Bridge updated to ${VERSION} and restarted."
echo "Run 'make logs' to verify startup."
