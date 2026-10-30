"""
codebud/core.py

This module defines the AgentCore class, the central orchestrator of the
privacy-first, local coding agent. It coordinates:

- LLM-driven planning
- Step execution
- Safety validation
- Memory timeline snapshots
- Tool registry access
- Sandbox execution
- Regeneration logic

AgentCore.handle_user_message(message) returns a plan, a chat reply or an
error; the CLI drives everything else through its public methods.

This file contains no UI logic. It is purely the internal agent brain.
"""

import os
from collections.abc import Callable
from typing import Any

from .executor import Executor
from .llm_stub import (
    LLMUnavailable,
    ModelHeartbeat,
    call_llm_plain,
    list_models,
    num_ctx,
    prewarm_model,
    summarize_conversation,
    warm_model_sync,
)
from .memory import MemoryStore
from .modes import MODES
from .planner import LLMPlanner
from .rag.context import context_block, retrieve
from .rag.context import enabled as rag_enabled
from .safety import SafetyEngine
from .sandbox import Sandbox
from .tools.tool_registry import ToolRegistry

_SUMMARY_LIMIT = 80


def step_summary(step: dict[str, Any]) -> str:
    """``tool(target)`` for a plan step, from its arguments: the path of a
    file tool, a patch's target file, a command line, a search query."""
    from .tools.patcher import patch_target

    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    tool = str(step.get("tool", "?"))
    if tool in ("file_read", "file_write"):
        target = args.get("path", "")
    elif tool == "patch":
        target = patch_target(str(args.get("patch") or "")) or ""
    elif tool == "command":
        target = args.get("cmd", "")
    elif tool == "web_search":
        target = args.get("query", "")
    else:
        target = ""
    target = " ".join(str(target).split())  # one line
    if len(target) > _SUMMARY_LIMIT:
        target = target[: _SUMMARY_LIMIT - 3] + "..."
    return f"{tool}({target})"


class AgentCore:
    """
    The central orchestrator of the agent.

    Responsibilities:
    - Receive user messages
    - Ask the LLM to generate a plan
    - Validate the plan
    - Execute steps (when requested)
    - Store memory snapshots
    """

    def __init__(self):
        # Initialize subsystems
        self.sandbox = Sandbox()
        self.tool_registry = ToolRegistry(self.sandbox)
        self.safety = SafetyEngine()
        self.memory = MemoryStore()
        self.planner = LLMPlanner(self.tool_registry, self.safety)
        self.executor = Executor(self.tool_registry, self.sandbox, self.memory, safety=self.safety)

        # Store last user message for regeneration
        self.last_user_message = None
        # Project-index chunks retrieved for the last request (phase 9), and
        # whether to retrieve at all (CODEBUD_RAG, --no-rag, /rag on|off).
        self.last_context: list = []
        self._rag = rag_enabled()

        # Rolling conversation history: list of {"role": "user"|"assistant", "content": str}
        self._history: list[dict[str, str]] = []

        # Start loading the model immediately so it's ready before the first request
        prewarm_model()

        # Keep the Ollama model loaded for the lifetime of this session
        self._heartbeat = ModelHeartbeat()
        self._heartbeat.start()

    def close(self) -> None:
        """End the session.  Stops the heartbeat so Ollama can unload the model."""
        self._heartbeat.stop()

    def __del__(self) -> None:
        self.close()

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def handle_user_message(
        self,
        message: str,
        on_chunk: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Called when the user sends a message. Returns a plan or plan_error.

        ``on_chunk`` is forwarded to the LLM backend to drive a live progress
        indicator in the CLI without coupling display logic to this class.
        """
        self.last_user_message = message
        # Project context from the index, if the project has one (phase 9).
        self.last_context = retrieve(message, history=self._history) if self._rag else []
        self._history.append({"role": "user", "content": message})

        plan_output = self.planner.generate_plan(
            message,
            on_chunk=on_chunk,
            history=self._history[:-1],  # exclude the turn we just appended
            context_hits=self.last_context,
        )

        # The mode or the safety policy forbids this plan, or the model's
        # steps miss required arguments: report it rather than chatting.
        if plan_output.get("status") in ("plan_refused", "plan_invalid", "llm_unavailable"):
            self._history.append({"role": "assistant", "content": plan_output["error"]})
            return plan_output

        # If planner failed, fall back to a plain-text conversational reply
        if plan_output.get("status") != "ok":
            try:
                reply = call_llm_plain(
                    message,
                    history=self._history[:-1],
                    on_chunk=on_chunk,
                    context_block=context_block(self.last_context),
                )
            except LLMUnavailable as exc:
                self._history.append({"role": "assistant", "content": str(exc)})
                return {"status": "llm_unavailable", "error": str(exc)}
            self._history.append({"role": "assistant", "content": reply or "(no reply)"})
            return {"status": "chat_reply", "reply": reply}

        # Validate plan structure
        if not self._validate_plan(plan_output):
            self._history.append({"role": "assistant", "content": "plan_error: malformed plan"})
            return {"status": "plan_error", "error": "Planner returned malformed plan"}

        # Record a compact assistant turn so the model sees what it planned.
        # It is built from what each step does (its tool and target), not
        # from the model's free-text description: text injected into a
        # description must not carry into later prompts or /compact
        # summaries (security review M1).
        step_summaries = ", ".join(step_summary(s) for s in plan_output.get("plan", []))
        self._history.append({"role": "assistant", "content": f"planned: {step_summaries}"})

        return plan_output

    def clear_history(self) -> None:
        """Wipe the conversation history (used by /clear in chat mode)."""
        self._history.clear()

    def get_history(self) -> list[dict[str, str]]:
        """Return a copy of the conversation history (used by /history in chat mode)."""
        return list(self._history)

    def restore_history(self, history: list[dict[str, str]]) -> None:
        """Replace the conversation history (used by chat --resume)."""
        self._history[:] = list(history)

    def is_powerful(self, step: dict[str, Any]) -> bool:
        """Return whether ``step`` runs code or changes files."""
        return self.safety.is_powerful(step)

    def confirmation_reason(self, step: dict[str, Any]) -> str | None:
        """Why ``step`` needs a "y" in auto mode and `run` (None: it does not)."""
        return self.safety.confirmation_reason(step)

    def get_rag(self) -> bool:
        """Whether requests get project context from the index."""
        return self._rag

    def set_rag(self, on: bool) -> None:
        """Turn project context on or off for this session (/rag on|off)."""
        self._rag = on

    def get_mode(self) -> str:
        """Return the active agent mode (see codebud/modes.py)."""
        return self.safety.mode

    def set_mode(self, mode: str) -> tuple[bool, str]:
        """Switch the agent mode; the SafetyEngine enforces it from now on."""
        if mode not in MODES:
            return False, f"Unknown mode '{mode}'. Choose one of: {', '.join(MODES)}."
        self.safety.mode = mode
        return True, f"Mode: {mode}"

    def reload_policy(self) -> None:
        """Re-read the policy files (after a profile switch)."""
        self.safety.reload()

    def get_model(self) -> str:
        """Return the active OLLAMA_MODEL, or '' if unset."""
        return os.environ.get("OLLAMA_MODEL", "")

    def set_model(
        self,
        model: str,
        on_chunk: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Switch the active model and warm it before returning.

        Validates the model is actually downloaded (never auto-pulls — the
        user must run `ollama pull <name>` themselves), then blocks on a
        synchronous streamed warmup call so the model is loaded before the
        next real request. on_chunk is forwarded to drive a live progress
        indicator the same way handle_user_message does.

        Returns (success, message). On failure OLLAMA_MODEL is left
        unchanged so a bad switch doesn't break an otherwise-working session.
        """
        available = list_models()
        if not available:
            return False, "Could not reach Ollama to verify available models."
        if model not in available:
            return False, f"Model '{model}' is not downloaded. Run: ollama pull {model}"

        previous = os.environ.get("OLLAMA_MODEL")
        os.environ["OLLAMA_MODEL"] = model
        if not warm_model_sync(model, on_chunk=on_chunk):
            if previous is not None:
                os.environ["OLLAMA_MODEL"] = previous
            else:
                os.environ.pop("OLLAMA_MODEL", None)
            return False, f"Failed to warm up '{model}'; kept using the previous model."
        return True, f"Switched to '{model}'."

    def context_usage(self) -> tuple[int, int]:
        """Return (estimated tokens of the next planner prompt, context window).

        Used by chat to tell the user when the conversation is filling the
        context window, before the planner has to start dropping old turns.
        """
        tokens = self.planner.estimate_prompt_tokens(self._history, self.last_context)
        return tokens, num_ctx()

    def compact_history(
        self,
        keep_last: int = 2,
        on_chunk: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Replace all but the last ``keep_last`` history entries with a summary.

        Used by /compact in chat mode to shrink a long conversation. The
        summary is stored as a single {"role": "summary"} entry, which the
        planner renders like any other turn. If the model returns no summary,
        history is left unchanged. Returns (success, message).
        """
        older = self._history[:-keep_last] if keep_last else list(self._history)
        if len(older) < 2:
            return False, "Nothing to compact yet."
        summary = summarize_conversation(older, on_chunk=on_chunk)
        if not summary.strip():
            return False, "Could not get a summary from the model; history unchanged."
        recent = self._history[len(older) :]
        before = sum(len(t["content"]) for t in older)
        self._history[:] = [{"role": "summary", "content": summary}, *recent]
        return True, (
            f"Compacted {len(older)} entries ({before} chars) into a "
            f"{len(summary)}-char summary; kept the last {len(recent)}."
        )

    def undo_last_turn(self) -> bool:
        """Remove the last user+assistant turn pair (used by /undo in chat mode).

        Returns True if a turn pair was removed, False if history was cleared
        instead because fewer than two entries remained.
        """
        if len(self._history) >= 2:
            self._history.pop()
            self._history.pop()
            return True
        self._history.clear()
        return False

    def _validate_plan(self, plan_output: dict[str, Any]) -> bool:
        """
        Ensures the plan follows the Cursor-style contract:

        {
            "status": "ok",
            "plan": [
                {
                    "id": "step_0",
                    "description": "...",
                    "tool": "file_write",
                    "args": {...}
                },
                ...
            ]
        }
        """
        plan = plan_output.get("plan")
        if not isinstance(plan, list):
            return False

        for step in plan:
            if not isinstance(step, dict):
                return False
            if "id" not in step:
                return False
            if "description" not in step:
                return False
            if "tool" not in step:
                return False
            if "args" not in step:
                return False

            # Validate tool exists
            if not self.tool_registry.has(step["tool"]):
                return False

            # Validate safety
            if not self.safety.validate_step(step):
                return False

        return True
