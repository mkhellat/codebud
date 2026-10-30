"""
codebud/sandbox.py

This module defines the Sandbox class, which provides a safe environment
for executing shell commands. It ensures:

- Restricted command execution
- Timeout enforcement
- Safe working directory
- Structured stdout/stderr/returncode output

Each command is isolated according to the privacy level (isolation.py):
no network unless the step declared it, project-only writes, secrets
hidden. At every privacy level, a step's environment is an allow-list
(step_environment()): tokens in codebud's environment never reach it.

The sandbox does NOT:
- validate plans
- validate tool usage
- interact with the LLM
"""

import os
import shlex
import subprocess
from typing import Any

from codebud import gitsafe, isolation

# What every step gets: what ordinary tools need to run, nothing that grants
# access to anything. LC_* stands for every locale variable.
STEP_ENV_BASE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "TZ",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "PYTHONPATH",
    "GOPATH",
    "GOROOT",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "JAVA_HOME",
)

# Only for steps that declared they need the network.
STEP_ENV_NETWORK = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def _matches(name: str, patterns) -> bool:
    return any(name.startswith(p[:-1]) if p.endswith("*") else name == p for p in patterns)


def extra_step_env() -> list[str]:
    """Names (or PREFIX_* patterns) the user added with step_env / CODEBUD_STEP_ENV."""
    raw = os.environ.get("CODEBUD_STEP_ENV", "")
    return [n for n in raw.replace(",", " ").split() if n]


def step_environment(network: bool = False) -> dict[str, str]:
    """The environment a command step runs with: an allow-list, not codebud's.

    Isolation hides secret *files*, but a child process inherits the
    environment, and tokens there (GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY,
    the encryption passphrase, ...) would be one `printenv` away. So a step
    gets only STEP_ENV_BASE, STEP_ENV_NETWORK if it declared the network,
    and what the user added with step_env. The encryption passphrase and
    codebud's secret settings are never passed, even if listed.
    """
    from codebud import crypto, settings

    never = {crypto.PASSPHRASE_ENV} | {s.env for s in settings.SETTINGS if s.secret}
    allowed = [*STEP_ENV_BASE, *(STEP_ENV_NETWORK if network else ()), *extra_step_env()]
    return {k: v for k, v in os.environ.items() if k not in never and _matches(k, allowed)}


class Sandbox:
    """
    Safe execution environment for shell commands.

    Responsibilities:
    - Execute commands with subprocess
    - Enforce timeouts
    - Refuse catastrophic commands again before running (safety.catastrophic_reason)
    - Return structured results
    """

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_command(self, cmd: str, network: bool = False) -> dict[str, Any]:
        """
        Execute a shell command safely, isolated (isolation.prepare());
        ``network`` is the step's declaration that it needs the internet.

        Returns:
        {
            "stdout": "...",
            "stderr": "...",
            "returncode": 0
        }
        """

        # Basic safety guard
        # The same backstop the safety policy applies at planning time,
        # checked again right before running (safety.catastrophic_reason).
        from codebud.safety import catastrophic_reason

        catastrophic = catastrophic_reason(cmd)
        if catastrophic:
            return {"stdout": "", "stderr": f"Refused: {catastrophic}", "returncode": 1}

        words = shlex.split(cmd)
        if words and words[0] == "git":
            # The repository's config must not run programs (gitsafe.py).
            words = gitsafe.git_argv(words[1:], cwd=os.getcwd())
        argv, refusal = isolation.prepare(words, network=network)
        if refusal:
            return {"stdout": "", "stderr": refusal, "returncode": 1}

        try:
            process = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=step_environment(network),
            )

            return {
                "stdout": process.stdout,
                "stderr": process.stderr,
                "returncode": process.returncode,
            }

        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Command timed out after {self.timeout} seconds",
                "returncode": 1,
            }

        except Exception as e:
            return {"stdout": "", "stderr": f"Sandbox error: {e}", "returncode": 1}
