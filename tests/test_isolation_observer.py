"""
tests/test_isolation_observer.py

Observer isolation (phase 10, step 5 plan, PR 2): for live triage a
profile can ask for steps that see the host's processes -- the host's
/proc mounted read-only -- while keeping their own process namespace
(they cannot signal a host process) and no network. Today's isolation
shows them only the sandbox: ps sees 2 processes where the host has
hundreds, a falsely clean picture for incident response.
"""

import subprocess

import pytest

from codebud import isolation, profiles
from codebud.sandbox import Sandbox

BWRAP = isolation.available() == "bwrap"
needs_bwrap = pytest.mark.skipif(not BWRAP, reason="bubblewrap / user namespaces unavailable")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv("CODEBUD_PRIVACY", raising=False)
    return root


@pytest.fixture
def observer(monkeypatch):
    profile = profiles.Profile("triage", isolation="observer", privacy="locked")
    monkeypatch.setattr(profiles, "_active", profile)
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    return profile


def test_profiles_accept_isolation_observer(tmp_path, monkeypatch):
    from codebud import paths

    directory = paths.config_dir() / "profiles"
    directory.mkdir(parents=True)
    (directory / "triage.toml").write_text('isolation = "observer"\n')
    assert profiles.get("triage").isolation == "observer"
    assert "observer" in profiles.get("triage").summary()


def test_an_unknown_isolation_is_refused(monkeypatch):
    from codebud import paths

    directory = paths.config_dir() / "profiles"
    directory.mkdir(parents=True)
    (directory / "bad.toml").write_text('isolation = "none"\n')
    with pytest.raises(profiles.ProfileError, match="isolation"):
        profiles.get("bad")


def test_the_default_is_unchanged(project):
    argv = isolation.wrap(["ps"], project, False, "bwrap")
    assert "--proc" in argv
    assert ["--ro-bind", "/proc", "/proc"] not in [argv[i : i + 3] for i in range(len(argv))]


def test_observer_mounts_the_host_proc_read_only(project, observer):
    argv = isolation.wrap(["ps"], project, False, "bwrap", observer=True)
    triples = [argv[i : i + 3] for i in range(len(argv))]
    assert ["--ro-bind", "/proc", "/proc"] in triples
    assert "--proc" not in argv
    assert "--unshare-all" in argv  # its own process and network namespaces
    assert "--share-net" not in argv


def test_observer_needs_bubblewrap(project, observer, monkeypatch):
    """With only unshare the host's processes could not be kept from its
    signals, so the step is refused rather than run less contained."""
    monkeypatch.setattr(isolation, "available", lambda: "unshare")
    argv, refusal = isolation.prepare(["ps"])
    assert argv is None
    assert "observer" in refusal and "bubblewrap" in refusal


@needs_bwrap
def test_an_observer_step_sees_the_host_processes(project, observer):
    host = len(
        subprocess.run(
            ["ps", "-e", "--no-headers"], capture_output=True, text=True
        ).stdout.splitlines()
    )
    result = Sandbox().run_command("ps -e --no-headers")
    assert result["returncode"] == 0, result["stderr"]
    assert len(result["stdout"].splitlines()) >= host - 5  # processes come and go


@needs_bwrap
def test_an_observer_step_cannot_signal_a_host_process(project, observer):
    victim = subprocess.Popen(["tail", "-f", "/dev/null"])
    try:
        Sandbox().run_command(f"kill -9 {victim.pid}")
        assert victim.poll() is None  # still running
    finally:
        victim.kill()
        victim.wait()


@needs_bwrap
def test_an_observer_step_has_no_network(project, observer):
    result = Sandbox().run_command("cat /proc/self/net/dev")
    interfaces = [line.split(":")[0].strip() for line in result["stdout"].splitlines()[2:]]
    assert interfaces == ["lo"]


@needs_bwrap
def test_an_observer_step_cannot_read_host_process_environments(project, observer):
    victim = subprocess.Popen(["tail", "-f", "/dev/null"], env={"SECRET_TOKEN": "x" * 20})
    try:
        result = Sandbox().run_command(f"cat /proc/{victim.pid}/environ")
        assert "SECRET_TOKEN" not in result["stdout"]
    finally:
        victim.kill()
        victim.wait()
