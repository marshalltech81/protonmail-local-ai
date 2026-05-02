.PHONY: build up down logs first-run update pull-models pull-models-host status clean sync sync-indexer sync-mcp test test-indexer test-mcp typecheck typecheck-indexer typecheck-mcp bridge-patch-check bridge-smoke bridge-upgrade-check open-webui-up open-webui-up-host-ollama open-webui-down open-webui-logs up-host-ollama down-host-ollama logs-host-ollama init-secrets validate-env help

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
	@echo "  up           Start the full stack"
	@echo "  down         Stop the full stack"
	@echo "  logs         Tail logs from all containers"
	@echo "  first-run    One-time interactive Bridge login"
	@echo "  bridge-patch-check  Verify Bridge source patch points still match upstream"
	@echo "  bridge-smoke        Build and smoke test the Bridge runtime image"
	@echo "  bridge-upgrade-check  Run Bridge patch-drift and smoke checks"
	@echo "  open-webui-up Start optional local Open WebUI on localhost"
	@echo "  open-webui-up-host-ollama Start Open WebUI pointed at native macOS Ollama"
	@echo "  open-webui-down Stop optional local Open WebUI"
	@echo "  open-webui-logs Tail optional Open WebUI logs"
	@echo "  up-host-ollama Start the stack pointed at native macOS Ollama (see docs/setup.md)"
	@echo "  down-host-ollama Stop the host-Ollama stack"
	@echo "  logs-host-ollama Tail logs for the host-Ollama stack"
	@echo "  update       Rebuild and restart Bridge with new version"
	@echo "  pull-models  Pull Ollama embedding and LLM models (containerized Ollama)"
	@echo "  pull-models-host Pull Ollama models via the native macOS Ollama (host-Ollama mode)"
	@echo "  status       Show container and index status"
	@echo "  sync         Sync local uv environments for Python services"
	@echo "  test         Run indexer and mcp-server unit tests locally with uv"
	@echo "  typecheck    Run mypy over both Python services"
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
	docker exec ollama ollama pull "$$(./scripts/validate-env.sh --get OLLAMA_EMBED_MODEL)"
	@echo "Pulling LLM model..."
	docker exec ollama ollama pull "$$(./scripts/validate-env.sh --get OLLAMA_LLM_MODEL)"

# Pull Ollama models via the native (host) Ollama. Use this in host-Ollama
# mode (docker-compose.host-ollama.yml). Requires `brew install ollama`
# and the launchd service to be running on 127.0.0.1:11434. See
# docs/setup.md for the host-side setup steps.
pull-models-host:
	@if ! command -v ollama >/dev/null 2>&1; then \
		printf 'ERROR: ollama CLI not found on host. Install with: brew install ollama\n' >&2; \
		exit 1; \
	fi
	@./scripts/check-host-ollama.sh
	@echo "Pulling embedding model (native)..."
	ollama pull "$$(./scripts/validate-env.sh --get OLLAMA_EMBED_MODEL)"
	@echo "Pulling LLM model (native)..."
	ollama pull "$$(./scripts/validate-env.sh --get OLLAMA_LLM_MODEL)"

# Start the stack with Ollama running natively on the macOS host.
# Containers reach Ollama via OrbStack's host.docker.internal. Requires the
# host to bind 0.0.0.0:11434 AND the macOS Application Firewall to be on.
# See docs/setup.md for one-time host setup.
up-host-ollama: init-secrets validate-env
	@./scripts/check-host-ollama.sh
	docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml up -d

down-host-ollama:
	docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml down

logs-host-ollama:
	docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml logs -f

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

open-webui-up: init-secrets validate-env
	@if [ ! -s .secrets/open_webui_secret_key.txt ]; then \
		if ! command -v openssl >/dev/null 2>&1; then \
			printf 'ERROR: openssl not found on host; cannot generate Open WebUI session key.\n' >&2; \
			printf 'Install openssl, or write 32+ random bytes to .secrets/open_webui_secret_key.txt manually.\n' >&2; \
			exit 1; \
		fi; \
		( \
			umask 077; \
			openssl rand -base64 32 | tr -d '\n' > .secrets/open_webui_secret_key.txt; \
		); \
		echo "  generated .secrets/open_webui_secret_key.txt"; \
	fi
	@./scripts/check-mcp-streamable.sh
	@port="$$(./scripts/validate-env.sh --get OPEN_WEBUI_PORT)"; \
	port="$${port:-8080}"; \
	printf '\n  Starting Open WebUI on http://localhost:%s\n' "$$port"; \
	printf '  Open WebUI uses the existing ollama container and MCP at http://mcp-server:3000/mcp.\n\n'
	docker compose -f docker-compose.yml -f docker-compose.open-webui.yml up -d open-webui

# Start Open WebUI when Ollama is running natively on the macOS host
# (host-Ollama mode). Layers all four overlay files so Open WebUI joins the
# host-Ollama stack and points at host.docker.internal:11434.
open-webui-up-host-ollama: init-secrets validate-env
	@if [ ! -s .secrets/open_webui_secret_key.txt ]; then \
		if ! command -v openssl >/dev/null 2>&1; then \
			printf 'ERROR: openssl not found on host; cannot generate Open WebUI session key.\n' >&2; \
			exit 1; \
		fi; \
		( \
			umask 077; \
			openssl rand -base64 32 | tr -d '\n' > .secrets/open_webui_secret_key.txt; \
		); \
		echo "  generated .secrets/open_webui_secret_key.txt"; \
	fi
	@./scripts/check-mcp-streamable.sh
	@./scripts/check-host-ollama.sh
	docker compose \
		-f docker-compose.yml \
		-f docker-compose.host-ollama.yml \
		-f docker-compose.open-webui.yml \
		-f docker-compose.open-webui.host-ollama.yml \
		up -d open-webui

open-webui-down:
	docker compose -f docker-compose.yml -f docker-compose.open-webui.yml stop open-webui

open-webui-logs:
	docker compose -f docker-compose.yml -f docker-compose.open-webui.yml logs -f open-webui

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

typecheck: typecheck-indexer typecheck-mcp

typecheck-indexer: sync-indexer
	cd indexer && uv run mypy src

typecheck-mcp: sync-mcp
	cd mcp-server && uv run mypy src

# Remove all containers and volumes
# WARNING: This deletes your email index and Bridge credentials.
# You will need to run first-run again after this.
#
# The open-webui overlay is layered in so that an open-webui container and
# its named volume (created by `make open-webui-up`) are also removed.
# host-ollama overlays are intentionally NOT layered: that overlay does
# `ollama: !reset null` and would hide the in-stack ollama service from
# `down -v`, leaving its volume behind.
clean:
	@echo "WARNING: This will delete all containers, volumes, your email index,"
	@echo "         and Bridge credentials. You will need to run make first-run"
	@echo "         again to re-authenticate with Proton."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker compose -f docker-compose.yml -f docker-compose.open-webui.yml down -v
	@# Truncate secrets tied to wiped local state. anthropic_api_key.txt is the
	@# user's external Claude key and is intentionally preserved.
	@if [ -f .secrets/bridge_pass.txt ]; then : > .secrets/bridge_pass.txt; fi
	@if [ -f .secrets/open_webui_secret_key.txt ]; then : > .secrets/open_webui_secret_key.txt; fi
	@echo "All containers and volumes removed."
	@echo "Cleared .secrets/bridge_pass.txt and .secrets/open_webui_secret_key.txt."
	@echo "Re-run make first-run, then paste the new Bridge password into .secrets/bridge_pass.txt."
