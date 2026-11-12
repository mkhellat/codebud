"""
codebud_sec/scope.py

Keeping network steps inside the engagement (phase 10, step 5 plan,
PR 4; decision D14i). Every target a network tool will contact is read
from its command line and checked against the engagement file:

- active tools (scanners, nc) are refused when a target is outside the
  scope, excluded, or the engagement window is closed -- a hard block,
  because a model's plan is untrusted input and a warning is clicked
  through (UK AISI, 4 Aug 2026; Akhawe & Felt 2013);
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

A step that declares the network with a tool that has no parser here is
refused. The guard is registered with the core's safety engine
(codebud.safety.add_step_check / add_step_notice) by whoever opens the
engagement; the core never imports this layer.
"""

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


def _list_file(path: str, targets: Targets) -> None:
    """Targets read from a file: only a file inside the project."""
    real = os.path.realpath(path)
    project = os.path.realpath(os.getcwd())
    if os.path.commonpath([real, project]) != project or not os.path.isfile(real):
        raise TargetError(f"target list {path}: only a file inside the project can be read")
    with open(real, encoding="utf-8", errors="replace") as f:
        for line in f:
            for spec in line.split("#", 1)[0].split():
                targets.add(spec, f"target in {path}")


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


def _nmap(args):
    targets = Targets(ACTIVE)
    for option, word in _walk(args, _NMAP_VALUE, _NMAP_FLAGS, _NMAP_PATTERNS):
        if option == "-iR":
            raise TargetError("nmap -iR scans random hosts, outside any scope")
        if option in ("--resume", "--script-args-file"):
            raise TargetError(f"nmap {option} reads from a file that cannot be judged")
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


def _masscan(args):
    targets = Targets(ACTIVE)
    for option, word in _walk(args, _MASSCAN_VALUE, _MASSCAN_FLAGS, (r"^-p[0-9U:,\-]+$",)):
        if option in ("-c", "--conf", "--resume"):
            raise TargetError(f"masscan {option} reads settings, targets included, from a file")
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


PARSERS = {
    "dig": _dig, "host": _host, "nmap": _nmap, "masscan": _masscan,
    "nc": _nc, "netcat": _nc, "ncat": _ncat,
}  # fmt: skip


def targets_of(words: list[str]) -> list[tuple[str, Targets]]:
    """(program, Targets) for every word of the command line that names a
    tool with a parser -- behind wrappers too. TargetError if any of them
    cannot be read."""
    found = []
    for i, word in enumerate(words):
        name = os.path.basename(word)
        name = "nc" if name.startswith("nc.") else name
        if name in PARSERS:
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
        if step.get("tool") != "command":
            return None, None
        from codebud import privacy

        if privacy.level() == "trusted":
            return (
                "an engagement needs isolated steps: privacy level trusted runs them "
                "unisolated, with the whole network",
                None,
            )
        args = step.get("args", {})
        try:
            words = shlex.split(args.get("cmd", ""))
            found = targets_of(words)
        except (ValueError, TargetError) as exc:
            return f"engagement scope: {exc}", None
        if not found:
            if args.get("network"):
                return (
                    f"engagement scope: {words[0] if words else 'this step'} needs the network, "
                    "but codebud cannot read its targets",
                    None,
                )
            return None, None
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

        safety.add_step_check(self.refusal)
        safety.add_step_notice(self.notice)

    def deactivate(self) -> None:
        from codebud import safety

        safety.remove_step_check(self.refusal)
        safety.remove_step_notice(self.notice)
