"""
tests/test_host_network.py

The host_network tool (phase 10, step 5 plan, PR 2): the host's socket,
route and ARP tables read from /proc/1/net -- the volatile network state
RFC 3227 puts first -- without network access and without a sandbox
that would hide them (ss and ip only see their own namespace). It is
offered only under a profile with observer isolation. Here /proc is a
fake tree.
"""

import pytest

from codebud import profiles
from codebud.tools.host_network import HostNetworkTool
from codebud.tools.tool_registry import ToolRegistry

TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 18237 1 0
   1: 0501A8C0:0016 0900000A:C350 01 00000000:00000000 02:00000A8B 00000000  1000        0 99001 2 0
"""
TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:0050 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4242 1 0
"""
UDP = """\
   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  100: 00000000:0044 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 555 2 0 0
"""
UNIX = """\
Num       RefCount Protocol Flags    Type St Inode Path
0000000000000000: 00000002 00000000 00010000 0001 01 777 /run/systemd/notify
0000000000000000: 00000003 00000000 00000000 0001 03 778
"""
ROUTE = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""
ARP = """\
IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0
"""


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    net = root / "1" / "net"
    net.mkdir(parents=True)
    for name, text in (("tcp", TCP), ("tcp6", TCP6), ("udp", UDP), ("udp6", UDP.splitlines()[0] + "\n"),
                       ("unix", UNIX), ("route", ROUTE), ("arp", ARP)):  # fmt: skip
        (net / name).write_text(text)
    sshd = root / "123"
    (sshd / "fd").mkdir(parents=True)
    (sshd / "comm").write_text("sshd\n")
    (sshd / "fd" / "3").symlink_to("socket:[99001]")
    return root


def _run(proc, **args):
    return HostNetworkTool(proc_root=proc).run(args)


def test_tcp_sockets_are_decoded(proc):
    out = _run(proc, table="tcp")
    assert out["returncode"] == 0, out["stderr"]
    text = out["stdout"]
    assert "127.0.0.1:631" in text and "LISTEN" in text
    assert "192.168.1.5:22" in text and "10.0.0.9:50000" in text and "ESTABLISHED" in text
    assert "[::1]:80" in text


def test_sockets_are_matched_to_their_process(proc):
    text = _run(proc, table="tcp")["stdout"]
    line = next(line for line in text.splitlines() if "192.168.1.5:22" in line)
    assert "123/sshd" in line


def test_udp_unix_route_and_arp(proc):
    text = _run(proc, table="all")["stdout"]
    assert "0.0.0.0:68" in text  # udp
    assert "/run/systemd/notify" in text  # unix
    assert "default via 192.168.1.1 dev eth0" in text  # route
    assert "192.168.1.0/24 dev eth0" in text
    assert "192.168.1.1" in text and "aa:bb:cc:dd:ee:ff" in text  # arp


def test_an_unknown_table_is_an_error(proc):
    out = _run(proc, table="firewall")
    assert out["returncode"] == 1
    assert "tcp" in out["stderr"]


def test_a_missing_table_is_reported_not_fatal(proc):
    (proc / "1" / "net" / "arp").unlink()
    out = _run(proc, table="all")
    assert out["returncode"] == 0
    assert "arp" in out["stdout"] and "not readable" in out["stdout"]


def test_the_real_host_tables_can_be_read():
    out = HostNetworkTool().run({"table": "route"})
    assert out["returncode"] == 0, out["stderr"]


# --- offered only under observer isolation --------------------------------


def test_hidden_from_the_planner_by_default():
    registry = ToolRegistry(sandbox=None)
    assert not registry.has("host_network")
    assert registry.get("host_network") is None
    assert "host_network" not in registry.names()
    assert "host_network" not in registry.describe_tools()


def test_offered_under_an_observer_profile(monkeypatch):
    monkeypatch.setattr(profiles, "_active", profiles.Profile("triage", isolation="observer"))
    registry = ToolRegistry(sandbox=None)
    assert registry.has("host_network")
    assert "host_network" in registry.names()
    assert "host_network" in registry.describe_tools()
