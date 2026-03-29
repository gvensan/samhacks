# SAM Hacks

Hackathon projects for [Solace Agent Mesh](https://github.com/SolaceLabs/solace-agent-mesh) (SAM) — exploring custom entrypoints and agents.

## Projects

### [SAM CLI Entrypoint](sam-cli-gateway/)

A terminal-based entrypoint adapter for SAM. Chat with agents directly from the command line with multi-session support, file uploads, artifact management, and markdown rendering.

**Key docs:**
- [README](sam-cli-gateway/README.md) — Setup and usage
- [Approach & Architecture](sam-cli-gateway/APPROACH.md) — Design decisions and how it fits into SAM

## Quick Start

```bash
cd sam-cli-gateway
pip install -e .
cp .env.example .env   # edit with your broker details
sam run config.yaml
```

---
