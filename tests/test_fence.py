"""
tests/test_fence.py

The network fence's mechanism in the core (phase 10, step 5 plan, PR 6):
a registered fence says which addresses a network step may reach, and
the step runs behind pasta and nftables -- nothing of the host
reachable, no DNS, no capabilities to change the rules.
"""

import ipaddress
import shutil
import socket
import subprocess
import threading

import pytest

from codebud import isolation

NET = ipaddress.ip_network


@pytest.fixture(autouse=True)
def no_fences(monkeypatch):
    monkeypatch.setattr(isolation, "_FENCES", [])
    monkeypatch.delenv("CODEBUD_PRIVACY", raising=False)


@pytest.fixture
def tools(monkeypatch):
    """Isolation and the fence's programs count as present."""
    monkeypatch.setattr(isolation, "available", lambda: "bwrap")
    monkeypatch.setattr(isolation, "fence_available", lambda: True)


FENCE = isolation.Fence(
    allow=(NET("10.0.0.0/16"), NET("2001:db8::/48")),
    deny=(NET("10.0.5.0/24"),),
    hosts=(("app.example.com", "10.0.0.7"),),
)


def test_denied_networks_come_before_allowed_ones():
    """10.0.5.0/24 lies inside the allowed 10.0.0.0/16: dropped first, it wins."""
    rules = isolation.nft_ruleset(FENCE).splitlines()
    deny = rules.index("    ip daddr 10.0.5.0/24 drop")
    assert deny < rules.index("    ip daddr 10.0.0.0/16 accept")
    assert "    ip6 daddr 2001:db8::/48 accept" in rules
    assert any("policy drop" in line for line in rules)


def test_a_fenced_step_runs_behind_pasta_with_nothing_of_the_host(tools, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    isolation.add_network_fence(lambda words: FENCE)
    argv, refusal, programs = isolation.prepare_with_programs(["curl", "x"], network=True)
    assert refusal is None
    assert argv[0] == "pasta"
    head = argv[: argv.index("--")]
    for option in ("--no-map-gw", "--config-net"):
        assert option in head
    for flag in ("-t", "-u", "-T", "-U"):
        assert head[head.index(flag) + 1] == "none"
    inner = argv[programs[-1] :]
    assert inner[0] == "bwrap" and inner[-2:] == ["curl", "x"]
    assert inner[inner.index("--cap-drop") + 1] == "ALL"
    assert "--share-net" in inner
    # The programs to pin: pasta, sh, nft, bwrap.
    assert [argv[i] for i in programs] == ["pasta", "sh", "nft", "bwrap"]


def test_a_fenced_step_gets_its_own_hosts_and_no_dns(tools, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    isolation.add_network_fence(lambda words: FENCE)
    argv, _, programs = isolation.prepare_with_programs(["curl", "x"], network=True)
    inner = argv[programs[-1] :]
    binds = {inner[i + 2]: inner[i + 1] for i, w in enumerate(inner) if w == "--ro-bind"}
    with open(binds["/etc/hosts"]) as f:
        assert "10.0.0.7 app.example.com" in f.read()
    with open(binds["/etc/resolv.conf"]) as f:
        assert "nameserver" not in f.read()


def test_without_a_fence_nothing_changes(tools, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    isolation.add_network_fence(lambda words: None)
    argv, refusal, programs = isolation.prepare_with_programs(["curl", "x"], network=True)
    assert refusal is None and argv[0] == "bwrap" and programs == [0]
    # A step without the network is never asked about.
    asked = []
    isolation.add_network_fence(lambda words: asked.append(words))
    isolation.prepare_with_programs(["ls"], network=False)
    assert asked == []


@pytest.mark.parametrize(
    "hook", [lambda words: "out of scope", lambda words: 1 / 0], ids=["refusal", "crash"]
)
def test_a_fence_refusal_or_failure_stops_the_step(tools, hook):
    isolation.add_network_fence(hook)
    argv, refusal, _ = isolation.prepare_with_programs(["curl", "x"], network=True)
    assert argv is None and refusal.startswith("Refused")


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("available", lambda: "unshare"),
        ("available", lambda: None),
        ("fence_available", lambda: False),
    ],
)
def test_a_fence_that_cannot_be_built_refuses(monkeypatch, setting, value):
    monkeypatch.setattr(isolation, "available", lambda: "bwrap")
    monkeypatch.setattr(isolation, "fence_available", lambda: True)
    monkeypatch.setattr(isolation, setting, value)
    isolation.add_network_fence(lambda words: FENCE)
    argv, refusal, _ = isolation.prepare_with_programs(["curl", "x"], network=True)
    assert argv is None and "fence" in refusal


def test_privacy_trusted_does_not_skip_the_fence(tools, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    isolation.add_network_fence(lambda words: FENCE)
    argv, refusal, _ = isolation.prepare_with_programs(["curl", "x"], network=True)
    assert argv is None and "fence" in refusal


def test_the_fence_programs_are_pinned(monkeypatch, tmp_path):
    """codebud integrity pin covers them when the security layer is there."""
    from codebud import profiles
    from codebud.cli import integrity as cli

    monkeypatch.setattr(isolation, "fence_available", lambda: True)
    monkeypatch.setattr(profiles, "shipped_profile_dir", lambda: tmp_path)
    assert set(isolation.FENCE_PROGRAMS) <= cli._programs(set())
    monkeypatch.setattr(profiles, "shipped_profile_dir", lambda: None)
    assert not set(isolation.FENCE_PROGRAMS) & (cli._programs(set()) - {"sh"})


# --- live, where bubblewrap, pasta and nft work -----------------------------------------


def _fence_works() -> bool:
    if not (shutil.which("bwrap") and isolation.fence_available()):
        return False
    probe = subprocess.run(
        ["pasta", "--config-net", "--quiet", "--", "nft", "list", "ruleset"],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0 and isolation.available() == "bwrap"


live = pytest.mark.skipif(not _fence_works(), reason="needs bwrap, pasta and nft")


@live
def test_live_the_step_cannot_reach_the_host_or_change_the_rules(tmp_path, monkeypatch):
    from codebud.sandbox import Sandbox

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEBUD_PRIVACY", "private")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    isolation.add_network_fence(lambda words: isolation.Fence())
    sandbox = Sandbox(timeout=30)
    try:
        reach = sandbox.run_command(
            f"timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}'", network=True
        )
        assert reach["returncode"] != 0  # the host's loopback is not the step's
        flush = sandbox.run_command("nft flush ruleset", network=True)
        assert flush["returncode"] != 0 and "not permitted" in flush["stderr"]
        dns = sandbox.run_command("getent hosts example.com", network=True)
        assert dns["returncode"] != 0
        wrote = sandbox.run_command("touch made-in-step", network=True)
        assert wrote["returncode"] == 0 and (tmp_path / "made-in-step").exists()
    finally:
        server.close()
