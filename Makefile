.PHONY: build up down logs first-run update pull-models status clean sync sync-indexer sync-mcp test test-indexer test-mcp bridge-patch-check bridge-smoke bridge-upgrade-check init-secrets validate-env help

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
	@echo "  up           Start the full stack"
	@echo "  down         Stop the full stack"
	@echo "  logs         Tail logs from all containers"
	@echo "  first-run    One-time interactive Bridge login"
	@echo "  bridge-patch-check  Verify Bridge source patch points still match upstream"
	@echo "  bridge-smoke        Build and smoke test the Bridge runtime image"
	@echo "  bridge-upgrade-check  Run Bridge patch-drift and smoke checks"
	@echo "  update       Rebuild and restart Bridge with new version"
	@echo "  pull-models  Pull Ollama embedding and LLM models"
	@echo "  status       Show container and index status"
	@echo "  sync         Sync local uv environments for Python services"
	@echo "  test         Run indexer and mcp-server unit tests locally with uv"
	@echo "  test-indexer Run indexer unit tests only"
	@echo "  test-mcp     Run mcp-server unit tests only"
	@echo "  clean        Remove all containers and volumes (destructive)"
	@echo ""

# Create placeholder secret files required by Docker Compose.
# Run this once during initial setup before make first-run or make up.
# bridge_pass.txt — overwrite with real Bridge password after make first-run.
# anthropic_api_key.txt — overwrite with your Claude API key for LLM_MODE=cloud,
#                         or leave empty for local-only mode.
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

# Build all images from source
build:
	docker compose build

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

# Pull Ollama models defined in .env.
# Brings the ollama container up first so the target works from a clean
# setup where no services are running; waits up to 120s for Ollama to
# become ready before attempting pulls.
pull-models:
	@docker compose up -d ollama
	@printf 'Waiting for Ollama to be ready'
	@for i in $$(seq 1 60); do \
		if docker exec ollama ollama list >/dev/null 2>&1; then \
			printf '\n'; \
			exit 0; \
		fi; \
		printf '.'; \
		sleep 2; \
	done; \
	printf '\nERROR: Ollama did not become ready within 120s.\n' >&2; \
	exit 1
	@echo "Pulling embedding model..."
	docker exec ollama ollama pull $$(grep '^OLLAMA_EMBED_MODEL=' .env | cut -d= -f2)
	@echo "Pulling LLM model..."
	docker exec ollama ollama pull $$(grep '^OLLAMA_LLM_MODEL=' .env | cut -d= -f2)

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
sync: sync-indexer sync-mcp

sync-indexer:
	cd indexer && uv sync --locked --dev

sync-mcp:
	cd mcp-server && uv sync --locked --dev

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
test: test-indexer test-mcp

test-indexer: sync-indexer
	cd indexer && uv run pytest -q

test-mcp: sync-mcp
	cd mcp-server && uv run pytest -q

# Remove all containers and volumes
# WARNING: This deletes your email index and Bridge credentials.
# You will need to run first-run again after this.
clean:
	@echo "WARNING: This will delete all containers, volumes, and your email index."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker compose down -v
	@echo "All containers and volumes removed."
