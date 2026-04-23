---
name: codebud
description: Local Cursor-style coding agent. Plans and executes multi-step coding tasks on-device using Ollama — no API key required.
metadata:
  {
    "openclaw": {
      "emoji": "🐛",
      "requires": { "bins": ["codebud"] }
    }
  }
---

# Codebud — Local Coding Agent

Codebud is a CPU-friendly coding agent that runs entirely on-device using Ollama.
It turns a natural-language request into a multi-step plan (read files, run commands,
write patches) and executes each step automatically.

## Prerequisites

Ollama must be running and the model must be pulled:

```bash
sudo systemctl start ollama
ollama pull qwen2.5-coder:3b-instruct-q4_K_M   # default model
```

Run the health checker to confirm everything is ready:

```bash
codebud doctor
```

## Usage

### One-shot task

```bash
codebud run "list the files in the current directory"
codebud run "read README.md and summarize it"
codebud run "run the tests and report which ones fail"
```

Use `--no-progress` to suppress the spinner when running inside another agent or script:

```bash
codebud --no-progress run "create hello.py that prints Hello, world"
```

### Plan only (no execution)

```bash
codebud plan "refactor agent/planner.py to add type hints"
codebud plan --json "run the tests"   # machine-readable JSON output
```

### Interactive chat (multi-turn)

```bash
codebud chat
```

Keeps memory across turns in the same session. At each step the agent asks `[y/N/a/q]`
(yes / no / auto-execute all remaining / quit).

### Health check

```bash
codebud doctor
```

Checks Python version, Ollama reachability, model availability, config files, disk
space, and RAM. Prints a PASS/FAIL line per check and the exact fix command for each
failure.

### List models

```bash
codebud models
```

Shows all Ollama models with the currently active model highlighted.

### Show config

```bash
codebud config
```

Prints all resolved environment variables (OLLAMA_BASE_URL, OLLAMA_MODEL, etc.).

## Working directory

Codebud operates on the files in the directory where it is invoked. Change into the
project you want to work on before running it:

```bash
cd ~/Projects/myproject
codebud run "add docstrings to every public function in utils.py"
```

## Global options

| Flag            | Effect                                          |
| --------------- | ----------------------------------------------- |
| `--no-progress` | Disable spinner and token counter               |
| `--verbose`     | Print full step output instead of one-line summary |
| `--help`        | Show help for any subcommand                    |

## Environment variables

| Variable          | Default                          | Purpose                         |
| ----------------- | -------------------------------- | ------------------------------- |
| `OLLAMA_BASE_URL` | `http://localhost:11434`         | Ollama API endpoint             |
| `OLLAMA_MODEL`    | `qwen2.5-coder:3b-instruct-q4_K_M` | Model to use for planning    |
| `WEB_SEARCH_ENABLED` | `0`                           | Set to `1` to enable DuckDuckGo search tool |
