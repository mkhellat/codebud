"""
tests/test_catastrophic.py

A parsed backstop for catastrophic, irreversible commands (security
review M5, 2026-10-29): powering the machine off, erasing a disk,
recursively deleting a vital directory. They are refused whatever the
allow-list says -- the user can widen it -- at planning time and again in
the sandbox. The old check substring-matched five literals and let
"rm -fr /", "rm  -rf /", "rm -rf /home/you" and "poweroff" through.
Steps run without a shell, so no glob, ~ or $VAR is ever expanded.
"""

import os

import pytest

from codebud.safety import SafetyEngine, catastrophic_reason
from codebud.sandbox import Sandbox


@pytest.fixture
def permissive():
    """An engine whose allow-list admits everything below."""
    engine = SafetyEngine()
    engine.harmless_commands = [
        "rm", "shutdown", "reboot", "poweroff", "halt", "systemctl", "mkfs.ext4",
        "wipefs", "dd", "sudo", "env", "timeout", "nice",
    ]  # fmt: skip
    return engine


def _refused(engine, cmd):
    return engine.refusal_reason({"id": "1", "tool": "command", "args": {"cmd": cmd}})


HOME = os.path.expanduser("~")

CATASTROPHIC = [
    # the reviewer's bypasses of the old substring check
    "rm -fr /",
    "rm  -rf /",
    "rm -rf /home/you",
    "poweroff",
    # deletion
    "rm -rf /",
    "rm -r /",
    "rm -R /usr",
    "rm --recursive /etc",
    "rm -rf /home",
    f"rm -rf {HOME}",
    "rm -rf /root",
    "rm -rf /Users/someone",
    "rm -rf //",
    "rm -rf /usr/../",
    "rm --no-preserve-root -rf /",
    "rm -fR ./build /var",
    # power
    "shutdown -h now",
    "reboot",
    "halt",
    "systemctl poweroff",
    "systemctl reboot",
    "/sbin/shutdown now",
    # disks
    "mkfs.ext4 /dev/sda1",
    "wipefs -a /dev/sdb",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    # wrappers do not hide the command
    "sudo rm -rf /",
    "sudo -n reboot",
    "env FOO=1 rm -rf /etc",
    "timeout 5 shutdown now",
    "nice -n 10 mkfs.ext4 /dev/sda1",
]


@pytest.mark.parametrize("cmd", CATASTROPHIC)
def test_catastrophic_commands_are_refused_even_when_allow_listed(permissive, cmd):
    reason = _refused(permissive, cmd)
    assert reason and "refused whatever the allow-list says" in reason


ORDINARY = [
    "rm build/x.o",
    "rm -rf build",
    "rm -rf ./dist",
    "rm -f /tmp/codebud-test.log",
    "rm -rf /tmp/scratch",
    f"rm -rf {HOME}/project/build",
    "rm -rf ~",  # no shell: "~" is a directory literally named ~ here
    "rm -rf *",  # no shell: no glob expansion
    "dd if=in.img of=out.img",
    "systemctl status sshd",
    "shutdown_helper.py",
]


@pytest.mark.parametrize("cmd", ORDINARY)
def test_ordinary_commands_are_not_catastrophic(cmd):
    assert catastrophic_reason(cmd) is None


def test_the_sandbox_refuses_them_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = Sandbox().run_command("rm -fr /")
    assert result["returncode"] == 1 and "Refused" in result["stderr"]
