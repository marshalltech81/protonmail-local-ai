.PHONY: build build-nocache up down logs first-run update status clean sync sync-indexer sync-mcp sync-mlx sync-mlx-lm test test-indexer test-mcp test-mlx typecheck typecheck-indexer typecheck-mcp typecheck-mlx bridge-patch-check bridge-smoke bridge-upgrade-check init-secrets validate-env help

UV_CACHE_DIR ?= /tmp/uv-cache
export UV_CACHE_DIR

# =============================================================================
# protonmail-local-ai — Makefile
# =============================================================================

help:
	@echo ""
	@echo "  protonmail-local-ai"
	@echo ""
	@echo "  init-secrets Create placeholder secret files under .secrets/ (run once on setup)"
	@echo "  validate-env Verify .env values and secret file permissions before startup"
	@echo "  build        Build all Docker images"
	@echo "  build-nocache Rebuild all Docker images from scratch (skips BuildKit cache)"
	@echo "  up           Start the full stack"
	@echo "  down         Stop the full stack"
	@echo "  logs         Tail logs from all containers"
	@echo "  first-run    One-time interactive Bridge login"
	@echo "  bridge-patch-check  Verify Bridge source patch points still match upstream"
	@echo "  bridge-smoke        Build and smoke test the Bridge runtime image"
	@echo "  bridge-upgrade-check  Run Bridge patch-drift and smoke checks"
	@echo "  update       Rebuild and restart Bridge with new version"
	@echo "  status       Show container and index status"
	@echo "  sync         Sync local uv environments for indexer, mcp-server, mlx-service, and mlx-lm-server"
	@echo "  test         Run indexer, mcp-server, and mlx-service unit tests locally with uv"
	@echo "  typecheck    Run mypy over all three Python services"
	@echo "  test-indexer Run indexer unit tests only"
	@echo "  test-mcp     Run mcp-server unit tests only"
	@echo "  test-mlx     Run mlx-service unit tests only"
	@echo "  clean        Remove all containers and volumes (destructive)"
	@echo ""

# Create placeholder secret files required by Docker Compose.
# Run this once during initial setup before make first-run or make up.
# bridge_pass.txt — overwrite with real Bridge password after make first-run.
# anthropic_api_key.txt — overwrite with your Claude API key for LLM_MODE=cloud,
#                         or leave empty for local-only mode.
# embed_api_key.txt    — overwrite with your provider key when EMBED_BASE_URL
#                         points at a cloud embedder (DeepInfra, OpenRouter,
#                         etc.); leave empty for the local mlx-service path.
init-secrets:
	@mkdir -p .secrets
	@chmod 700 .secrets
	@if [ ! -f .secrets/bridge_pass.txt ]; then \
		printf '' > .secrets/bridge_pass.txt; \
		chmod 600 .secrets/bridge_pass.txt; \
		echo "  created .secrets/bridge_pass.txt (placeholder — fill in after make first-run)"; \
	else \
		echo "  .secrets/bridge_pass.txt already exists, skipping"; \
	fi
	@if [ ! -f .secrets/anthropic_api_key.txt ]; then \
		printf '' > .secrets/anthropic_api_key.txt; \
		chmod 600 .secrets/anthropic_api_key.txt; \
		echo "  created .secrets/anthropic_api_key.txt (empty — fill in if LLM_MODE=cloud)"; \
	else \
		echo "  .secrets/anthropic_api_key.txt already exists, skipping"; \
	fi
	@if [ ! -f .secrets/embed_api_key.txt ]; then \
		printf '' > .secrets/embed_api_key.txt; \
		chmod 600 .secrets/embed_api_key.txt; \
		echo "  created .secrets/embed_api_key.txt (empty — fill in only for cloud embedder)"; \
	else \
		echo "  .secrets/embed_api_key.txt already exists, skipping"; \
	fi

# Build all images from source
build:
	docker compose build

# Build all images from source with the BuildKit cache disabled.
# Use after a base-image tag refresh, when chasing a "stale layer"
# bug, or when you want to confirm a Dockerfile change actually
# rebuilds the layer you think it does. Slower than ``make build``
# (Bridge's Go compile from upstream Proton source dominates the
# wall-clock; expect ~5-10 min on Apple Silicon).
#
# Pass SERVICES=indexer (or any compose service name list) to scope
# the rebuild — useful when only the Python services changed and you
# want to skip the heavy Bridge rebuild:
#
#   make build-nocache SERVICES="indexer mcp-server"
build-nocache:
	docker compose build --no-cache $(SERVICES)

validate-env:
	./scripts/validate-env.sh

# Start the full stack in detached mode
up: init-secrets validate-env
	docker compose up -d

# Stop everything
down:
	docker compose down

# Tail logs across all containers
logs:
	docker compose logs -f

# One-time interactive Bridge login
# Run this on first setup to authenticate with your Proton account.
# After login: copy username → .env (BRIDGE_USER), password → .secrets/bridge_pass.txt
#
# Uses a compose override (-f docker-compose.first-run.yml) that sets
# logging: driver: none for the bridge service, preventing Bridge credentials
# printed by `info` from being written to Docker log files on the host.
#
# NOTE: credentials will NOT appear in docker logs during this session.
# If the container exits unexpectedly, re-run make first-run to see terminal output.
first-run: init-secrets
	@echo ""
	@echo "  Starting ProtonBridge interactive login..."
	@echo "  Commands inside the CLI:"
	@echo "    login  → enter your Proton credentials + 2FA"
	@echo "    info   → copy the bridge username to .env (BRIDGE_USER)"
	@echo "           → write the bridge password to .secrets/bridge_pass.txt"
	@echo "    exit   → then run: make up"
	@echo ""
	@echo "  Logging is disabled for this session — credentials will not"
	@echo "  appear in docker logs."
	@echo ""
	docker compose -f docker-compose.yml -f docker-compose.first-run.yml \
		run --rm --no-deps protonmail-bridge

# Update Bridge to a new version
# 1. Bump BRIDGE_VERSION in .env
# 2. Run: make update
update: bridge-upgrade-check
	docker compose build protonmail-bridge
	docker compose up -d protonmail-bridge
	@echo "Bridge updated and restarted."

bridge-patch-check:
	./scripts/bridge-patch-drift.sh

bridge-smoke:
	./scripts/bridge-smoke.sh

bridge-upgrade-check: bridge-patch-check bridge-smoke

# Sync local Python environments using per-service uv projects
sync: sync-indexer sync-mcp sync-mlx sync-mlx-lm

sync-indexer:
	cd indexer && uv sync --locked --dev

sync-mcp:
	cd mcp-server && uv sync --locked --dev

sync-mlx:
	cd mlx-service && uv sync --locked --dev

# mlx-lm-server is a thin wrapper around upstream ``mlx_lm.server``
# (no project source, no tests/typecheck targets); ``sync-mlx-lm``
# just installs the pinned ``mlx-lm`` so the LaunchAgent has a venv
# to run from after a fresh clone.
sync-mlx-lm:
	cd mlx-lm-server && uv sync --locked

# Show running containers and basic index status
status:
	@echo ""
	@echo "=== Containers ==="
	docker compose ps
	@echo ""
	@echo "=== Index ==="
	docker exec mcp-server python -c \
		"from src.tools.system import get_index_status; \
		 import json; print(json.dumps(get_index_status(), indent=2))" \
		2>/dev/null || echo "  MCP server not running or index not ready."
	@echo ""

# Run unit tests locally using uv
test: test-indexer test-mcp test-mlx

test-indexer: sync-indexer
	cd indexer && uv run pytest -q

test-mcp: sync-mcp
	cd mcp-server && uv run pytest -q

test-mlx: sync-mlx
	cd mlx-service && uv run pytest -q

typecheck: typecheck-indexer typecheck-mcp typecheck-mlx

typecheck-indexer: sync-indexer
	cd indexer && uv run mypy src

typecheck-mcp: sync-mcp
	cd mcp-server && uv run mypy src

typecheck-mlx: sync-mlx
	cd mlx-service && uv run mypy src

# Remove all containers and volumes
# WARNING: This deletes your email index and Bridge credentials.
# You will need to run first-run again after this.
#
clean:
	@echo "WARNING: This will delete all containers, volumes, your email index,"
	@echo "         and Bridge credentials. You will need to run make first-run"
	@echo "         again to re-authenticate with Proton."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker compose down -v
	@# Truncate secrets tied to wiped local state. anthropic_api_key.txt is the
	@# user's external Claude key and is intentionally preserved.
	@if [ -f .secrets/bridge_pass.txt ]; then : > .secrets/bridge_pass.txt; fi
	@echo "All containers and volumes removed."
	@echo "Cleared .secrets/bridge_pass.txt."
	@echo "Re-run make first-run, then paste the new Bridge password into .secrets/bridge_pass.txt."
