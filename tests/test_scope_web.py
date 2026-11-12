"""
tests/test_scope_web.py

The engagement scope guard for web and TLS tools (phase 10, step 5
plan, PR 4b): curl, wget, whatweb, nikto, gobuster, ffuf, feroxbuster,
sslscan and testssl.sh. A URL's host is read by a strict grammar (an
ambiguous URL is refused), proxies, --resolve and --connect-to hosts
are targets, following redirects is refused, and a fuzz keyword may
only stand in the path.
"""

import shlex

import pytest

from codebud import safety
from codebud.safety import SafetyEngine
from codebud_sec.engagement import load
from codebud_sec.scope import ACTIVE, PASSIVE, ScopeGuard, TargetError, targets_of

SCOPE = """\
client = "ACME"
in_scope = ["10.0.0.0/16", "app.example.com", "2001:db8::/48"]
out_of_scope = ["10.0.5.0/24", "admin.app.example.com"]
"""


@pytest.fixture
def guard(tmp_path, monkeypatch):
    path = tmp_path / "acme.toml"
    path.write_text(SCOPE)
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])
    monkeypatch.delenv("CODEBUD_PRIVACY", raising=False)
    monkeypatch.setattr("codebud.isolation.available", lambda: "bwrap")
    g = ScopeGuard(load(path))
    g.activate()
    return g


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    (root / "words.txt").write_text("admin\nlogin\n/backup\n.git/HEAD\n")
    monkeypatch.chdir(root)
    return root


def _hosts(cmd):
    [(_, targets)] = targets_of(shlex.split(cmd))
    return sorted(targets.hosts)


NO_REDIRECTS = "--max-redirect=0"


# --- reading targets ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "hosts"),
    [
        ("curl -sS https://app.example.com/login", ["app.example.com"]),
        ("curl -sSo page.html http://10.0.0.5:8080/", ["10.0.0.5"]),
        ("curl -XPOST -d @body.json https://user:pw@app.example.com/api", ["app.example.com"]),
        ("curl -g 'http://[2001:db8::5]:80/'", ["2001:db8::5"]),
        ("curl app.example.com", ["app.example.com"]),
        (
            "curl -x http://proxy.example.org:3128 https://app.example.com/",
            ["app.example.com", "proxy.example.org"],
        ),
        ("curl -x http://127.0.0.1:8080 https://app.example.com/", ["app.example.com"]),
        (
            "curl --resolve app.example.com:443:10.0.0.9 https://app.example.com/",
            ["10.0.0.9", "app.example.com"],
        ),
        (
            "curl --connect-to app.example.com:443:10.0.0.7:8443 https://app.example.com/",
            ["10.0.0.7", "app.example.com"],
        ),
        ("curl --dns-servers 10.0.0.53 https://app.example.com/", ["10.0.0.53", "app.example.com"]),
        (
            "curl --doh-url https://dns.example.net/dns-query https://app.example.com/",
            ["app.example.com", "dns.example.net"],
        ),
        (f"wget {NO_REDIRECTS} -q -O out.html https://app.example.com/", ["app.example.com"]),
        (
            f"wget {NO_REDIRECTS} -r -np -e robots=off https://app.example.com/docs/",
            ["app.example.com"],
        ),
        (
            "whatweb --follow-redirect=never -a 3 https://app.example.com 10.0.0.5",
            ["10.0.0.5", "app.example.com"],
        ),
        ("nikto -h https://app.example.com -Tuning 123 -nointeractive", ["app.example.com"]),
        ("nikto -h 10.0.0.5 -p 8443 -ssl", ["10.0.0.5"]),
        ("gobuster dir -u https://app.example.com/ -w words.txt -x php -t 20", ["app.example.com"]),
        (
            "gobuster dir -u https://app.example.com/ -w words.txt -d -c session=1",
            ["app.example.com"],
        ),
        (
            "gobuster vhost -u https://10.0.0.5 -w words.txt --append-domain --domain app.example.com",
            ["10.0.0.5"],
        ),
        (
            "gobuster dns -d app.example.com -w words.txt -r 10.0.0.53 -c",
            ["10.0.0.53", "app.example.com"],
        ),
        ("ffuf -u https://app.example.com/FUZZ -w words.txt -mc 200,301", ["app.example.com"]),
        (
            "ffuf -u https://app.example.com/?id=FUZZ -w words.txt -H 'Host: FUZZ.app.example.com'",
            ["app.example.com"],
        ),
        (
            "ffuf --u=https://app.example.com/W1 -w words.txt:W1 -x http://127.0.0.1:8080",
            ["app.example.com"],
        ),
        ("feroxbuster -u https://app.example.com -w words.txt -n -q", ["app.example.com"]),
        ("feroxbuster -u https://app.example.com -w words.txt --burp", ["app.example.com"]),
        ("sslscan --no-colour app.example.com:443", ["app.example.com"]),
        ("sslscan --starttls-smtp [2001:db8::25]:25", ["2001:db8::25"]),
        ("testssl.sh --fast --jsonfile out.json https://app.example.com", ["app.example.com"]),
        ("testssl.sh -t smtp --ip 10.0.0.25 app.example.com:25", ["10.0.0.25", "app.example.com"]),
        ("testssl -U 10.0.0.5:443", ["10.0.0.5"]),
    ],
)
def test_targets_are_read(project, cmd, hosts):
    assert _hosts(cmd) == sorted(hosts), cmd


def test_tiers(project):
    [(_, ffuf)] = targets_of(shlex.split("ffuf -u https://app.example.com/FUZZ -w words.txt"))
    [(_, dns)] = targets_of(shlex.split("gobuster dns -d app.example.com -w words.txt"))
    assert ffuf.tier == ACTIVE and dns.tier == PASSIVE


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        # redirects go where the server says
        ("curl -L https://app.example.com/", "follows redirects"),
        ("curl -sSL https://app.example.com/", "follows redirects, in a cluster"),
        ("wget https://app.example.com/", "follows redirects by default"),
        ("wget --max-redirect=5 https://app.example.com/", "follows redirects"),
        ("whatweb https://app.example.com", "follows redirects by default"),
        ("nikto -h app.example.com -followredirects", "follows redirects"),
        ("gobuster dir -u https://app.example.com -w words.txt -r", "follows redirects"),
        ("ffuf -u https://app.example.com/FUZZ -w words.txt -r", "follows redirects"),
        ("feroxbuster -u https://app.example.com -w words.txt -r", "follows redirects"),
        # targets or options from elsewhere
        ("curl -K opts.txt", "config file"),
        (f"wget {NO_REDIRECTS} -i urls.txt", "input file"),
        (f"wget {NO_REDIRECTS} -e use_proxy=on https://app.example.com/", "wgetrc command"),
        (f"wget {NO_REDIRECTS} -r -H https://app.example.com/", "spans hosts"),
        ("ffuf -request req.txt -w words.txt", "raw request file"),
        ("ffuf -input-cmd 'seq 100' -u https://app.example.com/FUZZ", "runs a command"),
        ("feroxbuster --stdin -w words.txt", "targets from stdin"),
        ("testssl.sh --file targets.txt", "mass testing"),
        ("testssl.sh --mx example.com", "hosts a DNS answer names"),
        ("testssl.sh --openssl /tmp/x app.example.com", "runs a program"),
        ("nikto -config my.conf -h app.example.com", "settings file"),
        ("gobuster s3 -w words.txt", "third-party cloud"),
        ("gobuster fuzz -u https://FUZZ.example.com -w words.txt", "fuzz mode"),
        ("curl --unix-socket /run/docker.sock http://x/", "a local service"),
        # ambiguous or unjudgeable URLs
        ("curl 'http://a@evil.example.org@app.example.com/'", "two @"),
        ("curl 'http://app.example.com\\@evil.example.org/'", "a backslash"),
        ("curl 'http://%61pp.example.com/'", "an escape in the host"),
        ("curl 'http://10.0.0.[1-254]/'", "globbing"),
        ("curl 'http://{app,evil}.example.com/'", "globbing"),
        ("curl file:///etc/passwd", "not a web URL"),
        ("curl gopher://app.example.com/", "not a web URL"),
        ("gobuster dir -u app.example.com -w words.txt", "needs a scheme"),
        # a keyword before the path could become any host
        ("ffuf -u https://FUZZ.app.example.com/ -w words.txt", "keyword in the host"),
        ("ffuf -u https://FUZZ@app.example.com/ -w words.txt", "keyword in the userinfo"),
        ("ffuf -u https://app.example.comFUZZ -w words.txt", "keyword after the host"),
        ("ffuf -u FUZZ://app.example.com/ -w words.txt", "keyword in the scheme"),
        ("ffuf -u https://W1.app.example.com/ -w words.txt:W1", "custom keyword in the host"),
        # unknown options and missing targets
        ("curl --frobnicate https://app.example.com/", "unknown option"),
        ("nikto -h app.example.com extra", "stray operand"),
        ("ffuf -w words.txt", "no target"),
        ("feroxbuster -u https://app.example.com", "no wordlist to check"),
        ("testssl.sh a.example.com b.example.com", "two targets"),
    ],
)
def test_what_cannot_be_judged_is_an_error(project, cmd, why):
    with pytest.raises(TargetError):
        targets_of(shlex.split(cmd))


def test_target_files_are_refused(project):
    (project / "hosts.txt").write_text("10.0.0.5\n")
    for cmd in (
        "whatweb --follow-redirect=never hosts.txt",
        "nikto -h hosts.txt",
    ):
        with pytest.raises(TargetError):
            targets_of(shlex.split(cmd))


def test_sslscan_reads_a_target_list_in_the_project(project):
    (project / "tls.txt").write_text("# web\napp.example.com:443\n10.0.0.5:8443\n")
    assert _hosts("sslscan --targets=tls.txt") == ["10.0.0.5", "app.example.com"]
    with pytest.raises(TargetError):
        targets_of(shlex.split("sslscan --targets=/etc/hosts"))


@pytest.mark.parametrize(
    ("tool", "config"),
    [("feroxbuster", "ferox-config.toml"), ("nikto", "nikto.conf")],
)
def test_a_config_file_in_the_project_refuses_the_tool(project, tool, config):
    """A step could write it, adding a proxy or targets codebud never sees."""
    cmd = {
        "feroxbuster": "feroxbuster -u https://app.example.com -w words.txt",
        "nikto": "nikto -h app.example.com",
    }[tool]
    assert _hosts(cmd) == ["app.example.com"]
    (project / config).write_text('proxy = "http://evil.example.org:8080"\n')
    with pytest.raises(TargetError, match=config):
        targets_of(shlex.split(cmd))


@pytest.mark.parametrize(
    "line", ["//evil.example.org", "\\\\evil.example.org", "https:evil.example.org", "http://x"]
)
def test_feroxbuster_wordlist_words_that_leave_the_host_are_refused(project, line):
    """feroxbuster joins each word to the URL, and these would replace the host."""
    (project / "bad.txt").write_text(f"admin\n{line}\n")
    with pytest.raises(TargetError, match="line 2"):
        targets_of(shlex.split("feroxbuster -u https://app.example.com -w bad.txt"))


# --- the guard, through the safety engine ----------------------------------------


def _engine():
    engine = SafetyEngine()
    engine.powerful_commands = [
        "curl", "wget", "whatweb", "nikto", "gobuster", "ffuf", "feroxbuster", "sslscan",
        "testssl.sh",
    ]  # fmt: skip
    return engine


def _step(cmd):
    return {"tool": "command", "args": {"cmd": cmd, "network": True}}


@pytest.mark.parametrize(
    "cmd",
    [
        "curl -sS https://app.example.com/",
        f"wget {NO_REDIRECTS} -O out.html http://10.0.0.5/",
        "ffuf -u https://app.example.com/FUZZ -w words.txt",
        "testssl.sh --fast app.example.com",
    ],
)
def test_in_scope_web_steps_run(guard, project, cmd):
    assert _engine().refusal_reason(_step(cmd)) is None, cmd


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("curl https://evil.example.org/", "outside"),
        ("curl https://admin.app.example.com/", "excluded subdomain"),
        ("curl -x http://proxy.example.org:3128 https://app.example.com/", "a proxy outside"),
        (
            "curl --resolve app.example.com:443:192.168.1.1 https://app.example.com/",
            "resolved outside",
        ),
        ("curl --connect-to ::evil.example.org: https://app.example.com/", "connects outside"),
        ("sslscan 10.0.5.20", "excluded network"),
        ("curl -L https://app.example.com/", "redirects"),
        ("ffuf -u https://FUZZ.app.example.com/ -w words.txt", "keyword in the host"),
    ],
)
def test_out_of_scope_web_steps_are_refused(guard, project, cmd, why):
    reason = _engine().refusal_reason(_step(cmd))
    assert reason is not None and "engagement scope" in reason, f"{cmd} ({why})"


def test_dns_brute_forcing_out_of_scope_asks(guard, project):
    engine = SafetyEngine()
    engine.harmless_commands = ["gobuster"]
    step = _step("gobuster dns -d evil.example.org -w words.txt")
    assert engine.refusal_reason(step) is None
    notice = engine.confirmation_reason(step)
    assert notice is not None and "evil.example.org" in notice


# --- review of #20 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "env HTTPS_PROXY=http://evil.example.org:8080 curl https://app.example.com/",
        "env ALL_PROXY=socks5h://evil.example.org gobuster dir -u https://app.example.com/ -w w",
        f"env WGETRC=./x wget {NO_REDIRECTS} https://app.example.com/",
        "env HOME=. curl https://app.example.com/",
        "env -u LANG XDG_CONFIG_HOME=. ffuf -u https://app.example.com/FUZZ -w words.txt",
        "timeout 60 env CURL_HOME=. curl https://app.example.com/",
    ],
)
def test_environment_assignments_before_a_tool_are_refused(project, cmd):
    """Review of #20: a proxy or config set in the environment is not in
    the tool's words."""
    with pytest.raises(TargetError, match="before"):
        targets_of(shlex.split(cmd))


def test_harmless_environment_assignments_are_fine(project):
    assert _hosts("env LANG=C LC_ALL=C TZ=UTC curl https://app.example.com/") == ["app.example.com"]


@pytest.mark.parametrize(
    "cmd",
    [
        "sslscan --certs evil.example.org app.example.com",  # --certs takes =value only
        "sslscan --sni-name x app.example.com",
        "sslscan --xmpp-server evil.example.org app.example.com",  # a flag: two hosts
        "sslscan app.example.com evil.example.org",
    ],
)
def test_sslscan_is_read_as_sslscan_reads_it(project, cmd):
    """Review of #20: sslscan takes values only after =, and --xmpp-server
    is a flag, so reading them otherwise judged a host it does not scan."""
    with pytest.raises(TargetError):
        targets_of(shlex.split(cmd))


def test_sslscan_values_after_equals_are_fine(project):
    cmd = "sslscan --sni-name=app.example.com --xml=out.xml --xmpp-server 10.0.0.5:5269"
    assert _hosts(cmd) == ["10.0.0.5"]


def test_testssl_phone_out_is_refused(project):
    """Review of #20: it contacts the CRL and OCSP servers of third parties."""
    with pytest.raises(TargetError, match="phone-out"):
        targets_of(shlex.split("testssl.sh --phone-out x app.example.com"))


def test_gobuster_dir_needs_a_path_to_keep_words_out_of_the_host(project):
    """Review of #20: gobuster appends each word to the URL, and without a
    path a word like @evil.example.org would land in the authority."""
    with pytest.raises(TargetError, match="path"):
        targets_of(shlex.split("gobuster dir -u https://app.example.com -w words.txt"))
    assert _hosts("gobuster dir -u https://app.example.com/ -w words.txt") == ["app.example.com"]


@pytest.mark.parametrize(
    "line", ["\x01//evil.example.org", "/\t/evil.example.org", " \\/evil.example.org", "HTTPS:x"]
)
def test_feroxbuster_wordlist_is_judged_after_the_url_cleanup(project, line):
    """Review of #20: the URL standard strips leading control characters
    and removes tabs, so these become //evil.example.org."""
    (project / "bad.txt").write_text(f"admin\n{line}\n")
    with pytest.raises(TargetError, match="line 2"):
        targets_of(shlex.split("feroxbuster -u https://app.example.com -w bad.txt"))


def test_feroxbuster_wordlist_words_that_reach_no_host_are_fine(project):
    (project / "ok.txt").write_text("javascript:void(0)\nmailto:x\n/admin\nadmin\n")
    assert _hosts("feroxbuster -u https://app.example.com -w ok.txt") == ["app.example.com"]
