# Solace Agent Mesh CLI Entrypoint — Approach & Architecture

## Overview

The Solace Agent Mesh CLI Entrypoint is a terminal-based entrypoint for Solace Agent Mesh. It allows users to interact with Solace Agent Mesh agents directly from the command line — sending messages, uploading files, managing artifacts, and providing feedback on responses.

It is built as a **Solace Agent Mesh-compliant entrypoint plugin**, following the same `BaseGatewayApp` + `BaseGatewayComponent` pattern used by Solace Agent Mesh's built-in WebUI entrypoint.

## Why an Entrypoint, Not an Agent?

In Solace Agent Mesh's architecture, entrypoints and agents serve fundamentally different roles:

- **Agents** do work — they have skills, tools, and LLM access. They receive tasks, reason about them, and produce results.
- **Entrypoints** connect users to agents — they translate between an external platform and the A2A protocol. They don't think or reason; they route messages.

The CLI entrypoint is a **transport layer**. It takes typed input from a terminal, packages it as an A2A task, and delivers it to the orchestrator agent via the Solace broker. When agents respond, it renders the output to the terminal. This is identical in purpose to what the Slack entrypoint does for Slack or the REST entrypoint does for HTTP clients.

The CLI entrypoint makes zero LLM calls. All intelligence lives on the agent side.

## Where It Fits

```
┌───────────────────────────────────────────────────────────────┐
│                      Solace Event Broker                      │
│                     (Topic-based routing)                     │
└───────┬───────────────────┬───────────────────┬───────────────┘
        │                   │                   │
  ┌─────▼─────┐       ┌─────▼─────┐       ┌─────▼─────┐
  │    CLI    │       │   Slack   │       │   REST    │
  │ Entrypoint│       │ Entrypoint│       │ Entrypoint│
  └─────┬─────┘       └───────────┘       └───────────┘
        │
  ┌─────▼─────┐
  │ Terminal  │
  │ (stdin/   │
  │ stdout)   │
  └───────────┘
```

The CLI entrypoint is a **peer** to the other entrypoints — not a wrapper around them. It connects directly to the Solace broker as a first-class Solace Agent Mesh entrypoint.

## Message Flow

```
User types: "What agents are available?"

  ┌───────────┐     ┌────────────────┐     ┌──────────────┐     ┌──────────────┐
  │ Terminal  │────▶│ CLI Entrypoint │────▶│    Solace    │────▶│ Orchestrator │
  │  (stdin)  │     │   Component    │     │    Broker    │     │    Agent     │
  └───────────┘     └────────────────┘     └──────────────┘     └──────┬───────┘
                                                                       │
  ┌───────────┐     ┌────────────────┐     ┌──────────────┐            │
  │ Terminal  │◀────│ CLI Entrypoint │◀────│    Solace    │◀───────────┘
  │ (stdout)  │     │   Component    │     │    Broker    │   Streamed response
  └───────────┘     └────────────────┘     └──────────────┘
```

1. User types a message in the terminal
2. Our component's `_translate_external_input()` converts it into A2A parts
3. `submit_a2a_task()` publishes a JSON-RPC 2.0 request to `SAM/a2a/v1/agent/request/OrchestratorAgent`
4. The orchestrator agent processes the request, potentially delegating to other agents
5. Streamed responses arrive on `SAM/a2a/v1/gateway/status/{gateway_id}/>`
6. Final response arrives on `SAM/a2a/v1/gateway/response/{gateway_id}/>`
7. Solace Agent Mesh framework calls our `_send_update_to_external()` and `_send_final_response_to_external()`
8. We render the response as styled markdown in the terminal

## What Our Code Does vs What Solace Agent Mesh Handles

| Responsibility | Owner |
|---|---|
| Read user input from terminal | **Our component** |
| Render responses as markdown | **Our component** |
| Convert input to A2A parts | **Our component** |
| Handle file uploads | **Our component** |
| Provide user auth claims | **Our component** |
| Session labels, switching, and local index | **Our component** |
| Submit user feedback | **Our component** |
| Conversation history per session | Solace Agent Mesh framework |
| Artifact storage and retrieval per session | Solace Agent Mesh framework |
| Broker connection & reconnection | Solace Agent Mesh framework |
| A2A protocol (JSON-RPC 2.0) | Solace Agent Mesh framework |
| Topic routing & subscriptions | Solace Agent Mesh framework |
| Entrypoint card heartbeat (every 30s) | Solace Agent Mesh framework |
| Artifact service & embed resolution | Solace Agent Mesh framework |
| Agent discovery & registry | Solace Agent Mesh framework |

## Component Pattern

We implement Solace Agent Mesh's `BaseGatewayApp` + `BaseGatewayComponent` pattern — the same architecture used by the built-in WebUI (HTTP/SSE) entrypoint:

```python
class CliEntrypointApp(BaseGatewayApp):
    def _get_gateway_component_class(self):
        return CliEntrypointComponent

class CliEntrypointComponent(BaseGatewayComponent):

    # Lifecycle
    _start_listener()                    # Start REPL, print banner
    _stop_listener()                     # Cancel reader task, shutdown

    # Authentication
    _extract_initial_claims()            # Return CLI user identity

    # Inbound: Terminal → Solace Agent Mesh
    _translate_external_input()          # Convert user text → A2A parts + context
    # (submit_a2a_task() inherited from BaseGatewayComponent)

    # Outbound: Solace Agent Mesh → Terminal
    _send_update_to_external()           # Handle streaming text + status updates
    _send_final_response_to_external()   # Render markdown response
    _send_error_to_external()            # Display errors
```

All seven abstract methods must be implemented. The base class handles broker connections, A2A protocol, topic routing, artifact resolution, and agent discovery.

## Sessions

Sessions are identified by a `session_id` that scopes both conversation history and artifacts on Solace Agent Mesh's side.

- **Default session** — Deterministic ID based on gateway_id (e.g., `sam-cli-ep-01__default`). Automatically created and registered on first launch.
- **Named sessions** (`/new [label]`) — Generate a random ID prefixed with the gateway_id (e.g., `sam-cli-ep-01__cli-a1b2c3d4`). Optionally named with a human-readable label for easy switching.
- **Session switching** (`/switch <label|id>`) — Changes the `session_id` passed to `submit_a2a_task()`. Solace Agent Mesh automatically reloads full conversation history for that session — no client-side replay or injection needed.
- **Artifact scoping** — Artifacts are stored under `{user_id}/{session_id}/` in the artifact service. Each session has its own artifact namespace, managed entirely by Solace Agent Mesh.
- **Local session index** — A `SessionStore` persists session metadata (labels, message counts, timestamps) to `~/.sam-cli-entrypoint/sessions.json`. This is a lightweight address book — Solace Agent Mesh is the source of truth for history and artifacts.
- **Deletion** — `/delete` removes a session from the local index only. Solace Agent Mesh currently has no entrypoint-facing API to delete server-side session history or artifacts.

## Topics (Auto-Managed by Solace Agent Mesh)

| Direction | Topic |
|---|---|
| Send to agent | `SAM/a2a/v1/agent/request/OrchestratorAgent` |
| Receive status updates | `SAM/a2a/v1/gateway/status/{gateway_id}/>` |
| Receive final responses | `SAM/a2a/v1/gateway/response/{gateway_id}/>` |
| Agent/entrypoint discovery | `SAM/a2a/v1/discovery/>` |

We don't manage any of these directly. The Solace Agent Mesh framework subscribes, publishes, and routes automatically based on the `config.yaml`.

## Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Language | Python 3.10+ | Solace Agent Mesh entrypoint framework requirement |
| Framework | `solace-agent-mesh` | Entrypoint base classes, broker comms, A2A protocol |
| Terminal UI | `prompt_toolkit` | REPL with Tab auto-completion for commands, session labels, and artifact names |
| Rendering | `rich` | Markdown rendering with Solace-themed styling |
| Config validation | Pydantic | Adapter config via `get_config()` |
| Config format | YAML | Standard Solace Agent Mesh plugin config |
| Runtime | `sam run config.yaml` | Solace Agent Mesh's built-in plugin runner |

**Note on GDK/Google ADK**: The Google Agent Development Kit (ADK) powers the **agent side** of Solace Agent Mesh — LLM interaction, tool execution, state management. Entrypoints don't use ADK directly. We use the **A2A protocol** (the common messaging language between agents and entrypoints), which is handled by the Solace Agent Mesh framework.

## Project Structure

```
sam-cli-entrypoint/
├── src/sam_cli_entrypoint_adapter/
│   ├── __init__.py
│   ├── app.py                       # CliEntrypointApp — Gateway app wrapper
│   ├── component.py                 # CliEntrypointComponent — REPL, commands, response handlers
│   ├── logging_utils.py             # MkdirRotatingFileHandler — log rotation with auto-mkdir
│   └── session_store.py             # SessionStore — local session index persistence
├── config.yaml                      # Solace Agent Mesh entrypoint config
├── logging.yaml                     # Log rotation config (50MB, 3 backups)
├── pyproject.toml                   # Python package metadata (type=gateway in framework terms)
├── .env.example                     # Environment variable template
├── .gitignore
├── README.md                        # Setup & usage instructions
└── APPROACH.md                      # This file
```

## How to Run

```bash
# Install into Solace Agent Mesh venv
pip install -e .

# Copy and configure environment
cp .env.example .env

# Run via Solace Agent Mesh
sam run config.yaml
```

## Approach

The CLI entrypoint was built as a native Solace Agent Mesh entrypoint plugin rather than a standalone client for one key reason: **Solace Agent Mesh already solves the hard problems**. Broker connection management, A2A protocol handling, topic routing, entrypoint card heartbeats, artifact resolution, and agent discovery are all provided by the framework. Writing a custom broker client would mean reimplementing all of that — and staying compatible as Solace Agent Mesh evolves.

By studying the existing entrypoint implementations (WebUI HTTP/SSE) in `solace-agent-mesh`, we identified the `BaseGatewayApp` + `BaseGatewayComponent` pattern as the right abstraction. Each entrypoint follows the same contract: implement `_translate_external_input()` to convert platform input into A2A parts, and implement `_send_*` methods to deliver agent output back to the platform. The framework handles everything in between.

This meant the terminal is treated as just another platform — no different from Slack or a REST API from Solace Agent Mesh's perspective. The component translates stdin to A2A parts on the way in, and renders agent responses as styled markdown on the way out — fully compliant with Solace Agent Mesh conventions, installable as a pip package, and runnable via `sam run config.yaml` like any other Solace Agent Mesh plugin.

