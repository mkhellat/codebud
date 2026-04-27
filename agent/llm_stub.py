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

import logging
import os
import threading
import time
from collections.abc import Callable

import requests

logger = logging.getLogger(__name__)

# Ollama default base URL; override with OLLAMA_BASE_URL if needed.
_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def call_llm(
    prompt: str,
    timeout: float = 600.0,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Send ``prompt`` to the configured LLM and return the raw text output.

    Priority order:

    1. Ollama REST API if ``OLLAMA_MODEL`` is set and the server is reachable.
    2. OpenAI API if ``OPENAI_API_KEY`` is present.
    3. Otherwise return an empty string and log a warning.

    ``timeout`` is the maximum total wall-clock seconds to wait.  On CPU-only
    hardware prefill alone can take several minutes for a 300-token prompt, so
    the default is generous.  The Ollama call uses streaming internally so the
    HTTP read timeout never fires mid-generation.

    ``on_chunk`` is an optional callback that receives each text fragment as
    it arrives from the model.  The CLI uses this to drive the progress
    indicator without coupling the display code to this module.
    """

    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model and _ollama_available():
        return _call_ollama(ollama_model, prompt, timeout, on_chunk=on_chunk)

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


class ModelHeartbeat:
    """Keeps the Ollama model loaded by pinging it every ``interval`` seconds.

    Ollama unloads a model after it has been idle for its keep_alive window
    (default 5 minutes).  While a session is active we don't want that to
    happen between requests, so this class runs a daemon thread that sends a
    zero-token generate call before each idle-timeout window closes.

    Usage::

        hb = ModelHeartbeat()
        hb.start()          # begin pinging
        ...                 # session work
        hb.stop()           # stop pinging; Ollama will unload on its own schedule
    """

    def __init__(self, interval: float = 240.0):
        self._interval = interval  # ping every 4 min (< 5 min Ollama default)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        model = os.environ.get("OLLAMA_MODEL")
        if not model or not _ollama_available():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            self._ping()

    def _ping(self) -> None:
        model = os.environ.get("OLLAMA_MODEL")
        if not model:
            return
        try:
            requests.post(
                f"{_OLLAMA_BASE}/api/generate",
                json={"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 2}},
                timeout=(5, 60),
            )
        except Exception:
            pass  # server went away; AgentCore will surface the error on next real call


def prewarm_model() -> None:
    """Fire a tiny real request in a background daemon thread.

    Call this as early as possible in the session (e.g. in AgentCore.__init__)
    so the model is loaded into GPU/CPU memory before the user's first message
    arrives.  The thread is a daemon so it never blocks interpreter exit.
    """
    model = os.environ.get("OLLAMA_MODEL")
    if not model or not _ollama_available():
        return

    def _warm() -> None:
        try:
            logger.debug("Pre-warming model %s", model)
            requests.post(
                f"{_OLLAMA_BASE}/api/generate",
                json={"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 2}},
                timeout=(10, 120),
            )
            logger.debug("Pre-warmup complete")
        except Exception as exc:
            logger.debug("Pre-warmup failed (non-fatal): %s", exc)

    t = threading.Thread(target=_warm, daemon=True, name="codebud-prewarm")
    t.start()


def _ollama_available() -> bool:
    """Return True if the Ollama server answers a lightweight health check."""
    try:
        r = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _call_ollama(
    model: str,
    prompt: str,
    timeout: float,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Stream from the Ollama generate endpoint and return the full text.

    Using stream=True means the HTTP connection receives a token at a time,
    so the read timeout never fires during the long prefill phase on CPU-only
    hardware.  We use a generous connect timeout (10 s) and set the read
    timeout to the caller-supplied total ``timeout``.

    ``on_chunk`` is called with each response fragment so callers can drive
    a live progress indicator without blocking.
    """
    import json as _json
    import time as _time

    deadline = _time.monotonic() + timeout
    chunks = []
    try:
        r = requests.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": True,
                "options": {"temperature": 0},
            },
            stream=True,
            timeout=(10, timeout),
        )
        r.raise_for_status()
        for line in r.iter_lines():
            if _time.monotonic() > deadline:
                logger.warning("Ollama call exceeded total timeout of %ss", timeout)
                break
            if not line:
                continue
            chunk = _json.loads(line)
            text = chunk.get("response", "")
            chunks.append(text)
            if on_chunk is not None and text:
                on_chunk(text)
            if chunk.get("done"):
                break
        return "".join(chunks)
    except Exception as exc:
        logger.warning("Ollama call failed: %s", exc)
        return ""
