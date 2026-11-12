"""
tests/test_codebud_sec.py

The codebud-sec command (phase 10, step 5 plan, PR 7): a session opens
the engagement -- scope guard and network fence active, every step
recorded in its encrypted evidence log -- and hands over to codebud with
a security profile; evidence add/verify/show/close keep the log; pin is
codebud integrity pin.
"""

import pytest

from codebud import isolation, safety
from codebud_sec import cli, evidence

SCOPE = """\
client = "ACME"
ticket = "PT-42"
in_scope = ["10.0.0.0/16"]
"""


@pytest.fixture
def case(tmp_path, monkeypatch, fast_passphrase):
    from codebud import crypto

    crypto.create_key("pw")
    crypto.lock()
    monkeypatch.setenv("CODEBUD_PASSPHRASE", "pw")
    monkeypatch.setattr(safety, "_STEP_CHECKS", [])
    monkeypatch.setattr(safety, "_STEP_NOTICES", [])
    monkeypatch.setattr(safety, "_STEP_RECORDERS", [])
    monkeypatch.setattr(isolation, "_FENCES", [])
    monkeypatch.chdir(tmp_path)
    (tmp_path / "acme.toml").write_text(SCOPE)
    return tmp_path


@pytest.fixture
def core(monkeypatch):
    """codebud's own main, replaced: it records what it was started with,
    what was active, and runs one step through the recorders."""
    seen = {}

    def main():
        import sys

        seen["argv"] = list(sys.argv)
        seen["engagement"] = safety.provided("engagement")
        seen["fenced"] = bool(isolation._FENCES)
        step = {
            "id": "step_0",
            "tool": "command",
            "description": "scan",
            "args": {"cmd": "nmap 10.0.0.5"},
        }
        assert safety.record_step("start", step) is None
        safety.record_step("done", step, {"stdout": "80/tcp open", "stderr": "", "returncode": 0})
        if seen.get("exit"):
            raise SystemExit(seen["exit"])

    monkeypatch.setattr("codebud.cli.main.main", main)
    return seen


def _entries(case):
    from codebud import crypto

    crypto.ensure_unlocked()
    return evidence.entries(case / "acme.evidence.jsonl")


def test_a_session_runs_codebud_inside_the_engagement(case, core):
    status = cli.main(["chat", "--engagement", "acme.toml", "--resume"])
    assert status == 0
    assert core["argv"] == ["codebud", "--profile", "security-pentest", "chat", "--resume"]
    assert core["engagement"] and core["fenced"]
    # Everything is taken down afterwards.
    assert not safety.provided("engagement") and isolation._FENCES == []
    assert safety._STEP_RECORDERS == []


def test_every_step_is_in_the_encrypted_evidence_log(case, core):
    cli.main(["run", "--engagement", "acme.toml", "scan the web tier"])
    log = case / "acme.evidence.jsonl"
    assert "nmap" not in log.read_text()  # sealed
    entries = _entries(case)
    kinds = [(e["kind"], e["data"].get("event") or e["data"].get("session")) for e in entries]
    assert kinds == [
        ("open", None),
        ("note", "start"),
        ("step", "start"),
        ("step", "done"),
        ("note", "end"),
    ]
    assert entries[0]["data"]["engagement_sha256"]
    assert entries[3]["data"]["result"]["stdout"] == "80/tcp open"
    assert evidence.verify(log).ok


def test_later_sessions_append_to_the_same_log(case, core):
    cli.main(["run", "--engagement", "acme.toml", "one"])
    cli.main(["run", "--engagement", "acme.toml", "two"])
    entries = _entries(case)
    assert [e["kind"] for e in entries].count("open") == 1
    assert len(entries) == 9


def test_the_profile_and_log_can_be_chosen(case, core):
    cli.main(
        ["chat", "--engagement", "acme.toml", "--profile", "security-ir", "--evidence", "ir.jsonl"]
    )
    assert core["argv"][:3] == ["codebud", "--profile", "security-ir"]
    assert (case / "ir.jsonl").exists() and not (case / "acme.evidence.jsonl").exists()


def test_codebuds_exit_status_is_kept_and_recorded(case, core):
    core["exit"] = 3
    assert cli.main(["run", "--engagement", "acme.toml", "x"]) == 3
    assert _entries(case)[-1]["data"] == {"session": "end", "status": 3}


def test_long_output_is_kept_as_its_hash(case, core, monkeypatch):
    long = "x" * (cli._TEXT_LIMIT + 1)
    recorder = cli.EvidenceRecorder(evidence.EvidenceLog.create(case / "l.jsonl", encrypt=False))
    recorder("done", {"id": "s", "tool": "command", "args": {"cmd": "cat big"}}, {"stdout": long})
    stdout = evidence.entries(case / "l.jsonl")[-1]["data"]["result"]["stdout"]
    assert stdout["bytes"] == len(long) and len(stdout["sha256"]) == 64


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (["chat", "--engagement", "missing.toml"], "no engagement file"),
        (["chat"], "no engagement given"),
    ],
)
def test_a_session_needs_an_engagement(case, core, argv, why):
    with pytest.raises(SystemExit) if argv == ["chat"] else _nothing():
        assert cli.main(argv) == 2, why
    assert "argv" not in core


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_session_needs_a_key(case, core, monkeypatch):
    from codebud import crypto

    monkeypatch.setattr(crypto, "has_key", lambda: False)
    assert cli.main(["chat", "--engagement", "acme.toml"]) == 2
    assert "argv" not in core


def test_evidence_add_verify_show_close(case, capsys):
    (case / "disk.raw").write_bytes(b"\0" * 64)
    assert (
        cli.main(
            [
                "evidence",
                "add",
                "disk.raw",
                "-m",
                "image of the web server",
                "--engagement",
                "acme.toml",
            ]
        )
        == 0
    )
    assert evidence.file_sha256(case / "disk.raw") in capsys.readouterr().out
    assert cli.main(["evidence", "verify", "--files", "--engagement", "acme.toml"]) == 0
    assert "chain intact" in capsys.readouterr().out
    assert cli.main(["evidence", "show", "--engagement", "acme.toml"]) == 0
    assert "image of the web server" in capsys.readouterr().out
    assert cli.main(["evidence", "close", "--engagement", "acme.toml"]) == 0
    # Closed: nothing more is added.
    assert cli.main(["evidence", "add", "disk.raw", "--engagement", "acme.toml"]) == 2


def test_verify_reports_a_changed_file(case, capsys):
    (case / "disk.raw").write_bytes(b"\0" * 64)
    cli.main(["evidence", "add", "disk.raw", "--engagement", "acme.toml"])
    (case / "disk.raw").write_bytes(b"\1" * 64)
    assert cli.main(["evidence", "verify", "--files", "--engagement", "acme.toml"]) == 2
    assert "changed" in capsys.readouterr().err


def test_pin_is_codebud_integrity_pin(monkeypatch):
    seen = {}

    def main():
        import sys

        seen["argv"] = list(sys.argv)

    monkeypatch.setattr("codebud.cli.main.main", main)
    assert cli.main(["pin", "--command", "nmap"]) == 0
    assert seen["argv"] == ["codebud", "integrity", "pin", "--command", "nmap"]

