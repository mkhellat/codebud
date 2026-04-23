# Codebud — Local Cursor-Style Coding Agent

Codebud is a local coding agent that turns natural-language requests into
executable plans using a locally-running LLM (Ollama + Qwen2.5-Coder).
No data leaves your machine.

## Quick start

```bash
# 1. Install Ollama (Arch Linux — prefer the AUR package over the curl installer)
yay -S ollama          # or: paru -S ollama
# For other distros: curl -fsSL https://ollama.com/install.sh | sh

# 2. Start the server and pull the recommended model
sudo systemctl enable --now ollama
ollama pull qwen2.5-coder:3b-instruct-q4_K_M

# 3. Clone and set up the project
git clone https://github.com/mkhellat/codebud.git
cd codebud
make install-dev          # creates .venv/ and installs all dependencies

# 4. Run
export OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M
codebud doctor            # verify the environment
codebud run "your task"

# 5. Register the OpenClaw skill (optional — for the browser UI)
make install-skill
```

## Build system

The project ships a GNU-standard `Makefile`. Run `make help` to see all targets:

| Target | Effect |
|---|---|
| `make venv` | Create the virtual environment (`.venv/`) |
| `make install-dev` | Install with all development dependencies |
| `make test` | Run the full test suite |
| `make coverage` | Run tests with branch coverage report |
| `make lint` | Check code style with ruff |
| `make format` | Auto-format and auto-fix with ruff |
| `make docs` | Build all documentation (HTML, PDF, info) |
| `make dist` | Build source archive and wheel |
| `make install-skill` | Register the OpenClaw skill; link binary to `~/.local/bin` |
| `make uninstall` | Remove the binary link and pip-uninstall |
| `make clean` | Remove build artefacts and caches |
| `make distclean` | Remove everything not in version control |

Override `PREFIX` to install elsewhere:

```bash
make install-skill PREFIX=/usr/local
```

## Documentation

The full manual lives in `docs/` and is written in GNU Texinfo.

```bash
make html    # → docs/build/html/codebud.html
make pdf     # → docs/build/pdf/codebud.pdf
make info    # → docs/build/info/codebud.info
```

Topics covered: Installation · Model Management · Usage · Configuration ·
Architecture · Tools Reference · Safety & Sandbox · Development ·
Troubleshooting · Glossary.

## Project layout

```
agent/          Core agent (planner, executor, safety, sandbox, memory, tools)
config/         Safety policy JSON files
data/           Embedding index and memory snapshots
docs/           GNU Texinfo manual
openclaw/       OpenClaw SKILL.md (teaches the OpenClaw gateway to invoke codebud)
Makefile        GNU-standard build system
pyproject.toml  PEP 621 package metadata and tool configuration
tests/          Test suite (pytest)
run_agent.py    CLI entry point (also exposed as the `codebud` binary)
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | *(required)* | Model name as shown in `codebud models` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `EMBED_MODEL` | same as `OLLAMA_MODEL` | Override model used for embeddings |
| `WEB_SEARCH_ENABLED` | `0` | Set to `1` to enable DuckDuckGo search |
| `OPENAI_API_KEY` | — | Fallback to OpenAI if Ollama is unavailable |

Run `codebud config` to see the resolved values of all variables.

## License

GPL-3.0-or-later
