# Codebud — Privacy-First, Local Coding Agent

[![CI](https://github.com/mkhellat/codebud/actions/workflows/ci.yml/badge.svg)](https://github.com/mkhellat/codebud/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](COPYING)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Local / Ollama](https://img.shields.io/badge/backend-local%20Ollama-brightgreen.svg)](#quick-start)
[![Codeberg](https://img.shields.io/badge/Codeberg-mirror-2185D0.svg)](https://codeberg.org/mkhellat/codebud)
[![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/mkhellat/badge.svg)](https://hub.xerj.org/contributors/mkhellat)

Codebud is a privacy-first, local coding agent that turns natural-language requests into
executable plans using a locally-running LLM (Ollama + Qwen2.5-Coder).
By default no data leaves your machine: no cloud model, no telemetry,
isolated command steps and owner-only, secret-masked history (see the
manual's Privacy chapter).

Selected for the [XERJ Corpus Hub](https://hub.xerj.org/corpus/mkhellat--codebud)
as reference memory for coding agents (approach-only under GPL-3.0).

**For coding agents:** read [`llms.txt`](llms.txt) (install, privacy dial, modes,
when data can leave the machine). Hand them:
`Install codebud (docs: https://raw.githubusercontent.com/mkhellat/codebud/main/llms.txt), run doctor, index this project if useful, then run the user's task locally.`

## Quick start

```bash
# 1. Install Ollama (Arch Linux — prefer the AUR package over the curl installer)
yay -S ollama          # or: paru -S ollama
# For other distros: curl -fsSL https://ollama.com/install.sh | sh

# 2. Start the server and pull the recommended model
sudo systemctl enable --now ollama
ollama pull qwen2.5-coder:3b-instruct-q4_K_M
ollama pull all-minilm    # optional: small embedding model for project search
# Optional but recommended: bubblewrap isolates command steps (no network,
# project-only writes, secrets hidden): pacman -S bubblewrap / apt install bubblewrap

# 3. Get codebud and install it (GNU Autotools)
git clone https://github.com/mkhellat/codebud.git
cd codebud
./bootstrap               # git checkout only; release tarballs ship ./configure
./configure               # checks Python 3.10+, requests, prompt_toolkit, ollama
                          # (and numpy, optional: only project search needs it)
make && make check
sudo make install         # or: ./configure --prefix=$HOME/.local && make install

# 4. Run
export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
codebud doctor            # verify the environment
codebud run "your task"

# 5. Optional: let other agents (Claude Code, OpenClaw) use codebud
codebud skill install --for claude    # or: --for openclaw
# Agensi marketplace listing (ZIP SKILL.md at the root):
#   codebud skill show --for agensi
```

Alternatively, install it with pip like any Python package:
`pip install .` (add `"[rag]"` for project search, `"[openai]"` for the
optional OpenAI backend).

## Encryption at rest

Sessions, step history and project indexes can be encrypted with [age](https://age-encryption.org)
(needs `pip install ".[crypto]"`). Saving never asks for the passphrase;
reading (`codebud history`, `codebud sessions`, `chat --resume`) asks once
per run. Lose the passphrase and the data is gone — by design.

```bash
codebud key init                                    # create the key (asks for a passphrase)
echo 'encrypt = true' >> ~/.config/codebud/config.toml
codebud doctor                                      # Encryption at rest: on
codebud key encrypt-existing                        # encrypt older plaintext (backup kept for you)
```

## Project search

Index a project once and every request gets its most relevant code as
context, so plans read and patch files that really exist instead of guessed
ones (needs numpy: `pip install ".[rag]"` or your distribution's package).

```bash
codebud index             # ~1 min for a mid-sized repository on a CPU
codebud index --status    # what changed since the last run
codebud chat              # /sources, /rag on|off, /index inside chat
```

Search combines embeddings (`all-minilm` by default), BM25 keyword matching
and exact symbol names; nothing leaves your machine. The index is built
only when you ask, and chat says when it is out of date. For edits, use the
7B model (`-m qwen2.5-coder:7b-instruct-q4_K_M`): in our tests it planned
5 of 6 edits against the right file, the 3B model none. Details and
measurements: "The index Subcommand" and "Project Search Evaluation" in the
manual.

## Build system

`./configure && make` follows the GNU Coding Standards; targets are
GNU/Linux, macOS and FreeBSD (verified so far on Arch Linux). Run `make help`
for the full list.

| Target | Effect |
|---|---|
| `make` / `make check` | Build / run the test suite |
| `make install` / `make uninstall` | Install / remove codebud (prefix: `--prefix`) |
| `make installcheck` | Check the installed `codebud` runs |
| `make info` / `html` / `pdf` | Build the manual |
| `make dist` / `make distcheck` | Build / fully verify the release tarball |
| `make clean` / `make distclean` | Remove what make / configure produced |
| `make maintainer-clean` | Back to a fresh checkout (then `./bootstrap`); `make clean-venv` also drops `.venv/` |

For working on codebud itself:

```bash
./bootstrap --dev                          # also creates .venv/ with the dev tools
./configure PYTHON="$PWD/.venv/bin/python"
make && make check
make lint | format | coverage | dist-python | install-link | clean-dev
```

## Documentation

The full manual lives in `docs/` and is written in GNU Texinfo. What
changed in each release is in [NEWS](NEWS); versions follow
[Semantic Versioning](https://semver.org) (0.x while the interface may still
change).

```bash
make info    # → docs/codebud.info   (installed by make install; read with: info codebud)
make html    # → docs/codebud.html
make pdf     # → docs/codebud.pdf
```

Topics covered: Introduction · Installation · Model Management ·
LLM Execution · Performance Benchmarking · Usage · Configuration ·
Architecture · Tools Reference · Safety & Sandbox · Development ·
Troubleshooting · Glossary.

## Project layout

```
src/codebud/    The package: agent, CLI (cli/), tools, default policy
src/codebud_sec/ Security layer (engagement file, evidence log); not used yet
docs/           GNU Texinfo manual
scripts/        Developer scripts (benchmark, search and plan evals)
tests/          Test suite (pytest)
bootstrap       Generates ./configure (GNU Autotools)
configure.ac    Autoconf input
Makefile.am     Automake input (also in src/ and docs/)
pyproject.toml  PEP 621 package metadata and tool configuration
```

## Configuration

Settings come from environment variables or `~/.config/codebud/config.toml`
(the environment wins), for example:

```toml
model = "qwen2.5-coder:7b-instruct-q4_K_M"
num_ctx = 16384
```

`codebud config` shows every value and where it came from. A project's
`.codebud/config.toml` may set the model and similar options, but never
anything that sends data off the machine (the Ollama URL, OpenAI key, web
search), weakens protection (privacy, masking, encryption, `step_env`), or
costs you (`num_ctx`, `openai_model`).

A project's `.codebud/` command policy can narrow your allow-list at once;
it only *widens* it once you trust that project. `run`, `plan` and `chat`
show what it would add and ask; `codebud trust` does the same on demand
(`--revoke` withdraws it). Any change to those files asks again.

### Profiles

A profile sets the mode (how much codebud asks) and the privacy level (what
it may reach) in one name. `default` asks before each step and isolates
commands; `unattended` asks nothing, for CI and machines you trust (the
allow-list still applies):

```sh
codebud --profile unattended run "fix the failing test"
codebud profiles          # list built-in and your own
```

Your own go in `~/.config/codebud/profiles/<name>.toml` (`mode`, `privacy`,
`policy`, `prompt`, `description`, and `encrypt = true` to turn on
encryption at rest); `/profile <name>` switches in chat.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | *(required)* | Model name as shown in `codebud models` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `CODEBUD_NUM_CTX` | `8192` | Context window requested from Ollama on every call |
| `EMBED_MODEL` | `all-minilm` | Dedicated embedding model (`ollama pull all-minilm`) |
| `CODEBUD_RAG` | `1` | Set to `0` to plan without project context from `codebud index` |
| `CODEBUD_RAG_HINTS` | `1` | Set to `0` to stop chat from suggesting `/index` |
| `WEB_SEARCH_ENABLED` | `0` | Set to `1` to enable DuckDuckGo search |
| `CODEBUD_ALLOW_REMOTE_HTTP` | `0` | `1` allows a remote `OLLAMA_BASE_URL` over plain http (else refused) |
| `CODEBUD_SESSION_RETENTION_DAYS` | `0` | Delete sessions unused for N days when chat starts (0 = keep forever) |
| `CODEBUD_PRIVACY` | `private` | `locked` keeps everything on this machine; `trusted` drops privacy prompts |
| `CODEBUD_PROFILE` | `default` | Profile to start with (`codebud profiles` lists them) |
| `CODEBUD_MASK_SECRETS` | `1` | Mask API keys, tokens, passwords as `[redacted]` in saved sessions/history |
| `CODEBUD_ENCRYPT` | `0` | `1` encrypts chat sessions, step history and new project indexes with your age key (`codebud key init`) |
| `CODEBUD_STEP_ENV` | — | Extra environment variables for command steps (they get an allow-list, not your tokens) |
| `CODEBUD_PASSPHRASE` | — | Passphrase of the encryption key, for unattended runs only (else asked; `doctor` warns) |
| `CODEBUD_BACKEND` | `ollama` | `openai` sends prompts to OpenAI instead; never a fallback |
| `OPENAI_API_KEY` | — | Key for the `openai` backend (needs `pip install "codebud[openai]"`) |
| `OPENAI_MODEL` | `gpt-3.5-turbo` | Model for the `openai` backend |
| `XDG_DATA_HOME` / `XDG_CONFIG_HOME` | `~/.local/share` / `~/.config` | Where codebud keeps data / your policy files |

Run `codebud config` to see the resolved values (all but `OPENAI_MODEL`),
the data directory and the policy files in effect.

## License

Codebud is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version (GPL-3.0-or-later). See [COPYING](COPYING) for the full text.
