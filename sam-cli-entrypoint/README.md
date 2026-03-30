# Solace Agent Mesh CLI Entrypoint

A terminal-based entrypoint for [Solace Agent Mesh](https://github.com/SolaceLabs/solace-agent-mesh). Chat with Solace Agent Mesh agents directly from your command line.

## Prerequisites

- Python 3.10+
- A running Solace Agent Mesh environment with broker access
- `solace-agent-mesh` package installed

## Installation

```bash
# From within your Solace Agent Mesh project's virtual environment
pip install -e .
```

## Configuration

Copy the example environment file and edit it:

```bash
cp .env.example .env
```

Required environment variables:

| Variable | Description | Default |
|---|---|---|
| `NAMESPACE` | Solace Agent Mesh namespace | *(required)* |
| `SOLACE_BROKER_URL` | Solace broker WebSocket URL | `ws://localhost:8008` |
| `SOLACE_BROKER_USERNAME` | Broker username | `default` |
| `SOLACE_BROKER_PASSWORD` | Broker password | `default` |
| `SOLACE_BROKER_VPN` | Broker VPN name | `default` |
| `CLI_ENTRYPOINT_USER` | User identity for this session | `cli_entrypoint_user` |
| `CLI_ENTRYPOINT_ID` | Unique entrypoint ID (for multi-instance) | `sam-cli-ep-01` |
| `SAM_CLI_SESSIONS_DIR` | Directory for session index file | `~/.sam-cli-entrypoint` |

Or edit `config.yaml` directly.

## Usage

```bash
sam run config.yaml
```

For multiple instances, set a unique entrypoint ID per terminal:

```bash
CLI_ENTRYPOINT_ID=sam-cli-ep-01 sam run config.yaml
CLI_ENTRYPOINT_ID=sam-cli-ep-02 sam run config.yaml  # second terminal
```

## Features

- **Interactive REPL** with Tab auto-completion for all slash commands
- **Markdown rendering** of agent responses (headings, code blocks, tables, bullets) via Rich
- **Multi-session support** — create, name, switch between, and manage multiple concurrent sessions that persist across restarts
- **File upload** to send local files to agents for analysis
- **Artifact management** to list and download agent-created files (scoped per session)
- **Graceful exit** via `/quit` or Ctrl+D

## REPL Commands

### Sessions

| Command | Description |
|---|---|
| `/new [label]` | Start a new session, optionally named |
| `/sessions` | List all sessions with message counts and last active time |
| `/switch <label\|id>` | Switch to an existing session (Solace Agent Mesh reloads full history automatically) |
| `/rename <label>` | Rename the current session |
| `/delete <label\|id>` | Remove a session from the local index (conversation history and artifacts remain on Solace Agent Mesh) |

### General

| Command | Description |
|---|---|
| `/agents` | List registered agents |
| `/upload <file> [message]` | Send a file to an agent |
| `/artifacts` | List agent-created files in this session |
| `/download [file] [path]` | Save artifacts (interactive multi-select if no file given) |
| `/help` | Show available commands |
| `/quit` | Exit the CLI |

### How Sessions Work

Sessions are identified by an internal `session_id` that scopes both conversation history and artifacts on Solace Agent Mesh's side. The entrypoint maintains a local index (`~/.sam-cli-entrypoint/sessions.json`) that maps human-readable labels to session IDs and tracks metadata (message counts, timestamps).

- **Solace Agent Mesh is the source of truth** for conversation history and artifacts. The local index only stores labels and stats.
- **Switching sessions** changes the `session_id` passed to Solace Agent Mesh. The agent automatically picks up the full history for that session — no manual reload needed.
- **Deleting a session** removes it from the local index only. Solace Agent Mesh has no entrypoint-facing API to delete server-side history.
- **Sessions persist across restarts.** On launch, the entrypoint restores the last active session from the index.

## Config Options

Configured under `adapter_config` in `config.yaml`:

| Option | Default | Description |
|---|---|---|
| `prompt` | `"sam> "` | REPL prompt string |
| `user_id` | `"cli_entrypoint_user"` | User identity for this session |
| `show_status_updates` | `true` | Show agent progress updates |
