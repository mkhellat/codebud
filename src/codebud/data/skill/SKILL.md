---
name: codebud
description: Privacy-first, local coding agent that plans and runs multi-step coding tasks (read files, run commands, write patches) on-device with Ollama, no API key or network needed. Use it to delegate a coding task to a local model, e.g. when the user wants work done privately or offline.
metadata:
  {
    "openclaw": {
      "emoji": "🐛",
      "requires": { "bins": ["codebud"] }
    }
  }
---

# Codebud — privacy-first, local coding agent

Codebud turns a natural-language request into a plan of steps (read a file,
run a command, write or patch a file) and executes them, using a model that
runs locally in Ollama. Nothing leaves the machine.

## Before using it

Ollama must be running with a model downloaded, and `OLLAMA_MODEL` must name
it (there is no default):

```bash
ollama serve &                                   # if not already running
ollama pull qwen2.5-coder:3b-instruct-q4_K_M
export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
codebud doctor                                   # checks everything, prints fixes
```

Codebud works on the files of the directory it is started in, so `cd` into
the project first.

## Project search (index the project first)

Without an index the planner can only guess file names. Index the project
once, and every request gets the most relevant code as context, so plans
read and patch the files that really exist:

```bash
ollama pull all-minilm          # small embedding model (45 MB)
codebud index                   # about a minute per ~1,000 chunks on a CPU
codebud index --status          # what changed since the last run
```

Re-run `codebud index` after larger changes (it only embeds changed files).
It needs numpy (`pip install 'codebud[rag]'` or the distribution's
package); without an index or numpy, requests simply run without context.
`--no-rag` on `run`/`plan`/`chat` turns it off for one call. For edits,
prefer the 7B model (`-m qwen2.5-coder:7b-instruct-q4_K_M`): with context
it planned 5 of 6 test edits correctly, the 3B model none.

## Running a task from another agent

`codebud run "<task>"` plans the task and executes it. From another agent,
add the global `--no-progress` flag (before `run`) so the output has no
spinner:

```bash
codebud --no-progress run "read README.md and summarise it"
```

Powerful commands — ones that run code or change files (`python`, `pytest`,
`ruff`, `cp`, `mv`, `sed`, ...) — and web searches (when enabled) need a
confirmation. Called without a terminal, `run` skips them and exits with
status 1, saying so. Pass `--yes` to allow them unattended, once the user
has agreed:

```bash
codebud --no-progress run --yes "run the test suite and report failures"
```

Exit status: 0 when every step ran, 1 on a planning or execution error or
when a powerful step was not confirmed.

## Inspecting a plan without running it

```bash
codebud plan "add type hints to utils.py"
codebud plan --json "run the tests"     # machine-readable plan
codebud plan -x "list the python files" # plan, then execute it
```

## Interactive use (for the user, not for agents)

```bash
codebud chat                     # multi-turn session
codebud chat --mode plan         # modes: ask, auto, full-auto, plan, edit
codebud chat --resume            # continue the most recent session
codebud sessions                 # list saved sessions
```

Modes: `ask` approves the plan and each step; `auto` approves the plan once
(powerful steps and web searches still ask); `full-auto` asks nothing; `plan` never executes;
`edit` allows only file tools, no shell commands.

## Other commands

| Command            | Purpose                                           |
| ------------------ | ------------------------------------------------- |
| `codebud doctor`   | Check Python, Ollama, the model and policy files  |
| `codebud index`    | Build/refresh the project index (`--status`)      |
| `codebud models`   | List downloaded models, marking the active one    |
| `codebud config`   | Show settings, data directory and policy files    |
| `codebud history`  | Show past executed steps                          |
| `codebud trust`    | Show what a project's policy adds (trusting it is the user's call) |
| `codebud key status` | Whether encryption at rest is on, and the key   |
| `codebud version`  | Show versions                                     |

## Safety

Only commands on an allow-list run, matched by whole words; arguments that
turn a program into "run anything" (`find -exec`, `python -c`,
`awk 'BEGIN{system(...)}'`, `sed 'e ...'`, ...) are refused, and so are
catastrophic commands (powering off, erasing a disk, `rm -r` on `/` or a
home directory) whatever the allow-list says. File steps write only inside
the project (never `.git/` or `.codebud/`) and never read the user's
secret files. Command steps run isolated (no network unless declared,
project-only writes, secrets hidden) with an allow-listed environment.

A project's `.codebud/` policy can narrow the user's allow-list at once,
but widens it only after the **user** runs `codebud trust` there (or
answers yes when asked); without a terminal, widening is ignored with a
notice. Do not try to work around this: tell the user what the project
policy would add and let them decide. If the project index is encrypted
and no passphrase is available, codebud plans without project context and
says so.

## Environment variables

| Variable             | Default                  | Purpose                                |
| -------------------- | ------------------------ | -------------------------------------- |
| `OLLAMA_MODEL`       | *(required)*             | Model to plan with                     |
| `OLLAMA_BASE_URL`    | `http://localhost:11434` | Ollama server                          |
| `CODEBUD_NUM_CTX`    | `8192`                   | Context window requested from Ollama   |
| `CODEBUD_PRIVACY`    | `private`                | `locked`: nothing leaves the machine   |
| `EMBED_MODEL`        | `all-minilm`             | Embedding model for project search     |
| `CODEBUD_RAG`        | `1`                      | `0` plans without project context      |
| `WEB_SEARCH_ENABLED` | `0`                      | `1` enables the web search tool        |
