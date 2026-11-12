"""
codebud_sec/scope.py

Keeping network steps inside the engagement (phase 10, step 5 plan,
PR 4; decision D14i). Every target a network tool will contact is read
from its command line and checked against the engagement file:

- active tools (scanners, nc, web clients, fuzzers, TLS scanners) are
  refused when a target is outside the scope, excluded, or the
  engagement window is closed -- a hard block, because a model's plan is
  untrusted input and a warning is clicked through (UK AISI, 4 Aug 2026;
  Akhawe & Felt 2013);
- passive lookups (dig, host) out of scope ask for confirmation (D14's
  warn-and-confirm, kept for them).

A target that cannot be read is refused: each tool's parser knows which
options take values, and an option it does not know is refused, since
its value could be a target it cannot see. Hosts a tool contacts besides
its target -- a DNS server, a proxy, an idle-scan zombie, an FTP relay
-- are targets too. Target lists from a file are read only from a file
inside the project. A range must fit in the scope as a whole; a name is
never matched against addresses (that would mean resolving it, itself a
network action).

Only steps that declare the network are checked: under isolation a step
without it contacts no one (and reading "host" in a log is not running
it). So an engagement needs isolation; without it every command step is
refused. A step that declares the network with a tool that has no
parser here is refused. The guard is registered with the core's safety engine
(codebud.safety.add_step_check / add_step_notice) by whoever opens the
engagement; the core never imports this layer.
"""

import fnmatch
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass, field

PASSIVE, ACTIVE = "passive", "active"


class TargetError(Exception):
    """A command line whose targets cannot be read."""


@dataclass
class Targets:
    tier: str
    hosts: list[str] = field(default_factory=list)  # names and single addresses
    networks: list = field(default_factory=list)  # ipaddress networks (ranges, CIDRs)

    def add(self, spec: str, what: str = "target") -> None:
        kind, value = _spec(spec, what)
        (self.hosts if kind == "host" else self.networks).extend(value)


# --- target specifications ----------------------------------------------------

_NAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)*"
                   r"[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.?$")  # fmt: skip
_OCTET = re.compile(r"^(\*|\d{1,3}(-\d{1,3})?(,\d{1,3}(-\d{1,3})?)*)$")


def _spec(spec: str, what: str):
    """("host", [str]) or ("networks", [network, ...]) for one target
    specification, or TargetError if it is not one codebud can judge."""
    text = spec.strip().strip("[]")
    if not text:
        raise TargetError(f"empty {what}")
    try:
        return "host", [str(ipaddress.ip_address(text))]
    except ValueError:
        pass
    if "/" in text:
        try:
            return "networks", [ipaddress.ip_network(text, strict=False)]
        except ValueError as exc:
            raise TargetError(f"{what} {spec!r}: {exc}") from exc
    if text.count("-") == 1 and text.count(".") == 6:  # masscan: a.b.c.d-e.f.g.h
        first, last = text.split("-")
        try:
            lo, hi = ipaddress.ip_address(first), ipaddress.ip_address(last)
            return "networks", list(ipaddress.summarize_address_range(lo, hi))
        except (ValueError, TypeError) as exc:
            raise TargetError(f"{what} {spec!r}: {exc}") from exc
    octets = text.split(".")
    # Four dotted parts of digits, ranges and * (a plain address was handled
    # above) are a range, though "10.0.0.1-5" would also pass as a name.
    if len(octets) == 4 and all(_OCTET.match(o) for o in octets):
        # nmap octet ranges (10.0.0-3.1-254, 10.0.0.*): judged by their
        # bounding range, which holds every address they name.
        lows, highs = [], []
        for octet in octets:
            numbers = [0, 255] if octet == "*" else [int(n) for n in re.split(r"[-,]", octet)]
            if any(n > 255 for n in numbers):
                raise TargetError(f"{what} {spec!r}: not an address range")
            lows.append(min(numbers))
            highs.append(max(numbers))
        lo = ipaddress.ip_address(".".join(map(str, lows)))
        hi = ipaddress.ip_address(".".join(map(str, highs)))
        return "networks", list(ipaddress.summarize_address_range(lo, hi))
    if _NAME.match(text) and not text.replace(".", "").isdigit():
        return "host", [text.lower().rstrip(".")]
    raise TargetError(f"{what} {spec!r} is not an address, network, range or host name")


def _list_file(path: str, targets: Targets, spec=lambda text: text) -> None:
    """Targets read from a file: only a file inside the project. ``spec``
    turns each word into a target specification (host:port -> host)."""
    real = os.path.realpath(path)
    project = os.path.realpath(os.getcwd())
    if os.path.commonpath([real, project]) != project or not os.path.isfile(real):
        raise TargetError(f"target list {path}: only a file inside the project can be read")
    with open(real, encoding="utf-8", errors="replace") as f:
        for line in f:
            for word in line.split("#", 1)[0].split():
                targets.add(spec(word), f"target in {path}")


# --- a small option walker -------------------------------------------------------


def _walk(args, value_opts, flag_opts, flag_patterns=(), short_clusters=False):
    """Yield (option, value) for options and (None, word) for operands.
    An option that is neither a known flag nor takes a value is a
    TargetError: its value could be a target."""
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            yield from ((None, a) for a in args[i:])
            return
        if not arg.startswith("-") or arg == "-":
            yield None, arg
            continue
        name, eq, attached = arg.partition("=")
        if name in value_opts:
            if eq:
                yield name, attached
            elif i < len(args):
                yield name, args[i]
                i += 1
            else:
                raise TargetError(f"{name} needs a value")
            continue
        if arg in flag_opts or any(re.match(p, arg) for p in flag_patterns):
            yield arg, None
            continue
        if short_clusters and not arg.startswith("--") and len(arg) > 2:
            # -lvnp 4444: flags, the last one possibly taking the next word.
            letters = [f"-{c}" for c in arg[1:]]
            if all(c in flag_opts for c in letters[:-1]) and (
                letters[-1] in flag_opts or letters[-1] in value_opts
            ):
                for letter in letters[:-1]:
                    yield letter, None
                if letters[-1] in value_opts:
                    if i >= len(args):
                        raise TargetError(f"{letters[-1]} needs a value")
                    yield letters[-1], args[i]
                    i += 1
                else:
                    yield letters[-1], None
                continue
        raise TargetError(f"{arg}: an option codebud does not know, so it cannot tell its targets")


# --- the tools -------------------------------------------------------------------

_DNS_TYPES = {
    "A", "AAAA", "ANY", "AXFR", "CAA", "CNAME", "DNSKEY", "DS", "HINFO", "IXFR", "MX",
    "NAPTR", "NS", "NSEC", "NSEC3", "PTR", "RRSIG", "SOA", "SPF", "SRV", "SSHFP",
    "TLSA", "TXT", "IN", "CH", "HS",
}  # fmt: skip


def _dig(args):
    targets = Targets(PASSIVE)
    value = {"-t", "-c", "-p", "-b", "-k", "-y", "-q", "-x", "-f"}
    flags = {"-4", "-6", "-m", "-r", "-u", "-h", "-v", "-i"}
    for option, word in _walk(args, value, flags):
        if option == "-f":
            raise TargetError("dig -f reads queries from a file, which cannot be judged")
        if option in ("-x", "-q"):
            targets.add(word, "lookup")
        elif option is None:
            if word.startswith("@"):
                targets.add(word[1:], "DNS server")
            elif word.startswith("+"):
                continue
            elif word.upper() not in _DNS_TYPES:
                targets.add(word, "lookup")
    return targets


def _host(args):
    targets = Targets(PASSIVE)
    value = {"-t", "-c", "-W", "-R", "-m", "-N"}
    flags = {
        "-a",
        "-A",
        "-C",
        "-d",
        "-i",
        "-l",
        "-r",
        "-s",
        "-T",
        "-U",
        "-v",
        "-V",
        "-w",
        "-4",
        "-6",
    }
    operands = [w for o, w in _walk(args, value, flags) if o is None]
    if not operands or len(operands) > 2:
        raise TargetError("host takes a name and optionally a DNS server")
    targets.add(operands[0], "lookup")
    if len(operands) == 2:
        targets.add(operands[1], "DNS server")
    return targets


_NMAP_VALUE = {
    "-p", "--exclude-ports", "--top-ports", "--port-ratio", "-oN", "-oX", "-oS", "-oG", "-oA",
    "-e", "-g", "--source-port", "--data-length", "--data", "--data-string", "--ttl",
    "--spoof-mac", "--scan-delay", "--max-scan-delay", "--min-rate", "--max-rate",
    "--max-retries", "--host-timeout", "--min-rtt-timeout", "--max-rtt-timeout",
    "--initial-rtt-timeout", "--min-hostgroup", "--max-hostgroup", "--min-parallelism",
    "--max-parallelism", "--version-intensity", "--script", "--script-args", "--mtu",
    "--datadir", "--servicedb", "--versiondb", "--stylesheet", "--exclude", "--excludefile",
    "--script-timeout", "--max-os-tries", "-iL", "-iR", "--dns-servers", "-sI", "-b", "-S",
    "-D", "--proxies", "--resume", "--script-args-file",
}  # fmt: skip
_NMAP_FLAGS = {
    "-6", "-A", "-F", "-r", "-n", "-R", "-f", "-ff", "-O", "--open", "--reason", "--traceroute",
    "--packet-trace", "--badsum", "--privileged", "--unprivileged", "--system-dns",
    "--osscan-limit", "--osscan-guess", "--version-light", "--version-all", "--version-trace",
    "--script-trace", "--defeat-rst-ratelimit", "--append-output", "--no-stylesheet",
    "--webxml", "--noninteractive", "--send-eth", "--send-ip", "-h", "--help", "-V", "--version",
}  # fmt: skip
_NMAP_PATTERNS = (r"^-s[A-HJ-Za-z]{1,2}$", r"^-P[A-Za-z][0-9,\-]*$", r"^-T[0-5]$", r"^-[vd]+$",
                  r"^-p[0-9TUS:,\-]+$")  # fmt: skip


# nmap scripts (scanning tier, T1). Which scripts an expression selects
# is worked out from nmap's own script database, the way nmap does it:
# names, categories, globs, "all", and/or/not, commas. Refused when any
# selected script is in a category beyond scanning -- exploit, brute, dos,
# intrusive, fuzzer -- or reaches past the command line: broadcast
# (sends to the whole local network), external (contacts third parties;
# even "default" holds two, http-open-proxy and socks-open-proxy), and
# targets-* (adds the hosts it finds as targets). -sC and -A run
# "default". A script given as a file runs code nobody checked. Without
# the database no script can be judged, so --script, -sC and -A are
# refused. -sV runs the "version" scripts and is allowed, although nmap
# marks one of them, iec61850-mms (ICS, port 102), intrusive.
_NMAP_SCRIPT_DBS = (
    "/usr/share/nmap/scripts/script.db",
    "/usr/local/share/nmap/scripts/script.db",
    "/opt/homebrew/share/nmap/scripts/script.db",
)
_NMAP_REFUSED_CATEGORIES = frozenset(
    {"exploit", "brute", "dos", "intrusive", "fuzzer", "broadcast", "external"}
)
_NMAP_ENTRY = re.compile(r'Entry \{ filename = "([^"]+)\.nse", categories = \{([^}]*)\} \}')


def _nmap_scripts() -> dict[str, frozenset] | None:
    """{script name: its categories} from nmap's script.db, or None."""
    for path in _NMAP_SCRIPT_DBS:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        return {
            name: frozenset(re.findall(r'"([^"]+)"', categories))
            for name, categories in _NMAP_ENTRY.findall(text)
        }
    return None


def _nmap_select(expression: str, scripts: dict[str, frozenset]) -> set[str]:
    """The scripts ``expression`` selects (nmap --script grammar)."""
    tokens = re.findall(r"\(|\)|,|[^\s,()]+", expression)
    position = 0

    def peek():
        return tokens[position].lower() if position < len(tokens) else None

    def take():
        nonlocal position
        position += 1
        return tokens[position - 1]

    def atom(word: str) -> set[str]:
        name = word.lstrip("+").lower()
        name = name[:-4] if name.endswith(".nse") else name
        if "/" in name or "\\" in name:
            raise TargetError(
                f"nmap --script {word}: a script file runs code codebud has not checked"
            )
        if name == "all":
            return set(scripts)
        if any(c in name for c in "*?["):
            chosen = {s for s in scripts if fnmatch.fnmatch(s, name)}
        else:
            chosen = {s for s, categories in scripts.items() if name in categories}
            chosen |= {name} if name in scripts else set()
        if not chosen:
            raise TargetError(f"nmap --script {word}: not a script or category nmap knows")
        return chosen

    def factor() -> set[str]:
        word = peek()
        if word is None or word in (")", ",", "and", "or"):
            raise TargetError(f"nmap --script {expression!r} cannot be read")
        take()
        if word == "not":
            return set(scripts) - factor()
        if word == "(":
            chosen = union()
            if peek() != ")":
                raise TargetError(f"nmap --script {expression!r} cannot be read")
            take()
            return chosen
        return atom(tokens[position - 1])

    def conjunction() -> set[str]:
        chosen = factor()
        while peek() == "and":
            take()
            chosen &= factor()
        return chosen

    def union() -> set[str]:
        chosen = conjunction()
        while peek() in ("or", ","):
            take()
            chosen |= conjunction()
        return chosen

    chosen = union()
    if position != len(tokens):
        raise TargetError(f"nmap --script {expression!r} cannot be read")
    return chosen


def _nmap_script_problem(expression: str) -> str | None:
    scripts = _nmap_scripts()
    if scripts is None:
        return "nmap scripts are judged by nmap's script.db, which was not found"
    beyond = sorted(
        s
        for s in _nmap_select(expression, scripts)
        if scripts[s] & _NMAP_REFUSED_CATEGORIES or s.startswith(("broadcast", "targets-"))
    )
    if not beyond:
        return None
    hint = ' (--script "default and not external" runs the rest)' if "default" in expression else ""
    shown = ", ".join(beyond[:3]) + (f" and {len(beyond) - 3} more" if len(beyond) > 3 else "")
    return (
        f"nmap --script {expression} selects scripts beyond scanning or past the command "
        f"line ({shown}){hint}"
    )


def _nmap(args):
    targets = Targets(ACTIVE)
    for option, word in _walk(args, _NMAP_VALUE, _NMAP_FLAGS, _NMAP_PATTERNS):
        if option == "-iR":
            raise TargetError("nmap -iR scans random hosts, outside any scope")
        if option in ("--resume", "--script-args-file"):
            raise TargetError(f"nmap {option} reads from a file that cannot be judged")
        if option in ("--script", "-sC", "-A"):
            problem = _nmap_script_problem(word if option == "--script" else "default")
            if problem:
                raise TargetError(problem.replace("--script default", option))
        if option == "--script-args" and "newtargets" in word.lower():
            raise TargetError(
                "nmap --script-args newtargets adds the hosts scripts find as targets"
            )
        if option == "-iL":
            _list_file(word, targets)
        elif option == "--dns-servers":
            for server in word.split(","):
                targets.add(server, "DNS server")
        elif option == "-sI":
            targets.add(word.split(":")[0], "idle-scan zombie")
        elif option == "-b":
            relay = word.rsplit("@", 1)[-1].split(":")[0]
            targets.add(relay, "FTP relay")
        elif option == "--proxies":
            for proxy in word.split(","):
                targets.add(re.sub(r"^\w+://", "", proxy).split(":")[0].split("/")[0], "proxy")
        elif option is None:
            targets.add(word)
    if not targets.hosts and not targets.networks:
        raise TargetError("nmap names no target")
    return targets


_MASSCAN_VALUE = {
    "-p", "--ports", "--rate", "-oL", "-oX", "-oJ", "-oG", "-oB", "--exclude", "--excludefile",
    "--adapter", "--adapter-ip", "--adapter-port", "--adapter-mac", "--router-mac",
    "--source-ip", "--source-port", "--wait", "--retries", "--ttl", "--shard", "--seed",
    "--connection-timeout", "--range", "-iL", "--includefile", "-c", "--conf", "--resume",
    "--readscan", "--http-user-agent",
}  # fmt: skip
_MASSCAN_FLAGS = {
    "--banners",
    "--open",
    "--open-only",
    "--ping",
    "--offline",
    "-v",
    "-vv",
    "--echo",
}


# masscan sends at --rate packets a second (100 unless given); faster than
# this risks the availability of the client's network and hosts.
MASSCAN_MAX_RATE = 1000


def _masscan(args):
    targets = Targets(ACTIVE)
    for option, word in _walk(args, _MASSCAN_VALUE, _MASSCAN_FLAGS, (r"^-p[0-9U:,\-]+$",)):
        if option in ("-c", "--conf", "--resume"):
            raise TargetError(f"masscan {option} reads settings, targets included, from a file")
        if option == "--rate":
            try:
                rate = float(word)
            except ValueError:
                raise TargetError(f"masscan --rate {word}: not a number") from None
            if not 0 < rate <= MASSCAN_MAX_RATE:
                raise TargetError(
                    f"masscan --rate {word}: at most {MASSCAN_MAX_RATE} packets a second, "
                    "so the scan does not take the client's network down"
                )
        if option in ("-iL", "--includefile"):
            _list_file(word, targets)
        elif option == "--range" or option is None:
            for spec in word.split(","):
                targets.add(spec)
    if not targets.hosts and not targets.networks:
        raise TargetError("masscan names no target")
    return targets


_NC_VALUE = {"-p", "-s", "-w", "-i", "-q", "-x", "-X", "-T", "-I", "-O", "-M", "-m", "-P", "-V"}
_NC_FLAGS = {
    "-l",
    "-k",
    "-n",
    "-u",
    "-v",
    "-z",
    "-4",
    "-6",
    "-N",
    "-d",
    "-D",
    "-h",
    "-C",
    "-t",
    "-b",
    "-r",
}


def _nc(args):
    targets = Targets(ACTIVE)
    listen, operands = False, []
    for option, word in _walk(args, _NC_VALUE, _NC_FLAGS, (r"^-v+$",), short_clusters=True):
        if option == "-l":
            listen = True
        elif option == "-x":
            targets.add(word.rsplit(":", 1)[0] if word.count(":") == 1 else word, "proxy")
        elif option is None:
            operands.append(word)
    if listen:
        return targets  # listening: nothing contacted, only proxies (if any)
    if not operands:
        raise TargetError("nc names no host")
    targets.add(operands[0])
    return targets


_NCAT_VALUE = {
    "-s", "--source", "-p", "--source-port", "-w", "--wait", "-i", "--idle-timeout",
    "--proxy", "--proxy-type", "--proxy-auth", "-o", "--output", "-x", "--hex-dump",
    "--allow", "--deny", "--allowfile", "--denyfile", "-m", "--max-conns", "--ssl-cert",
    "--ssl-key", "--ssl-trustfile", "--ssl-ciphers", "--ssl-alpn", "-d", "--delay",
}  # fmt: skip
_NCAT_FLAGS = {
    "-l", "--listen", "-k", "--keep-open", "-u", "--udp", "--ssl", "--ssl-verify", "--send-only",
    "--recv-only", "-n", "--nodns", "-v", "--verbose", "-C", "--crlf", "-4", "-6", "-z",
    "-t", "--telnet", "--broker", "--chat", "-h", "--help", "--version",
}  # fmt: skip


def _ncat(args):
    targets = Targets(ACTIVE)
    listen, operands = False, []
    for option, word in _walk(args, _NCAT_VALUE, _NCAT_FLAGS, (r"^-v+$",), short_clusters=True):
        if option in ("-l", "--listen"):
            listen = True
        elif option == "--proxy":
            targets.add(word.rsplit(":", 1)[0] if word.count(":") == 1 else word, "proxy")
        elif option is None:
            operands.append(word)
    if listen:
        return targets
    if not operands:
        raise TargetError("ncat names no host")
    targets.add(operands[0])
    return targets


# --- web and TLS tools (PR 4b) -----------------------------------------------------
#
# Their targets are URLs and host[:port]s. The host of a URL is read by
# codebud's own strict grammar, not a URL library's: a URL that two
# parsers could read differently (a second @, a backslash, a %-escape in
# the host) is refused rather than judged. A redirect sends a tool where
# the server says, so following redirects is refused (and, for the tools
# that follow them by default, turning it off is required). A config file
# that the tool reads from the project directory -- which steps can
# write -- could add targets or a proxy unseen, so its presence refuses
# the tool.

_AUTHORITY = re.compile(
    r"^(?:[^@/\\\s%]*@)?(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(?P<port>\d*))?$"
)
_WEB_SCHEMES = ("http", "https")


def _host_port(text: str, what: str) -> str:
    """The host of ``host``, ``host:port``, ``[v6]:port`` or a bare IPv6
    address."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        if rest and not re.fullmatch(r":\d*", rest):
            raise TargetError(f"{what} {text!r} is not host[:port]")
        return host
    if text.count(":") > 1:
        return text  # a bare IPv6 address
    host, _, port = text.partition(":")
    if port and not port.isdigit():
        raise TargetError(f"{what} {text!r} is not host[:port]")
    return host


def _url(url: str, targets: Targets, what: str = "URL", schemes=_WEB_SCHEMES,
         keywords=(), default_scheme: str | None = "http",
         need_path: bool = False) -> None:  # fmt: skip
    """Add the host of ``url``. A URL without a scheme gets
    ``default_scheme`` (None: the scheme is required). ``keywords`` are
    words a fuzzer replaces (FUZZ): one before the path could become any
    host, so it is refused. ``need_path``: the tool appends words to the
    URL, so a path must keep them out of the host."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        if default_scheme is None:
            raise TargetError(f"{what} {url!r} needs a scheme (https://...)")
        scheme, rest = default_scheme, url
    if scheme.lower() not in schemes:
        raise TargetError(f"{what} {url!r}: only {', '.join(schemes)} URLs can be judged")
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    for keyword in keywords:
        if keyword in scheme or keyword in authority:
            raise TargetError(
                f"{what} {url!r}: {keyword} in the host could become any host "
                "(the wordlist is not read), so only the path can be fuzzed"
            )
    match = _AUTHORITY.match(authority)
    if not match:
        raise TargetError(f"{what} {url!r}: its host cannot be read unambiguously")
    if need_path and not rest[len(authority) :].startswith("/"):
        raise TargetError(
            f"{what} {url!r}: words are appended to it, so it needs a path "
            "(.../) to keep them out of the host"
        )
    targets.add(match["host"], what)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _proxy(spec: str, targets: Targets) -> None:
    """A proxy: [scheme://][user@]host[:port][/]. One on this machine (an
    intercepting proxy such as Burp) passes requests on to their own
    host, which is judged as the target; any other is a target itself."""
    rest = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", spec).rstrip("/")
    match = _AUTHORITY.match(rest)
    if not match:
        raise TargetError(f"proxy {spec!r} cannot be read")
    if not _loopback(match["host"]):
        targets.add(match["host"], "proxy")


def _no_local_config(tool: str, *names: str) -> None:
    for name in names:
        if os.path.lexists(name):
            raise TargetError(
                f"{tool} reads ./{name}, which could add targets or a proxy codebud does not see"
            )


def _need_targets(tool: str, targets: Targets) -> Targets:
    if not targets.hosts and not targets.networks:
        raise TargetError(f"{tool} names no target")
    return targets


# curl: no -L (redirects), no -K (a config file), URL globbing only with -g.
_CURL_VALUE = {
    "-X", "--request", "-H", "--header", "-d", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "--json", "-F", "--form", "-o", "--output", "-A", "--user-agent",
    "-e", "--referer", "-b", "--cookie", "-c", "--cookie-jar", "-u", "--user", "-m",
    "--max-time", "--connect-timeout", "-w", "--write-out", "-T", "--upload-file", "-r",
    "--range", "--retry", "--retry-delay", "--retry-max-time", "--limit-rate", "--cacert",
    "--capath", "-E", "--cert", "--key", "--cert-type", "--key-type", "--ciphers",
    "--tls-max", "--tls13-ciphers", "--url", "-x", "--proxy", "-U", "--proxy-user",
    "--preproxy", "--resolve", "--connect-to", "--dns-servers", "--doh-url", "--interface",
    "-Y", "--speed-limit", "-y", "--speed-time", "--output-dir", "-D", "--dump-header",
    "--trace", "--trace-ascii", "--stderr", "--max-filesize",
    "--oauth2-bearer", "--aws-sigv4", "-z", "--time-cond", "-K", "--config",
    "--unix-socket", "--abstract-unix-socket",
}  # fmt: skip
_CURL_FLAGS = {
    "-s", "--silent", "-S", "--show-error", "-v", "--verbose", "-i", "--include", "-I",
    "--head", "-k", "--insecure", "-f", "--fail", "--fail-with-body", "-g", "--globoff",
    "-G", "--get", "-4", "--ipv4", "-6", "--ipv6", "--compressed", "-O", "--remote-name",
    "-J", "--remote-header-name", "-q", "--disable", "-:", "--next", "-#",
    "--progress-bar", "-N", "--no-buffer", "--http1.0", "--http1.1", "--http2", "--http3",
    "--tlsv1.2", "--tlsv1.3", "--ssl-reqd", "--path-as-is", "--raw", "--tcp-nodelay",
    "--no-keepalive", "--create-dirs", "-n", "--netrc", "--anyauth", "--basic", "--digest",
    "--ntlm", "--negotiate", "-l", "--list-only", "-h", "--help", "-V", "--version",
    "-L", "--location", "--location-trusted", "--proxy-insecure", "--noproxy-all",
    "--fail-early", "--no-progress-meter", "--http2-prior-knowledge",
}  # fmt: skip
_CURL_PATTERNS = (r"^-X[A-Z]+$",)
_CONNECT_TO = re.compile(r"^(\[[^\]]*\]|[^:]*):([^:]*):(\[[^\]]*\]|[^:]*):(.*)$")


def _curl_sends_a_file(option, word) -> bool:
    """Whether ``option word`` makes curl send the contents of a file."""
    if option in ("-T", "--upload-file"):
        return True
    if option in ("-d", "--data", "--data-binary", "--json", "-H", "--header"):
        return word.startswith("@")
    if option == "--data-urlencode":  # @file or name@file
        return bool(re.match(r"^[^=]*@", word))
    if option in ("-F", "--form"):  # name=@file or name=<file
        return bool(re.search(r"=[@<]", word))
    return False


def _curl(args):
    targets = Targets(ACTIVE)
    urls, globoff = [], False
    for option, word in _walk(args, _CURL_VALUE, _CURL_FLAGS, _CURL_PATTERNS, short_clusters=True):
        if option in ("-L", "--location", "--location-trusted"):
            raise TargetError("curl -L follows redirects to hosts the server chooses")
        if option in ("-K", "--config"):
            raise TargetError("curl -K reads options, URLs included, from a file")
        if option in ("--unix-socket", "--abstract-unix-socket"):
            raise TargetError(f"curl {option} talks to a local service, not a target")
        if _curl_sends_a_file(option, word):
            raise TargetError(
                f"curl {option} {word}: sends a file to the target; files of the project "
                "(evidence, other engagements) do not leave for a target"
            )
        if option in ("-g", "--globoff"):
            globoff = True
        elif option in ("-x", "--proxy", "--preproxy"):
            _proxy(word, targets)
        elif option == "--doh-url":
            _url(word, targets, "DoH server", default_scheme=None)
        elif option == "--dns-servers":
            for server in word.split(","):
                targets.add(_host_port(server, "DNS server"), "DNS server")
        elif option == "--resolve":
            # [+]host:port:addr[,addr]... ; -host:port removes an entry.
            if not word.startswith("-"):
                parts = word.lstrip("+").split(":", 2)
                if len(parts) != 3:
                    raise TargetError(f"curl --resolve {word!r} cannot be read")
                for addr in parts[2].split(","):
                    targets.add(addr, "--resolve address")
        elif option == "--connect-to":
            match = _CONNECT_TO.match(word)
            if not match:
                raise TargetError(f"curl --connect-to {word!r} cannot be read")
            if match[3]:
                targets.add(match[3], "--connect-to host")
        elif option == "--url" or option is None:
            urls.append(word)
    for url in urls:
        if not globoff and any(c in url for c in "[]{}"):
            raise TargetError(f"curl {url!r}: URL globbing names hosts codebud cannot list; use -g")
        _url(url, targets, schemes=("http", "https", "ftp", "ftps"))
    return _need_targets("curl", targets)


# wget: redirects off (it follows them by default), no wgetrc commands or
# other config, no input files, no spanning hosts.
_WGET_VALUE = {
    "-O", "--output-document", "-o", "--output-file", "-a", "--append-output", "-P",
    "--directory-prefix", "-U", "--user-agent", "--header", "--method", "--body-data",
    "--body-file", "--post-data", "--post-file", "-T", "--timeout", "-t", "--tries", "-w",
    "--wait", "--user", "--password", "--http-user", "--http-password", "-l", "--level",
    "-A", "--accept", "-R", "--reject", "--load-cookies", "--save-cookies",
    "--ca-certificate", "--certificate", "--private-key", "--max-redirect", "-e",
    "--execute", "-i", "--input-file", "--config", "-B", "--base", "-D", "--domains",
    "--limit-rate", "-Q", "--quota", "--referer", "--secure-protocol",
}  # fmt: skip
_WGET_FLAGS = {
    "-q", "--quiet", "-v", "--verbose", "-nv", "--no-verbose", "-S", "--server-response",
    "-c", "--continue", "-N", "--timestamping", "-nc", "--no-clobber", "-r", "--recursive",
    "-np", "--no-parent", "-k", "--convert-links", "-p", "--page-requisites", "-m",
    "--mirror", "-nd", "--no-directories", "-x", "--force-directories", "--spider",
    "--no-check-certificate", "--no-config", "--no-proxy", "-4", "--inet4-only", "-6",
    "--inet6-only", "--content-disposition", "--no-cookies", "--keep-session-cookies",
    "-H", "--span-hosts", "-h", "--help", "-V", "--version", "-d", "--debug",
}  # fmt: skip


def _wget(args):
    targets = Targets(ACTIVE)
    redirects = None
    for option, word in _walk(args, _WGET_VALUE, _WGET_FLAGS, short_clusters=True):
        if option in ("-e", "--execute") and word.replace(" ", "") in ("robots=off", "robots=on"):
            continue
        if option in ("-e", "--execute", "--config"):
            raise TargetError(
                f"wget {option} sets options, a proxy included, codebud does not read"
            )
        if option in ("-i", "--input-file", "-B", "--base"):
            raise TargetError(f"wget {option} takes URLs from a file")
        if option in ("--post-file", "--body-file"):
            raise TargetError(
                f"wget {option}: sends a file to the target; files of the project "
                "(evidence, other engagements) do not leave for a target"
            )
        if option in ("-H", "--span-hosts", "-D", "--domains"):
            raise TargetError(f"wget {option} lets it go to other hosts")
        if option == "--max-redirect":
            redirects = word
        elif option is None:
            _url(word, targets, schemes=("http", "https", "ftp", "ftps"))
    if redirects != "0":
        raise TargetError(
            "wget follows redirects to hosts the server chooses; give --max-redirect=0"
        )
    return _need_targets("wget", targets)


# whatweb: operands are URLs, hosts, addresses, ranges -- or a file of them.
_WHATWEB_VALUE = {
    "-a", "--aggression", "-U", "--user-agent", "-H", "--header", "--proxy",
    "--proxy-user", "--follow-redirect", "--max-redirects", "-u", "--user", "-c",
    "--cookie", "--log-brief", "--log-verbose", "--log-json", "--log-xml", "--log-sql",
    "--open-timeout", "--read-timeout", "-t", "--max-threads", "--wait", "-p", "--plugins",
}  # fmt: skip
_WHATWEB_FLAGS = {
    "-v", "--verbose", "-q", "--quiet", "--no-errors", "--color=never", "--colour=never",
    "-h", "--help", "--version",
}  # fmt: skip


def _whatweb(args):
    targets = Targets(ACTIVE)
    redirects = None
    for option, word in _walk(args, _WHATWEB_VALUE, _WHATWEB_FLAGS):
        if option in ("-p", "--plugins") and ("/" in word or word.startswith(".")):
            raise TargetError("whatweb --plugins from a path runs code codebud does not read")
        if option == "--follow-redirect":
            redirects = word
        elif option == "--proxy":
            _proxy(word, targets)
        elif option is None:
            if "://" in word:
                _url(word, targets)
            elif os.path.lexists(word):
                raise TargetError(f"whatweb {word}: reads targets from a file")
            else:
                targets.add(_host_port(word, "target"))
    if redirects != "never":
        raise TargetError(
            "whatweb follows redirects to hosts the server chooses; give --follow-redirect=never"
        )
    return _need_targets("whatweb", targets)


# nikto: Perl's option parser takes -opt and --opt alike.
_NIKTO_VALUE = {
    "h", "host", "p", "port", "Tuning", "Display", "o", "output", "Format", "maxtime",
    "timeout", "Pause", "root", "vhost", "id", "Plugins", "useragent", "mutate", "evasion",
    "useproxy", "config",
}  # fmt: skip
_NIKTO_FLAGS = {"ssl", "nossl", "no404", "nointeractive", "ask=no", "noslash", "ipv4", "ipv6",
                "Help", "Version", "followredirects", "nolookup", "list-plugins"}  # fmt: skip


def _nikto(args):
    targets = Targets(ACTIVE)
    value = {d + o for o in _NIKTO_VALUE for d in ("-", "--")}
    flags = {d + o for o in _NIKTO_FLAGS for d in ("-", "--")}
    _no_local_config("nikto", "nikto.conf")
    for option, word in _walk(args, value, flags):
        name = (option or "").lstrip("-")
        if name == "followredirects":
            raise TargetError("nikto -followredirects goes to hosts the server chooses")
        if name == "config":
            raise TargetError("nikto -config reads settings, a proxy included, from a file")
        if name == "useproxy":
            _proxy(word, targets)
        elif name in ("h", "host"):
            if os.path.lexists(word):
                raise TargetError(f"nikto -h {word}: reads targets from a file")
            for host in word.split(","):
                if "://" in host:
                    _url(host, targets)
                else:
                    targets.add(_host_port(host, "target"))
        elif option is None:
            raise TargetError(f"nikto {word}: targets are given with -h")
    return _need_targets("nikto", targets)


# gobuster: dir and vhost send requests to -u; dns looks names up (passive).
# fuzz (FUZZ anywhere), s3 and gcs (third-party clouds) and tftp are refused.
# Letters mean different things per mode (-r, -d, -c), so each has its own.
_GOBUSTER_COMMON_VALUE = {"-w", "--wordlist", "-t", "--threads", "-o", "--output", "--delay",
                          "--timeout"}  # fmt: skip
_GOBUSTER_COMMON_FLAGS = {"-q", "--quiet", "-z", "--no-progress", "--no-error", "-v",
                          "--verbose", "-h", "--help"}  # fmt: skip
_GOBUSTER_MODES = {
    "dir": (
        {"-u", "--url", "-x", "--extensions", "-s", "--status-codes", "-b",
         "--status-codes-blacklist", "-c", "--cookies", "-H", "--headers", "-a",
         "--useragent", "-U", "--username", "-P", "--password", "--proxy", "-m", "--method",
         "--exclude-length", "--client-cert-pem", "--client-cert-pem-key"},
        {"-k", "--no-tls-validation", "-e", "--expanded", "-n", "--no-status", "-f",
         "--add-slash", "-d", "--discover-backup", "--hide-length", "--random-agent",
         "-r", "--follow-redirect"},
    ),
    "vhost": (
        {"-u", "--url", "-c", "--cookies", "-H", "--headers", "-a", "--useragent", "-U",
         "--username", "-P", "--password", "--proxy", "-m", "--method", "--domain",
         "--exclude-length", "--client-cert-pem", "--client-cert-pem-key"},
        {"-k", "--no-tls-validation", "--append-domain", "--random-agent", "-r",
         "--follow-redirect"},
    ),
    "dns": (
        {"-d", "--domain", "-r", "--resolver"},
        {"-c", "--show-cname", "-i", "--show-ips", "--wildcard"},
    ),
}  # fmt: skip


def _gobuster(args):
    if not args or args[0] not in _GOBUSTER_MODES:
        mode = args[0] if args else "(none)"
        raise TargetError(f"gobuster mode {mode}: only dir, vhost and dns can be judged")
    mode, args = args[0], args[1:]
    targets = Targets(PASSIVE if mode == "dns" else ACTIVE)
    value, flags = _GOBUSTER_MODES[mode]
    for option, word in _walk(args, value | _GOBUSTER_COMMON_VALUE, flags | _GOBUSTER_COMMON_FLAGS):
        if mode != "dns" and option in ("-r", "--follow-redirect"):
            raise TargetError("gobuster -r follows redirects to hosts the server chooses")
        if mode != "dns" and option in ("-u", "--url"):
            _url(word, targets, default_scheme=None, need_path=mode == "dir")
        elif mode == "dns" and option in ("-d", "--domain"):
            targets.add(word, "domain")
        elif mode == "dns" and option in ("-r", "--resolver"):
            targets.add(_host_port(word, "DNS server"), "DNS server")
        elif option == "--proxy":
            _proxy(word, targets)
        elif option is None:
            raise TargetError(f"gobuster {word}: targets are given with -u or -d")
    return _need_targets("gobuster", targets)


# ffuf: Go flags (-opt or --opt). FUZZ and the -w file:KEYWORD keywords may
# be in the path, query and headers, never in the scheme or host.
_FFUF_VALUE = {
    "u", "w", "H", "X", "d", "b", "x", "replay-proxy", "t", "p", "rate", "maxtime",
    "maxtime-job", "timeout", "e", "mc", "ml", "mr", "ms", "mt", "mw", "fc", "fl", "fr",
    "fs", "ft", "fw", "o", "of", "od", "mode", "recursion-depth", "recursion-strategy",
    "acc", "sni", "debug-log", "input-num", "cc", "ck", "request", "request-proto",
    "input-cmd", "config",
}  # fmt: skip
_FFUF_FLAGS = {"ac", "c", "v", "s", "sa", "se", "sf", "ic", "k", "recursion", "or", "noninteractive",
               "json", "r", "h", "V", "http2", "raw", "ignore-body", "ach", "D"}  # fmt: skip


def _ffuf(args):
    targets = Targets(ACTIVE)
    # Go's flag package takes "-opt", "--opt" and "-opt=value" alike.
    value = {"-" + o for o in _FFUF_VALUE}
    flags = {"-" + o for o in _FFUF_FLAGS}
    words = [a[1:] if a.startswith("--") and len(a) > 2 else a for a in args]
    keywords, urls = {"FUZZ"}, []
    for option, word in _walk(words, value, flags):
        if option == "-input-cmd":
            raise TargetError("ffuf -input-cmd runs a command to make its inputs")
        if option in ("-request", "-request-proto", "-config"):
            raise TargetError(f"ffuf {option} reads the request or settings from a file")
        if option == "-r":
            raise TargetError("ffuf -r follows redirects to hosts the server chooses")
        if option == "-w":
            _, sep, keyword = word.rpartition(":")
            if sep and keyword and "/" not in keyword:
                keywords.add(keyword)
        elif option in ("-x", "-replay-proxy"):
            _proxy(word, targets)
        elif option == "-u":
            urls.append(word)
        elif option is None:
            raise TargetError(f"ffuf {word}: the target is given with -u")
    for url in urls:
        _url(url, targets, keywords=sorted(keywords), default_scheme=None)
    return _need_targets("ffuf", targets)


# feroxbuster: joins each word to the URL, and a word like //evil.example
# or https:evil.example would leave the host, so the wordlist is read.
_FEROX_VALUE = {
    "-u", "--url", "-w", "--wordlist", "-p", "--proxy", "-P", "--replay-proxy", "-t",
    "--threads", "-d", "--depth", "-x", "--extensions", "-H", "--headers", "-b",
    "--cookies", "-m", "--methods", "--data", "-a", "--user-agent", "-o", "--output",
    "-s", "--status-codes", "-C", "--filter-status", "-S", "--filter-size", "-W",
    "--filter-words", "-N", "--filter-lines", "-X", "--filter-regex", "-T", "--timeout",
    "--time-limit", "--rate-limit", "-L", "--scan-limit", "--dont-scan", "-R",
    "--replay-codes", "--resume-from", "--debug-log",
}  # fmt: skip
_FEROX_FLAGS = {
    "-k", "--insecure", "-n", "--no-recursion", "-q", "--quiet", "--silent", "-v",
    "--verbosity", "-e", "--extract-links", "--auto-tune", "--auto-bail", "-f",
    "--add-slash", "-r", "--redirects", "--stdin", "--burp", "--burp-replay", "--json",
    "--force-recursion", "--dont-extract-links", "-h", "--help", "-V", "--version",
    "--no-state",
}  # fmt: skip
# After the URL standard's clean-up (WHATWG URL: leading and trailing C0
# controls and spaces stripped, tabs and newlines removed anywhere), a
# word starting // or with a backslash in their place, or with a special
# scheme that reaches a host, would replace the host when joined.
_JOIN_ESCAPE = re.compile(r"^([/\\]{2}|(https?|ftp|wss?):)", re.IGNORECASE)
_C0_AND_SPACE = "".join(map(chr, range(0x21)))


def _wordlist_problem(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f, 1):
                word = re.sub(r"[\t\n\r]", "", line).strip(_C0_AND_SPACE)
                if _JOIN_ESCAPE.match(word):
                    return f"line {number} ({line.strip()!r}) would leave the target host"
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    return None


def _feroxbuster(args):
    targets = Targets(ACTIVE)
    _no_local_config("feroxbuster", "ferox-config.toml")
    wordlist = None
    for option, word in _walk(args, _FEROX_VALUE, _FEROX_FLAGS, (r"^-v+$",)):
        if option in ("-r", "--redirects"):
            raise TargetError("feroxbuster -r follows redirects to hosts the server chooses")
        if option in ("--stdin", "--resume-from"):
            raise TargetError(f"feroxbuster {option} takes targets from elsewhere")
        if option in ("-w", "--wordlist"):
            wordlist = word
        elif option in ("-p", "--proxy", "-P", "--replay-proxy"):
            _proxy(word, targets)
        elif option in ("-u", "--url"):
            _url(word, targets, default_scheme=None)
        elif option is None:
            raise TargetError(f"feroxbuster {word}: targets are given with -u")
    if wordlist is None:
        raise TargetError("feroxbuster needs -w: its wordlist is checked before it runs")
    problem = _wordlist_problem(wordlist)
    if problem:
        raise TargetError(f"feroxbuster wordlist {wordlist}: {problem}")
    return _need_targets("feroxbuster", targets)


# sslscan: values only as --opt=value (sslscan does not take the next
# word); one host[:port], or a list in --targets=file.
_SSLSCAN_PATTERNS = (
    r"^--(ipv4|ipv6|show-certificate|show-certs|show-client-cas|show-ciphers|show-cipher-ids"
    r"|show-times|show-sigs|no-ciphersuites|no-compression|no-fallback|no-groups|no-heartbleed"
    r"|no-renegotiation|no-check-certificate|no-cipher-details|no-colour|no-color|ocsp|tlsall"
    r"|ssl2|ssl3|tls10|tls11|tls12|tls13|bugs|verbose|version|help|iana-names|rdp|xmpp-server"
    r"|starttls-(ftp|imap|irc|ldap|mysql|pop3|psql|smtp|xmpp))$",
    r"^--(targets|sni-name|xml|timeout|connect-timeout|sleep|certs|pk|pkpass)=.+$",
    r"^-[46]$",
)


def _sslscan(args):
    targets = Targets(ACTIVE)
    hosts = []
    for option, word in _walk(args, set(), set(), _SSLSCAN_PATTERNS):
        if option and option.startswith("--targets="):
            _list_file(option.partition("=")[2], targets, lambda w: _host_port(w, "target"))
        elif option is None:
            hosts.append(word)
    if len(hosts) > 1:
        raise TargetError("sslscan takes one host (or a list with --targets=file)")
    for host in hosts:
        targets.add(_host_port(host, "target"))
    return _need_targets("sslscan", targets)


# testssl.sh: one URI (host[:port] or https://...); mass testing and --mx
# (which tests the hosts a DNS answer names) are refused.
_TESTSSL_VALUE = {
    "-t", "--starttls", "--xmpphost", "--ip", "--proxy", "--sneaky", "--jsonfile",
    "--jsonfile-pretty", "--csvfile", "--htmlfile", "--logfile", "--outfile", "--severity",
    "--connect-timeout", "--openssl-timeout", "--basicauth", "--reqheader", "--color",
    "--add-ca", "--mapping", "--file", "-iL", "--input-file", "--mx",
    "--openssl", "--warnings",
}  # fmt: skip
_TESTSSL_FLAGS = {
    "-p", "--protocols", "-e", "--each-cipher", "-E", "--cipher-per-proto", "-s",
    "--std", "--categories", "-f", "--fs", "--forward-secrecy", "-S", "--server-defaults",
    "-P", "--server-preference", "-h", "--header", "--headers", "-U", "--vulnerable",
    "-c", "--client-simulation", "-g", "--grease", "-q", "--quiet", "--wide", "--fast",
    "--ids-friendly", "--hints", "-4", "-6", "--nodns", "--ssl-native", "--sneaky",
    "-b", "--banner", "-v", "--version", "--help", "-9", "--full", "-H", "--heartbleed",
    "-I", "--ccs", "-T", "--ticketbleed", "-R", "--renegotiation", "-C", "--compression",
    "-B", "--breach", "-O", "--poodle", "-Z", "--tls-fallback", "-W", "--sweet32",
    "-A", "--beast", "-L", "--lucky13", "-F", "--freak", "-J", "--logjam", "-D", "--drown",
    "--rc4", "--appelbaum", "--phone-out",
}  # fmt: skip


def _testssl(args):
    targets = Targets(ACTIVE)
    operands = []
    for option, word in _walk(args, _TESTSSL_VALUE - {"--sneaky"}, _TESTSSL_FLAGS):
        if option in ("--file", "-iL", "--input-file"):
            raise TargetError("testssl.sh mass testing reads targets and options from a file")
        if option == "--mx":
            raise TargetError("testssl.sh --mx tests the hosts a DNS answer names")
        if option == "--phone-out":
            raise TargetError(
                "testssl.sh --phone-out contacts CRL and OCSP servers of third parties"
            )
        if option == "--openssl":
            raise TargetError("testssl.sh --openssl runs a program codebud has not checked")
        if option == "--proxy":
            if word != "auto":
                _proxy(word, targets)
            else:
                raise TargetError("testssl.sh --proxy auto takes a proxy from the environment")
        elif option == "--ip":
            if word not in ("one", "proxy"):
                targets.add(word, "--ip address")
        elif option is None:
            operands.append(word)
    if len(operands) != 1:
        raise TargetError("testssl.sh takes one target")
    if "://" in operands[0]:
        _url(operands[0], targets)
    else:
        targets.add(_host_port(operands[0], "target"))
    return targets


# whois (GNU/Linux whois): one query to a registry's server -- passive,
# like a DNS lookup; a server given with -h is judged as a target.
_WHOIS_VALUE = {"-h", "--host", "-p", "--port", "-i", "-T", "-s", "-g", "-q", "-t", "-v"}
_WHOIS_FLAGS = {"-H", "-I", "--verbose", "--no-recursion", "-l", "-L", "-m", "-M", "-c", "-x",
                "-b", "-B", "-G", "-d", "-r", "-R", "-a", "-K", "--help", "--version"}  # fmt: skip


def _whois(args):
    targets = Targets(PASSIVE)
    queries = []
    for option, word in _walk(args, _WHOIS_VALUE, _WHOIS_FLAGS):
        if option in ("-h", "--host"):
            targets.add(_host_port(word, "whois server"), "whois server")
        elif option is None:
            queries.append(word)
    if len(queries) != 1:
        raise TargetError("whois takes one query")
    query = queries[0]
    if not re.fullmatch(r"(?i)AS\d+", query):  # an AS number names no host
        targets.add(query, "lookup")
    return targets


PARSERS = {
    "dig": _dig, "host": _host, "nmap": _nmap, "masscan": _masscan,
    "nc": _nc, "netcat": _nc, "ncat": _ncat,
    "curl": _curl, "wget": _wget, "whatweb": _whatweb, "nikto": _nikto, "nikto.pl": _nikto,
    "gobuster": _gobuster, "ffuf": _ffuf, "feroxbuster": _feroxbuster,
    "sslscan": _sslscan, "testssl.sh": _testssl, "testssl": _testssl, "whois": _whois,
}  # fmt: skip


# An environment assignment (env NAME=value tool ...) can give a tool a
# proxy (HTTPS_PROXY, ALL_PROXY) or a config it reads (WGETRC, CURL_HOME,
# HOME, XDG_CONFIG_HOME) that its words do not show; only these are fine.
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_HARMLESS_ENV = re.compile(r"^(LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|NO_COLOR|COLUMNS)$")


def targets_of(words: list[str]) -> list[tuple[str, Targets]]:
    """(program, Targets) for every word of the command line that names a
    tool with a parser -- behind wrappers too. TargetError if any of them
    cannot be read, or an environment assignment before one could change
    where it connects."""
    found = []
    for i, word in enumerate(words):
        name = os.path.basename(word)
        name = "nc" if name.startswith("nc.") else name
        if name in PARSERS:
            for before in words[:i]:
                match = _ASSIGNMENT.match(before)
                if match and not _HARMLESS_ENV.match(match[1]):
                    raise TargetError(
                        f"{before.split('=', 1)[0]}= before {name} could set a proxy or a "
                        "config file codebud does not read"
                    )
            found.append((name, PARSERS[name](words[i + 1 :])))
    return found


# --- the guard -------------------------------------------------------------------


class ScopeGuard:
    """The scope check for one engagement, to register with the core's
    safety engine (activate) and remove again (deactivate)."""

    def __init__(self, engagement):
        self.engagement = engagement

    def _problems(self, targets: Targets) -> list[str]:
        e = self.engagement
        problems = [f"{h} is not in the engagement scope" for h in targets.hosts if not e.covers(h)]
        problems += [
            f"{n} is not wholly in the engagement scope"
            for n in targets.networks
            if not e.covers_network(n)
        ]
        if not e.in_window():
            problems.append("the engagement window is not open now")
        return problems

    def _judge(self, step) -> tuple[str | None, str | None]:
        """(refusal, notice) for ``step``."""
        if step.get("tool") == "web_search":
            return (
                "engagement scope: web search sends the query to a third party "
                "(DuckDuckGo), outside any engagement",
                None,
            )
        if step.get("tool") != "command":
            return None, None  # file tools and host_network stay on this machine
        from codebud import isolation, privacy

        # A step without the network can reach no one -- only if steps really
        # are isolated; otherwise an undeclared step has the network too.
        if privacy.level() == "trusted" or isolation.available() is None:
            return (
                "an engagement needs isolated steps (bubblewrap or user namespaces, "
                "at privacy private or locked); without them any step has the network",
                None,
            )
        args = step.get("args", {})
        if not args.get("network"):
            return None, None  # isolated without the network: contacts no one
        try:
            words = shlex.split(args.get("cmd", ""))
            found = targets_of(words)
        except (ValueError, TargetError) as exc:
            return f"engagement scope: {exc}", None
        if not found:
            return (
                f"engagement scope: {words[0] if words else 'this step'} needs the network, "
                "but codebud cannot read its targets",
                None,
            )
        name = self.engagement.path.name
        for program, targets in found:
            problems = self._problems(targets)
            if not problems:
                continue
            reason = f"{program}: {'; '.join(problems)} ({name})"
            if targets.tier == ACTIVE:
                return f"engagement scope: {reason}", None
            return None, f"Outside the engagement: {reason}"
        return None, None

    def refusal(self, step):
        return self._judge(step)[0]

    def notice(self, step):
        return self._judge(step)[1]

    def activate(self) -> None:
        from codebud import safety

        safety.add_step_check(self.refusal, provides="engagement")
        safety.add_step_notice(self.notice)

    def deactivate(self) -> None:
        from codebud import safety

        safety.remove_step_check(self.refusal)
        safety.remove_step_notice(self.notice)
