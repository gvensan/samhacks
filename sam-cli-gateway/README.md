# SAM CLI Gateway Adapter

A terminal-based gateway adapter for [Solace Agent Mesh](https://github.com/SolaceLabs/solace-agent-mesh). Chat with SAM agents directly from your command line.

## Prerequisites

- Python 3.10+
- A running Solace Agent Mesh environment with broker access
- `solace-agent-mesh` package installed

## Installation

```bash
# From within your SAM project's virtual environment
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
| `NAMESPACE` | SAM namespace | *(required)* |
| `SOLACE_BROKER_URL` | Solace broker WebSocket URL | `ws://localhost:8008` |
| `SOLACE_BROKER_USERNAME` | Broker username | `default` |
| `SOLACE_BROKER_PASSWORD` | Broker password | `default` |
| `SOLACE_BROKER_VPN` | Broker VPN name | `default` |
| `CLI_GATEWAY_USER` | User identity for this session | `cli_gateway_user` |
| `CLI_GATEWAY_ID` | Unique gateway ID (for multi-instance) | `sam-cli-gw-01` |

Or edit `config.yaml` directly.

## Usage

```bash
sam run config.yaml
```

For multiple instances, set a unique gateway ID per terminal:

```bash
CLI_GATEWAY_ID=sam-cli-gw-01 sam run config.yaml
CLI_GATEWAY_ID=sam-cli-gw-02 sam run config.yaml  # second terminal
```

## Features

- **Interactive REPL** with Tab auto-completion for all slash commands
- **Markdown rendering** of agent responses (headings, code blocks, tables, bullets) via Rich
- **File upload** to send local files to agents for analysis
- **Artifact management** to list and download agent-created files
- **Session management** with deterministic sessions that survive restarts
- **Feedback** to rate agent responses (published to SAM's feedback topic)
- **Graceful exit** via `/quit` or Ctrl+D

## REPL Commands

| Command | Description |
|---|---|
| `/new` | Start a new conversation session (with confirmation) |
| `/agents` | List registered agents |
| `/upload <file> [message]` | Send a file to an agent |
| `/artifacts` | List agent-created files in this session |
| `/download [file] [path]` | Save artifacts (interactive multi-select if no file given) |
| `/feedback up\|down [comment]` | Rate the last response |
| `/help` | Show available commands |
| `/quit` | Exit the CLI |

## Adapter Config Options

Configured under `adapter_config` in `config.yaml`:

| Option | Default | Description |
|---|---|---|
| `prompt` | `"sam> "` | REPL prompt string |
| `user_id` | `"cli_gateway_user"` | User identity for this session |
| `show_status_updates` | `true` | Show agent progress updates |
