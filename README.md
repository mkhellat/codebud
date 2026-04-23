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
ollama serve &
ollama pull qwen2.5-coder:3b-instruct-q4_K_M

# 3. Set up the Python environment
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 4. Run
OLLAMA_MODEL=qwen2.5-coder:3b-instruct-q4_K_M python run_agent.py -i "your task"
```

## Documentation

The full manual lives in `docs/` and is written in GNU Texinfo.

```bash
cd docs
make html    # → docs/build/html/codebud.html
make pdf     # → docs/build/pdf/codebud.pdf
make info    # → docs/build/info/codebud.info  (read with: info -f ...)
```

Topics covered: Installation · Model Management · Usage · Configuration ·
Architecture · Tools Reference · Safety & Sandbox · Development ·
Troubleshooting · Glossary.

See [`docs/README.md`](docs/README.md) for build prerequisites.

## Project layout

```
agent/          Core agent (planner, executor, safety, sandbox, memory, tools)
config/         Safety policy JSON files
data/           Embedding index and memory snapshots
docs/           GNU Texinfo manual
openclaw/       OpenClaw skill wrapper
tests/          Test suite (pytest)
run_agent.py    CLI entry point
```

## Running tests

```bash
pytest -q
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_MODEL` | *(required)* | Model name as shown in `ollama list` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `EMBED_MODEL` | same as `OLLAMA_MODEL` | Override model used for embeddings |
| `WEB_SEARCH_ENABLED` | `0` | Set to `1` to enable DuckDuckGo search |
| `OPENAI_API_KEY` | — | Fallback to OpenAI if Ollama unavailable |

## License

GPL-3.0-or-later
