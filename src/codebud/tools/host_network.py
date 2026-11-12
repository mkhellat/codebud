"""
codebud/tools/host_network.py

The host_network tool: the host's socket, route and ARP tables, read
from /proc/1/net -- the network state RFC 3227 (section 2.1) and NIST SP
800-86 (section 5.2.1.3) put first in live response.

ss and ip report the network namespace they run in, so inside a
sandboxed step they show a loopback interface and nothing on it. This
tool runs inside codebud, like file_read: it only reads /proc, never
opens a socket, and needs no network. Sockets are matched to the process
holding them where /proc/<pid>/fd can be read (your own processes; all
of them as root).

It is offered only under a profile with observer isolation
(profiles.py), so the planner prompt of every other profile is
unchanged.
"""

import ipaddress
import os
import socket
import struct
from pathlib import Path
from typing import Any

TABLES = ("tcp", "udp", "unix", "route", "arp")
TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING", "0C": "NEW_SYN_RECV",
}  # fmt: skip
UDP_STATES = {"01": "ESTABLISHED", "07": "UNCONN"}
UNIX_TYPES = {"0001": "STREAM", "0002": "DGRAM", "0005": "SEQPACKET"}
UNIX_STATES = {"01": "UNCONNECTED", "02": "CONNECTING", "03": "CONNECTED", "04": "DISCONNECTING"}


def _address(hex_addr: str) -> str:
    """An IPv4 or IPv6 address as /proc/net writes it (32-bit words in
    host byte order) in its usual form."""
    raw = bytes.fromhex(hex_addr)
    words = struct.unpack(f"={len(raw) // 4}I", raw)
    packed = b"".join(struct.pack("!I", w) for w in words)
    return str(ipaddress.ip_address(packed))


def _endpoint(field: str) -> str:
    addr, port = field.split(":")
    host = _address(addr)
    host = f"[{host}]" if ":" in host else host
    return f"{host}:{int(port, 16)}"


class HostNetworkTool:
    """
    Show the host's network tables.

    Args:
    {
        "table": "tcp" | "udp" | "unix" | "route" | "arp" | "all"  (default "all")
    }
    """

    description = (
        "Show the host's network sockets (tcp, udp, unix), routes and ARP table, "
        "with the process holding each socket where known. Read-only; no network."
    )
    usage_hint = (
        'Optional: "table" -- one of tcp, udp, unix, route, arp, all (default all). '
        "Use it instead of ss, netstat or ip, which only see the sandbox."
    )

    def __init__(self, proc_root: str | Path = "/proc"):
        self.proc = Path(proc_root)

    @staticmethod
    def available() -> bool:
        from codebud import profiles

        return profiles.active().isolation == "observer"

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        table = str(args.get("table") or "all").lower()
        if table != "all" and table not in TABLES:
            return {
                "stdout": "",
                "stderr": f"unknown table '{table}': use one of {', '.join(TABLES)}, all",
                "returncode": 1,
            }
        owners = self._socket_owners()
        sections = []
        for name in TABLES if table == "all" else (table,):
            sections.append(getattr(self, f"_{name}")(owners))
        return {"stdout": "\n\n".join(sections) + "\n", "stderr": "", "returncode": 0}

    # ------------------------------------------------------------------

    def _read(self, name: str) -> list[str] | None:
        try:
            return (self.proc / "1" / "net" / name).read_text().splitlines()
        except OSError:
            return None

    def _socket_owners(self) -> dict[str, str]:
        """socket inode -> "pid/command", for the processes whose fds can be read."""
        owners = {}
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            return owners
        for pid in pids:
            fd_dir = self.proc / pid / "fd"
            try:
                comm = (self.proc / pid / "comm").read_text().strip()
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd_dir / fd)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    owners.setdefault(target[8:-1], f"{pid}/{comm}")
        return owners

    def _inet(self, kind: str, states: dict[str, str], owners: dict[str, str]) -> str:
        rows = [("PROTO", "STATE", "LOCAL", "REMOTE", "UID", "PROCESS")]
        missing = []
        for name in (kind, f"{kind}6"):
            lines = self._read(name)
            if lines is None:
                missing.append(name)
                continue
            for line in lines[1:]:
                fields = line.split()
                if len(fields) < 10:
                    continue
                rows.append((
                    name, states.get(fields[3], fields[3]), _endpoint(fields[1]),
                    _endpoint(fields[2]), fields[7], owners.get(fields[9], "-"),
                ))  # fmt: skip
        return self._section(f"{kind.upper()} sockets (/proc/1/net/{kind}, {kind}6)", rows, missing)

    def _tcp(self, owners):
        return self._inet("tcp", TCP_STATES, owners)

    def _udp(self, owners):
        return self._inet("udp", UDP_STATES, owners)

    def _unix(self, owners):
        lines = self._read("unix")
        rows = [("TYPE", "STATE", "INODE", "PATH", "PROCESS")]
        for line in (lines or [])[1:]:
            fields = line.split()
            if len(fields) < 7:
                continue
            path = fields[7] if len(fields) > 7 else ""
            rows.append((UNIX_TYPES.get(fields[4], fields[4]), UNIX_STATES.get(fields[5], fields[5]),
                         fields[6], path, owners.get(fields[6], "-")))  # fmt: skip
        return self._section("Unix sockets (/proc/1/net/unix)", rows, [] if lines else ["unix"])

    def _route(self, owners):
        lines = self._read("route")
        out = []
        for line in (lines or [])[1:]:
            fields = line.split()
            if len(fields) < 8:
                continue
            iface, dest, gateway, mask, metric = (
                fields[0],
                fields[1],
                fields[2],
                fields[7],
                fields[6],
            )
            ip = _address(dest)
            prefix = bin(int.from_bytes(socket.inet_aton(_address(mask)), "big")).count("1")
            target = "default" if ip == "0.0.0.0" and prefix == 0 else f"{ip}/{prefix}"
            via = "" if _address(gateway) == "0.0.0.0" else f" via {_address(gateway)}"
            out.append(f"{target}{via} dev {iface} metric {metric}")
        title = "IPv4 routes (/proc/1/net/route)"
        if lines is None:
            return f"{title}\n  not readable"
        return title + "\n" + ("\n".join(f"  {line}" for line in out) or "  (none)")

    def _arp(self, owners):
        lines = self._read("arp")
        rows = [("ADDRESS", "HW ADDRESS", "DEVICE")]
        for line in (lines or [])[1:]:
            fields = line.split()
            if len(fields) >= 6:
                rows.append((fields[0], fields[3], fields[5]))
        return self._section("ARP table (/proc/1/net/arp)", rows, [] if lines else ["arp"])

    @staticmethod
    def _section(title: str, rows: list[tuple], missing: list[str]) -> str:
        widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
        body = [
            "  " + "  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
            for row in rows
        ]
        if len(rows) == 1:
            body.append("  (none)")
        if missing:
            body.append(f"  not readable: {', '.join(missing)}")
        return title + "\n" + "\n".join(body)
