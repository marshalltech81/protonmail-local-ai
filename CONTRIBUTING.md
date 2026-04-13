# Contributing

Thanks for contributing to `protonmail-local-ai`.

## Before you start

- Read [AGENTS.md](AGENTS.md) for repository guardrails and commit conventions.
- Read [PLAN.md](PLAN.md) for current priorities and active work.
- Read the relevant docs in `docs/` before changing setup, TLS, Bridge behavior, indexing, or MCP tools.

## Local workflow

1. Copy `.env.example` to `.env`.
2. Keep secrets out of git. Never commit `.env`, `.secrets/bridge_pass.txt`, or ad hoc key material.
3. Install `uv` locally for Python work in `indexer/` and `mcp-server/`.
4. Make focused changes with clear commit boundaries.
5. Run the relevant checks before opening a pull request.

## Recommended checks

- `pre-commit run --all-files`
- `cd indexer && uv sync --locked --dev && uv run pytest -q`
- `cd mcp-server && uv sync --locked`
- `docker compose config --quiet`
- `docker compose build protonmail-bridge mbsync indexer mcp-server`
- `make bridge-upgrade-check` for changes that touch `bridge/`, `BRIDGE_VERSION`, or Bridge build/patch logic

If a change affects only one area, run the smallest relevant subset and explain what you ran in the pull request.

## Pull requests

- Keep pull requests narrow and explain why the change is needed.
- Update docs when behavior changes.
- Preserve privacy-first, local-first defaults.
- Do not weaken secret handling or broaden network exposure without explicit approval.

## Commit messages

Follow the repository's lowercase Conventional Commit style:

- `fix(bridge): patch TLS cert SAN for protonmail-bridge`
- `docs(setup): add mbsync verification steps`
- `chore(pre-commit): add detect-secrets baseline`

## Reporting security issues

Do not open public issues for vulnerabilities. Use the private reporting flow in [SECURITY.md](SECURITY.md).
