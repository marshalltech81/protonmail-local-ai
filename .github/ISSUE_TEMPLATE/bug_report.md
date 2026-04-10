mkdir -p .github/ISSUE_TEMPLATE

cat > .github/ISSUE_TEMPLATE/bug_report.md << 'EOF'
---
name: Bug report
about: Something isn't working
---

## Container

Which container is affected?
- [ ] protonmail-bridge
- [ ] mbsync
- [ ] indexer
- [ ] ollama
- [ ] mcp-server

## What happened

A clear description of what went wrong.

## What you expected

What should have happened instead.

## Logs
