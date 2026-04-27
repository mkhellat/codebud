# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
make install-dev          # create .venv/ and install all dependencies (including dev)

# Development
make test                 # run the full test suite with pytest
make coverage             # run tests with branch coverage → htmlcov/index.html
make lint                 # ruff check
make format               # ruff format + ruff check --fix

# Run a single test
.venv/bin/pytest tests/test_planner.py::test_foo -v

# Run the agent (after activating or using the venv binary)
export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
codebud doctor            # verify environment before first run
codebud run "your task"   # plan + auto-execute
codebud run -i "your task" # plan + interactive per-step approval (planned)
codebud plan "your task"  # plan only (add --json for raw JSON output)
codebud chat              # interactive REPL — continuous conversation with history
codebud models            # list downloaded Ollama models
codebud config            # show resolved env-var configuration
codebud version           # codebud + ollama + python versions

# Distribution
make dist                 # build sdist + wheel into dist/
```

The `codebud` binary is provided by the `[project.scripts]` entry in `pyproject.toml`, pointing to `run_agent:main`. A bare `codebud "message"` (no subcommand) is equivalent to `codebud run "message"`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | (required) | Model tag as shown by `codebud models` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `EMBED_MODEL` | same as `OLLAMA_MODEL` | Model used for embeddings |
| `WEB_SEARCH_ENABLED` | `0` | Set to `1` to enable DuckDuckGo search |
| `OPENAI_API_KEY` | — | Fallback to OpenAI if Ollama is unavailable |

## Architecture

### Request flow

```
CLI (run_agent.py)
  → AgentCore.handle_user_message()
      → LLMPlanner.generate_plan()    # builds structured prompt, calls LLM, parses JSON
      → AgentCore._validate_plan()    # checks structure + ToolRegistry + SafetyEngine
  → Executor.execute_plan()           # runs steps one at a time, stops on first failure
      → ToolRegistry.get(tool_name).run(args)
          → Sandbox.run_command()     # for the 'command' tool
      → MemoryStore.add_snapshot()    # persists step + result to data/memory/entries.json
```

`AgentCore` (`agent/core.py`) is the only orchestrator. It owns one instance of each subsystem and wires them together. It has no UI knowledge.

### Plan contract

The LLM must return, and the rest of the system expects, this exact JSON shape:

```json
{
  "status": "ok",
  "plan": [
    {"id": "step_0", "description": "...", "tool": "<tool_name>", "args": {...}},
    ...
  ]
}
```

Step ids must be sequential (`step_0`, `step_1`, …). The planner makes one primary attempt and one automatic retry (with a minimal no-preamble prompt) if the first response is not valid JSON.

### LLM backend (`agent/llm_stub.py`)

`call_llm(prompt)` is the single entry point to the model. Priority:

1. Ollama REST API (`/api/generate`, streaming) if `OLLAMA_MODEL` is set and the server is reachable.
2. OpenAI ChatCompletion if `OPENAI_API_KEY` is set.
3. Returns an empty string and logs a warning if neither is configured.

The default Ollama timeout is 600 s (generous for CPU-only inference). Streaming is used so the HTTP read timeout never fires mid-generation.

### Tools (`agent/tools/`)

All tools are registered in `ToolRegistry.__init__`. Each tool implements `.run(args) → {"stdout": str, "stderr": str, "returncode": int}`. Available tools:

| Name | Class | Purpose |
|---|---|---|
| `file_read` | `FileReadTool` | Read a specific named file |
| `file_write` | `FileWriteTool` | Write/overwrite a file |
| `patch` | `PatchTool` | Apply a unified diff |
| `command` | `CommandTool` | Run a shell command via `Sandbox` |
| `web_search` | `WebSearchTool` | DuckDuckGo search (requires `WEB_SEARCH_ENABLED=1`) |
| `embed` | `EmbedTool` | Generate embeddings |
| `search_embeddings` | `EmbeddingSearchTool` | Semantic search over the embedding index |

### Safety (`agent/safety.py`)

`SafetyEngine` is consulted twice: by `LLMPlanner._validate_plan_structure()` and again by `AgentCore._validate_plan()`. For `command` steps it checks the command string against `config/harmless_commands.json` (always allowed) and `config/powerful_commands.json` (also allowed). Any command not matching either list is rejected. Edit these JSON arrays to expand or restrict what the agent may run.

`Sandbox` adds a second, independent guard: a hardcoded blocklist (`rm -rf /`, `shutdown`, etc.) and a 10-second subprocess timeout.

### Memory (`agent/memory.py`)

After each successful step, `MemoryStore.add_snapshot()` appends a JSON record to `data/memory/entries.json`. The file is loaded on startup so history persists across runs.

### Conversation history (in progress)

`AgentCore` will maintain `self._history: list[dict]` — a rolling window of the
last N user/assistant turns. Each turn is passed to `LLMPlanner.generate_plan(history=...)`
and injected into the prompt so the model sees what it said and did before.

Inside `codebud chat`, `/`-prefixed inputs are parsed as local slash commands
(`/help`, `/history`, `/clear`, `/plan`, `/undo`, `/doctor`, `/model`) and never
sent to the planner.

### OpenClaw integration (`openclaw/SKILL.md`)

`make install-skill` copies `openclaw/SKILL.md` to `~/.openclaw/skills/codebud/` and symlinks the `codebud` binary into `BINDIR`. The SKILL.md teaches the OpenClaw browser gateway how to invoke codebud. `AgentCore.regenerate()` is the hook OpenClaw calls to request a fresh plan for the same user message.

OpenClaw is the browser UI layer on top of the same `AgentCore` backbone. Once
conversation history is solid in the CLI, `openclaw/skill.py` will maintain a
`session_id → AgentCore` map so each browser session has its own persistent context.

## Adding a new tool

1. Create `agent/tools/mytool.py` with a class that exposes `.run(args) → dict` and optionally `.description` and `.usage_hint` string attributes.
2. Import and register it in `ToolRegistry.__init__` in `agent/tools/tool_registry.py`.
3. If the tool executes shell commands, add the relevant prefixes to `config/harmless_commands.json` or `config/powerful_commands.json`.
4. Add tests under `tests/`.
