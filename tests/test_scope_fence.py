"""
tests/test_scope_fence.py

The engagement's network fence policy (phase 10, step 5 plan, PR 6):
which addresses a network step may reach. Scanning steps are fenced to
exactly their targets (names resolved per step), excluded networks are
dropped first, passive lookups are not fenced, and scans that need raw
sockets are refused with that reason.
"""

import ipaddress
import shlex

import pytest

from codebud import isolation, safety
from codebud_sec import fence as fence_module
from codebud_sec.engagement import load
from codebud_sec.fence import ScopeFence
from codebud_sec.scope import ScopeGuard

SCOPE = """\
client = "ACME"
in_scope = ["10.0.0.0/16", "app.example.com"]
out_of_scope = ["10.0.5.0/24", "10.0.9.9"]
"""
NET = ipaddress.ip_network


@pytest.fixture(autouse=True)
def resolver(monkeypatch):
    names = {"app.example.com": ["10.0.0.7", "2001:db8::7"]}

    def resolve(name):
        if name not in names:
            raise OSError("no such name")
        return names[name]

    monkeypatch.setattr(fence_module, "_resolve", resolve)
    return names


def _fence(tmp_path, extra=""):
    path = tmp_path / "acme.toml"
    path.write_text(SCOPE + extra)
    return ScopeFence(load(path))


def _decide(fence, cmd):
    return fence.decide(shlex.split(cmd))


def test_a_scan_may_reach_exactly_its_targets(tmp_path):
    decision = _decide(_fence(tmp_path), "nmap --unprivileged -Pn -n 10.0.0.5 10.0.1.0/24")
    assert isinstance(decision, isolation.Fence)
    assert set(decision.allow) == {NET("10.0.0.5/32"), NET("10.0.1.0/24")}
    assert decision.hosts == ()


def test_names_are_resolved_into_the_steps_hosts(tmp_path):
    decision = _decide(_fence(tmp_path), "curl -sS https://app.example.com/")
    assert set(decision.allow) == {NET("10.0.0.7/32"), NET("2001:db8::7/128")}
    assert set(decision.hosts) == {
        ("app.example.com", "10.0.0.7"),
        ("app.example.com", "2001:db8::7"),
    }


def test_excluded_networks_are_always_dropped(tmp_path):
    decision = _decide(_fence(tmp_path), "nmap --unprivileged 10.0.0.0/16")
    assert set(decision.deny) == {NET("10.0.5.0/24"), NET("10.0.9.9/32")}
    rules = isolation.nft_ruleset(decision)
    assert rules.index("10.0.5.0/24 drop") < rules.index("10.0.0.0/16 accept")


def test_a_name_that_does_not_resolve_is_refused(tmp_path):
    decision = _decide(_fence(tmp_path), "curl https://gone.app.example.com/")
    assert isinstance(decision, str) and "cannot resolve" in decision


@pytest.mark.parametrize("cmd", ["dig app.example.com", "host 10.0.0.5", "whois app.example.com"])
def test_passive_lookups_are_not_fenced(tmp_path, cmd):
    assert _decide(_fence(tmp_path), cmd) is None


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("nmap -Pn 10.0.0.5", "--unprivileged"),
        ("nmap --unprivileged -sS 10.0.0.5", "raw sockets"),
        ("nmap --unprivileged -sU 10.0.0.5", "raw sockets"),
        ("nmap --unprivileged -O 10.0.0.5", "raw sockets"),
        ("masscan -p80 10.0.1.0/24", "raw sockets"),
    ],
)
def test_scans_that_need_raw_sockets_are_refused(tmp_path, cmd, why):
    decision = _decide(_fence(tmp_path), cmd)
    assert isinstance(decision, str) and why in decision, cmd


def test_fence_scanning_false_gives_raw_scans_back(tmp_path):
    fence = _fence(tmp_path, "fence_scanning = false\n")
    for cmd in ("nmap -sS 10.0.0.5", "masscan -p80 10.0.1.0/24", "curl https://app.example.com/"):
        assert _decide(fence, cmd) is None, cmd


def test_fence_scanning_must_be_a_boolean(tmp_path):
    from codebud_sec.engagement import EngagementError

    path = tmp_path / "bad.toml"
    path.write_text(SCOPE + 'fence_scanning = "no"\n')
    with pytest.raises(EngagementError):
        load(path)


def test_the_guard_turns_the_fence_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])
    monkeypatch.setattr(isolation, "_FENCES", [])
    path = tmp_path / "acme.toml"
    path.write_text(SCOPE)
    guard = ScopeGuard(load(path))
    guard.activate()
    assert isolation._fence_for(["nmap", "--unprivileged", "10.0.0.5"]) is not None
    guard.deactivate()
    assert isolation._FENCES == []
