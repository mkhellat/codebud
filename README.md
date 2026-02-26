## Codebud — Local Cursor‑Style Coding Agent (Arch‑Linux‑friendly)

Codebud is a **local, Cursor‑style coding agent** that:

- **Plans with an LLM**: turns your natural‑language request into a linear,
  JSON plan.
- **Executes safely**: runs tools in a sandbox, with safety policies and a
  memory timeline.
- **Integrates with OpenClaw**: you can drive it from the OpenClaw UI, step
  by step.
- **Works well on CPU‑only machines**: the instructions below are tuned for
  a 16 GB Arch Linux laptop using **Ollama + Qwen2.5‑Coder‑7B‑Q4_K_M**.

This README is deliberately **beginner‑friendly** and walks you through:

- How to set up your Arch Linux system.
- How to install Ollama and the Qwen2.5‑Coder model.
- How to create the Codebud project structure.
- How to run the agent from the CLI and from OpenClaw.
- How the internal pieces (planner, executor, tools, safety, memory) fit
  together.
- How to tune everything for maximum speed on CPU‑only hardware.

If you are on another Linux distribution, the high‑level ideas still apply,
but package names and commands may differ.

---

## 1. Project overview

- **Project name**: `codebud`
- **Goal**: a small, understandable, local coding agent you can run on your
  own machine, with a clear separation between:
  - LLM‑driven planning.
  - Tool execution and sandboxing.
  - Safety rules.
  - Memory and history.
  - UI integration.
- **Primary use case**: beginner‑friendly, step‑by‑step coding help that you
  fully control, instead of a black‑box remote service.

### High‑level architecture

At the core is the `AgentCore` class (`agent/core.py`):

- Calls an LLM‑based planner to build a plan.
- Validates the plan using safety rules and the tool registry.
- Optionally executes steps through an executor.
- Records what happened in a memory timeline.
- Supports regenerating the plan for the last user message.

Main building blocks:

- `agent/planner.py` (`LLMPlanner`)
  - Builds a prompt describing tools, safety rules, and the required JSON
    format.
  - Calls `call_llm(...)` (defined in `agent/llm_stub.py` in your real
    setup) to ask the LLM for a plan.
  - Parses and validates the JSON plan.
- `agent/executor.py` (`Executor`)
  - Runs each plan step sequentially.
  - Looks up tools in the `ToolRegistry`.
  - Executes tools in the sandbox and collects `stdout`, `stderr`,
    `returncode`, and metadata.
  - Records memory snapshots and stops on the first error.
- `agent/sandbox.py` (`Sandbox`)
  - Runs shell commands with a timeout.
  - Blocks obviously dangerous patterns (for example `rm -rf /`).
  - Returns structured `stdout` / `stderr` / `returncode`.
- `agent/safety.py` (`SafetyEngine`)
  - Loads safety policies from JSON files under `config/`.
  - Validates each LLM‑generated step (especially shell commands).
  - Exposes a human‑readable safety description for the planner prompt.
- `agent/memory.py` (`MemoryStore`)
  - Stores a timeline of execution snapshots in
    `data/memory/entries.json`.
  - Each snapshot includes a timestamp, the step, and the result.
- `agent/tools/*`
  - `tool_registry.py` (`ToolRegistry`): central registry for all tools.
  - `file_io.py`: file read/write tools.
  - `patcher.py`: a minimal unified‑diff patch tool.
  - `commands.py`: executes shell commands through the sandbox.
  - `web_search.py`: stubbed web search tool (returns fake results for now).
  - `embeddings.py`: stubbed embedding and embedding search tools.
- `openclaw/*`
  - `skill.py` (`OpenClawSkill`): wraps `AgentCore` for use inside OpenClaw.
  - `register.py`: exposes a `register()` function returning your skill.
  - `main.py`: small CLI entrypoint for testing the skill without the UI.
- Root‑level helpers:
  - `run_agent.py`: run the agent directly from the terminal.
  - `config/*.json`: safety policy files.
  - `data/*/*.json`: on‑disk storage for memory and embeddings.
  - `pyproject.toml` / `requirements.txt`: packaging and dependencies.

---

## 2. Prerequisites

These instructions assume:

- **Operating system**: Arch Linux (or an Arch‑based distro).
- **RAM**: at least **16 GB**.
- **Python**: 3.11 or newer.
- **Node.js + npm**: required for the OpenClaw UI.

You will install:

- **Ollama** (local model runner).
- **Qwen2.5‑Coder‑7B‑Q4_K_M** (a 7B coding‑oriented model) via Ollama.
- **OpenClaw** (Node‑based UI that can talk to your agent).

> Note  
> All of the commands below are meant to be run in a terminal. Copy and
> paste them exactly as written, unless you understand what you are
> changing.

---

## 3. Guided setup for Arch Linux

This section mirrors the step‑by‑step plan from your `note` file, but
rewritten as a proper README.

Throughout the examples, we will assume your project root is:

```bash
mkdir -p ~/coding-agent
cd ~/coding-agent
```

You can, of course, use another directory name; just adjust the paths.

### Phase 1 — System setup (steps 1–10)

1. **Update your system**

   ```bash
   sudo pacman -Syu
   ```

2. **Install Python, pip, and virtualenv**

   ```bash
   sudo pacman -S python python-pip python-virtualenv
   ```

3. **Install git**

   ```bash
   sudo pacman -S git
   ```

4. **Install curl** (needed for the Ollama installer)

   ```bash
   sudo pacman -S curl
   ```

5. **Install Node.js + npm** (for OpenClaw)

   ```bash
   sudo pacman -S nodejs npm
   ```

6. **Install build tools** (used by some Python packages)

   ```bash
   sudo pacman -S base-devel
   ```

7. **Verify Python**

   ```bash
   python --version
   ```

8. **Verify pip**

   ```bash
   pip --version
   ```

9. **Verify Node.js + npm**

   ```bash
   node --version
   npm --version
   ```

10. **Create your project root**

    ```bash
    mkdir -p ~/coding-agent
    cd ~/coding-agent
    ```

### Phase 2 — Install Ollama and Qwen2.5‑Coder‑7B‑Q4_K_M (steps 11–20)

11. **Install Ollama**

    Arch Linux does not ship Ollama in `pacman`, so use the official
    installer:

    ```bash
    curl -fsSL https://ollama.com/install.sh | sh
    ```

12. **Enable and start the Ollama service**

    ```bash
    sudo systemctl enable ollama
    sudo systemctl start ollama
    ```

13. **Verify Ollama**

    ```bash
    ollama --version
    ```

14. **Pull the Qwen2.5‑Coder model**

    ```bash
    ollama pull qwen2.5-coder:7b-q4_K_M
    ```

15. **Verify the model is installed**

    ```bash
    ollama list
    ```

    You should see a line containing:

    ```text
    qwen2.5-coder:7b-q4_K_M
    ```

16. **Test the model interactively**

    ```bash
    ollama run qwen2.5-coder:7b-q4_K_M
    ```

    Then type:

    ```text
    print("Hello from Qwen!")
    ```

    Exit with:

    ```text
    /bye
    ```

17. **Check RAM usage (recommended for 16 GB machines)**

    In a second terminal:

    ```bash
    htop
    ```

    While the model is running, you should see roughly:

    - 5–6 GB RAM usage.
    - Some CPU usage, depending on the prompt.

18. **Force CPU‑only mode (optional but recommended on laptops)**

    ```bash
    mkdir -p ~/.ollama
    echo 'gpu: false' >> ~/.ollama/config.yaml
    ```

19. **Restart Ollama to apply settings**

    ```bash
    sudo systemctl restart ollama
    ```

20. **Final verification**

    ```bash
    ollama run qwen2.5-coder:7b-q4_K_M
    ```

    If the model responds, your Ollama setup is ready.

### Phase 3 — Create and install the Codebud agent (steps 21–35)

All commands in this phase run inside:

```bash
cd ~/coding-agent
```

21. **Create the folder structure**

    ```bash
    mkdir -p agent/tools
    mkdir -p openclaw
    mkdir -p config
    mkdir -p data/memory
    mkdir -p data/embeddings
    ```

22. **Create empty Python agent files**  
    (you already have these in this repository, but these steps are kept
    for completeness)

    ```bash
    touch agent/__init__.py
    touch agent/core.py
    touch agent/planner.py
    touch agent/executor.py
    touch agent/sandbox.py
    touch agent/safety.py
    touch agent/memory.py
    ```

23. **Create empty tool files**

    ```bash
    touch agent/tools/__init__.py
    touch agent/tools/tool_registry.py
    touch agent/tools/file_io.py
    touch agent/tools/patcher.py
    touch agent/tools/commands.py
    touch agent/tools/web_search.py
    touch agent/tools/embeddings.py
    ```

24. **Create OpenClaw integration files**

    ```bash
    touch openclaw/skill.py
    touch openclaw/register.py
    touch openclaw/main.py
    ```

25. **Create config files**

    ```bash
    touch config/harmless_commands.json
    touch config/powerful_commands.json
    touch config/trusted_sequences.json
    ```

26. **Create data files**

    ```bash
    echo "[]" > data/memory/entries.json
    echo "[]" > data/embeddings/index.json
    ```

27. **Create root‑level files**

    ```bash
    touch README.md
    touch requirements.txt
    touch pyproject.toml
    touch run_agent.py
    ```

    In this repository, these files are already populated with the code
    you see in the tree.

28. **Create a Python virtual environment**

    ```bash
    python -m venv .venv
    ```

29. **Activate the virtual environment**

    ```bash
    source .venv/bin/activate
    ```

30. **Install Python dependencies**

    ```bash
    pip install -r requirements.txt
    ```

31. **Verify the agent runs standalone**

    From the project root:

    ```bash
    python run_agent.py "hello"
    ```

    You should see a JSON object describing a plan or a planning error.

32. **Fix any missing imports or typos (if needed)**

    If Python reports that a module or file is missing, open the file it
    mentions and correct the import path or file name, then run the
    command again.

33. **Confirm the agent can call your LLM**

    The planner uses a small helper located in `agent/llm_stub.py`.
    That module chooses a backend based on environment variables:

    * `OLLAMA_MODEL` – if set and the `ollama` CLI is installed, the
      prompt is forwarded to the local Ollama model.
    * `OPENAI_API_KEY` – if present, the OpenAI Python client is used
      (model name controlled by `OPENAI_MODEL`, default `gpt-3.5-turbo`).

    You can export one of the variables before running; for example

    ```bash
    export OLLAMA_MODEL=qwen2.5-coder-7b-q4_k_m
    # or
    export OPENAI_API_KEY="sk-..."
    export OPENAI_MODEL=gpt-4o-mini
    ```

    Then execute the CLI test:

    ```bash
    python run_agent.py "Write a Python function to add two numbers."
    ```

    A successful plan (even if it simply dumps a JSON plan with a
    placeholder step) means the end‑to‑end loop is working.

    If no backend is configured you will see a warning message and the
    planner will return a `plan_error` result; set the appropriate
    environment variable and rerun.

### Phase 4 — OpenClaw integration (steps 36–45)

Still inside:

```bash
cd ~/coding-agent
```

36. **Install OpenClaw globally**

    ```bash
    sudo npm install -g openclaw
    ```

37. **Verify OpenClaw installation**

    ```bash
    openclaw --version
    ```

38. **Link your project as an OpenClaw skill**

    ```bash
    openclaw link ~/coding-agent
    ```

    OpenClaw will now know how to call your `register()` function from
    `openclaw/register.py`.

39. **Verify OpenClaw sees your skill**

    ```bash
    openclaw skills
    ```

    The output should include:

    ```text
    local_coding_agent
    ```

40. **Start the OpenClaw UI**

    ```bash
    openclaw ui
    ```

    This launches the UI in your browser, typically at:

    ```text
    http://localhost:5173
    ```

41. **Select your skill in the UI**

    In the left sidebar, choose:

    ```text
    Local Coding Agent
    ```

42. **Send your first message**

    For example:

    ```text
    Hello, what can you do?
    ```

    Expected behaviour:

    - The agent generates a plan.
    - The UI shows the plan steps.
    - You approve or deny each step.
    - The agent executes tools.
    - Results appear in the timeline.

43. **Test a coding task**

    Try:

    ```text
    Create a Python file that prints the Fibonacci sequence.
    ```

    Expected:

    - Planner creates steps.
    - Executor writes a file via the file tools.
    - Sandbox runs any requested shell commands.
    - Memory logs snapshots in `data/memory/entries.json`.

44. **Test a patch**

    ```text
    Modify the Fibonacci function to use recursion.
    ```

    Expected:

    - Planner generates a patch.
    - Patch tool applies it.
    - File updates appear in the UI.

45. **Test stubbed web search**

    ```text
    Search the web for Python best practices and summarize them.
    ```

    Expected:

    - Planner calls the `web_search` tool.
    - Tool returns stub results (from `stub_search_api`).
    - Agent summarizes them.
    - UI displays everything.

At this point, your agent, Ollama, and OpenClaw are all wired
together.

---

## 4. Internal modules in more detail

This section explains the most important modules in beginner‑friendly
language.

### AgentCore (`agent/core.py`)

- Creates and wires together:
  - `Sandbox`
  - `ToolRegistry`
  - `SafetyEngine`
  - `MemoryStore`
  - `LLMPlanner`
  - `Executor`
- Exposes two main methods:
  - `handle_user_message(message: str)`  
    Stores the message, asks the planner for a plan, validates it, and
    returns either a valid plan or a `plan_error`.
  - `regenerate(payload: Dict[str, Any])`  
    Uses the last stored user message to ask the planner for a new plan.

### Planner (`agent/planner.py`)

- Builds a prompt that tells the LLM:
  - What tools exist and what they do.
  - What safety rules are in effect.
  - Exactly what JSON format it must return.
- Calls `call_llm(prompt)` (which you will implement to talk to Ollama
  or another LLM).
- Parses the JSON response.
- Validates that:
  - `status` is `"ok"` or `"plan_error"`.
  - If `"ok"`, `plan` is a list of steps, each with `id`, `description`,
    `tool`, and `args`.
  - Each referenced tool exists and passes safety checks.

### Executor (`agent/executor.py`)

- Receives a validated plan from the planner.
- For each step:
  - Looks up the tool in the `ToolRegistry`.
  - Calls `tool.run(args)`.
  - Validates the tool result shape.
  - Stores `stdout`, `stderr`, and `returncode`, plus helpful metadata.
  - Adds a snapshot to the `MemoryStore`.
  - Stops on the first non‑zero `returncode` and returns a `step_error`.
- On success, returns:

  ```json
  {
    "status": "ok",
    "results": {
      "step_0": {
        "stdout": "...",
        "stderr": "",
        "returncode": 0,
        "metadata": { "...": "..." }
      }
    }
  }
  ```

### Sandbox (`agent/sandbox.py`)

- Provides a safe way to run shell commands.
- Before running a command:
  - Checks for obviously dangerous patterns (for example `rm -rf /`).
- Uses `subprocess.run` with:
  - Argument splitting via `shlex.split`.
  - A timeout (default 10 s).
  - Captured `stdout` and `stderr`.
- On timeout or error, returns a non‑zero `returncode` and a clear
  error message instead of raising an exception.

### Safety engine (`agent/safety.py`)

- Loads JSON configuration from:
  - `config/harmless_commands.json`
  - `config/powerful_commands.json`
  - `config/trusted_sequences.json`
- For each step:
  - If the tool is `command`, checks whether the `cmd` starts with an
    allowed harmless or powerful prefix.
  - You can extend this with more tool‑specific rules.
- Returns a human‑readable safety summary string for the planner prompt,
  so the LLM knows what it is allowed to do.

### Memory store (`agent/memory.py`)

- Maintains a list of snapshots in memory and persists them to:

  ```text
  data/memory/entries.json
  ```

- Each snapshot looks like:

  ```json
  {
    "timestamp": "2026-02-25T12:34:56.789Z",
    "data": {
      "step": { "...": "..." },
      "result": { "...": "..." }
    }
  }
  ```

- When the agent restarts, the history is loaded back into memory.

### Tools (`agent/tools/*`)

- `tool_registry.py`
  - Registers all tools by name.
  - Provides:
    - `get(name)`: returns the tool instance.
    - `has(name)`: checks if a tool exists.
    - `describe_tools()`: returns a human‑readable list of tools and
      their descriptions for the planner prompt.
- `file_io.py`
  - `FileWriteTool`:
    - Creates or overwrites files.
    - Ensures parent directories exist.
  - `FileReadTool`:
    - Reads and returns file contents.
- `patcher.py`
  - Minimal unified‑diff patcher:
    - Parses `---`/`+++` headers to find the target file.
    - Applies `+` (add), `-` (remove), and space (context) lines.
    - Writes the result back to disk.
- `commands.py`
  - `CommandTool`:
    - Accepts a `"cmd"` string.
    - Delegates execution to `Sandbox.run_command`.
- `web_search.py`
  - `WebSearchTool`:
    - Uses a stub search function that returns fake results for now.
    - Good for wiring and UI tests.
- `embeddings.py`
  - `EmbedTool`:
    - Uses a hash‑based stub to produce deterministic 16‑element vectors.
  - `EmbeddingSearchTool`:
    - Loads an index from `data/embeddings/index.json`.
    - Uses cosine similarity to return the top‑k matches.

---

## 5. Running the agent

### Option A — Direct CLI (no OpenClaw)

From the project root:

```bash
source .venv/bin/activate
python run_agent.py "Write a Python function to add two numbers."
```

You will see a JSON object printed to the terminal with:

- `status`: `"ok"` or `"plan_error"`.
- If `"ok"`, a `plan` with steps.
#### Interactive approval mode

To mimic the step‑by‑step UX of the Claude CLI, `run_agent.py` accepts an
`--interactive` (or `-i`) flag. When enabled, the agent presents each step
in turn and prompts you to execute it. For example:

```bash
python run_agent.py -i "Create a Python file that prints hello"
```

The CLI will display each plan step, ask `Execute? [y/N]`, run approved
steps, and show their outputs. This provides a simple text‑based
conversation that doesn't require the OpenClaw UI.
### Option B — Through OpenClaw

Once you have completed **Phase 4** above:

1. Start the UI:

   ```bash
   openclaw ui
   ```

2. In the browser, select **Local Coding Agent**.
3. Type a request (for example *“Create a Python file that prints the
   Fibonacci sequence.”*).
4. Approve or deny each step as the plan is shown.

---

## 6. Performance tuning for CPU‑only Arch Linux

The following sections summarise the **maximum‑speed** guidance from your
notes. All of these tweaks are:

- Designed for a **16 GB Arch Linux laptop**.
- Focused on **CPU‑only Qwen2.5‑Coder‑7B‑Q4_K_M**.
- Intended to be **safe and reversible**, but always read a command
  before running it.

### 6.1 Ollama configuration for speed

Create or edit:

```bash
mkdir -p ~/.ollama
nano ~/.ollama/config.yaml
```

Example CPU‑only, speed‑optimised config:

```yaml
gpu: false
num_threads: 0
context_length: 2048
kv_cache_size: 1024
mlock: false

temperature: 0.2
top_p: 0.9
top_k: 40
repeat_penalty: 1.05
```

Apply changes:

```bash
sudo systemctl restart ollama
```

To verify:

```bash
ollama run qwen2.5-coder:7b-q4_K_M
```

Inside the session, you can use `/system` to inspect runtime settings.

### 6.2 Linux system tuning (CPU‑only)

These steps are common Linux performance practices; consider them
carefully on your system.

1. **Set CPU governor to performance**

   ```bash
   sudo pacman -S cpupower
   sudo systemctl enable --now cpupower
   sudo cpupower frequency-set -g performance
   ```

2. **Optionally reduce swap usage**

   ```bash
   echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf
   sudo sysctl --system
   ```

3. **Disable power‑saving daemons that downclock the CPU**

   ```bash
   sudo systemctl stop power-profiles-daemon
   sudo systemctl disable power-profiles-daemon
   ```

4. **Improve scheduler responsiveness**

   ```bash
   echo "kernel.sched_migration_cost_ns=500000" | \
     sudo tee /etc/sysctl.d/99-scheduler.conf
   sudo sysctl --system
   ```

5. **Reduce background load**

   ```bash
   systemctl --type=service --state=running
   ```

   Then stop anything you do not need, for example:

   ```bash
   sudo systemctl stop bluetooth
   sudo systemctl stop cups
   sudo systemctl stop avahi-daemon
   ```

6. **Use lightweight applications while coding**

   - Close heavy browsers and Electron apps if possible.
   - Use a lightweight terminal (Alacritty, Kitty, Xfce Terminal, etc.).

7. **Reboot to apply all system‑level changes**

   ```bash
   sudo reboot
   ```

### 6.3 Agent and OpenClaw tuning (optional code tweaks)

The codebase is intentionally simple, but you can add small constants
and settings to trade a little capability for extra speed. Examples:

- In `agent/planner.py`, you can add:

  ```python
  MAX_PLAN_STEPS = 3
  ```

  and make the planner stop generating very long plans.

- In your LLM call (`call_llm` implementation), use:

  ```python
  max_tokens = 512
  ```

  which is enough for most coding tasks and keeps responses fast.

- In `agent/memory.py`, you can choose to store only short summaries of
  each step to keep the memory file small.

- In `openclaw/skill.py` or the OpenClaw configuration, you can
  restrict:

  - Maximum input characters per message (for example `2000`).
  - Frequency of UI updates (for example every `200 ms` instead of
    every frame).

These are **optional** and depend on how much you want to squeeze out
extra performance.

---

## 7. Where to go next

- Implement `agent/llm_stub.py` so that `call_llm` talks to your local
  Ollama model (or to the OpenAI API if you prefer).
- Extend the toolset:
  - Add tools for running tests, formatting code, or interacting with
    other services you care about.
- Refine safety policies in `config/*.json` to match your comfort level.
- Experiment with different models (for example other Qwen sizes) once
  you are comfortable with the default setup.

The goal of Codebud is not just to give you a useful agent, but also to
serve as a learning scaffold: every file is small, and the architecture
is meant to be read and understood by beginners.

