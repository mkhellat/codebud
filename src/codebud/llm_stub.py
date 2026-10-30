"""
codebud/llm_stub.py

Stub layer that hides the details of talking to an LLM.  The rest of the
codebase calls `call_llm(prompt)` and expects a string containing the raw
model output (usually JSON).  No parsing or error handling is performed by
this module.

The backend is chosen explicitly (phase 8, decision D12a) with
CODEBUD_BACKEND or `backend` in config.toml:

* "ollama" (default): the Ollama REST API at OLLAMA_BASE_URL
  (`/api/generate`, streamed) with OLLAMA_MODEL, sending the same `num_ctx`
  on every request and bypassing HTTP proxies for a local server.
* "openai": the OpenAI Python client (the optional extra `codebud[openai]`)
  with OPENAI_API_KEY and OPENAI_MODEL (default gpt-3.5-turbo).

There is no fallback from one to the other: prompts go to OpenAI only when
it was chosen. When the chosen backend cannot be used, `call_llm` raises
LLMUnavailable saying why.

It also holds the helpers around the model: warm-up and heartbeat, model
listing, conversation summaries for /compact. No network operations are
performed unless a real model is requested.
"""

import ipaddress
import logging
import os
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

import requests

from codebud import privacy

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE = "http://localhost:11434"
BACKENDS = ("ollama", "openai")


class LLMUnavailable(RuntimeError):
    """The chosen LLM backend cannot be used; the message says why."""


def backend() -> str:
    """The chosen backend: CODEBUD_BACKEND (lower-cased), default "ollama"."""
    return (os.environ.get("CODEBUD_BACKEND") or "ollama").strip().lower()


def backend_description() -> str:
    """One line saying where prompts go, for doctor and chat."""
    if backend() == "openai":
        return f"OpenAI ({openai_model()}): prompts leave this machine"
    base = ollama_base()
    return f"ollama at {base} ({'local' if _is_loopback(base) else 'remote'})"


def ollama_base() -> str:
    """The Ollama server URL: OLLAMA_BASE_URL, else localhost.

    Read on every call, not at import, so a value from the config file
    (settings.apply) is never missed.
    """
    return os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE


# Context window requested from Ollama on every call. Must be identical for
# all requests: a different num_ctx makes Ollama reload the model and drop
# the cached prompt prefix. Override with CODEBUD_NUM_CTX.
_DEFAULT_NUM_CTX = 8192


# Model used when the OpenAI backend is active and OPENAI_MODEL is unset.
DEFAULT_OPENAI_MODEL = "gpt-3.5-turbo"


def openai_model() -> str:
    """Return the OpenAI model: OPENAI_MODEL, else DEFAULT_OPENAI_MODEL."""
    return os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL


def num_ctx() -> int:
    """Return the context window (tokens) to request from Ollama.

    Ollama's own default (4096 on 0.40) is small enough that long planner
    prompts get their beginning, i.e. the instructions, silently cut off.
    Invalid CODEBUD_NUM_CTX values fall back to the default with a warning.
    """
    raw = os.environ.get("CODEBUD_NUM_CTX", "")
    if not raw:
        return _DEFAULT_NUM_CTX
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning("Ignoring invalid CODEBUD_NUM_CTX=%r; using %d", raw, _DEFAULT_NUM_CTX)
        return _DEFAULT_NUM_CTX
    return value


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def ollama_proxies(base: str) -> dict[str, None] | None:
    """Return the ``proxies`` argument for a request to the Ollama at ``base``.

    requests honours HTTP_PROXY even for localhost, so with a system-wide
    proxy (e.g. Privoxy) and no NO_PROXY, every call to a local Ollama would
    be sent to the proxy and fail. Loopback addresses therefore bypass any
    proxy; a remote Ollama keeps the environment's proxy settings (None).
    """
    if _is_loopback(base):
        return {"http": None, "https": None, "all": None}
    return None


def is_remote(base: str) -> bool:
    """Whether the URL ``base`` points at another machine."""
    return not _is_loopback(base)


def remote_http_refusal(base: str) -> str | None:
    """Why prompts must not go to ``base``, or None if they may.

    A remote server over plain http would receive prompts unencrypted, so
    it needs CODEBUD_ALLOW_REMOTE_HTTP (decision D12a); https and this
    machine need nothing.
    """
    if not is_remote(base) or urlsplit(base).scheme != "http":
        return None
    from codebud import settings

    if settings.flag("CODEBUD_ALLOW_REMOTE_HTTP", False, unknown=False):
        return None
    return (
        f"OLLAMA_BASE_URL {base} is another machine over plain http: prompts would "
        "cross the network unencrypted. Use https, or allow it with "
        "CODEBUD_ALLOW_REMOTE_HTTP=1 (allow_remote_http = true in your config.toml)."
    )


def _is_loopback(base: str) -> bool:
    """Whether the URL ``base`` points at this machine (localhost, 127.x, ::1)."""
    host = urlsplit(base).hostname or ""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def call_llm(
    prompt: str,
    timeout: float = 600.0,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Send ``prompt`` to the configured LLM and return the raw text output.

    Uses the backend chosen with CODEBUD_BACKEND (see the module
    docstring); raises LLMUnavailable if it cannot be used.

    ``timeout`` is the maximum total wall-clock seconds to wait.  On CPU-only
    hardware prefill alone can take several minutes for a 300-token prompt, so
    the default is generous.  The Ollama call uses streaming internally so the
    HTTP read timeout never fires mid-generation.

    ``on_chunk`` is an optional callback that receives each text fragment as
    it arrives from the model.  The CLI uses this to drive the progress
    indicator without coupling the display code to this module.
    """

    chosen = backend()
    if privacy.level() == "locked":
        if chosen == "openai":
            raise LLMUnavailable(
                privacy.locked_refusal("the openai backend (prompts go to OpenAI)")
            )
        if is_remote(ollama_base()):
            raise LLMUnavailable(privacy.locked_refusal(f"the Ollama at {ollama_base()}"))
    if chosen == "openai":
        return _call_openai(prompt)
    if chosen != "ollama":
        raise LLMUnavailable(
            f"Unknown CODEBUD_BACKEND '{chosen}': choose ollama or openai "
            "(or set `backend` in config.toml)."
        )
    ollama_model = os.environ.get("OLLAMA_MODEL")
    if not ollama_model:
        raise LLMUnavailable(
            "No model to plan with: OLLAMA_MODEL is not set (or `model` in config.toml). "
            "OpenAI is used only when chosen with CODEBUD_BACKEND=openai."
        )
    refusal = remote_http_refusal(ollama_base())
    if refusal:
        raise LLMUnavailable(refusal)
    if not _ollama_available():
        raise LLMUnavailable(
            f"Ollama is not reachable at {ollama_base()}. Prompts are never sent "
            "anywhere else unless you choose CODEBUD_BACKEND=openai."
        )
    return _call_ollama(ollama_model, prompt, timeout, on_chunk=on_chunk)


def _call_openai(prompt: str) -> str:
    """Send ``prompt`` to OpenAI (only when CODEBUD_BACKEND=openai)."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise LLMUnavailable("CODEBUD_BACKEND=openai but OPENAI_API_KEY is not set.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMUnavailable(
            "CODEBUD_BACKEND=openai but the OpenAI client is not installed; "
            "run: pip install 'codebud[openai]'"
        ) from exc
    try:
        resp = OpenAI().chat.completions.create(
            model=openai_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
    except Exception as exc:  # network, auth, quota...
        raise LLMUnavailable(f"OpenAI call failed: {exc}") from exc
    return resp.choices[0].message.content or ""


def call_llm_plain(
    user_message: str,
    history: list[dict[str, str]] | None = None,
    on_chunk: Callable[[str], None] | None = None,
    context_block: str = "",
) -> str:
    """Send a conversational (non-JSON) prompt to the LLM and return plain text.

    Used as a fallback when the planner cannot fulfil the user's request so the
    agent can still give a helpful natural-language reply.  ``context_block``
    is retrieved project context (codebud.rag.context.format_context); the
    model is asked to cite the files it uses from it.
    """
    history_lines = ""
    if history:
        history_lines = "\n".join(f"{t['role'].capitalize()}: {t['content']}" for t in history[-6:])
        history_lines = f"Conversation so far:\n{history_lines}\n\n"
    if context_block:
        context_block += (
            "When your answer relies on the project context, cite each file you use "
            "as path:start-end.\n\n"
        )

    prompt = (
        f"You are a helpful coding assistant.\n\n{history_lines}{context_block}"
        f"User: {user_message}\nAssistant:"
    )
    return call_llm(prompt, on_chunk=on_chunk)


def summarize_conversation(
    turns: list[dict[str, str]],
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Ask the LLM for a short summary of ``turns`` (used by /compact in chat).

    Returns the summary text, or '' if the model gave no answer.
    """
    transcript = "\n".join(f"{t['role'].capitalize()}: {t['content']}" for t in turns)
    prompt = (
        "Summarize the following conversation between a user and a coding agent "
        "in a few sentences. Keep file names, commands, decisions and anything "
        "the user may refer back to. Output only the summary.\n\n"
        f"{transcript}\n\nSummary:"
    )
    try:
        return call_llm(prompt, on_chunk=on_chunk).strip()
    except LLMUnavailable as exc:
        logger.warning("No summary: %s", exc)
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
                f"{ollama_base()}/api/generate",
                proxies=ollama_proxies(ollama_base()),
                json={
                    "model": model,
                    "prompt": "hi",
                    "stream": False,
                    "options": {"num_predict": 2, "num_ctx": num_ctx()},
                },
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
                f"{ollama_base()}/api/generate",
                proxies=ollama_proxies(ollama_base()),
                json={
                    "model": model,
                    "prompt": "hi",
                    "stream": False,
                    "options": {"num_predict": 2, "num_ctx": num_ctx()},
                },
                timeout=(10, 120),
            )
            logger.debug("Pre-warmup complete")
        except Exception as exc:
            logger.debug("Pre-warmup failed (non-fatal): %s", exc)

    t = threading.Thread(target=_warm, daemon=True, name="codebud-prewarm")
    t.start()


def warm_model_sync(
    model: str,
    on_chunk: Callable[[str], None] | None = None,
    timeout: float = 120.0,
) -> bool:
    """Synchronously load ``model`` into memory, streaming so on_chunk can
    drive a live progress indicator. Returns True once the model responds.

    Unlike prewarm_model() (fire-and-forget, background thread, used for
    silent session startup) this blocks and reports success/failure, for use
    when a user actively switches models and is waiting to know it worked.
    """
    if not _ollama_available():
        return False
    try:
        r = requests.post(
            f"{ollama_base()}/api/generate",
            proxies=ollama_proxies(ollama_base()),
            json={
                "model": model,
                "prompt": "hi",
                "stream": True,
                "options": {"num_predict": 2, "num_ctx": num_ctx()},
            },
            stream=True,
            timeout=(10, timeout),
        )
        r.raise_for_status()
        import json as _json

        for line in r.iter_lines():
            if not line:
                continue
            chunk = _json.loads(line)
            text = chunk.get("response", "")
            if on_chunk is not None and text:
                on_chunk(text)
            if chunk.get("done"):
                return True
        return True
    except Exception as exc:
        logger.warning("Model warmup failed for %s: %s", model, exc)
        return False


def _ollama_available() -> bool:
    """Return True if the Ollama server answers a lightweight health check."""
    try:
        r = requests.get(
            f"{ollama_base()}/api/tags", timeout=2, proxies=ollama_proxies(ollama_base())
        )
        return r.status_code == 200
    except Exception:
        return False


def list_models() -> list[str]:
    """Return the names of models currently downloaded in Ollama, or [] on failure."""
    try:
        r = requests.get(
            f"{ollama_base()}/api/tags", timeout=5, proxies=ollama_proxies(ollama_base())
        )
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return []


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
            f"{ollama_base()}/api/generate",
            proxies=ollama_proxies(ollama_base()),
            json={
                "model": model,
                "prompt": prompt,
                "stream": True,
                "options": {"temperature": 0, "num_ctx": num_ctx()},
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
