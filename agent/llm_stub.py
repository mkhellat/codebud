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
import logging

import requests

logger = logging.getLogger(__name__)

# Ollama default base URL; override with OLLAMA_BASE_URL if needed.
_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def call_llm(prompt: str, timeout: float = 120.0) -> str:
    """Send ``prompt`` to the configured LLM and return the raw text output.

    Priority order:

    1. Ollama REST API if ``OLLAMA_MODEL`` is set and the server is reachable.
    2. OpenAI API if ``OPENAI_API_KEY`` is present.
    3. Otherwise return an empty string and log a warning.

    ``timeout`` applies to the HTTP request.  120 s gives CPU inference
    (7-B models) time to finish without hanging indefinitely.
    """

    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model and _ollama_available():
        return _call_ollama(ollama_model, prompt, timeout)

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI

            client = OpenAI()
            model = os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo")
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:  # pragma: no cover
            logger.warning("OpenAI call failed: %s", exc)
            return ""

    logger.warning("No LLM backend configured (set OLLAMA_MODEL or OPENAI_API_KEY)")
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ollama_available() -> bool:
    """Return True if the Ollama server answers a lightweight health check."""
    try:
        r = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _call_ollama(model: str, prompt: str, timeout: float) -> str:
    """POST to the Ollama generate endpoint and return the concatenated text.

    Ollama streams one JSON object per line; we join all ``response`` fields.
    """
    try:
        r = requests.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json().get("response", "")
    except Exception as exc:
        logger.warning("Ollama call failed: %s", exc)
        return ""
