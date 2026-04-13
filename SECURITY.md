# Security Policy

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

This project handles email credentials and decrypted message content.
Please report vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/marshalltech81/protonmail-local-ai/security/advisories/new).

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Which component is affected (bridge, mbsync, indexer, mcp-server)

You will receive a response within 7 days.

## Scope

In scope:
- Credential exposure (Bridge password, API keys)
- Container escape or privilege escalation
- Unintended network exposure of email data or credentials
- Vulnerabilities in the MCP tool interface that could exfiltrate data

Out of scope:
- Vulnerabilities in ProtonBridge itself — report those upstream to
  [Proton's vulnerability disclosure policy](https://proton.me/security/vulnerability-disclosure)
- Vulnerabilities in Ollama — report to the
  [Ollama project](https://github.com/ollama/ollama/security)
- Theoretical issues with no practical exploit path

## Security Design

- All email stays in Docker volumes — nothing leaves the machine by default
- The only optional external call is to the Claude API (`LLM_MODE=cloud`),
  which is opt-in and sends only retrieved email excerpts
- Bridge credentials are stored as a Docker Compose secret
  (`.secrets/bridge_pass.txt`, mode 600), never in environment variables
- The Bridge GPG key intentionally has no passphrase so the container can
  restart unattended; the protection model is volume isolation, strict
  filesystem permissions, and host-level disk encryption/backups rather than
  an interactive unlock prompt inside the container
- Only one port is exposed to the host: `127.0.0.1:3000` (MCP server,
  localhost only — not accessible from the network)
- All containers run on an isolated Docker bridge network
