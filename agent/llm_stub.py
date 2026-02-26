"""
agent/llm_stub.py

Stub layer that hides the details of talking to an LLM.  The rest of the
codebase calls `call_llm(prompt)` and expects a string containing the raw
model output (usually JSON).  No parsing or error handling is performed by
this module.

The implementation is small and flexible:

* Check for an Ollama installation and a configured model via the
  `OLLAMA_MODEL` environment variable.  If available we shell out to the
  `ollama` CLI (the lightweight local inference tool used in the README).
* Otherwise, if `OPENAI_API_KEY` is set we fall back to the OpenAI Python
  library and send a ChatCompletion request using `OPENAI_MODEL` or
  `gpt-3.5-turbo` by default.
* If neither backend is configured, `call_llm` returns an empty string and
  logs a warning.

For simplicity this stub keeps dependencies minimal; the package already
lists `openai` in requirements so importing it is safe.  No network
operations are performed unless a real model is requested.
"""

import os
import subprocess
import logging
from typing import Optional


# Configure logger for this module
logger = logging.getLogger(__name__)


def call_llm(prompt: str, timeout: float = 30.0) -> str:
    """Send ``prompt`` to the configured LLM and return the raw text output.

    Priority order:

    1. Ollama CLI if ``OLLAMA_MODEL`` is set and the binary is available.
    2. OpenAI API if ``OPENAI_API_KEY`` is present.
    3. Otherwise return an empty string and log a warning.

    ``timeout`` controls how long we wait for the process / network call.
    """

    # try Ollama first
    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model and _ollama_available():
        return _call_ollama(ollama_model, prompt, timeout)

    # fall back to OpenAI
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai

            model = os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo")
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            # ChatCompletion returns a list; grab the first
            text = resp.choices[0].message.content
            return text
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("OpenAI call failed: %s", exc)
            return ""

    logger.warning("No LLM backend configured (set OLLAMA_MODEL or OPENAI_API_KEY)")
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ollama_available() -> bool:
    """Return True if the ``ollama`` executable is on PATH."""
    from shutil import which

    return which("ollama") is not None


def _call_ollama(model: str, prompt: str, timeout: float) -> str:
    """Run ``ollama generate <model>`` with the given prompt.

    We capture stdout and return it.  If the subprocess fails we log a
    warning and return an empty string.
    """
    try:
        proc = subprocess.run(
            ["ollama", "generate", model, "--prompt", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning("ollama exited %d: %s", proc.returncode, proc.stderr)
            return ""
        return proc.stdout
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("ollama call failed: %s", exc)
        return ""
