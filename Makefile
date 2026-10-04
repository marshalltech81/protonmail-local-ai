.PHONY: build build-nocache up down logs status requeue-dead clean sync sync-indexer sync-mcp test test-indexer test-mcp test-mbsync test-mbsync-tls test-mbsync-layout test-compose test-validate-env restart-indexer baseline eval-answers eval-answers-compare typecheck typecheck-indexer typecheck-mcp init-secrets validate-env help

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
	@echo "  up           Start the full stack (mbsync reaches the Bridge app on the host)"
	@echo "  down         Stop the full stack"
	@echo "  restart-indexer  Run validate-env, then restart the indexer (after editing config/authority.toml)"
	@echo "  logs         Tail logs from all containers"
	@echo "  status       Show container and index status"
	@echo "  requeue-dead Requeue dead-lettered indexing jobs (optional CLASS=retryable|permanent_source_failure|operator_action_required)"
	@echo "  sync         Sync local uv environments for indexer and mcp-server"
	@echo "  test         Run indexer, mcp-server, mbsync, Compose and validate-env script tests locally"
	@echo "  typecheck    Run mypy over the indexer and mcp-server Python services"
	@echo "  test-indexer Run indexer unit tests only"
	@echo "  test-mcp     Run mcp-server unit tests only"
	@echo "  test-mbsync  Run mbsync entrypoint tests only"
	@echo "  test-mbsync-tls  Run the mbsync TLS check against a synthetic Bridge (needs Docker)"
	@echo "  test-mbsync-layout  Run the mbsync Maildir layout and UIDVALIDITY checks with synthetic stores (needs Docker)"
	@echo "  test-compose Run Compose rendering and merged-hardening tests"
	@echo "  test-validate-env  Run validate-env.sh and mcp-auth-headers.sh tests against synthetic fixtures"
	@echo "  baseline     Run the retrieval regression baseline (UPDATE=1 rewrites the rank snapshot)"
	@echo "  eval-answers Opt-in ask_mailbox answer-quality run on the synthetic corpus (calls INFERENCE_* and JUDGE_* providers)"
	@echo "  eval-answers-compare  Compare two answer-evaluation reports (BASELINE=... CANDIDATE=...)"
	@echo "  clean        Remove all containers and volumes (destructive)"
	@echo ""

# Create placeholder secret files required by Docker Compose.
# Run this once during initial setup before make up.
# Each layer (inference / embed / rerank) has one secret file. Each
# file must exist with mode 600 so the docker-compose ``secrets:``
# reference resolves cleanly. The file must be NON-EMPTY when its
# matching layer is enabled (EMBED_MODE is always enabled;
# INFERENCE_MODE != none; RERANK_MODE == cohere). For unauthenticated
# host-side servers (LM Studio, vLLM, mlx_lm.server) write any
# placeholder string (e.g. ``unauthenticated``). Leave the file empty
# only when the matching layer's *_MODE=none.
# The MCP bearer token (.secrets/mcp_auth_token.txt) is generated here
# with openssl when absent; every /mcp request must send it.
init-secrets:
	@mkdir -p .secrets
	@chmod 700 .secrets
	@if [ ! -f .secrets/bridge_pass.txt ]; then \
		printf '' > .secrets/bridge_pass.txt; \
		chmod 600 .secrets/bridge_pass.txt; \
		echo "  created .secrets/bridge_pass.txt (placeholder — fill in with the Bridge app's IMAP password)"; \
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
	@if [ ! -f .secrets/mcp_auth_token.txt ]; then \
		(umask 077 && openssl rand -hex 32 > .secrets/mcp_auth_token.txt) || { rm -f .secrets/mcp_auth_token.txt; exit 1; }; \
		chmod 600 .secrets/mcp_auth_token.txt; \
		echo "  created .secrets/mcp_auth_token.txt (random MCP bearer token; MCP clients send it as Authorization: Bearer <token>)"; \
	else \
		echo "  .secrets/mcp_auth_token.txt already exists, skipping"; \
	fi

# Build all images from source
build:
	docker compose build

# Build all images from source with the BuildKit cache disabled.
# Use after a base-image tag refresh, when chasing a "stale layer"
# bug, or when you want to confirm a Dockerfile change actually
# rebuilds the layer you think it does. Slower than ``make build``.
#
# Pass SERVICES=indexer (or any compose service name list) to scope
# the rebuild:
#
#   make build-nocache SERVICES="indexer mcp-server"
build-nocache:
	docker compose build --no-cache $(SERVICES)

validate-env:
	./scripts/validate-env.sh

# Start the full stack in detached mode
up: init-secrets validate-env
	docker compose up -d

# Restart the indexer after editing config/authority.toml, running the
# same preflight as `make up` first so a loosened file mode fails here.
# `restart` keeps each container's existing configuration, so the
# hardened overlay stays in effect if the stack was started with it.
restart-indexer: validate-env
	docker compose restart indexer

# Stop everything
down:
	docker compose down

# Tail logs across all containers
logs:
	docker compose logs -f

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
test: test-indexer test-mcp test-mbsync test-compose test-validate-env

test-indexer: sync-indexer
	cd indexer && uv run pytest -q

test-mcp: sync-mcp
	cd mcp-server && uv run pytest -q

test-mbsync:
	bash mbsync/tests/entrypoint_test.sh

test-mbsync-tls:
	bash mbsync/tests/tls_check.sh

test-mbsync-layout:
	bash mbsync/tests/layout_check.sh

test-compose:
	bash scripts/tests/compose_test.sh

test-validate-env:
	bash scripts/tests/validate_env_test.sh
	bash scripts/tests/mcp_auth_headers_test.sh

# Retrieval regression baseline. Step 1 indexes the synthetic mailbox with
# the real indexer and a hashed embedder; step 2 checks the golden
# questions and the rank snapshot in mcp-server. UPDATE=1 rewrites
# mcp-server/tests/baseline/snapshot.json after an intended ranking change;
# any other value (UPDATE=0, UPDATE=no) only checks it.
baseline: sync-indexer sync-mcp
	@dir=$$(mktemp -d) && \
	( cd indexer && uv run python -m tests.baseline.build "$$dir/out" ../mcp-server/tests/baseline/golden.json ../mcp-server/tests/answer_eval/cases.json ) && \
	( cd mcp-server && BASELINE_DIR="$$dir/out" uv run pytest -q --no-cov tests/baseline $(if $(filter 1,$(UPDATE)),--update-baseline) ); \
	status=$$?; rm -rf "$$dir"; exit $$status

# Opt-in answer-quality evaluation of ask_mailbox (#604): builds the
# synthetic baseline index, runs every case through the real handler with
# the INFERENCE_* answerer and the optional JUDGE_* judge, and writes a
# mode-600 report under EVAL_OUT (git-ignored by default). It calls the
# configured providers, so it is never part of `make test` or CI. EVAL_ARGS
# passes extra flags (e.g. `--case ask-roof-total --detail <path>`).
EVAL_OUT ?= $(CURDIR)/.answer-eval
eval-answers: sync-indexer sync-mcp
	@dir=$$(mktemp -d) && stamp=$$(date -u +%Y%m%dT%H%M%SZ) && \
	( cd indexer && uv run python -m tests.baseline.build "$$dir/out" ../mcp-server/tests/baseline/golden.json ../mcp-server/tests/answer_eval/cases.json >/dev/null ) && \
	( cd mcp-server && uv run python -m tests.answer_eval run --index-dir "$$dir/out" --out "$(EVAL_OUT)/run-$$stamp.json" --source-commit "$$(git rev-parse HEAD)" $(EVAL_ARGS) ); \
	status=$$?; rm -rf "$$dir"; exit $$status

# Compare two answer-evaluation reports: per-case and per-category changes.
eval-answers-compare: sync-mcp
	@if [ -z "$(BASELINE)" ] || [ -z "$(CANDIDATE)" ]; then \
		echo "usage: make eval-answers-compare BASELINE=<run.json> CANDIDATE=<run.json>"; exit 2; fi
	cd mcp-server && uv run python -m tests.answer_eval compare "$(abspath $(BASELINE))" "$(abspath $(CANDIDATE))" $(EVAL_ARGS)

typecheck: typecheck-indexer typecheck-mcp

typecheck-indexer: sync-indexer
	cd indexer && uv run mypy src

typecheck-mcp: sync-mcp
	cd mcp-server && uv run mypy src

# Remove all containers and volumes
# WARNING: This deletes the local Maildir, your email index and mbsync's
# pinned Bridge certificate. The Bridge app on the host keeps its own
# login, so .secrets/ is left as it is; make up then syncs from scratch.
#
clean:
	@echo "WARNING: This will delete all containers and volumes: the local"
	@echo "         Maildir, your email index and the pinned Bridge certificate."
	@echo "         The next make up downloads and indexes all mail again."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker compose down -v
	@echo "All containers and volumes removed."
