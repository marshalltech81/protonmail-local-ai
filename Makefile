.PHONY: build build-nocache up down logs first-run update status requeue-dead clean sync sync-indexer sync-mcp test test-indexer test-mcp test-mbsync test-bridge test-bridge-smoke test-validate-env baseline typecheck typecheck-indexer typecheck-mcp bridge-patch-check bridge-smoke bridge-upgrade-check init-secrets validate-env help

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
	@echo "  requeue-dead Requeue dead-lettered indexing jobs (optional CLASS=retryable|permanent_source_failure|operator_action_required)"
	@echo "  sync         Sync local uv environments for indexer and mcp-server"
	@echo "  test         Run indexer, mcp-server, mbsync, Bridge entrypoint, bridge-smoke and validate-env script tests locally"
	@echo "  typecheck    Run mypy over the indexer and mcp-server Python services"
	@echo "  test-indexer Run indexer unit tests only"
	@echo "  test-mcp     Run mcp-server unit tests only"
	@echo "  test-mbsync  Run mbsync entrypoint tests only"
	@echo "  test-bridge  Run Bridge entrypoint tests only"
	@echo "  test-bridge-smoke  Run bridge-smoke.sh pass/fail tests (no Docker)"
	@echo "  test-validate-env  Run validate-env.sh tests against synthetic .env fixtures"
	@echo "  baseline     Run the retrieval regression baseline (UPDATE=1 rewrites the rank snapshot)"
	@echo "  clean        Remove all containers and volumes (destructive)"
	@echo ""

# Create placeholder secret files required by Docker Compose.
# Run this once during initial setup before make first-run or make up.
# Each layer (inference / embed / rerank) has one secret file. Each
# file must exist with mode 600 so the docker-compose ``secrets:``
# reference resolves cleanly. The file must be NON-EMPTY when its
# matching layer is enabled (EMBED_MODE is always enabled;
# INFERENCE_MODE != none; RERANK_MODE == cohere). For unauthenticated
# host-side servers (LM Studio, vLLM, mlx_lm.server) write any
# placeholder string (e.g. ``unauthenticated``). Leave the file empty
# only when the matching layer's *_MODE=none.
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
	@if [ ! -f .secrets/inference_api_key.txt ]; then \
		printf '' > .secrets/inference_api_key.txt; \
		chmod 600 .secrets/inference_api_key.txt; \
		echo "  created .secrets/inference_api_key.txt (empty — must be non-empty when INFERENCE_MODE != none; use any placeholder for unauthenticated host servers)"; \
	else \
		echo "  .secrets/inference_api_key.txt already exists, skipping"; \
	fi
	@if [ ! -f .secrets/embed_api_key.txt ]; then \
		printf '' > .secrets/embed_api_key.txt; \
		chmod 600 .secrets/embed_api_key.txt; \
		echo "  created .secrets/embed_api_key.txt (empty — must be non-empty before make up; use any placeholder for unauthenticated host servers)"; \
	else \
		echo "  .secrets/embed_api_key.txt already exists, skipping"; \
	fi
	@if [ ! -f .secrets/rerank_api_key.txt ]; then \
		printf '' > .secrets/rerank_api_key.txt; \
		chmod 600 .secrets/rerank_api_key.txt; \
		echo "  created .secrets/rerank_api_key.txt (empty — fill in for RERANK_MODE=cohere)"; \
	else \
		echo "  .secrets/rerank_api_key.txt already exists, skipping"; \
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
# printed by `info` from being written to Docker log files on the host, and
# BRIDGE_FORCE_CLI=true, so the interactive CLI opens even when the volume
# already holds a vault (a retried, unfinished login, or to run `info` again).
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
# 1. Bump BRIDGE_VERSION and BRIDGE_COMMIT in .env (see .env.example)
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

# Show running containers and mailbox (sync + index) status
status:
	@echo ""
	@echo "=== Containers ==="
	docker compose ps
	@echo ""
	@echo "=== Mailbox ==="
	docker exec mcp-server python -c \
		"from src.tools.system import get_mailbox_status; \
		 import json; print(json.dumps(get_mailbox_status(), indent=2))" \
		2>/dev/null || echo "  MCP server not running or index not ready."
	@echo ""

# Requeue dead-lettered indexing jobs once their cause is fixed.
# The running indexer drains them on its next pass.
requeue-dead:
	docker exec indexer python -m src.requeue_dead $(if $(CLASS),--class $(CLASS),)

# Run unit tests locally using uv
test: test-indexer test-mcp test-mbsync test-bridge test-bridge-smoke test-validate-env

test-indexer: sync-indexer
	cd indexer && uv run pytest -q

test-mcp: sync-mcp
	cd mcp-server && uv run pytest -q

test-mbsync:
	bash mbsync/tests/entrypoint_test.sh

test-bridge:
	bash bridge/tests/entrypoint_test.sh

test-bridge-smoke:
	bash scripts/tests/bridge_smoke_test.sh

test-validate-env:
	bash scripts/tests/validate_env_test.sh

# Retrieval regression baseline. Step 1 indexes the synthetic mailbox with
# the real indexer and a hashed embedder; step 2 checks the golden
# questions and the rank snapshot in mcp-server. UPDATE=1 rewrites
# mcp-server/tests/baseline/snapshot.json after an intended ranking change.
baseline: sync-indexer sync-mcp
	@dir=$$(mktemp -d) && \
	( cd indexer && uv run python -m tests.baseline.build "$$dir/out" ../mcp-server/tests/baseline/golden.json ) && \
	( cd mcp-server && BASELINE_DIR="$$dir/out" uv run pytest -q --no-cov tests/baseline $(if $(UPDATE),--update-baseline) ); \
	status=$$?; rm -rf "$$dir"; exit $$status

typecheck: typecheck-indexer typecheck-mcp

typecheck-indexer: sync-indexer
	cd indexer && uv run mypy src

typecheck-mcp: sync-mcp
	cd mcp-server && uv run mypy src

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
	@# Truncate secrets tied to wiped local state. Inference provider keys are
	@# external credentials and are intentionally preserved.
	@if [ -f .secrets/bridge_pass.txt ]; then : > .secrets/bridge_pass.txt; fi
	@echo "All containers and volumes removed."
	@echo "Cleared .secrets/bridge_pass.txt."
	@echo "Re-run make first-run, then paste the new Bridge password into .secrets/bridge_pass.txt."
