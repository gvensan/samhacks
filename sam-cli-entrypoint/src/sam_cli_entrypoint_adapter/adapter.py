"""
CLI Entrypoint Adapter for the Solace Agent Mesh Generic Entrypoint Framework.

Provides an interactive terminal REPL for conversing with SAM agents.
"""

import asyncio
import logging
import mimetypes
import os
import shlex
import signal
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from prompt_toolkit import PromptSession, prompt as pt_prompt
from prompt_toolkit.completion import Completer, Completion, WordCompleter
from prompt_toolkit.shortcuts import checkboxlist_dialog
from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

from solace_agent_mesh.gateway.adapter.base import GatewayAdapter
from solace_agent_mesh.gateway.adapter.types import (
    AuthClaims,
    GatewayContext,
    ResponseContext,
    SamDataPart,
    SamError,
    SamFeedback,
    SamFilePart,
    SamTask,
    SamTextPart,
)

from sam_cli_entrypoint_adapter.session_store import SessionStore

# Max upload size: 50 MB (matches SAM default gateway_max_upload_size_bytes)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

log = logging.getLogger(__name__)

# Solace brand green: \033[38;2;0;200;149m (RGB 0,200,149 ≈ #00C895)
_SOLACE_GREEN = "\033[38;2;0;200;149m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

BANNER = (
    _BOLD + _SOLACE_GREEN
    + r"""
 ____    _    __  __    ____ _     ___   _____ _   _ _____ ______   ______   ___ ___ _   _ _____
/ ___|  / \  |  \/  |  / ___| |   |_ _| | ____| \ | |_   _|  _ \ \ / /  _ \ / _ \_ _| \ | |_   _|
\___ \ / _ \ | |\/| | | |   | |    | |  |  _| |  \| | | | | |_) \ V /| |_) | | | | ||  \| | | |
 ___) / ___ \| |  | | | |___| |___ | |  | |___| |\  | | | |  _ < | | |  __/| |_| | || |\  | | |
|____/_/   \_\_|  |_|  \____|_____|___| |_____|_| \_| |_| |_| \_\|_| |_|    \___/___|_| \_| |_|
"""
    + _RESET
)

# Rich console for markdown rendering
_solace_theme = Theme({
    "markdown.h1": "bold rgb(0,200,149)",
    "markdown.h2": "bold rgb(0,200,149)",
    "markdown.h3": "bold rgb(0,200,149)",
    "markdown.code": "dim white on grey11",
    "markdown.item.bullet": "rgb(0,200,149)",
})
_console = Console(theme=_solace_theme, highlight=False)

# Slash command auto-completion
_COMMANDS = [
    "/new", "/sessions", "/switch", "/rename", "/delete",
    "/agents", "/upload", "/artifacts", "/download",
    "/feedback", "/help", "/quit", "/exit",
]

# Commands whose first argument should complete with session labels
_SESSION_ARG_COMMANDS = {"/switch", "/delete"}


class _CliCompleter(Completer):
    """Dynamic completer: command names first, then session labels for /switch and /delete."""

    def __init__(self):
        self._session_store: Optional[SessionStore] = None

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        # First token — complete command names
        if " " not in text:
            for cmd in _COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))
            return
        # Second token — complete session labels for applicable commands
        cmd, _, partial = text.partition(" ")
        partial = partial.lstrip()
        if cmd in _SESSION_ARG_COMMANDS and self._session_store:
            for session in self._session_store.list_sessions():
                label = session.get("label")
                if label and label.startswith(partial):
                    yield Completion(label, start_position=-len(partial))


class CliAdapterConfig(BaseModel):
    """Configuration model for the CLI adapter."""

    prompt: str = Field(
        "sam> ",
        description="The prompt string shown in the REPL.",
    )
    user_id: str = Field(
        "cli_entrypoint_user",
        description="User identity for this CLI session.",
    )
    show_status_updates: bool = Field(
        True,
        description="Show agent status/progress updates in the terminal.",
    )


class CliEntrypointAdapter(GatewayAdapter):
    """A terminal-based entrypoint adapter for Solace Agent Mesh."""

    ConfigModel = CliAdapterConfig

    def __init__(self):
        self.context: Optional[GatewayContext] = None
        self.config: Optional[CliAdapterConfig] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._response_event: Optional[asyncio.Event] = None
        self._current_response_text: str = ""
        self._is_first_chunk: bool = True
        # Track current and last task for /feedback
        self._current_task_id: Optional[str] = None
        self._last_task_id: Optional[str] = None
        self._last_session_id: Optional[str] = None
        # Prompt session with auto-completion
        self._prompt_session: Optional[PromptSession] = None
        # Session management
        self._session_store: Optional[SessionStore] = None

    async def init(self, context: GatewayContext) -> None:
        """Initialize the CLI adapter and start the stdin reader loop."""
        self.context = context
        self.config = context.adapter_config
        log.info("Initializing CLI Entrypoint Adapter...")

        self._response_event = asyncio.Event()

        # Initialize session store and ensure a default session exists
        self._session_store = SessionStore(entrypoint_id=context.gateway_id)
        default_id = self._default_session_id()
        if not self._session_store.get(default_id):
            self._session_store.create(default_id, label="default")
        # Restore or set active session (validate it still exists)
        stored_active = self._session_store.active_session
        if not stored_active or not self._session_store.get(stored_active):
            self._session_store.active_session = default_id

        active_id = self._session_store.active_session
        active_meta = self._session_store.get(active_id) or {}
        active_label = active_meta.get("label") or active_id

        # Print banner
        g = _SOLACE_GREEN
        r = _RESET
        print(BANNER)
        print(f"  {g}Entrypoint ID:{r}  {context.gateway_id}")
        print(f"  {g}Namespace:{r}     {context.namespace}")
        print(f"  {g}User:{r}          {self.config.user_id}")
        print(f"  {g}Session:{r}       {active_label}")
        print()
        print(f"  Type a message to chat with SAM agents.")
        print(f"  Type {g}/help{r} for available commands.")
        print()

        # Start the interactive REPL as a background task
        self._reader_task = asyncio.create_task(self._repl_loop())
        log.info("CLI Entrypoint Adapter initialized.")

    async def cleanup(self) -> None:
        """Clean up the reader task on shutdown."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        log.info("CLI Entrypoint Adapter shut down.")

    # --- Authentication ---

    async def extract_auth_claims(
        self,
        external_input: Dict[str, Any],
        endpoint_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[AuthClaims]:
        """Return claims for the CLI user."""
        return AuthClaims(
            id=self.config.user_id,
            source="cli",
        )

    # --- Inbound: Terminal -> A2A ---

    async def prepare_task(
        self,
        external_input: Dict[str, Any],
        endpoint_context: Optional[Dict[str, Any]] = None,
    ) -> SamTask:
        """Convert CLI input into a SamTask."""
        text = external_input.get("text", "")
        session_id = external_input.get("session_id", "cli-default")
        target_agent = external_input.get("target_agent")
        file_parts: List[SamFilePart] = external_input.get("file_parts", [])

        parts = []
        if text:
            parts.append(SamTextPart(text=text))
        parts.extend(file_parts)

        return SamTask(
            parts=parts,
            session_id=session_id,
            target_agent=target_agent or self.context.config.get("default_agent_name", "OrchestratorAgent"),
            is_streaming=True,
            platform_context={
                "source": "cli",
            },
        )

    # --- Outbound: A2A -> Terminal ---

    async def handle_text_chunk(self, text: str, context: ResponseContext) -> None:
        """Accumulate streaming text chunks for markdown rendering on completion."""
        if self._is_first_chunk:
            sys.stdout.write(f"\r{_SOLACE_GREEN}  Receiving...{_RESET}")
            sys.stdout.flush()
            self._is_first_chunk = False
        self._current_response_text += text

    async def handle_status_update(self, status_text: str, context: ResponseContext) -> None:
        """Show agent progress updates."""
        if self.config and self.config.show_status_updates:
            print(f"\r\033[90m  [{status_text}]\033[0m", end="", flush=True)

    async def handle_file(self, file_part: SamFilePart, context: ResponseContext) -> None:
        """Notify user about file artifacts."""
        name = file_part.name
        mime = file_part.mime_type or "unknown"
        if file_part.uri:
            print(f"\n  📎 File: {name} ({mime}) — {file_part.uri}")
        else:
            size = len(file_part.content_bytes) if file_part.content_bytes else 0
            print(f"\n  📎 File: {name} ({mime}, {size} bytes)")

    async def handle_data_part(self, data_part: SamDataPart, context: ResponseContext) -> None:
        """Handle structured data parts."""
        log.debug("Received data part: %s", data_part.data)

    async def handle_task_complete(self, context: ResponseContext) -> None:
        """Render accumulated response as markdown and signal completion."""
        # Clear the "Receiving..." line
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        if self._current_response_text:
            print()
            _console.print(Markdown(self._current_response_text))
        print()
        # Track for /feedback
        self._last_task_id = context.task_id
        self._last_session_id = context.session_id
        self._current_task_id = None
        self._current_response_text = ""
        self._is_first_chunk = True
        self._response_event.set()

    async def handle_error(self, error: SamError, context: ResponseContext) -> None:
        """Display errors in the terminal."""
        print(f"\n\033[91m  Error [{error.category}]: {error.message}\033[0m\n")
        # Track for /feedback (errors are still ratable)
        self._last_task_id = context.task_id
        self._last_session_id = context.session_id
        self._current_task_id = None
        self._current_response_text = ""
        self._is_first_chunk = True
        self._response_event.set()

    # --- REPL Loop ---

    def _default_session_id(self) -> str:
        """Generate a deterministic session ID for the default session.

        Format: {gateway_id}__default
        This ensures the same user reconnecting to the same entrypoint resumes
        their prior conversation and artifacts.
        """
        return f"{self.context.gateway_id}__default"

    def _new_session_id(self) -> str:
        """Generate a new unique session ID, prefixed with gateway_id.

        Format: {gateway_id}__cli-{random}
        All sessions from this entrypoint are visually grouped in artifact storage.
        """
        return f"{self.context.gateway_id}__cli-{uuid.uuid4().hex[:8]}"

    async def _repl_loop(self) -> None:
        """Run the interactive read-eval-print loop."""
        session_id = self._session_store.active_session or self._default_session_id()
        loop = asyncio.get_running_loop()

        # Initialize prompt_toolkit session with dynamic auto-completion
        prompt_str = self.config.prompt if self.config else "sam> "
        completer = _CliCompleter()
        completer._session_store = self._session_store
        self._prompt_session = PromptSession(
            message=prompt_str,
            completer=completer,
            complete_while_typing=True,
        )

        while True:
            try:
                # Read input using prompt_toolkit (supports Tab completion)
                line = await loop.run_in_executor(
                    None, lambda: self._prompt_session.prompt()
                )
                line = line.strip()

                if not line:
                    continue

                # Handle CLI commands
                if line.startswith("/"):
                    result = await self._handle_command(line, session_id)
                    if result == "exit":
                        break
                    if isinstance(result, str) and result.startswith("new_session:"):
                        session_id = result.split(":", 1)[1]
                        self._session_store.active_session = session_id
                    continue

                # Catch bare exit/quit and offer the real command
                if line.lower() in ("exit", "quit"):
                    confirm = await loop.run_in_executor(
                        None,
                        lambda: pt_prompt("  Did you mean /quit? [Y/n] "),
                    )
                    if confirm.strip().lower() in ("", "y", "yes"):
                        self._shutdown("Goodbye!")
                        return
                    continue

                # Reset response state and submit to SAM
                self._response_event.clear()
                self._current_response_text = ""
                self._is_first_chunk = True
                self._current_task_id = None

                event = {
                    "text": line,
                    "session_id": session_id,
                    "user_id": self.config.user_id,
                }

                try:
                    task_id = await self.context.handle_external_input(event)
                    self._current_task_id = task_id
                    self._session_store.increment_message_count(session_id)
                except Exception as e:
                    print(f"\n\033[91m  Error submitting task: {e}\033[0m\n")
                    continue

                # Wait for response
                await self._wait_for_response()

            except EOFError:
                # Ctrl+D
                self._shutdown("Goodbye!")
                return
            except KeyboardInterrupt:
                self._shutdown("Goodbye!")
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.exception("Error in REPL loop: %s", e)
                print(f"\n\033[91m  Unexpected error: {e}\033[0m\n")

    async def _wait_for_response(self) -> None:
        """Wait for the task response event, with a timeout."""
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("\n\033[93m  Response timed out (10m)\033[0m\n")

    async def _handle_command(self, command: str, session_id: str) -> Optional[str]:
        """Handle CLI commands. Returns 'exit', 'new_session', or None."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Fallback if shlex fails (e.g., unmatched quotes)
            tokens = command.split()
        cmd = tokens[0].lower()
        args = tokens[1:]

        if cmd in ("/quit", "/exit", "/q"):
            self._shutdown("Goodbye!")
            return "exit"

        elif cmd == "/new":
            return self._cmd_new(args, session_id)

        elif cmd == "/sessions":
            self._cmd_sessions(session_id)

        elif cmd == "/switch":
            return self._cmd_switch(args, session_id)

        elif cmd == "/rename":
            self._cmd_rename(args, session_id)

        elif cmd == "/delete":
            self._cmd_delete(args, session_id)

        elif cmd == "/agents":
            await self._cmd_agents()

        elif cmd == "/upload":
            await self._cmd_upload(args, session_id)

        elif cmd == "/artifacts":
            await self._cmd_artifacts(session_id)

        elif cmd == "/download":
            await self._cmd_download(args, session_id)

        elif cmd == "/feedback":
            await self._cmd_feedback(args, session_id)

        elif cmd == "/help":
            self._cmd_help()

        else:
            print(f"\033[93m  Unknown command: {cmd}. Type /help for available commands.\033[0m\n")

        return None

    # --- Command Implementations ---

    def _cmd_new(self, args: List[str], current_session_id: str) -> Optional[str]:
        """Start a new session, optionally with a label."""
        label = args[0] if args else None

        # Enforce unique labels
        if label and self._session_store.label_exists(label):
            print(f"\033[93m  A session with label \"{label}\" already exists. Use /switch {label} or pick a different name.\033[0m\n")
            return None

        new_id = self._new_session_id()
        self._session_store.create(new_id, label=label)
        display = label or new_id
        print(f"\033[92m  New session started: {display}\033[0m\n")
        return f"new_session:{new_id}"

    def _cmd_sessions(self, current_session_id: str) -> None:
        """List all sessions."""
        sessions = self._session_store.list_sessions()
        if not sessions:
            print("\n  No sessions.\n")
            return

        print()
        for s in sessions:
            sid = s["id"]
            label = s.get("label")
            count = s.get("message_count", 0)
            last = s.get("last_active", "")
            # Show relative time if possible
            age = self._format_age(last)

            marker = "*" if sid == current_session_id else " "
            if label:
                print(f"  {marker} {_BOLD}{label}{_RESET}  ({count} msgs, {age})")
            else:
                # Show short ID suffix for unnamed sessions
                short_id = sid.split("__")[-1] if "__" in sid else sid[-12:]
                print(f"  {marker} {short_id}  ({count} msgs, {age})")
        print()

    def _cmd_switch(self, args: List[str], current_session_id: str) -> Optional[str]:
        """Switch to an existing session by label or ID."""
        if not args:
            print("\033[93m  Usage: /switch <label|id>\033[0m\n")
            return None

        target = args[0]
        try:
            session_id = self._session_store.resolve(target)
        except ValueError as e:
            print(f"\033[93m  {e}\033[0m\n")
            return None
        if not session_id:
            print(f"\033[93m  No session found matching \"{target}\". Use /sessions to list.\033[0m\n")
            return None

        if session_id == current_session_id:
            print("\033[93m  Already in that session.\033[0m\n")
            return None

        meta = self._session_store.get(session_id) or {}
        display = meta.get("label") or session_id
        count = meta.get("message_count", 0)
        print(f"\033[92m  Switched to: {display} ({count} msgs)\033[0m\n")
        return f"new_session:{session_id}"

    def _cmd_rename(self, args: List[str], current_session_id: str) -> None:
        """Rename the current session."""
        if not args:
            print("\033[93m  Usage: /rename <new-label>\033[0m\n")
            return

        new_label = args[0]
        if self._session_store.label_exists(new_label):
            print(f"\033[93m  A session with label \"{new_label}\" already exists.\033[0m\n")
            return

        self._session_store.update(current_session_id, label=new_label)
        print(f"\033[92m  Current session renamed to: {new_label}\033[0m\n")

    def _cmd_delete(self, args: List[str], current_session_id: str) -> None:
        """Delete a session from the local index."""
        if not args:
            print("\033[93m  Usage: /delete <label|id>\033[0m\n")
            return

        target = args[0]
        try:
            session_id = self._session_store.resolve(target)
        except ValueError as e:
            print(f"\033[93m  {e}\033[0m\n")
            return
        if not session_id:
            print(f"\033[93m  No session found matching \"{target}\".\033[0m\n")
            return

        if session_id == current_session_id:
            print("\033[93m  Cannot delete the active session. Switch to another session first.\033[0m\n")
            return

        if session_id == self._default_session_id():
            print("\033[93m  Cannot delete the default session.\033[0m\n")
            return

        meta = self._session_store.get(session_id) or {}
        display = meta.get("label") or session_id
        self._session_store.delete(session_id)
        print(f"\033[92m  Removed session \"{display}\" from local index.\033[0m")
        print(f"\033[90m  Note: Conversation history and artifacts remain on SAM's side.\033[0m\n")

    @staticmethod
    def _format_age(iso_timestamp: str) -> str:
        """Format an ISO timestamp as a human-readable relative age."""
        if not iso_timestamp:
            return "unknown"
        try:
            then = datetime.fromisoformat(iso_timestamp)
            now = datetime.now(timezone.utc)
            delta = now - then
            seconds = int(delta.total_seconds())
            if seconds < 60:
                return "just now"
            elif seconds < 3600:
                m = seconds // 60
                return f"{m}m ago"
            elif seconds < 86400:
                h = seconds // 3600
                return f"{h}h ago"
            else:
                d = seconds // 86400
                return f"{d}d ago"
        except (ValueError, TypeError):
            return "unknown"

    async def _cmd_agents(self) -> None:
        """List registered agents."""
        agents = self.context.list_agents()
        if agents:
            print("\n  Available agents:")
            for agent in agents:
                name = getattr(agent, "name", str(agent))
                desc = getattr(agent, "description", "")
                print(f"    \033[1m{name}\033[0m")
                if desc:
                    print(f"      {desc}")
            print()
        else:
            print("\n  No agents currently registered.\n")

    async def _cmd_upload(self, args: List[str], session_id: str) -> None:
        """Upload a file with an optional message."""
        if not args:
            print("\033[93m  Usage: /upload <filepath> [message]\033[0m\n")
            return

        filepath = args[0]
        message = " ".join(args[1:]) if len(args) > 1 else ""

        # Resolve path
        filepath = os.path.expanduser(filepath)
        if not os.path.isabs(filepath):
            filepath = os.path.abspath(filepath)

        if not os.path.isfile(filepath):
            print(f"\033[91m  File not found: {filepath}\033[0m\n")
            return

        # Read file
        filename = os.path.basename(filepath)
        mime_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except Exception as e:
            print(f"\033[91m  Error reading file: {e}\033[0m\n")
            return

        # Check file size
        if len(content) > MAX_UPLOAD_BYTES:
            max_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
            print(f"\033[91m  File too large. Maximum upload size is {max_mb:.0f} MB.\033[0m\n")
            return

        size_kb = len(content) / 1024
        print(f"\033[90m  Uploading {filename} ({mime_type}, {size_kb:.1f} KB)...\033[0m")

        file_part = SamFilePart(
            name=filename,
            content_bytes=content,
            mime_type=mime_type,
        )

        # Reset response state and submit
        self._response_event.clear()
        self._current_response_text = ""
        self._is_first_chunk = True
        self._current_task_id = None

        event = {
            "text": message or f"I've uploaded a file: {filename}",
            "session_id": session_id,
            "user_id": self.config.user_id,
            "file_parts": [file_part],
        }

        try:
            task_id = await self.context.handle_external_input(event)
            self._current_task_id = task_id
            self._session_store.increment_message_count(session_id)
        except Exception as e:
            print(f"\033[91m  Error uploading: {e}\033[0m\n")
            return

        # Wait for response
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("\n\033[93m  Response timed out (10m)\033[0m\n")

    async def _cmd_artifacts(self, session_id: str) -> None:
        """List artifacts in the current session."""
        context = ResponseContext(
            task_id=self._last_task_id or "none",
            session_id=session_id,
            user_id=self.config.user_id,
            platform_context={"source": "cli"},
        )

        try:
            artifacts = await self.context.list_artifacts(context)
        except Exception as e:
            print(f"\033[91m  Error listing artifacts: {e}\033[0m\n")
            return

        if not artifacts:
            print("\n  No artifacts in this session.\n")
            return

        print("\n  Artifacts:")
        for artifact in artifacts:
            name = getattr(artifact, "filename", str(artifact))
            version = getattr(artifact, "version", "?")
            print(f"    \033[1m{name}\033[0m (v{version})")
        print()
        print("  Use /download to select and save, or /download <filename> for a specific file.\n")

    async def _cmd_download(self, args: List[str], session_id: str) -> None:
        """Download artifacts to local disk. Interactive multi-select if no args."""
        context = ResponseContext(
            task_id=self._last_task_id or "none",
            session_id=session_id,
            user_id=self.config.user_id,
            platform_context={"source": "cli"},
        )

        if args:
            # Direct download: /download <filename> [local_path]
            await self._download_artifact(context, args[0], args[1] if len(args) > 1 else args[0])
            return

        # Interactive mode: list artifacts and let user multi-select
        try:
            artifacts = await self.context.list_artifacts(context)
        except Exception as e:
            print(f"\033[91m  Error listing artifacts: {e}\033[0m\n")
            return

        if not artifacts:
            print("\n  No artifacts in this session.\n")
            return

        # Build checkbox choices
        choices = []
        for artifact in artifacts:
            name = getattr(artifact, "filename", str(artifact))
            version = getattr(artifact, "version", "?")
            choices.append((name, f"{name} (v{version})"))

        loop = asyncio.get_running_loop()
        selected = await loop.run_in_executor(None, lambda: checkboxlist_dialog(
            title="Download Artifacts",
            text="Select artifacts to download (Space to toggle, Enter to confirm):",
            values=choices,
        ).run())

        if not selected:
            print("  No artifacts selected.\n")
            return

        # Download each selected artifact
        for filename in selected:
            await self._download_artifact(context, filename, filename)

    async def _download_artifact(
        self, context: ResponseContext, filename: str, local_path: str
    ) -> None:
        """Download a single artifact to local disk."""
        try:
            content = await self.context.load_artifact_content(context, filename)
        except Exception as e:
            print(f"\033[91m  Error loading artifact '{filename}': {e}\033[0m")
            return

        if content is None:
            print(f"\033[91m  Artifact not found: {filename}\033[0m")
            return

        # Write to local disk
        local_path = os.path.expanduser(local_path)
        if not os.path.isabs(local_path):
            local_path = os.path.abspath(local_path)

        if os.path.exists(local_path):
            print(f"\033[93m  Overwriting: {local_path}\033[0m")

        try:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(content)
            size = len(content)
            if size < 1024:
                size_str = f"{size} bytes"
            else:
                size_str = f"{size / 1024:.1f} KB"
            print(f"\033[92m  Saved: {local_path} ({size_str})\033[0m")
        except Exception as e:
            print(f"\033[91m  Error saving '{filename}': {e}\033[0m")

    async def _cmd_feedback(self, args: List[str], session_id: str) -> None:
        """Submit feedback for the last completed task."""
        if not args or args[0] not in ("up", "down"):
            print("\033[93m  Usage: /feedback up|down [comment]\033[0m\n")
            return

        if not self._last_task_id:
            print("\033[93m  No completed task to give feedback on.\033[0m\n")
            return

        rating = args[0]
        comment = " ".join(args[1:]) if len(args) > 1 else None

        feedback = SamFeedback(
            task_id=self._last_task_id,
            session_id=self._last_session_id or session_id,
            user_id=self.config.user_id,
            rating=rating,
            comment=comment,
        )

        try:
            await self.context.submit_feedback(feedback)
            icon = "\033[92m+1\033[0m" if rating == "up" else "\033[91m-1\033[0m"
            msg = f"  [{icon}] Feedback submitted"
            if comment:
                msg += f" — \"{comment}\""
            print(msg + "\n")
        except Exception as e:
            print(f"\033[91m  Error submitting feedback: {e}\033[0m\n")

    def _shutdown(self, message: str = "Goodbye!") -> None:
        """Shut down the CLI and signal SAM to exit gracefully.

        Note: This sends SIGTERM to the entire SAM process, which will shut down
        all entrypoints running in the same config. The CLI entrypoint is designed to be
        run as the sole entrypoint in its own `sam run config.yaml` process.
        """
        print(message)
        log.info("CLI exit requested, scheduling SIGTERM for graceful shutdown.")
        # Schedule SIGTERM slightly deferred so the REPL loop can exit cleanly first.
        # This avoids a race where SIGTERM fires while the loop is still unwinding
        # and the executor thread holding prompt() blocks SAM's cleanup.
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    def _cmd_help(self) -> None:
        """Show available commands."""
        print()
        print("  \033[1mChat\033[0m")
        print("    Just type a message to chat with SAM agents.")
        print()
        print("  \033[1mSessions\033[0m")
        print("    /new [label]                — Start a new session (optionally named)")
        print("    /sessions                   — List all sessions")
        print("    /switch <label|id>          — Switch to an existing session")
        print("    /rename <label>             — Rename the current session")
        print("    /delete <label|id>          — Remove a session from local index (history stays on SAM)")
        print()
        print("  \033[1mCommands\033[0m")
        print("    /agents                     — List registered agents")
        print("    /upload <file> [message]    — Send a file to an agent")
        print("    /artifacts                  — List agent-created files in this session")
        print("    /download [file] [path]     — Save artifacts (interactive if no file given)")
        print("    /feedback up|down [comment] — Rate the last response")
        print("    /help                       — Show this help message")
        print("    /quit                       — Exit the CLI")
        print()
