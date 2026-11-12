"""
tests/test_security_ir.py

The security-ir profile shipped with codebud_sec (phase 10, step 5 plan,
PR 3; decision D14i): live triage that sees the host and changes nothing
on it, evidence analysis on copies in the project. Locked (no network),
observer isolation, encrypted, integrity required, ask mode.
"""

import logging

import pytest

from codebud import paths, profiles
from codebud.safety import SafetyEngine


@pytest.fixture
def ir(monkeypatch):
    profile = profiles.get("security-ir")
    monkeypatch.setattr(profiles, "_active", profile)
    monkeypatch.setattr(paths, "PROFILE_POLICY_DIR", profile.policy)
    return profile


def _engine():
    return SafetyEngine()


def _step(cmd):
    return {"tool": "command", "args": {"cmd": cmd}}


def test_it_ships_and_is_found():
    assert "security-ir" in profiles.available()
    profile = profiles.get("security-ir")
    assert (profile.mode, profile.privacy, profile.isolation) == ("ask", "locked", "observer")
    assert profile.encrypt and profile.integrity
    assert "RFC 3227" in profile.prompt
    assert profile.source.endswith("codebud_sec/data/profiles/security-ir.toml")


def test_its_lists_load_without_a_dropped_entry(ir, caplog):
    with caplog.at_level(logging.WARNING):
        engine = _engine()
    assert "Ignoring" not in caplog.text
    assert "ps" in engine.harmless_commands and "yara" in engine.powerful_commands
    assert engine.trusted_sequences == []  # not the default pip install sequence


@pytest.mark.parametrize(
    "cmd",
    [
        "ps -ef",
        "pstree -p",
        "journalctl -u sshd --since today",
        "last -F",
        "sha256sum evidence/auth.log",
        "grep -c Failed /var/log/auth.log",
        "tcpdump -r capture.pcap -nn",
        "tar -czf evidence/ssh.tgz /etc/ssh",
        "cp /var/log/auth.log evidence/",
        "yara rules.yar evidence/",
        "vol -f evidence/mem.raw windows.pslist",
    ],
)
def test_triage_and_analysis_are_allowed(ir, cmd):
    assert _engine().refusal_reason(_step(cmd)) is None, cmd


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("ss -tan", "sandbox view"),
        ("ip addr", "sandbox view"),
        ("df -h", "sandbox view"),
        ("findmnt", "sandbox view"),
        ("rm evidence/x", "changes things"),
        ("kill 1234", "changes things"),
        ("systemctl stop sshd", "changes things"),
        ("tar -xf suspect.tar", "extracting rewrites files"),
        ("tar xf suspect.tar", "extracting rewrites files"),
        ("tar -rf evidence/a.tar x", "appending changes an archive"),
        ("sort -o /etc/passwd /etc/passwd", "writes a file"),
        ("rg --pre=sh secret /", "runs a program"),
        ("tcpdump -r a.pcap -z sh", "runs a program"),
        ("tshark -r a.pcap -X lua_script:x.lua", "runs a script"),
        ("vol --plugin-dirs /tmp/p -f mem.raw windows.pslist", "loads code"),
        ("python3 -c 'print(1)'", "not on the list"),
        ("curl https://example.org", "not on the list"),
    ],
)
def test_what_would_change_the_host_or_mislead_is_refused(ir, cmd, why):
    assert _engine().refusal_reason(_step(cmd)) is not None, f"{cmd} ({why})"


def test_collection_and_forensic_tools_ask_every_time(ir):
    engine = _engine()
    for cmd in ("cp a evidence/", "tar -czf e.tgz x", "yara r.yar x", "fls -r image.dd"):
        assert engine.is_powerful(_step(cmd)), cmd
    assert not engine.is_powerful(_step("ps -ef"))


def test_host_network_is_offered(ir):
    from codebud.tools.tool_registry import ToolRegistry

    assert ToolRegistry(sandbox=None).has("host_network")


def test_network_steps_are_refused(ir, monkeypatch):
    monkeypatch.setenv("CODEBUD_PRIVACY", "locked")
    step = {"tool": "command", "args": {"cmd": "ps -ef", "network": True}}
    assert "locked" in _engine().refusal_reason(step)


@pytest.mark.parametrize(
    "cmd", ["date -s 2020-01-01", "date --set=2020-01-01", "date --set 2020-01-01"]
)
def test_setting_the_clock_is_refused(ir, cmd):
    """It would falsify every later timestamp of the timeline (review of #16)."""
    assert _engine().refusal_reason(_step(cmd)) is not None, cmd


def test_reading_the_clock_is_fine(ir):
    assert _engine().refusal_reason(_step("date -u +%s")) is None


def test_iso_timestamps_have_an_allowed_spelling(ir):
    """-Iseconds holds an "s" in a short-option cluster and is refused by
    the conservative rule; the long spelling does the same and runs."""
    assert _engine().refusal_reason(_step("date --iso-8601=seconds")) is None


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "case"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "disk.raw").write_bytes(b"\0" * 512)
    monkeypatch.chdir(root)
    return root


def test_imaging_an_image_file_is_allowed_and_asked(ir, case):
    engine = _engine()
    for cmd in (
        "dc3dd if=evidence/disk.raw of=evidence/copy.raw hash=sha256 log=evidence/copy.log",
        "ewfacquire -u -C C1 -e me -E E1 -D disk -d sha256 -l evidence/a.log -t evidence/disk evidence/disk.raw",
    ):
        assert engine.refusal_reason(_step(cmd)) is None, cmd
        assert engine.is_powerful(_step(cmd)), cmd


def test_reading_images_and_metadata_is_harmless(ir, case):
    engine = _engine()
    for cmd in (
        "ewfverify -d sha256 evidence/disk.E01",
        "ewfinfo evidence/disk.E01",
        "exiftool -a -G1 evidence/",
    ):
        assert engine.refusal_reason(_step(cmd)) is None, cmd
        assert not engine.is_powerful(_step(cmd)), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "dc3dd if=/dev/sda of=evidence/x.raw hash=sha256 log=evidence/x.log",
        "dc3dd wipe=/dev/sda",
        "exiftool -overwrite_original -Artist=x evidence/a.jpg",
        "dd if=/dev/sda of=evidence/x.raw",
    ],
)
def test_acquiring_from_devices_and_writing_metadata_are_refused(ir, case, cmd):
    assert _engine().refusal_reason(_step(cmd)) is not None, cmd
