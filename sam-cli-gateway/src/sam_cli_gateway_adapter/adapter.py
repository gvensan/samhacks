"""
CLI Gateway Adapter for the Solace Agent Mesh Generic Gateway Framework.

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
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from prompt_toolkit import PromptSession, prompt as pt_prompt
from prompt_toolkit.completion import WordCompleter
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
  ____    _    __  __    ____ _     ___    ____    _  _____ _______        ___ __   __
 / ___|  / \  |  \/  |  / ___| |   |_ _|  / ___|  / \|_   _| ____\ \      / / \\ \ / /
 \___ \ / _ \ | |\/| | | |   | |    | |  | |  _  / _ \ | | |  _|  \ \ /\ / / _ \\ V /
  ___) / ___ \| |  | | | |___| |___ | |  | |_| |/ ___ \| | | |___  \ V  V / ___ \| |
 |____/_/   \_\_|  |_|  \____|_____|___|  \____/_/   \_\_| |_____|  \_/\_/_/   \_\_|
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
    "/new", "/agents", "/upload", "/artifacts", "/download",
    "/feedback", "/help", "/quit", "/exit",
]
_command_completer = WordCompleter(_COMMANDS, sentence=True)


class CliAdapterConfig(BaseModel):
    """Configuration model for the CLI adapter."""

    prompt: str = Field(
        "sam> ",
        description="The prompt string shown in the REPL.",
    )
    user_id: str = Field(
        "cli_gateway_user",
        description="User identity for this CLI session.",
    )
    show_status_updates: bool = Field(
        True,
        description="Show agent status/progress updates in the terminal.",
    )


class CliGatewayAdapter(GatewayAdapter):
    """A terminal-based gateway adapter for Solace Agent Mesh."""

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

    async def init(self, context: GatewayContext) -> None:
        """Initialize the CLI adapter and start the stdin reader loop."""
        self.context = context
        self.config = context.adapter_config
        log.info("Initializing CLI Gateway Adapter...")

        self._response_event = asyncio.Event()

        # Print banner
        g = _SOLACE_GREEN
        r = _RESET
        print(BANNER)
        print(f"  {g}Gateway ID:{r}    {context.gateway_id}")
        print(f"  {g}Namespace:{r}     {context.namespace}")
        print(f"  {g}User:{r}          {self.config.user_id}")
        print(f"  {g}Session:{r}       {self._default_session_id()}")
        print()
        print(f"  Type a message to chat with SAM agents.")
        print(f"  Type {g}/help{r} for available commands.")
        print()

        # Start the interactive REPL as a background task
        self._reader_task = asyncio.create_task(self._repl_loop())
        log.info("CLI Gateway Adapter initialized.")

    async def cleanup(self) -> None:
        """Clean up the reader task on shutdown."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        log.info("CLI Gateway Adapter shut down.")

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
        This ensures the same user reconnecting to the same gateway resumes
        their prior conversation and artifacts.
        """
        return f"{self.context.gateway_id}__default"

    def _new_session_id(self) -> str:
        """Generate a new unique session ID, prefixed with gateway_id.

        Format: {gateway_id}__cli-{random}
        All sessions from this gateway are visually grouped in artifact storage.
        """
        return f"{self.context.gateway_id}__cli-{uuid.uuid4().hex[:8]}"

    async def _repl_loop(self) -> None:
        """Run the interactive read-eval-print loop."""
        session_id = self._default_session_id()
        loop = asyncio.get_running_loop()

        # Initialize prompt_toolkit session with auto-completion
        prompt_str = self.config.prompt if self.config else "sam> "
        self._prompt_session = PromptSession(
            message=prompt_str,
            completer=_command_completer,
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
            return await self._cmd_new(loop=asyncio.get_running_loop())

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

    async def _cmd_new(self, loop: asyncio.AbstractEventLoop) -> Optional[str]:
        """Start a new session with confirmation."""
        print("\033[93m  Warning: Starting a new session will start a fresh conversation.")
        print("  You will lose access to current conversation history and artifacts.\033[0m")
        confirm = await loop.run_in_executor(
            None, lambda: pt_prompt("  Continue? (y/n): ")
        )
        if confirm.strip().lower() in ("y", "yes"):
            new_id = self._new_session_id()
            print(f"\033[92m  New session started: {new_id}\033[0m\n")
            return f"new_session:{new_id}"
        else:
            print("  Cancelled.\n")
            return None

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
        all gateways running in the same config. The CLI gateway is designed to be
        run as the sole gateway in its own `sam run config.yaml` process.
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
        print("  \033[1mCommands\033[0m")
        print("    /new                        — Start a new conversation session")
        print("    /agents                     — List registered agents")
        print("    /upload <file> [message]    — Send a file to an agent")
        print("    /artifacts                  — List agent-created files in this session")
        print("    /download [file] [path]     — Save artifacts (interactive if no file given)")
        print("    /feedback up|down [comment] — Rate the last response")
        print("    /help                       — Show this help message")
        print("    /quit                       — Exit the CLI")
        print()
