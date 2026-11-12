"""
codebud_sec/fence.py

The engagement's network fence (phase 10, step 5 plan, PR 6): which
addresses a network step may reach, for the core's fence (codebud.
isolation: pasta + nftables, no DNS, no capabilities). The scope guard
(scope.py) reads a step's targets from its command line; the fence makes
the network itself hold to them, so a tool that reaches further than its
command line says -- a redirect, a script, a bug, a plan that slipped
past a parser -- meets a closed door.

Per step (the operator's decision, 2026-11-12):
- passive lookups (dig, host, whois) are not fenced: they need DNS or a
  registry, and the scope guard alone judges them;
- scanning steps are fenced unless the engagement sets
  fence_scanning = false (raw scans need raw sockets, which the fence
  removes);
- a fenced step may reach exactly its targets: addresses and networks as
  written, names resolved now (outside the fence) and written into the
  step's /etc/hosts; the engagement's excluded networks are dropped
  first. The client's DNS decides what a name resolves to, and an address
  shared with other sites (a CDN) is reachable while the step runs.

Under the fence nmap must run --unprivileged (connect scans), and scans
that need raw sockets -- SYN, UDP, OS detection, masscan -- are refused
with that reason rather than left to fail.
"""

import ipaddress
import socket

from codebud import isolation

from .scope import PASSIVE, TargetError, targets_of

# nmap options that need raw sockets.
_NMAP_RAW = {"-sS", "-sU", "-sA", "-sW", "-sM", "-sN", "-sF", "-sX", "-sY", "-sZ", "-sO",
             "-sI", "-O", "-A", "--traceroute", "-f", "-ff", "--mtu", "-D", "-S", "--spoof-mac",
             "--badsum", "--send-eth", "--send-ip", "--privileged"}  # fmt: skip


def _raw_socket_problem(program: str, words: list[str]) -> str | None:
    if program == "masscan":
        return (
            "masscan needs raw sockets, which the network fence removes; "
            "set fence_scanning = false in the engagement to run it"
        )
    if program == "nmap":
        raw = sorted(set(words) & _NMAP_RAW)
        if raw:
            return (
                f"nmap {' '.join(raw)} needs raw sockets, which the network fence removes; "
                "use connect scans (-sT), or set fence_scanning = false in the engagement"
            )
        if "--unprivileged" not in words:
            return (
                "behind the network fence nmap needs --unprivileged "
                "(it has no raw sockets there and would stop)"
            )
    return None


def _resolve(name: str) -> list[str]:
    infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _network(address: str):
    return ipaddress.ip_network(address)


class ScopeFence:
    """The fence for one engagement, to register with the core
    (codebud.isolation.add_network_fence)."""

    def __init__(self, engagement):
        self.engagement = engagement

    def _fenced(self, found) -> bool:
        if not found or all(targets.tier == PASSIVE for _, targets in found):
            return False
        return self.engagement.fence_scanning

    def problem(self, words: list[str]) -> str | None:
        """Why ``words`` cannot run behind the fence (raw sockets), or None:
        for the scope guard, so a plan hears it before it runs."""
        try:
            found = targets_of(words)
        except TargetError:
            return None  # the scope guard reports it
        if not self._fenced(found):
            return None
        for program, _ in found:
            problem = _raw_socket_problem(program, words)
            if problem:
                return problem
        return None

    def decide(self, words: list[str]):
        """A Fence for ``words``, None (not fenced), or why it must not run."""
        try:
            found = targets_of(words)
        except TargetError as exc:
            return f"engagement scope: {exc}"
        if not self._fenced(found):
            return None
        problem = self.problem(words)
        if problem:
            return f"engagement scope: {problem}"
        allow, hosts = set(), []
        for _, targets in found:
            allow |= set(targets.networks)
            for host in targets.hosts:
                try:
                    allow.add(_network(host))
                    continue
                except ValueError:
                    pass  # a name
                try:
                    addresses = _resolve(host)
                except OSError as exc:
                    return f"engagement scope: cannot resolve {host} for the network fence ({exc})"
                allow |= {_network(a) for a in addresses}
                hosts += [(host, a) for a in addresses]
        deny = tuple(t.network for t in self.engagement.out_of_scope if t.network is not None)
        return isolation.Fence(allow=tuple(sorted(allow, key=str)), deny=deny, hosts=tuple(hosts))

    def activate(self) -> None:
        isolation.add_network_fence(self.decide)

    def deactivate(self) -> None:
        isolation.remove_network_fence(self.decide)
