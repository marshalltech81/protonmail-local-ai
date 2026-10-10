.PHONY: build build-nocache up down logs status requeue-dead reparse clean sync sync-indexer sync-mcp test test-indexer test-mcp test-mbsync test-mbsync-tls test-mbsync-layout test-compose test-validate-env test-make-status test-image-pins test-trivy-flags test-maven-checksums ppt-checksums trivy trivy-images restart-indexer backup-index restore-index test-index-backup baseline baseline-real-embedder eval-answers eval-answers-compare typecheck typecheck-indexer typecheck-mcp init-secrets validate-env help

# Per-checkout uv cache (#896): a cache shared between checkouts or
# worktrees running make targets at the same time fails with missing-file
# errors while another uv process writes to it. Ignored by git, Docker
# build contexts and Semgrep; `?=` lets an explicit UV_CACHE_DIR win.
UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
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
	@echo "  ppt-checksums Rewrite indexer/java/checksums/checksums.sha256 after an indexer/java/pom.xml change (downloads from Maven Central)"
	@echo "  up           Start the full stack (mbsync reaches the Bridge app on the host)"
	@echo "  down         Stop the full stack"
	@echo "  restart-indexer  Run validate-env, then restart the indexer (after editing config/authority.toml or config/identity.toml)"
	@echo "  logs         Tail logs from all containers"
	@echo "  status       Show container, privacy (LOCAL or REMOTE) and index status"
	@echo "  backup-index Copy the live index to BACKUP_DIR=<dir outside the checkout> (mode 700/600), checked with integrity_check"
	@echo "  restore-index Replace the index with BACKUP=<file from backup-index> (asks first; stops indexer and mcp-server, restarts mcp-server once the indexer verifies the index)"
	@echo "  requeue-dead Requeue dead-lettered indexing jobs (optional CLASS=retryable|permanent_source_failure|operator_action_required)"
	@echo "  reparse      Queue every indexed message to be parsed again in place, without embedding calls (except a thread left with no chunk embeds its subject once)"
	@echo "  sync         Sync local uv environments for indexer and mcp-server"
	@echo "  test         Run indexer, mcp-server, mbsync, Compose, validate-env, make status, index backup, image pin, Trivy flag and Maven checksum script tests locally"
	@echo "  typecheck    Run mypy over the indexer and mcp-server Python services"
	@echo "  test-indexer Run indexer unit tests only"
	@echo "  test-mcp     Run mcp-server unit tests only"
	@echo "  test-mbsync  Run mbsync entrypoint tests only"
	@echo "  test-mbsync-tls  Run the mbsync TLS check against a synthetic Bridge (needs Docker)"
	@echo "  test-mbsync-layout  Run the mbsync Maildir layout and UIDVALIDITY checks with synthetic stores (needs Docker)"
	@echo "  test-compose Run Compose rendering and merged-hardening tests"
	@echo "  test-validate-env  Run validate-env.sh and mcp-auth-headers.sh tests against synthetic fixtures"
	@echo "  test-index-backup  Run backup-index and restore-index tests against a fake docker (no daemon)"
	@echo "  test-make-status  Run make status tests against a fake docker (no daemon)"
	@echo "  test-image-pins  Check tls_check.sh pins the python image the indexer and mcp-server Dockerfiles build from, and docker.yml and tests.yml the same BuildKit image"
	@echo "  test-trivy-flags  Check that make trivy and the Trivy jobs in .github/workflows/security.yml and docker.yml agree (no Trivy install)"
	@echo "  test-maven-checksums  Check the indexer build and security.yml verify every Maven artifact against the committed checksums, which cover indexer/java/pom.xml (no Maven)"
	@echo "  trivy        Run the CI Trivy scans locally: dependency scans of indexer/ and mcp-server/, offline misconfig scan of the repository, then the image gates (needs trivy and the built images)"
	@echo "  trivy-images Run the CI Trivy image gates of .github/workflows/docker.yml on the built indexer, mcp-server and mbsync images (needs trivy, make build)"
	@echo "  baseline     Run the retrieval regression baseline (UPDATE=1 rewrites the rank snapshot)"
	@echo "  baseline-real-embedder  Opt-in retrieval floors on the synthetic corpus with the real EMBED_* model (sends synthetic text only; REAL_EMBED_ARGS=\"--repeats N --max-requests N --max-runtime-secs N --batch-size N\")"
	@echo "  eval-answers Opt-in answer-quality run of the intelligence tools on the synthetic corpus (calls INFERENCE_* and JUDGE_* providers)"
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

# Source commit baked into each image (the org.opencontainers.image.revision
# label and GIT_COMMIT) and logged at startup (#887): the short HEAD hash,
# plus -dirty when a tracked file is modified or an untracked file is not
# git-ignored (.env and .secrets/ are ignored, so they never count). Empty
# outside a checkout, which Compose turns into "unknown". Always taken from
# the checkout, so a GIT_COMMIT left in the shell or CI cannot replace it;
# label a build explicitly with GIT_COMMIT_OVERRIDE=<value>. Deferred (=),
# so git runs only for the build targets.
SOURCE_COMMIT = $(or $(GIT_COMMIT_OVERRIDE),$(shell head=$$(git rev-parse --short HEAD) && status=$$(git status --porcelain --untracked-files=normal) && printf '%s%s' "$$head" "$${status:+-dirty}"))

# Build all images from source
build:
	GIT_COMMIT=$(SOURCE_COMMIT) docker compose build

# Build all images from source with the BuildKit cache disabled.
# Use after a base-image tag refresh, when chasing a "stale layer"
# bug, or when you want to confirm a Dockerfile change actually
# rebuilds the layer you think it does. Slower than ``make build``.
# The indexer's Maven repository lives in a BuildKit cache mount, which
# --no-cache also starts empty, so the jars are downloaded again (#1070).
#
# Pass SERVICES=indexer (or any compose service name list) to scope
# the rebuild:
#
#   make build-nocache SERVICES="indexer mcp-server"
build-nocache:
	GIT_COMMIT=$(SOURCE_COMMIT) docker compose build --no-cache $(SERVICES)

# Rewrite indexer/java/checksums/checksums.sha256, the SHA-256 of every
# artifact Maven resolves for the .ppt reader, which the indexer build
# and the Trivy dependency scan check each artifact against (#1117).
# Run it after a change to indexer/java/pom.xml (a Dependabot bump) and
# commit the file with it. It builds the Dockerfile's ppt-checksums
# stage: the Maven a clean build installs (ppt-tools is rebuilt too, so
# a cached apt layer cannot record with an older Maven), from an empty
# repository and never from the layer cache, downloads from Maven
# Central and records.
ppt-checksums:
	docker build --target ppt-checksums --no-cache-filter ppt-tools,ppt-checksums-record --provenance=false --output type=local,dest=indexer/java/checksums indexer

validate-env:
	./scripts/validate-env.sh

# Start the full stack in detached mode
up: init-secrets validate-env
	docker compose up -d

# Restart the indexer after editing config/authority.toml or
# config/identity.toml, running the same preflight as `make up` first so
# a loosened file mode fails here.
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

# Show running containers and mailbox (sync + index) status. Fails when
# mcp-server is not running, when the check cannot run (its stderr is
# shown), or when the helper reports status=error, which carries only an
# exception type (#732, #733). An index that is not current still passes.
# The Privacy section reads the running mcp-server's settings and prints
# each layer's mode and endpoint host name only, never a key or a full
# URL; `local` must match `_HOST_LOCAL_HOSTS` in both services' main.py
# (#768).
status:
	@echo ""
	@echo "=== Containers ==="
	docker compose ps
	@running=$$(docker ps --quiet --filter 'name=^mcp-server$$' --filter status=running) || exit 1; \
	if [ -z "$$running" ]; then \
		echo "  MCP server is not running; start the stack with make up." >&2; \
		exit 1; \
	fi
	@echo ""
	@echo "=== Privacy ==="
	@docker exec mcp-server python -c \
		"import os, urllib.parse; \
		 local = {'127.0.0.1', '::1', 'localhost', 'host.docker.internal'}; \
		 defaults = {'anthropic': 'api.anthropic.com', 'openai': 'api.openai.com', 'cohere': 'api.cohere.com'}; \
		 env = lambda name, fallback='': os.environ.get(name, fallback).strip(); \
		 host = lambda mode, url: defaults.get(mode) if url.lower() == 'default' else urllib.parse.urlsplit(url).hostname; \
		 where = lambda mode, url: 'disabled' if mode == 'none' else ('LOCAL' if (h := host(mode, url)) in local else 'REMOTE') + ' (' + (h or 'unknown host') + ')'; \
		 layers = [(name, env(name.upper() + '_MODE', fallback).lower()) for name, fallback in (('embed', 'openai'), ('inference', 'none'), ('rerank', 'none'))]; \
		 [print(f'  {name:<10} {mode:<10} ' + where(mode, env(name.upper() + '_BASE_URL'))) for name, mode in layers]"
	@net=$$(docker inspect --format '{{range $$name, $$_ := .NetworkSettings.Networks}}{{$$name}}{{end}}' mcp-server) || exit 1; \
	internal=$$(docker network inspect --format '{{.Internal}}' "$$net") || exit 1; \
	if [ "$$internal" = true ]; then \
		echo "  No-egress overlay: active (app-net is internal)"; \
	else \
		echo "  No-egress overlay: not active (app-net can reach the internet)"; \
	fi
	@echo "  Results returned to a cloud-backed MCP client leave the host regardless."
	@echo ""
	@echo "=== Mailbox ==="
	docker exec mcp-server python -c \
		"import json, sys; \
		 from src.tools.system import get_mailbox_status; \
		 status = get_mailbox_status(); \
		 print(json.dumps(status, indent=2)); \
		 sys.exit(status['status'] != 'ok')"
	@echo ""

# Requeue dead-lettered indexing jobs once their cause is fixed.
# The running indexer drains them on its next pass.
requeue-dead:
	docker exec indexer python -m src.requeue_dead $(if $(CLASS),--class $(CLASS),)

# Queue every indexed message for an in-place reparse (#1078), the same
# statement a migration that needs one ends with. For recovery; the
# running indexer drains the jobs without embedding calls, except that a
# thread a drop of stale attachment occurrences leaves with no chunk embeds
# its subject once (#1375).
reparse:
	docker exec indexer python -m src.reparse

# Copy the live index while the stack runs (#1005), for example before
# deploying a schema change. The copy holds the whole mailbox, so it goes
# only to BACKUP_DIR, which must be outside the checkout; see
# scripts/backup-index.sh and docs/troubleshooting.md ("Back up and
# restore the index").
backup-index:
	BACKUP_DIR="$(BACKUP_DIR)" ./scripts/backup-index.sh

# Replace the index with a backup-index copy (#1005): asks for "yes",
# stops indexer and mcp-server, checks the copy and swaps it in, starts
# the indexer and prints its schema and embedder identity lines, then
# starts mcp-server once the indexer has verified the index.
restore-index:
	BACKUP="$(BACKUP)" ./scripts/restore-index.sh

# Run unit tests locally using uv
test: test-indexer test-mcp test-mbsync test-compose test-validate-env test-make-status test-index-backup test-image-pins test-trivy-flags test-maven-checksums

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

test-make-status:
	bash scripts/tests/make_status_test.sh

test-index-backup:
	bash scripts/tests/index_backup_test.sh

test-image-pins:
	bash scripts/tests/image_pin_test.sh

test-trivy-flags:
	bash scripts/tests/trivy_flags_test.sh

test-maven-checksums:
	bash scripts/tests/maven_checksums_test.sh

# The Trivy scans of .github/workflows/security.yml and the image gates
# of .github/workflows/docker.yml, locally (#1017, #1065): the
# dependency (vuln) scans of indexer/ and mcp-server/ and the offline
# misconfiguration scan of the repository (#1047), then the vuln scans
# of the built indexer, mcp-server and mbsync images (#977), with the
# workflows' severity, exit code, skip-dirs and ignore-unfixed;
# test-trivy-flags fails when the Makefile and a workflow drift. Every
# scan runs even when an earlier one fails, as in CI. The dependency
# and image scans pass --offline-scan=false explicitly: Trivy reads any
# option from a TRIVY_* variable, so an exported TRIVY_OFFLINE_SCAN
# would otherwise make them skip the dependencies not cached locally
# and still pass. TRIVY names the binary, and the targets warn when its
# version is not the one the workflows pin. trivy-images runs the image
# gates alone; they need the images `make build` produced, taken from
# `docker compose config --images` (the three services, named after
# the project: protonmail-local-ai in docker.yml, locally the directory
# name or COMPOSE_PROJECT_NAME), and fail, naming the image, when one
# is not built. The gates scan whatever `make build` last produced,
# not the checkout, and warn (without failing) when an image's
# revision label is not the checkout's SOURCE_COMMIT (#1103).
TRIVY ?= trivy
TRIVY_VERSION := v0.75.0
TRIVY_SEVERITY := CRITICAL,HIGH
TRIVY_MISCONFIG_SKIP_DIRS := .git,.ruff_cache,.pytest_cache,.venv,indexer/.venv,mcp-server/.venv,.uv-cache

# The lines trivy and trivy-images start with: the binary is present,
# and its version is the pinned one or a warning says so.
define trivy-preflight
	@command -v "$(TRIVY)" >/dev/null 2>&1 || { \
		echo "trivy not found: install Trivy $(TRIVY_VERSION) (brew install trivy, or https://trivy.dev/docs/getting-started/installation/), or set TRIVY=<path>" >&2; \
		exit 1; }
	@installed=$$("$(TRIVY)" --version | sed -n 's/^Version: *//p' | head -n 1); \
	if [ "v$$installed" != "$(TRIVY_VERSION)" ]; then \
		echo "warning: trivy $$installed is installed but CI pins $(TRIVY_VERSION); findings may differ" >&2; \
	fi
endef

# The image gates, as one shell fragment for a recipe that set
# `status=0` before it and exits with `$$status` after it: take the
# image names from docker compose, refuse (status 1, no scan) when an
# image is not built, else warn about each image whose
# org.opencontainers.image.revision label is missing or is not
# SOURCE_COMMIT (always when the checkout is -dirty, since its files
# may have changed since the build), then scan each image and keep
# going on a finding. The warning does not change the exit status.
define trivy-image-scans
	images=$$(docker compose config --images); \
	if [ -z "$$images" ]; then echo "docker compose config --images listed no image" >&2; exit 1; fi; \
	built=1; \
	for image in $$images; do \
		docker image inspect "$$image" >/dev/null 2>&1 || { echo "image $$image is not built: run make build first" >&2; built=0; }; \
	done; \
	if [ "$$built" -eq 1 ]; then \
		source='$(SOURCE_COMMIT)'; \
		for image in $$images; do \
			revision=$$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$$image"); \
			if [ -z "$$revision" ]; then \
				echo "warning: image $$image has no revision label (org.opencontainers.image.revision) and the checkout is $$source; it may be stale: run make build" >&2; \
			elif [ "$$revision" != "$$source" ] || [ "$${source%-dirty}" != "$$source" ]; then \
				echo "warning: image $$image has revision label $$revision but the checkout is $$source (a -dirty checkout always differs); it may be stale: run make build" >&2; \
			fi; \
		done; \
		for image in $$images; do \
			"$(TRIVY)" image --scanners vuln --severity $(TRIVY_SEVERITY) --exit-code 1 --ignore-unfixed --offline-scan=false "$$image" || status=1; \
		done; \
	else status=1; fi
endef

trivy:
	$(trivy-preflight)
	@status=0; \
	"$(TRIVY)" fs --scanners vuln --severity $(TRIVY_SEVERITY) --exit-code 1 --offline-scan=false indexer || status=1; \
	"$(TRIVY)" fs --scanners vuln --severity $(TRIVY_SEVERITY) --exit-code 1 --offline-scan=false mcp-server || status=1; \
	"$(TRIVY)" fs --scanners misconfig --severity $(TRIVY_SEVERITY) --exit-code 1 --offline-scan --skip-dirs $(TRIVY_MISCONFIG_SKIP_DIRS) . || status=1; \
	$(trivy-image-scans); \
	exit $$status

trivy-images:
	$(trivy-preflight)
	@status=0; \
	$(trivy-image-scans); \
	exit $$status

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

# Opt-in retrieval baseline with a real embedding model (#1439, stage 1
# of #1425): builds the synthetic baseline once per repeat with the
# EMBED_BASE_URL / EMBED_MODEL in the environment and the key read by
# the build from REAL_EMBED_SECRETS/embed_api_key.txt (default
# .secrets/; never an argument), then
# checks golden.json's real_embedder_floors for that model on every
# repeat and reports ranking flips, vector variation and spend. Only
# the synthetic corpus and questions are sent. Vectors are cached in
# REAL_EMBED_CACHE (git-ignored), so a rerun with nothing changed sends
# no requests. REAL_EMBED_ARGS passes --repeats, --max-requests,
# --max-runtime-secs and --batch-size; a run that reaches the request or
# runtime cap exits 3 (inconclusive), never a pass. It calls a paid provider, so it is
# never part of `make test` or CI.
REAL_EMBED_CACHE ?= $(CURDIR)/.real-embedder-cache
REAL_EMBED_SECRETS ?= $(CURDIR)/.secrets
baseline-real-embedder: sync-indexer sync-mcp
	@dir=$$(mktemp -d) && \
	( cd indexer && uv run python -m tests.baseline.real_embedder "$$dir/out" ../mcp-server/tests/baseline/golden.json --cache-dir "$(REAL_EMBED_CACHE)" --secrets-dir "$(REAL_EMBED_SECRETS)" $(REAL_EMBED_ARGS) ) && \
	( cd mcp-server && REAL_EMBED_DIR="$$dir/out" uv run pytest -q -s --no-cov tests/baseline/test_real_embedder_baseline.py ); \
	status=$$?; rm -rf "$$dir"; exit $$status

# Opt-in answer-quality evaluation of ask_mailbox, summarize_thread,
# extract_from_emails (#1137) and the experimental brief_issue and
# check_conclusion (#604, #656, #1240;
# the experimental tools are registered for the run only, whatever
# MCP_EXPERIMENTAL_TOOLS says): builds the synthetic baseline index,
# runs every case through the real handler of its tool with the
# INFERENCE_* answerer and the optional JUDGE_* judge, and writes a
# mode-600 report under EVAL_OUT (git-ignored by default). It calls the
# configured providers, so it is never part of `make test` or CI. EVAL_ARGS
# passes extra flags (e.g. `--case ask-roof-total --detail <path>`);
# relative --out/--detail paths resolve against the repository root, and
# the arguments are checked (--preflight) before the index is built. The
# preflight prints the planned provider calls and models and refuses a
# run over EVAL_MAX_CALLS (optional; e.g. `make eval-answers
# EVAL_MAX_CALLS=80`), so no index is built for a refused run (#839).
EVAL_OUT ?= $(CURDIR)/.answer-eval
eval-answers: sync-indexer sync-mcp
	@dir=$$(mktemp -d) && stamp=$$(date -u +%Y%m%dT%H%M%SZ) && \
	( cd mcp-server && uv run python -m tests.answer_eval run --preflight --index-dir "$$dir/out" --out "$(EVAL_OUT)/run-$$stamp.json" --path-base "$(CURDIR)" $(EVAL_ARGS) ) && \
	( cd indexer && uv run python -m tests.baseline.build "$$dir/out" ../mcp-server/tests/baseline/golden.json ../mcp-server/tests/answer_eval/cases.json >/dev/null ) && \
	( cd mcp-server && uv run python -m tests.answer_eval run --index-dir "$$dir/out" --out "$(EVAL_OUT)/run-$$stamp.json" --path-base "$(CURDIR)" --source-commit "$$(git rev-parse HEAD)" $(EVAL_ARGS) ); \
	status=$$?; rm -rf "$$dir"; exit $$status

# Compare two answer-evaluation reports: per-case and per-category changes.
eval-answers-compare: sync-mcp
	@if [ -z "$(BASELINE)" ] || [ -z "$(CANDIDATE)" ]; then \
		echo "usage: make eval-answers-compare BASELINE=<run.json> CANDIDATE=<run.json>"; exit 2; fi
	cd mcp-server && uv run python -m tests.answer_eval compare "$(abspath $(BASELINE))" "$(abspath $(CANDIDATE))" --path-base "$(CURDIR)" $(EVAL_ARGS)

typecheck: typecheck-indexer typecheck-mcp

typecheck-indexer: sync-indexer
	cd indexer && uv run mypy src

typecheck-mcp: sync-mcp
	cd mcp-server && uv run mypy src tests/answer_eval

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
