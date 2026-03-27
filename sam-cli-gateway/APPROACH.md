# SAM CLI Gateway — Approach & Architecture

## Overview

The SAM CLI Gateway is a terminal-based gateway adapter for Solace Agent Mesh (SAM). It allows users to interact with SAM agents directly from the command line — sending messages, uploading files, managing artifacts, and providing feedback on responses.

It is built as a **SAM-compliant gateway plugin**, following the same adapter pattern used by the official Slack, MCP, REST, and Webhook gateways.

## Why a Gateway, Not an Agent?

In SAM's architecture, gateways and agents serve fundamentally different roles:

- **Agents** do work — they have skills, tools, and LLM access. They receive tasks, reason about them, and produce results.
- **Gateways** connect users to agents — they translate between an external platform and the A2A protocol. They don't think or reason; they route messages.

The CLI gateway is a **transport layer**. It takes typed input from a terminal, packages it as an A2A task, and delivers it to the orchestrator agent via the Solace broker. When agents respond, it renders the output to the terminal. This is identical in purpose to what the Slack gateway does for Slack or the REST gateway does for HTTP clients.

The CLI gateway makes zero LLM calls. All intelligence lives on the agent side.

## Where It Fits

```
┌─────────────────────────────────────────────────────────┐
│                   Solace Event Broker                    │
│                  (Topic-based routing)                   │
└──────┬──────────────┬──────────────────┬────────────────┘
       │              │                  │
  ┌────▼────┐   ┌─────▼─────┐    ┌──────▼──────┐
  │   CLI   │   │   Slack   │    │    REST     │
  │ Gateway │   │  Gateway  │    │   Gateway   │
  │  (new)  │   │           │    │             │
  └────┬────┘   └───────────┘    └─────────────┘
       │
  ┌────▼────┐
  │Terminal │
  │ (stdin/ │
  │ stdout) │
  └─────────┘
```

The CLI gateway is a **peer** to the other gateways — not a wrapper around them. It connects directly to the Solace broker as a first-class SAM gateway.

## Message Flow

```
User types: "What agents are available?"

  ┌──────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
  │ Terminal  │────▶│ CLI Gateway  │────▶│   Solace     │────▶│ Orchestrator │
  │  (stdin)  │     │  Adapter     │     │   Broker     │     │    Agent     │
  └──────────┘     └──────────────┘     └─────────────┘     └──────┬───────┘
                                                                    │
  ┌──────────┐     ┌──────────────┐     ┌─────────────┐           │
  │ Terminal  │◀────│ CLI Gateway  │◀────│   Solace     │◀──────────┘
  │ (stdout)  │     │  Adapter     │     │   Broker     │   Streamed response
  └──────────┘     └──────────────┘     └─────────────┘
```

1. User types a message in the terminal
2. Our adapter's `prepare_task()` converts it into a `SamTask`
3. SAM framework publishes a JSON-RPC 2.0 request to `SAM/a2a/v1/agent/request/OrchestratorAgent`
4. The orchestrator agent processes the request, potentially delegating to other agents
5. Streamed responses arrive on `SAM/a2a/v1/gateway/status/{gateway_id}/>`
6. Final response arrives on `SAM/a2a/v1/gateway/response/{gateway_id}/>`
7. SAM framework calls our `handle_text_chunk()` and `handle_task_complete()`
8. We render the response as styled markdown in the terminal

## What Our Code Does vs What SAM Handles

| Responsibility | Owner |
|---|---|
| Read user input from terminal | **Our adapter** |
| Render responses as markdown | **Our adapter** |
| Convert input to `SamTask` | **Our adapter** |
| Handle file uploads | **Our adapter** |
| Provide user auth claims | **Our adapter** |
| Manage session lifecycle | **Our adapter** |
| Submit user feedback | **Our adapter** |
| Broker connection & reconnection | SAM framework |
| A2A protocol (JSON-RPC 2.0) | SAM framework |
| Topic routing & subscriptions | SAM framework |
| Gateway card heartbeat (every 30s) | SAM framework |
| Artifact service & embed resolution | SAM framework |
| Agent discovery & registry | SAM framework |

## Adapter Pattern

We implement SAM's `GatewayAdapter` interface — the same pattern used by the Slack and MCP gateways:

```python
class CliGatewayAdapter(GatewayAdapter):

    # Lifecycle
    init()                    # Start REPL, print banner
    cleanup()                 # Cancel reader task, shutdown

    # Authentication
    extract_auth_claims()     # Return CLI user identity

    # Inbound: Terminal → SAM
    prepare_task()            # Convert user text + files → SamTask

    # Outbound: SAM → Terminal
    handle_text_chunk()       # Accumulate streamed text
    handle_status_update()    # Show progress indicator
    handle_file()             # Notify about file artifacts
    handle_task_complete()    # Render markdown response
    handle_error()            # Display errors
```

The only **required** method is `prepare_task()`. All response handlers are optional with sensible defaults.

## Sessions

Sessions are identified by a `session_id` that scopes both conversation history and artifacts.

- **Default session** — Deterministic ID based on gateway_id (e.g., `sam-cli-gw-01__default`). The same user reconnecting to the same gateway resumes their prior conversation and artifacts.
- **New session** (`/new`) — Generates a random ID prefixed with the gateway_id (e.g., `sam-cli-gw-01__cli-a1b2c3d4`). Starts a fresh conversation. The user is warned that they will lose access to current history and artifacts, and must confirm.
- **Artifact scoping** — Artifacts are stored under `{user_id}/{session_id}/` in the artifact service. Switching sessions makes prior artifacts inaccessible (though they remain on disk).

## Topics (Auto-Managed by SAM)

| Direction | Topic |
|---|---|
| Send to agent | `SAM/a2a/v1/agent/request/OrchestratorAgent` |
| Receive status updates | `SAM/a2a/v1/gateway/status/{gateway_id}/>` |
| Receive final responses | `SAM/a2a/v1/gateway/response/{gateway_id}/>` |
| Agent/gateway discovery | `SAM/a2a/v1/discovery/>` |

We don't manage any of these directly. The SAM framework subscribes, publishes, and routes automatically based on the `config.yaml`.

## Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Language | Python 3.10+ | SAM gateway framework requirement |
| Framework | `solace-agent-mesh` | Gateway base classes, broker comms, A2A protocol |
| Terminal UI | `prompt_toolkit` | REPL with Tab auto-completion for slash commands |
| Rendering | `rich` | Markdown rendering with Solace-themed styling |
| Config validation | Pydantic | `CliAdapterConfig` model |
| Config format | YAML | Standard SAM plugin config |
| Runtime | `sam run config.yaml` | SAM's built-in plugin runner |

**Note on GDK/Google ADK**: The Google Agent Development Kit (ADK) powers the **agent side** of SAM — LLM interaction, tool execution, state management. Gateways don't use ADK directly. We use the **A2A protocol** (the common messaging language between agents and gateways), which is handled by the SAM framework.

## Project Structure

```
cli-agent/
├── src/sam_cli_gateway_adapter/
│   ├── __init__.py
│   └── adapter.py                  # CliGatewayAdapter
├── config.yaml                      # SAM-compliant gateway config
├── pyproject.toml                   # Python package metadata (type=gateway)
├── .env.example                     # Environment variable template
├── .gitignore
├── README.md                        # Setup & usage instructions
├── APPROACH.md                      # This file
└── SAM_ARTIFACT_LIFECYCLE_GAP.md    # Framework issue writeup
```

## How to Run

```bash
# Install into SAM venv
pip install -e .

# Copy and configure environment
cp .env.example .env

# Run via SAM
sam run config.yaml
```

## Approach

The CLI gateway was built as a native SAM gateway plugin rather than a standalone client for one key reason: **SAM already solves the hard problems**. Broker connection management, A2A protocol handling, topic routing, gateway card heartbeats, artifact resolution, and agent discovery are all provided by the framework. Writing a custom broker client would mean reimplementing all of that — and staying compatible as SAM evolves.

By studying the existing gateway implementations (REST, Slack, MCP, Webhook) in `solace-agent-mesh-core-plugins`, we identified the `GatewayAdapter` pattern as the right abstraction. Each of these gateways follows the same contract: implement `prepare_task()` to translate inbound platform input into a `SamTask`, and implement response handlers to translate agent output back to the platform. The framework handles everything in between.

This meant the terminal is treated as just another platform — no different from Slack or a REST API from SAM's perspective. The adapter translates stdin to `SamTask` on the way in, and renders agent responses as styled markdown on the way out — fully compliant with SAM conventions, installable as a pip package, and runnable via `sam run config.yaml` like any other SAM plugin.

Built with **Claude Code** (Anthropic's agentic coding CLI).
