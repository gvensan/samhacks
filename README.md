# Solace Agent Mesh - Plugins

Project for [Solace Agent Mesh](https://github.com/SolaceLabs/solace-agent-mesh) — exploring custom entrypoints and agents.

## Projects

### [Solace Agent Mesh CLI Entrypoint](sam-cli-entrypoint/)

A terminal-based entrypoint for Solace Agent Mesh. Chat with agents directly from the command line with multi-session support, file uploads, artifact management, and markdown rendering.

**Key docs:**
- [README](sam-cli-entrypoint/README.md) — Setup and usage
- [Approach & Architecture](sam-cli-entrypoint/APPROACH.md) — Design decisions and architecture

## Quick Start

```bash
cd sam-cli-entrypoint
pip install -e .
cp .env.example .env   # edit with your broker details
sam run config.yaml
```

---
