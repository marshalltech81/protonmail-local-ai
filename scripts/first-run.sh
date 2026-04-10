#!/bin/bash
# =============================================================================
# first-run.sh
# One-time interactive Bridge login helper.
# Run via: make first-run
# =============================================================================
set -e

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           protonmail-local-ai — First Run Setup              ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  This will start ProtonBridge in interactive mode so you    ║"
echo "║  can authenticate your Proton account.                      ║"
echo "║                                                              ║"
echo "║  Inside the Bridge CLI:                                      ║"
echo "║    login  → enter your Proton email + password + 2FA        ║"
echo "║    info   → copy bridge username + password into .env       ║"
echo "║    exit   → then run: make up                               ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env not found. Run: cp .env.example .env"
    exit 1
fi

docker compose run --rm protonmail-bridge
