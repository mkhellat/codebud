"""
tests/test_isolation.py

Isolated command steps (phase 8, Part C). At privacy level private and
locked, a command runs without network, with the file system read-only
except the project, a fresh /tmp, and the user's secrets hidden. A step
that needs the internet declares it. trusted runs steps unisolated.
"""

import os
import subprocess

import pytest

from codebud import isolation
from codebud.sandbox import Sandbox

BWRAP = isolation.available() == "bwrap"
needs_bwrap = pytest.mark.skipif(not BWRAP, reason="bubblewrap / user namespaces unavailable")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home with secrets in the usual places."""
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text("PRIVATE")
    (h / ".netrc").write_text("machine x password y")
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def private(monkeypatch):
    monkeypatch.delenv("CODEBUD_PRIVACY", raising=False)
    monkeypatch.setattr(isolation, "_warned", False)


# --- the wrapper ------------------------------------------------------------


def test_wrapped_command_is_isolated_by_default(project, home):
    argv = isolation.wrap(["ls"], project, network=False, kind="bwrap")
    assert argv[0] == "bwrap" and argv[-2:] == ["--", "ls"] or argv[-1] == "ls"
    assert "--unshare-all" in argv and "--share-net" not in argv
    assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3]
    i = argv.index("--bind")
    assert argv[i + 1 : i + 3] == [str(project), str(project)]
    assert ["--tmpfs", str(home / ".ssh")] == argv[
        argv.index(str(home / ".ssh")) - 1 : argv.index(str(home / ".ssh")) + 1
    ]
    j = argv.index(str(home / ".netrc"))
    assert argv[j - 2 : j] == ["--ro-bind", "/dev/null"]
    assert str(home / ".gnupg") not in argv  # only paths that exist are hidden


def test_network_steps_keep_the_network(project, home):
    assert "--share-net" in isolation.wrap(["git", "fetch"], project, network=True, kind="bwrap")


def test_unshare_fallback_only_cuts_the_network(project):
    assert isolation.wrap(["ls"], project, network=False, kind="unshare")[:4] == [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
    ]
    assert isolation.wrap(["ls"], project, network=True, kind="unshare") == ["ls"]


# --- the sandbox, live -------------------------------------------------------


def _interfaces(result):
    return [ln.split(":")[0].strip() for ln in result["stdout"].splitlines()[2:]]


@needs_bwrap
def test_isolated_step_has_no_network(project, home):
    result = Sandbox().run_command("cat /proc/net/dev")
    assert result["returncode"] == 0, result["stderr"]
    assert _interfaces(result) == ["lo"]


@needs_bwrap
def test_declared_network_step_has_the_network(project, home):
    result = Sandbox().run_command("cat /proc/net/dev", network=True)
    assert result["returncode"] == 0, result["stderr"]
    assert len(_interfaces(result)) > 1 or _interfaces(result) != ["lo"]


@needs_bwrap
def test_secrets_are_hidden_and_the_project_is_writable(project, home, tmp_path):
    sandbox = Sandbox()
    assert sandbox.run_command(f"ls -A {home / '.ssh'}")["stdout"] == ""
    assert sandbox.run_command(f"cat {home / '.netrc'}")["stdout"] == ""
    assert sandbox.run_command("touch made-here.txt")["returncode"] == 0
    assert (project / "made-here.txt").exists()
    # Outside the project nothing reaches the real file system: here the
    # path is under /tmp, which is the step's own throwaway tmpfs.
    outside = tmp_path / "outside.txt"
    sandbox.run_command(f"touch {outside}")
    assert not outside.exists()


@needs_bwrap
def test_trusted_runs_unisolated(project, home, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    result = Sandbox().run_command(f"ls -A {home / '.ssh'}")
    assert "id_ed25519" in result["stdout"]


# --- where isolation is unavailable ------------------------------------------


def test_private_without_isolation_warns_once_and_runs(project, monkeypatch, caplog):
    monkeypatch.setattr(isolation, "available", lambda: None)
    sandbox = Sandbox()
    with caplog.at_level("WARNING"):
        assert sandbox.run_command("ls")["returncode"] == 0
        sandbox.run_command("ls")
    assert caplog.text.count("not isolated") == 1


def test_locked_without_isolation_refuses(project, monkeypatch):
    monkeypatch.setattr(isolation, "available", lambda: None)
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    result = Sandbox().run_command("ls")
    assert result["returncode"] == 1 and "privacy is locked" in result["stderr"]


def test_locked_refuses_network_steps(project, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    result = Sandbox().run_command("git fetch", network=True)
    assert result["returncode"] == 1 and "privacy is locked" in result["stderr"]


def test_available_reports_what_works():
    kind = isolation.available()
    assert kind in ("bwrap", "unshare", None)
    if kind == "bwrap":
        assert subprocess.run(["bwrap", "--version"], capture_output=True).returncode == 0


# --- planning and confirming network steps ----------------------------------


def test_planner_is_told_how_to_declare_network():
    from codebud.tools.tool_registry import ToolRegistry

    described = ToolRegistry(Sandbox()).describe_tools()
    assert '"network": true' in described


def test_command_tool_passes_the_declaration(project, monkeypatch):
    from codebud.tools.commands import CommandTool

    seen = {}

    class FakeSandbox:
        def run_command(self, cmd, network=False):
            seen["network"] = network
            return {"stdout": "", "stderr": "", "returncode": 0}

    CommandTool(FakeSandbox()).run({"cmd": "git fetch", "network": True})
    assert seen == {"network": True}


def _engine():
    from codebud.safety import SafetyEngine

    engine = SafetyEngine.__new__(SafetyEngine)
    engine.harmless_commands = ["git status", "ls"]
    engine.powerful_commands = ["pip"]
    engine.trusted_sequences = []
    return engine


def test_network_steps_need_confirmation_except_when_trusted(monkeypatch):
    step = {"tool": "command", "args": {"cmd": "git status", "network": True}}
    reason = _engine().confirmation_reason(step)
    assert reason and "network" in reason.lower() and "git status" in reason
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    assert _engine().confirmation_reason(step) is None


def test_network_steps_are_refused_when_locked(monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    step = {"tool": "command", "args": {"cmd": "git status", "network": True}}
    reason = _engine().refusal_reason(step)
    assert reason and "locked" in reason


def test_doctor_reports_isolation(monkeypatch):
    from codebud.cli import doctor

    monkeypatch.setattr(isolation, "available", lambda: "bwrap")
    assert doctor.check_isolation()[0] is True
    monkeypatch.setattr(isolation, "available", lambda: None)
    passed, label, hint = doctor.check_isolation()
    assert passed is None and "bubblewrap" in hint
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    assert doctor.check_isolation()[0] is False
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    assert "off" in doctor.check_isolation()[1]


def test_home_is_resolved_at_call_time(project, home):
    assert str(home / ".ssh") in isolation.wrap(["ls"], project, network=False, kind="bwrap")
    assert os.environ["HOME"] == str(home)


# --- codebud's own directories, wherever they are (follow-up to the audit) --


@pytest.fixture
def outside_tmp(monkeypatch):
    """codebud's data, config and runtime dirs outside /tmp (bwrap replaces
    /tmp, which would hide them even without the fix)."""
    import shutil
    import tempfile
    from pathlib import Path

    try:
        base = Path(tempfile.mkdtemp(prefix="codebud-iso-", dir="/var/tmp"))
    except OSError:
        pytest.skip("no writable /var/tmp")
    for sub in ("data", "config", "run"):
        (base / sub).mkdir(mode=0o700)
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base / "run"))
    secrets = {
        "data": base / "data" / "codebud" / "sessions" / "s.json",
        "key": base / "config" / "codebud" / "keys" / "identity.age",
        "copy": base / "run" / "codebud" / "index-1-x" / "index.sqlite3",
    }
    for path in secrets.values():
        path.parent.mkdir(parents=True)
        path.write_text("SECRET")
    yield secrets
    shutil.rmtree(base)


def test_wrap_hides_codebud_dirs_wherever_they_are(outside_tmp, project, home):
    from pathlib import Path

    argv = isolation.wrap(["true"], project, network=False, kind="bwrap")
    # wrap() resolves paths (macOS: /var/tmp -> /private/var/tmp); compare resolved.
    hidden = {str(Path(argv[i + 1]).resolve()) for i, a in enumerate(argv) if a == "--tmpfs"}
    for path in outside_tmp.values():
        codebud_dir = next(p for p in path.parents if p.name == "codebud").resolve()
        # hidden itself, or by a hidden parent (the whole runtime directory)
        assert any(str(codebud_dir) == h or str(codebud_dir).startswith(h + "/") for h in hidden)


def test_a_project_inside_a_codebud_dir_is_not_hidden(outside_tmp, home, monkeypatch):
    from pathlib import Path

    project = Path(os.environ["XDG_DATA_HOME"]) / "codebud" / "proj"
    project.mkdir()
    argv = isolation.wrap(["true"], project, network=False, kind="bwrap")
    hidden = {argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"}
    assert str(Path(os.environ["XDG_DATA_HOME"]) / "codebud") not in hidden


@needs_bwrap
@pytest.mark.parametrize("what", ["data", "key", "copy"])
def test_a_step_cannot_read_codebud_dirs(outside_tmp, project, home, what):
    result = Sandbox().run_command(f"cat {outside_tmp[what]}")
    assert "SECRET" not in result["stdout"]
    assert result["returncode"] != 0


# --- the runtime directory and D-Bus (security review C2) -------------------


def test_wrap_hides_the_whole_runtime_dir_and_the_system_bus(project, home, tmp_path, monkeypatch):
    run = tmp_path / "run-user"
    (run / "codebud").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    argv = isolation.wrap(["true"], project, network=False, kind="bwrap")
    hidden = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert str(run.resolve()) in hidden
    assert str((run / "codebud").resolve()) not in hidden  # covered by its parent
    if os.path.isdir("/run/dbus"):
        assert "/run/dbus" in hidden


@needs_bwrap
@pytest.mark.skipif(
    not os.path.exists(f"/run/user/{os.getuid()}/bus"), reason="no session D-Bus socket here"
)
def test_a_step_cannot_reach_the_session_bus(project, home):
    bus = f"/run/user/{os.getuid()}/bus"
    result = Sandbox().run_command(f"ls {bus}")
    assert result["returncode"] != 0 and bus not in result["stdout"]


def test_a_network_step_under_unshare_warns_it_is_unisolated(project, home, monkeypatch, caplog):
    # Security review (low): this ran with no isolation and no notice.
    monkeypatch.setattr(isolation, "available", lambda: "unshare")
    monkeypatch.setattr(isolation, "_warned_unshare_network", False)
    argv, refusal = isolation.prepare(["pip", "download", "x"], network=True)
    assert refusal is None and argv == ["pip", "download", "x"]
    assert "runs without isolation" in caplog.text
    caplog.clear()
    isolation.prepare(["pip", "download", "y"], network=True)
    assert "runs without isolation" not in caplog.text  # once per run


def test_a_network_step_under_unshare_is_still_refused_when_locked(project, home, monkeypatch):
    monkeypatch.setattr(isolation, "available", lambda: "unshare")
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    argv, refusal = isolation.prepare(["pip", "download", "x"], network=True)
    assert argv is None and refusal
