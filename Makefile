.PHONY: build up down logs first-run update pull-models status clean test help

# =============================================================================
# protonmail-local-ai — Makefile
# =============================================================================

help:
	@echo ""
	@echo "  protonmail-local-ai"
	@echo ""
	@echo "  build        Build all Docker images"
	@echo "  up           Start the full stack"
	@echo "  down         Stop the full stack"
	@echo "  logs         Tail logs from all containers"
	@echo "  first-run    One-time interactive Bridge login"
	@echo "  update       Rebuild and restart Bridge with new version"
	@echo "  pull-models  Pull Ollama embedding and LLM models"
	@echo "  status       Show container and index status"
	@echo "  test         Run indexer unit tests locally (requires uv)"
	@echo "  clean        Remove all containers and volumes (destructive)"
	@echo ""

# Build all images from source
build:
	docker compose build

# Start the full stack in detached mode
up:
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
first-run:
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

# Pull Ollama models defined in .env
pull-models:
	@echo "Pulling embedding model..."
	docker exec ollama ollama pull $$(grep OLLAMA_EMBED_MODEL .env | cut -d= -f2)
	@echo "Pulling LLM model..."
	docker exec ollama ollama pull $$(grep OLLAMA_LLM_MODEL .env | cut -d= -f2)

# Update Bridge to a new version
# 1. Bump BRIDGE_VERSION in .env
# 2. Run: make update
update:
	docker compose build protonmail-bridge
	docker compose up -d protonmail-bridge
	@echo "Bridge updated and restarted."

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

# Run indexer unit tests locally using uv
test:
	uv venv -q && uv pip install -r indexer/requirements.txt -q && uv run pytest

# Remove all containers and volumes
# WARNING: This deletes your email index and Bridge credentials.
# You will need to run first-run again after this.
clean:
	@echo "WARNING: This will delete all containers, volumes, and your email index."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker compose down -v
	@echo "All containers and volumes removed."
