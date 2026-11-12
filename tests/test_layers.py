"""
tests/test_layers.py

codebud_sec is a separate layer over the codebud core (phase 10): the core
never imports it, and it uses only the core modules listed in CORE_API.
Adding a module to CORE_API is a deliberate widening of the interface the
security layer depends on -- the interface it would need if it were ever
packaged or moved on its own.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# The core modules codebud_sec may import.
CORE_API = {
    "codebud.cli.main",  # codebud-sec hands its sessions over to codebud (PR 7)
    "codebud.confine",  # the evidence log is never inside the project (review of #23)
    "codebud.crypto",  # encryption at rest: the evidence log (D14b)
    "codebud.fsutil",
    "codebud.isolation",  # the scope guard needs isolated steps (review of #18)
    "codebud.paths",  # the evidence log's default place, codebud's data dir (review of #23)
    "codebud.privacy",  # the scope guard refuses an engagement at privacy trusted
    "codebud.safety",  # the scope guard registers its step check (D14i, PR 4)
}


def _imports(package: str) -> dict[str, set[str]]:
    """Map each module file of ``package`` to the absolute modules it imports."""
    found = {}
    for path in sorted((SRC / package).rglob("*.py")):
        modules = set()
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module in ("codebud", "codebud_sec"):
                    # `from codebud import crypto` imports the module codebud.crypto
                    modules.update(f"{node.module}.{alias.name}" for alias in node.names)
                else:
                    modules.add(node.module)
        found[str(path.relative_to(SRC))] = modules
    return found


def _top(module: str) -> str:
    return module.split(".")[0]


def test_core_never_imports_the_security_layer():
    offenders = {
        path: sorted(m for m in modules if _top(m) == "codebud_sec")
        for path, modules in _imports("codebud").items()
    }
    assert {p: m for p, m in offenders.items() if m} == {}


def test_security_layer_uses_only_the_core_api():
    offenders = {
        path: sorted(m for m in modules if _top(m) == "codebud" and m not in CORE_API)
        for path, modules in _imports("codebud_sec").items()
    }
    assert {p: m for p, m in offenders.items() if m} == {}
