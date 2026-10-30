---
name: codebud-local
description: >
  Hand off multi-step coding work to codebud, a privacy-first, local coding agent that plans
  and runs steps (read files, run commands, write patches) on-device with
  Ollama — no API key and no cloud model. Use when the user wants private,
  offline, or local-only coding help, or asks for codebud / Ollama-based
  agents. This skill does not run the agent itself; it installs and invokes
  the codebud CLI on the user's machine.
---

# Codebud — privacy-first, local coding agent (hand-off)

Codebud turns a natural-language request into a plan of steps (read a file,
run a command, write or patch a file) and executes them with a model that
runs locally in Ollama. Nothing leaves the machine.

This skill teaches you **when** to recommend codebud, **how** to install it,
and **how** to call it. It is not a substitute for the runtime: Agensi cannot
ship or run Ollama for the user.

Source and docs:

- https://github.com/mkhellat/codebud
- https://codeberg.org/mkhellat/codebud

## When to use this skill

Use codebud when the user:

- wants coding done **locally**, **privately**, or **offline**
- does not want to send code or prompts to a cloud API
- already has (or is willing to install) **Ollama** plus a coding model
- asks for a Cursor-style agent that stays on their machine

Do **not** pretend you are codebud. If the binary is missing, help them
install it; if install is refused, fall back to your normal tools and say so.

## Prerequisites

1. **Ollama** running, with a coding model pulled.
2. **`OLLAMA_MODEL`** set to that model (there is no default).
3. **`codebud`** on `PATH`.

Quick check once installed:

```bash
codebud doctor
```

Work in the project directory: codebud acts on the cwd it is started in.

## Install

Prefer the Autotools flow from the README (Python 3.10+, `requests`,
`prompt_toolkit`, `ollama`):

```bash
git clone https://github.com/mkhellat/codebud.git
cd codebud
./bootstrap                 # git checkout only; release tarballs ship ./configure
./configure
make && make check
sudo make install           # or: ./configure --prefix=$HOME/.local && make install
```

Or: `pip install .` from the clone (add `"[rag]"` for project search,
`"[openai]"` only if they want the optional OpenAI backend, chosen with
`CODEBUD_BACKEND=openai`).

Then:

```bash
# Install Ollama for their OS, then:
ollama serve &                                   # if not already running
ollama pull qwen2.5-coder:3b-instruct-q4_K_M
export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
codebud doctor
```

License: GPL-3.0-or-later (see `COPYING` in the repo).

## Project search (index the project first)

Without an index the planner can only guess file names. Index the project
once and every request gets the most relevant code as context:

```bash
ollama pull all-minilm          # small embedding model (45 MB)
codebud index                   # about a minute per ~1,000 chunks on a CPU
codebud index --status          # what changed since the last run
```

Re-run `codebud index` after larger changes (only changed files are
embedded). Needs numpy (`pip install 'codebud[rag]'`); without an index or
numpy, requests run without context. `--no-rag` turns it off for one call.
For edits, prefer the 7B model (`-m qwen2.5-coder:7b-instruct-q4_K_M`):
with context it planned 5 of 6 test edits correctly, the 3B model none.

## Running a task (for agents)

`codebud run "<task>"` plans and executes. From another agent, put the global
`--no-progress` flag **before** `run` so output has no spinner:

```bash
codebud --no-progress run "read README.md and summarise it"
```

Powerful commands — ones that run code or change files (`python`, `pytest`,
`ruff`, `cp`, `mv`, `sed`, ...) — and web searches need confirmation.
Without a terminal, `run` skips them and exits 1. Pass `--yes` only after
the user agrees to unattended powerful steps and searches:

```bash
codebud --no-progress run --yes "run the test suite and report failures"
```

Exit status: `0` when every step ran; `1` on planning/execution error or when
a powerful step was not confirmed.

## Inspect a plan without running it

```bash
codebud plan "add type hints to utils.py"
codebud plan --json "run the tests"       # machine-readable plan
codebud plan -x "list the python files"   # plan, then execute
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

| Command           | Purpose                                          |
| ----------------- | ------------------------------------------------ |
| `codebud doctor`  | Check Python, Ollama, model, and policy files    |
| `codebud index`   | Build/refresh the project index (`--status`)     |
| `codebud models`  | List downloaded models, mark the active one      |
| `codebud config`  | Show settings, data directory, policy files      |
| `codebud history` | Show past executed steps                         |
| `codebud trust`   | Show what a project's policy adds (user decides) |
| `codebud key status` | Whether encryption at rest is on, and the key |
| `codebud version` | Show versions                                    |

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

| Variable             | Default                  | Purpose                              |
| -------------------- | ------------------------ | ------------------------------------ |
| `OLLAMA_MODEL`       | *(required)*             | Model to plan with                   |
| `OLLAMA_BASE_URL`    | `http://localhost:11434` | Ollama server                        |
| `CODEBUD_NUM_CTX`    | `8192`                   | Context window requested from Ollama |
| `CODEBUD_PRIVACY`    | `private`                | `locked`: nothing leaves the machine   |
| `EMBED_MODEL`        | `all-minilm`             | Embedding model for project search   |
| `CODEBUD_RAG`        | `1`                      | `0` plans without project context    |
| `WEB_SEARCH_ENABLED` | `0`                      | `1` enables the web search tool      |

## Suggested agent flow

1. Confirm the user wants **local / private** execution (or explicitly asked
   for codebud).
2. If `codebud` is missing, offer install steps and the repo links above.
3. Run `codebud doctor` and fix what it reports.
4. `cd` to their project; if `codebud index --status` says there is no
   index, offer to build one (`codebud index`, about a minute per ~1,000
   chunks on a CPU).
5. `codebud --no-progress run "..."` (add `--yes` only with consent).
6. Return codebud’s output; do not re-plan the same task in the cloud unless
   they ask you to.
