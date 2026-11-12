"""
tests/test_scope.py

The engagement scope guard (phase 10, step 5 plan, PR 4; D14i): every
target of a network tool is read from its command line and checked;
active tools out of scope are refused, passive lookups ask; a target
that cannot be read is refused.
"""

import ipaddress
import shlex

import pytest

from codebud import safety
from codebud.safety import SafetyEngine
from codebud_sec.engagement import load
from codebud_sec.scope import ACTIVE, PASSIVE, ScopeGuard, TargetError, targets_of

SCOPE = """\
client = "ACME"
in_scope = ["10.0.0.0/16", "app.example.com", "2001:db8::/48"]
out_of_scope = ["10.0.5.0/24", "10.0.9.9"]
"""


# A small nmap script.db with the categories nmap gives these scripts.
SCRIPT_DB = {
    "http-title": ("default", "discovery", "safe"),
    "ssl-cert": ("default", "discovery", "safe"),
    "http-open-proxy": ("default", "discovery", "external", "safe"),
    "banner": ("discovery", "safe"),
    "broadcast-ping": ("broadcast", "discovery", "safe"),
    "broadcast-dhcp-discover": ("broadcast", "safe"),
    "targets-sniffer": ("broadcast", "discovery", "safe"),
    "whois-ip": ("discovery", "external", "safe"),
    "smb-vuln-ms17-010": ("vuln", "safe"),
    "http-shellshock": ("exploit", "vuln", "intrusive"),
    "ftp-brute": ("brute", "intrusive"),
    "http-slowloris": ("dos", "intrusive", "vuln"),
    "iec61850-mms": ("discovery", "intrusive", "version"),
}


@pytest.fixture(autouse=True)
def script_db(tmp_path, monkeypatch):
    path = tmp_path / "script.db"
    path.write_text(
        "".join(
            f'Entry {{ filename = "{name}.nse", categories = {{ '
            + "".join(f'"{c}", ' for c in categories)
            + "} }\n"
            for name, categories in SCRIPT_DB.items()
        )
    )
    monkeypatch.setattr("codebud_sec.scope._NMAP_SCRIPT_DBS", (str(path),))
    return path


@pytest.fixture
def engagement(tmp_path):
    path = tmp_path / "acme.toml"
    path.write_text(SCOPE)
    return load(path)


@pytest.fixture
def guard(engagement, monkeypatch):
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])
    monkeypatch.delenv("CODEBUD_PRIVACY", raising=False)
    # Steps count as isolated here whatever the machine has (macOS CI has
    # no bubblewrap); tests of the no-isolation case set it back.
    monkeypatch.setattr("codebud.isolation.available", lambda: "bwrap")
    g = ScopeGuard(engagement)
    g.activate()
    return g


def _targets(cmd):
    [(program, targets)] = targets_of(shlex.split(cmd))
    return targets


def _hosts(cmd):
    return sorted(_targets(cmd).hosts)


def _nets(cmd):
    return [str(n) for n in _targets(cmd).networks]


# --- reading targets -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "hosts"),
    [
        ("nmap -sV -p 22,80 -T4 10.0.0.5", ["10.0.0.5"]),
        ("nmap -Pn -oN out.txt --top-ports 100 app.example.com", ["app.example.com"]),
        ("nmap -p80 -sS --open 10.0.0.5 10.0.0.6", ["10.0.0.5", "10.0.0.6"]),
        ("nmap --dns-servers 8.8.8.8 10.0.0.5", ["10.0.0.5", "8.8.8.8"]),
        ("nmap -sI zombie.example.org:80 10.0.0.5", ["10.0.0.5", "zombie.example.org"]),
        ("nmap -b anon@ftp.example.org:21 10.0.0.5", ["10.0.0.5", "ftp.example.org"]),
        ("dig @1.1.1.1 app.example.com MX +short", ["1.1.1.1", "app.example.com"]),
        ("dig -x 10.0.0.5", ["10.0.0.5"]),
        ("host app.example.com 10.0.0.53", ["10.0.0.53", "app.example.com"]),
        ("nc -zv 10.0.0.5 22", ["10.0.0.5"]),
        ("nc -lvnp 4444", []),  # listening contacts no one
        ("nc -x proxy.example.org:1080 10.0.0.5 80", ["10.0.0.5", "proxy.example.org"]),
        ("ncat --proxy 10.0.0.7:3128 10.0.0.5 80", ["10.0.0.5", "10.0.0.7"]),
    ],
)
def test_targets_are_read_including_third_parties(cmd, hosts):
    assert _hosts(cmd) == sorted(hosts)


@pytest.mark.parametrize(
    ("cmd", "networks"),
    [
        ("nmap 10.0.1.0/24", ["10.0.1.0/24"]),
        ("nmap 10.0.0.1-5", ["10.0.0.1/32", "10.0.0.2/31", "10.0.0.4/31"]),
        ("masscan -p80 --rate 1000 10.0.2.0-10.0.2.255", ["10.0.2.0/24"]),
        ("masscan -p80 --range 10.0.3.0/24", ["10.0.3.0/24"]),
    ],
)
def test_ranges_are_read_as_networks(cmd, networks):
    assert _nets(cmd) == networks


def test_an_octet_range_is_judged_by_its_bounds():
    """10.0.0-3.1-254 names 4 x 254 addresses; their bounding range,
    10.0.0.1 - 10.0.3.254, holds them all, so judging it errs strict."""
    nets = _targets("nmap 10.0.0-3.1-254").networks
    first, last = nets[0].network_address, nets[-1].broadcast_address
    assert (str(first), str(last)) == ("10.0.0.1", "10.0.3.254")


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("nmap -iR 100", "random hosts"),
        ("nmap --resume scan.log", "a file"),
        ("nmap --frobnicate x 10.0.0.5", "unknown option"),
        ("nmap -sV", "no target"),
        ("nmap 'not a host!'", "not a target"),
        ("masscan -c scan.conf", "settings file"),
        ("dig -f queries.txt", "batch file"),
        ("nc -Z 10.0.0.5 80", "unknown option"),
        ("nmap -iL /etc/hosts", "a list outside the project"),
    ],
)
def test_what_cannot_be_read_is_an_error(cmd, why):
    with pytest.raises(TargetError):
        targets_of(shlex.split(cmd))


def test_a_target_list_in_the_project_is_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hosts.txt").write_text("# web tier\n10.0.0.5\napp.example.com 10.0.1.0/24\n")
    targets = _targets("nmap -iL hosts.txt")
    assert sorted(targets.hosts) == ["10.0.0.5", "app.example.com"]
    assert targets.networks == [ipaddress.ip_network("10.0.1.0/24")]


def test_tiers():
    assert _targets("dig app.example.com").tier == PASSIVE
    assert _targets("nmap 10.0.0.5").tier == ACTIVE


def test_tools_are_found_behind_wrappers_and_by_variant_name():
    assert [p for p, _ in targets_of(shlex.split("timeout 60 nmap 10.0.0.5"))] == ["nmap"]
    assert [p for p, _ in targets_of(shlex.split("nc.openbsd 10.0.0.5 80"))] == ["nc"]


# --- the guard, through the safety engine ----------------------------------------


def _engine():
    engine = SafetyEngine()
    engine.harmless_commands = ["dig", "host", "ls"]
    engine.powerful_commands = ["nmap", "masscan", "nc", "ncat", "timeout", "socat"]
    return engine


def _step(cmd, network=True):
    return {"tool": "command", "args": {"cmd": cmd, "network": network}}


@pytest.mark.parametrize(
    "cmd",
    [
        "nmap -sV 10.0.0.5",
        "nmap -sn 10.0.1.0/24",
        "nmap app.example.com",
        "nmap 2001:db8::5",
        "masscan -p443 10.0.2.0/24",
        "nc -zv 10.0.0.5 22",
        "timeout 600 nmap 10.0.0.5",
    ],
)
def test_in_scope_active_steps_run(guard, cmd):
    assert _engine().refusal_reason(_step(cmd)) is None, cmd


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("nmap 192.168.1.1", "outside"),
        ("nmap 10.0.5.20", "excluded network"),
        ("nmap 10.0.9.9", "excluded address"),
        ("nmap 10.0.0.0/8", "wider than the scope"),
        ("nmap 10.0.9.0/24", "holds an excluded address"),
        ("nmap evil.example.org", "a name not in scope"),
        ("nmap --dns-servers 8.8.8.8 10.0.0.5", "a DNS server outside"),
        ("nmap -sI zombie.example.org 10.0.0.5", "an idle-scan zombie outside"),
        ("nc -x proxy.example.org:1080 10.0.0.5 80", "a proxy outside"),
        ("timeout 60 nmap 192.168.1.1", "behind a wrapper"),
        ("nmap -iR 10", "random targets"),
        ("nmap --weird 10.0.0.5", "unreadable"),
    ],
)
def test_out_of_scope_active_steps_are_refused(guard, cmd, why):
    reason = _engine().refusal_reason(_step(cmd))
    assert reason is not None and "engagement scope" in reason, f"{cmd} ({why})"


def test_passive_lookups_out_of_scope_ask_instead(guard):
    engine = _engine()
    step = _step("dig evil.example.org")
    assert engine.refusal_reason(step) is None
    notice = engine.confirmation_reason(step)
    assert notice is not None and "evil.example.org" in notice
    # In scope: only the usual question for a network step, no scope warning.
    in_scope = engine.confirmation_reason(_step("dig app.example.com"))
    assert in_scope is None or "Outside the engagement" not in in_scope


def test_an_unparsed_network_tool_is_refused(guard):
    reason = _engine().refusal_reason(_step("socat - TCP:app.example.com:80"))
    assert reason is not None and "cannot read its targets" in reason


def test_steps_without_network_tools_are_untouched(guard):
    assert _engine().refusal_reason(_step("ls -la", network=False)) is None


def test_a_closed_window_refuses_active_steps(tmp_path, monkeypatch):
    path = tmp_path / "late.toml"
    path.write_text(SCOPE + "starts = 2020-01-01T00:00:00Z\nends = 2020-01-02T00:00:00Z\n")
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])
    monkeypatch.setattr("codebud.isolation.available", lambda: "bwrap")
    ScopeGuard(load(path)).activate()
    reason = _engine().refusal_reason(_step("nmap 10.0.0.5"))
    assert reason is not None and "window" in reason


def test_privacy_trusted_refuses_every_command(guard, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "trusted")
    reason = _engine().refusal_reason(_step("ls", network=False))
    assert reason is not None and "isolated steps" in reason


def test_deactivate_removes_the_guard(guard):
    guard.deactivate()
    assert _engine().refusal_reason(_step("nmap 192.168.1.1")) is None


# --- review of #18 -----------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["grep host /etc/hosts", "grep -c nmap auth.log", "cat nc.log"])
def test_tool_names_as_data_in_steps_without_network_are_fine(guard, monkeypatch, cmd):
    """An isolated step without the network contacts no one; reading "host"
    or "nmap" in a log is not running them."""
    monkeypatch.setattr("codebud.isolation.available", lambda: "bwrap")
    engine = _engine()
    engine.harmless_commands += ["grep", "cat"]
    assert engine.refusal_reason(_step(cmd, network=False)) is None, cmd


@pytest.mark.parametrize("kind", [None])
def test_an_engagement_needs_isolation(guard, monkeypatch, kind):
    """Without isolation an undeclared step has the network too, so the
    guard could not trust the declaration: every command is refused."""
    monkeypatch.setattr("codebud.isolation.available", lambda: kind)
    reason = _engine().refusal_reason(_step("ls", network=False))
    assert reason is not None and "isolat" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "nmap --script broadcast-dhcp-discover 10.0.0.5",
        "nmap --script=broadcast 10.0.0.5",
        "nmap --script discovery 10.0.0.5",  # holds the broadcast-* scripts
        "nmap --script safe 10.0.0.5",  # so does safe
        "nmap --script external 10.0.0.5",  # contacts third-party services
        "nmap --script all 10.0.0.5",
        "nmap --script 'b*' 10.0.0.5",
        "nmap --script 'default or safe' 10.0.0.5",
        "nmap --script targets-sniffer 10.0.0.5",
        "nmap --script http-title --script-args newtargets 10.0.0.5",
        "nmap --script-args=newtargets,maxdepth=2 --script http-title 10.0.0.5",
    ],
)
def test_nmap_scripts_that_reach_past_the_command_line_are_refused(guard, cmd):
    """Review of #18: broadcast scripts send to the whole local network,
    targets-* with newtargets add the hosts they discover, external ones
    contact third-party services."""
    reason = _engine().refusal_reason(_step(cmd))
    assert reason is not None and "engagement scope" in reason, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "nmap -sV 10.0.0.5",
        "nmap --script http-title,ssl-cert 10.0.0.5",
        "nmap --script 'default and not external' 10.0.0.5",
        "nmap --script smb-vuln-ms17-010 10.0.0.5",
        "nmap --script '(vuln and safe) or banner' 10.0.0.5",
        "nmap --script +http-title.nse 10.0.0.5",
    ],
)
def test_ordinary_scripts_run(guard, cmd):
    assert _engine().refusal_reason(_step(cmd)) is None, cmd


def test_web_search_is_refused_under_an_engagement(guard):
    """Re-review of #18: it sends the query to a third party (DuckDuckGo),
    outside any scope, whatever the web search setting."""
    reason = _engine().refusal_reason({"tool": "web_search", "args": {"query": "acme vpn"}})
    assert reason is not None and "engagement" in reason


def test_local_tools_are_untouched(guard):
    for step in (
        {"tool": "file_read", "args": {"path": "notes.txt"}},
        {"tool": "host_network", "args": {}},
    ):
        assert _engine().refusal_reason(step) is None, step


def test_the_guard_provides_the_engagement(guard):
    """A profile with engagement = true runs steps only while it is active."""
    assert safety.provided("engagement")
    guard.deactivate()
    assert not safety.provided("engagement")


# --- scripts judged by nmap's script database (PR 5) -----------------------------


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("nmap --script http-shellshock 10.0.0.5", "exploit"),
        ("nmap --script ftp-brute 10.0.0.5", "brute"),
        ("nmap --script http-slowloris 10.0.0.5", "dos"),
        ("nmap --script vuln 10.0.0.5", "the category holds exploit and dos scripts"),
        ("nmap --script intrusive 10.0.0.5", "intrusive"),
        ("nmap --script 'http-*' 10.0.0.5", "a glob holding exploits"),
        ("nmap --script 'not safe' 10.0.0.5", "everything but safe"),
        ("nmap --script 'vuln and not dos' 10.0.0.5", "still holds the exploit"),
        ("nmap --script ./custom.nse 10.0.0.5", "a script file"),
        ("nmap --script /tmp/x 10.0.0.5", "a script path"),
        ("nmap --script nosuch 10.0.0.5", "unknown to nmap"),
        ("nmap --script '(default' 10.0.0.5", "unreadable"),
    ],
)
def test_scripts_beyond_scanning_are_refused(guard, cmd, why):
    reason = _engine().refusal_reason(_step(cmd))
    assert reason is not None and "engagement scope" in reason, f"{cmd} ({why})"


@pytest.mark.parametrize("flag", ["-sC", "-A"])
def test_default_scripts_hold_external_ones(guard, flag):
    """nmap's own default category holds http-open-proxy and
    socks-open-proxy, which nmap marks external."""
    reason = _engine().refusal_reason(_step(f"nmap {flag} 10.0.0.5"))
    assert reason is not None and "http-open-proxy" in reason
    assert "default and not external" in reason


def test_without_nmaps_script_database_no_script_is_judged(guard, monkeypatch):
    monkeypatch.setattr("codebud_sec.scope._NMAP_SCRIPT_DBS", ("/nonexistent/script.db",))
    for cmd in ("nmap --script http-title 10.0.0.5", "nmap -sC 10.0.0.5"):
        reason = _engine().refusal_reason(_step(cmd))
        assert reason is not None and "script.db" in reason, cmd
    assert _engine().refusal_reason(_step("nmap -sV 10.0.0.5")) is None
