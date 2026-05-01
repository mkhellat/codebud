#!/usr/bin/env python3
"""
benchmark.py -- Measure Codebud planning prompt vs short prompt timing.

Produces concrete, reproducible evidence of where time goes in a Codebud
request by querying Ollama's /api/generate with stream=False, which returns
precise internal timing fields (prompt_eval_duration, eval_duration, etc.).

Usage (from Codebud project root, Ollama must be running with model warm):

    export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
    python scripts/benchmark.py

    # To pre-warm the model before measuring:
    python scripts/benchmark.py --warm-first

    # To test a different prompt:
    python scripts/benchmark.py --prompt "your prompt here"

    # To output raw JSON for each test:
    python scripts/benchmark.py --json

Requirements: requests (already in requirements.txt)
The script must be run from the project root so 'agent' is importable.
"""

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, ".")


OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "")


# ---------------------------------------------------------------------------
# Core measurement function
# ---------------------------------------------------------------------------


def ollama_timed_call(label: str, prompt: str, raw_json: bool = False) -> dict:
    """POST prompt to Ollama with stream=False and return parsed timing dict."""
    print(f"\n{'='*65}")
    print(f"  TEST: {label}")
    print(f"  Prompt: {len(prompt)} chars / first 70 chars: {prompt[:70]!r}")
    print(f"  Sending to {OLLAMA_BASE}/api/generate ...")
    sys.stdout.flush()

    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=(10, 600),
        )
        r.raise_for_status()
    except requests.exceptions.Timeout:
        print("  ERROR: request timed out (600 s). Is the model too slow?")
        return {}
    except requests.exceptions.RequestException as exc:
        print(f"  ERROR: {exc}")
        return {}

    wall = time.monotonic() - t0
    data = r.json()

    if raw_json:
        print("\n  Raw Ollama response:")
        print(json.dumps(data, indent=4))

    def ns_to_s(field):
        return data.get(field, 0) / 1e9

    load_s  = ns_to_s("load_duration")
    pe_s    = ns_to_s("prompt_eval_duration")
    e_s     = ns_to_s("eval_duration")
    total_s = ns_to_s("total_duration")
    pe_n    = data.get("prompt_eval_count", 0)
    e_n     = data.get("eval_count", 0)

    print(f"\n  Ollama timing (nanoseconds → seconds):")
    print(f"    load_duration        : {load_s:7.2f} s  (model warm check)")
    print(f"    prompt_eval_count    : {pe_n:7d} tokens")
    print(f"    prompt_eval_duration : {pe_s:7.2f} s  "
          f"[prefill @ {pe_n/pe_s:.1f} tok/s]" if pe_s > 0 else
          f"    prompt_eval_duration : {pe_s:7.2f} s")
    print(f"    eval_count           : {e_n:7d} tokens")
    print(f"    eval_duration        : {e_s:7.2f} s  "
          f"[generation @ {e_n/e_s:.1f} tok/s]" if e_s > 0 else
          f"    eval_duration        : {e_s:7.2f} s")
    print(f"    total_duration       : {total_s:7.2f} s")
    print(f"    wall-clock           : {wall:7.2f} s")
    print(f"\n  Model response (first 150 chars):")
    print(f"    {data.get('response', '')[:150]!r}")

    return {
        "label": label,
        "prompt_chars": len(prompt),
        "prompt_tokens": pe_n,
        "load_s": load_s,
        "prefill_s": pe_s,
        "gen_tokens": e_n,
        "gen_s": e_s,
        "total_s": total_s,
        "wall_s": wall,
        "prefill_tok_per_s": pe_n / pe_s if pe_s > 0 else 0,
        "gen_tok_per_s": e_n / e_s if e_s > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Pre-warmup helper
# ---------------------------------------------------------------------------


def warm_model():
    """Send a small request to load the model before timing."""
    print(f"Pre-warming {MODEL} ...")
    try:
        requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": MODEL, "prompt": "hi", "stream": False,
                  "options": {"num_predict": 2}},
            timeout=(10, 300),
        )
        print("Model warm.\n")
    except Exception as exc:
        print(f"Warm-up failed (non-fatal): {exc}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", default="say hello in exactly 5 words",
                        help="User prompt to benchmark (default: 'say hello in exactly 5 words')")
    parser.add_argument("--warm-first", action="store_true",
                        help="Send a small warm-up request before benchmarking")
    parser.add_argument("--json", action="store_true",
                        help="Print raw JSON response from Ollama for each test")
    args = parser.parse_args()

    if not MODEL:
        print("ERROR: set OLLAMA_MODEL environment variable before running.")
        print("  export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M")
        sys.exit(1)

    # Check Ollama is up
    try:
        requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3).raise_for_status()
    except Exception as exc:
        print(f"ERROR: cannot reach Ollama at {OLLAMA_BASE}: {exc}")
        print("  Start it with: ollama serve")
        sys.exit(1)

    print(f"Codebud benchmark")
    print(f"  Model    : {MODEL}")
    print(f"  Ollama   : {OLLAMA_BASE}")
    print(f"  Prompt   : {args.prompt!r}")

    if args.warm_first:
        warm_model()

    # Build the planning prompt
    try:
        from agent.planner import LLMPlanner
        from agent.safety import SafetyEngine
        from agent.sandbox import Sandbox
        from agent.tools.tool_registry import ToolRegistry

        sandbox = Sandbox()
        registry = ToolRegistry(sandbox)
        safety = SafetyEngine()
        planner = LLMPlanner(registry, safety)
        planning_prompt = planner._build_prompt(args.prompt)
    except ImportError as exc:
        print(f"ERROR: cannot import agent package: {exc}")
        print("  Run from the project root with the venv active.")
        sys.exit(1)

    # Run the two API tests
    r_short = ollama_timed_call(
        "SHORT PROMPT (raw model baseline)",
        args.prompt,
        raw_json=args.json,
    )
    r_plan = ollama_timed_call(
        "PLANNING PROMPT (what Codebud sends)",
        planning_prompt,
        raw_json=args.json,
    )

    if not r_short or not r_plan:
        print("\nOne or more tests failed. Check Ollama status.")
        sys.exit(1)

    # Summary
    print(f"\n{'='*65}")
    print("  SUMMARY")
    print(f"\n  {'Test':<40} {'Tokens':>7} {'Prefill':>9} {'Gen':>7} {'Total':>8}")
    print(f"  {'-'*40} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")
    for r in [r_short, r_plan]:
        print(f"  {r['label']:<40} {r['prompt_tokens']:>7d} "
              f"{r['prefill_s']:>8.2f}s {r['gen_s']:>6.2f}s "
              f"{r['total_s']:>7.2f}s")

    token_diff   = r_plan["prompt_tokens"] - r_short["prompt_tokens"]
    prefill_diff = r_plan["prefill_s"] - r_short["prefill_s"]
    pct_prefill  = r_plan["prefill_s"] / r_plan["total_s"] * 100

    print(f"\n  Extra tokens in planning prompt  : {token_diff}")
    print(f"  Extra prefill time               : {prefill_diff:.2f} s")
    if token_diff > 0:
        print(f"  Prefill cost per extra token     : {prefill_diff / token_diff * 1000:.1f} ms/token")
    print(f"  Prefill as % of planning total   : {pct_prefill:.1f}%")
    print(f"\n  Prefill speed (short)            : {r_short['prefill_tok_per_s']:.1f} tok/s")
    print(f"  Prefill speed (planning)         : {r_plan['prefill_tok_per_s']:.1f} tok/s")
    print(f"  Generation speed (short)         : {r_short['gen_tok_per_s']:.1f} tok/s")
    print(f"  Generation speed (planning)      : {r_plan['gen_tok_per_s']:.1f} tok/s")


if __name__ == "__main__":
    main()
