"""
agent/cli/errors.py

Maps error strings returned by the planner/executor to beginner-friendly
explanations and actionable fix hints. Mirrors the Troubleshooting chapter
so the CLI surfaces what the docs already document.
"""


_RULES: list[tuple[str, str, str]] = [
    # (substring to match, explanation, fix command)
    (
        "Connection refused",
        "The Ollama server is not running.",
        "sudo systemctl start ollama  (or: ollama serve)",
    ),
    (
        "could not connect",
        "Codebud cannot reach the Ollama server.",
        "sudo systemctl start ollama  (or: ollama serve)",
    ),
    (
        "model not found",
        "The model named in OLLAMA_MODEL is not downloaded.",
        "ollama pull $OLLAMA_MODEL",
    ),
    (
        "LLM returned empty output",
        "The model produced no response. This usually means the server "
        "is not running or OLLAMA_MODEL is not set.",
        "Run `codebud doctor` to diagnose.",
    ),
    (
        "LLM returned invalid JSON",
        "The model's output could not be parsed as JSON. "
        "Try `codebud plan` to inspect the raw output.",
        "Switch to a larger model (e.g. 3b instead of 1.5b) for more reliable JSON.",
    ),
    (
        "LLM returned malformed plan",
        "The model returned JSON but its structure was wrong "
        "(missing required fields or invalid tool name).",
        "Try rephrasing the request, or use `codebud plan` to inspect.",
    ),
    (
        "HTTP 500",
        "Ollama returned an internal server error. "
        "This is often caused by the OS out-of-memory killer terminating the runner.",
        "Run: dmesg | grep -i oom   Then switch to a smaller model.",
    ),
    (
        "out of memory",
        "The model weights did not fit in available RAM.",
        "Close other applications, or switch to a smaller model variant.",
    ),
    (
        "timed out",
        "The request took too long. On CPU-only hardware the 3B model "
        "takes ~5 minutes per call — this is normal.",
        "Wait a little longer, or switch to the 1.5B model for faster (but less accurate) results.",
    ),
    (
        "No previous user message",
        "There is nothing to regenerate — no request has been sent yet.",
        "Send a request first with `codebud run`.",
    ),
    (
        "step is not safe",
        "A plan step was blocked by the safety engine.",
        "Rephrase the request, or review config/harmless_commands.json.",
    ),
    (
        "not safe",
        "A plan step was rejected by the safety engine.",
        "Rephrase the request, or review config/harmless_commands.json.",
    ),
]


def explain(error: str) -> tuple[str, str] | None:
    """Return (explanation, fix_hint) for a known error, or None if unknown."""
    lower = error.lower()
    for fragment, explanation, fix in _RULES:
        if fragment.lower() in lower:
            return explanation, fix
    return None


def print_error(error: str, *, label: str = "Error") -> None:
    """Print a formatted error with an explanation and fix hint if known."""
    import sys

    print(f"\n{label}: {error}", file=sys.stderr)
    result = explain(error)
    if result:
        explanation, fix = result
        print(f"  What happened: {explanation}", file=sys.stderr)
        print(f"  How to fix:    {fix}", file=sys.stderr)
    else:
        print("  Run `codebud doctor` to check your environment.", file=sys.stderr)
    print(file=sys.stderr)
