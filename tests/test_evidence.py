"""
tests/test_evidence.py

The hash-chained evidence log (phase 10): an append-only record of a
security engagement. Each entry hashes the previous one, so editing,
removing or reordering an entry breaks the chain, and verify() says where.
"""

import hashlib
import json
import os
import stat

import pytest

from codebud_sec.engagement import load as load_engagement
from codebud_sec.evidence import GENESIS, EvidenceError, EvidenceLog, verify


def _lines(path):
    return path.read_text().splitlines()


def _rewrite(path, lines):
    path.write_text("".join(line + "\n" for line in lines))


@pytest.fixture
def log(tmp_path):
    return EvidenceLog.create(tmp_path / "case.log", header="Acme PT-42")


# --- writing ----------------------------------------------------------------


def test_create_writes_an_open_entry_linked_to_genesis(log):
    entries = [json.loads(line) for line in _lines(log.path)]
    assert len(entries) == 1
    first = entries[0]
    assert first["seq"] == 0
    assert first["kind"] == "open"
    assert first["prev"] == GENESIS
    assert first["data"]["header"] == "Acme PT-42"


def test_create_records_the_engagement(tmp_path):
    eng_path = tmp_path / "acme.toml"
    eng_path.write_text('client = "Acme"\nticket = "PT-42"\nin_scope = ["10.0.0.0/24"]\n')
    engagement = load_engagement(eng_path)
    log = EvidenceLog.create(tmp_path / "case.log", engagement=engagement)
    data = json.loads(_lines(log.path)[0])["data"]
    assert data["header"] == engagement.header()
    assert data["engagement_sha256"] == hashlib.sha256(eng_path.read_bytes()).hexdigest()
    assert data["in_scope"] == ["10.0.0.0/24"]


def test_create_refuses_to_overwrite(log):
    with pytest.raises(EvidenceError, match="already exists"):
        EvidenceLog.create(log.path)


def test_log_file_is_private(log):
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600


def test_append_chains_entries(log):
    a = log.append("step", {"command": "ss -tlnp"})
    b = log.append("note", {"text": "listener on 4444"})
    assert (a["seq"], b["seq"]) == (1, 2)
    first = json.loads(_lines(log.path)[0])
    assert a["prev"] == first["hash"]
    assert b["prev"] == a["hash"]


def test_append_survives_reopening(log):
    log.append("step", {"command": "ps aux"})
    again = EvidenceLog.open(log.path)
    entry = again.append("note", {"text": "after reopen"})
    assert entry["seq"] == 2
    assert verify(log.path).ok


def test_append_records_artefact_hashes(log, tmp_path):
    artefact = tmp_path / "auth.log"
    artefact.write_bytes(b"Failed password for root\n")
    entry = log.append("step", {"command": "cp /var/log/auth.log ."}, artefacts=[artefact])
    (record,) = entry["artefacts"]
    assert record["path"] == str(artefact.resolve())
    assert record["sha256"] == hashlib.sha256(b"Failed password for root\n").hexdigest()
    assert record["size"] == 25


def test_append_refuses_unknown_kind(log):
    with pytest.raises(EvidenceError, match="kind"):
        log.append("bogus", {})


def test_append_after_close_is_refused(log):
    log.close()
    with pytest.raises(EvidenceError, match="closed"):
        log.append("note", {"text": "too late"})


def test_open_refuses_a_broken_log(log):
    log.append("step", {"command": "id"})
    lines = _lines(log.path)
    lines[1] = lines[1].replace('"id"', '"whoami"')
    _rewrite(log.path, lines)
    with pytest.raises(EvidenceError, match="line 2"):
        EvidenceLog.open(log.path)


def test_open_missing_file(tmp_path):
    with pytest.raises(EvidenceError, match="cannot read"):
        EvidenceLog.open(tmp_path / "nope.log")


# --- verifying --------------------------------------------------------------


def test_verify_a_good_log(log):
    log.append("step", {"command": "journalctl -u sshd"})
    last = log.close()
    result = verify(log.path)
    assert result.ok
    assert result.entries == 3
    assert result.closed
    assert result.head == last["hash"]
    assert result.error is None


def test_verify_reports_an_open_log(log):
    assert not verify(log.path).closed


def test_verify_detects_an_edited_entry(log):
    log.append("step", {"command": "id"})
    log.append("step", {"command": "uname -a"})
    lines = _lines(log.path)
    lines[1] = lines[1].replace('"id"', '"whoami"')
    _rewrite(log.path, lines)
    result = verify(log.path)
    assert not result.ok
    assert "line 2" in result.error and "hash" in result.error


def test_verify_detects_a_rehashed_edit(log):
    # Editing an entry and recomputing its own hash still breaks the next link.
    from codebud_sec.evidence import entry_hash

    log.append("step", {"command": "id"})
    log.append("step", {"command": "uname -a"})
    lines = _lines(log.path)
    entry = json.loads(lines[1])
    entry["data"]["command"] = "whoami"
    entry["hash"] = entry_hash(entry)
    lines[1] = json.dumps(entry, sort_keys=True)
    _rewrite(log.path, lines)
    result = verify(log.path)
    assert not result.ok
    assert "line 3" in result.error and "previous" in result.error


def test_verify_detects_a_removed_entry(log):
    log.append("step", {"command": "id"})
    log.append("step", {"command": "uname -a"})
    lines = _lines(log.path)
    del lines[1]
    _rewrite(log.path, lines)
    result = verify(log.path)
    assert not result.ok
    assert "line 2" in result.error


def test_verify_detects_garbage(log):
    with open(log.path, "a") as f:
        f.write("not json\n")
    result = verify(log.path)
    assert not result.ok
    assert "line 2" in result.error


def test_verify_detects_entries_after_close(log):
    log.close()
    lines = _lines(log.path)
    _rewrite(log.path, [*lines, lines[-1]])
    assert not verify(log.path).ok


def test_verify_empty_file(tmp_path):
    path = tmp_path / "empty.log"
    path.write_text("")
    result = verify(path)
    assert not result.ok
    assert "empty" in result.error


def test_verify_checks_artefacts_on_request(log, tmp_path):
    kept = tmp_path / "kept.bin"
    kept.write_bytes(b"one")
    changed = tmp_path / "changed.bin"
    changed.write_bytes(b"two")
    gone = tmp_path / "gone.bin"
    gone.write_bytes(b"three")
    log.append("step", {"command": "collect"}, artefacts=[kept, changed, gone])
    changed.write_bytes(b"TWO")
    gone.unlink()

    assert verify(log.path).ok  # the chain itself is intact
    result = verify(log.path, check_artefacts=True)
    assert not result.ok
    assert result.artefact_problems == [
        f"{changed.resolve()}: changed since it was logged",
        f"{gone.resolve()}: missing",
    ]


# --- encrypted at rest (D14b) ----------------------------------------------


@pytest.fixture
def sealed_log(tmp_path, monkeypatch, fast_passphrase):
    """An encrypted log; the key is locked again afterwards."""
    pytest.importorskip("pyrage")
    from codebud import crypto

    crypto.create_key("pw")
    crypto.lock()
    monkeypatch.setattr("getpass.getpass", lambda _p="": pytest.fail("asked for the passphrase"))
    return EvidenceLog.create(tmp_path / "sealed.log", header="Acme PT-42", encrypt=True)


def test_encrypted_log_writes_without_the_passphrase(sealed_log):
    sealed_log.append("step", {"command": "journalctl -u sshd"})
    for line in _lines(sealed_log.path):
        record = json.loads(line)
        assert set(record) == {"seq", "kind", "prev", "hash", "sealed"}
        assert record["sealed"].startswith("age:")
    assert "journalctl" not in sealed_log.path.read_text()
    assert "Acme" not in sealed_log.path.read_text()


def test_encrypted_log_reopens_and_appends_while_locked(sealed_log):
    sealed_log.append("step", {"command": "id"})
    again = EvidenceLog.open(sealed_log.path)  # readable fields only
    assert again.encrypted
    assert again.append("note", {"text": "more"})["seq"] == 2


def test_verify_an_encrypted_log_needs_the_key(sealed_log):
    from codebud import crypto
    from codebud_sec.evidence import entries

    sealed_log.append("step", {"command": "id"})
    with pytest.raises(EvidenceError, match="unlock"):
        verify(sealed_log.path)
    crypto.unlock("pw")
    result = verify(sealed_log.path)
    assert result.ok and result.encrypted and result.entries == 2
    assert [e["kind"] for e in entries(sealed_log.path)] == ["open", "step"]
    assert entries(sealed_log.path)[0]["data"]["header"] == "Acme PT-42"


def test_encrypted_log_detects_a_swapped_body(sealed_log):
    from codebud import crypto

    sealed_log.append("step", {"command": "id"})
    sealed_log.append("step", {"command": "uname -a"})
    lines = _lines(sealed_log.path)
    one, two = json.loads(lines[1]), json.loads(lines[2])
    one["sealed"], two["sealed"] = two["sealed"], one["sealed"]
    _rewrite(sealed_log.path, [lines[0], json.dumps(one), json.dumps(two)])
    crypto.unlock("pw")
    result = verify(sealed_log.path)
    assert not result.ok
    assert "line 2" in result.error and "do not match" in result.error


def test_encrypted_log_detects_a_removed_entry_while_locked(sealed_log):
    sealed_log.append("step", {"command": "id"})
    sealed_log.append("step", {"command": "uname -a"})
    lines = _lines(sealed_log.path)
    del lines[1]
    _rewrite(sealed_log.path, lines)
    with pytest.raises(EvidenceError, match="line 2"):
        EvidenceLog.open(sealed_log.path)


def test_a_plain_entry_in_an_encrypted_log_is_refused(sealed_log, tmp_path):
    plain = EvidenceLog.create(tmp_path / "plain.log", encrypt=False)
    entry = plain.append("note", {"text": "x"})
    with open(sealed_log.path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    with pytest.raises(EvidenceError, match="mixes"):
        EvidenceLog.open(sealed_log.path)


def test_encrypting_needs_a_key(tmp_path, fast_passphrase):
    pytest.importorskip("pyrage")
    with pytest.raises(EvidenceError, match="no encryption key"):
        EvidenceLog.create(tmp_path / "x.log", encrypt=True)
    assert not (tmp_path / "x.log").exists()


def test_follows_the_encryption_setting(tmp_path, monkeypatch, fast_passphrase):
    pytest.importorskip("pyrage")
    from codebud import crypto

    crypto.create_key("pw")
    monkeypatch.setenv("CODEBUD_ENCRYPT", "1")
    assert EvidenceLog.create(tmp_path / "auto.log").encrypted


def test_every_entry_has_a_random_nonce(log):
    a = log.append("note", {"text": "same"})
    b = log.append("note", {"text": "same"})
    assert len(a["nonce"]) == 32 and a["nonce"] != b["nonce"]


# --- appends never recreate the log (security review, low) -----------------


def test_a_deleted_log_is_not_recreated(log):
    log.path.unlink()
    with pytest.raises(EvidenceError, match="gone"):
        log.append("note", {"text": "after delete"})
    assert not log.path.exists()


def test_a_symlink_in_place_of_the_log_is_refused(log, tmp_path):
    other = tmp_path / "other.log"
    other.write_text("")
    log.path.unlink()
    log.path.symlink_to(other)
    with pytest.raises(EvidenceError, match="cannot open"):
        log.append("note", {"text": "x"})
    assert other.read_text() == ""


def test_appends_keep_the_log_private(log):
    log.append("note", {"text": "x"})
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600


def test_a_consistent_rewrite_needs_the_recorded_head_to_be_caught(log):
    # Documented limit: the chain is checked against a head hash kept
    # elsewhere. Rewriting an entry and every hash after it passes verify(),
    # but the head no longer matches the one recorded before.
    from codebud_sec.evidence import entry_hash

    log.append("step", {"command": "id"})
    recorded_head = verify(log.path).head
    lines = _lines(log.path)
    entry = json.loads(lines[1])
    entry["data"]["command"] = "whoami"
    entry["hash"] = entry_hash(entry)
    _rewrite(log.path, [lines[0], json.dumps(entry, sort_keys=True)])
    result = verify(log.path)
    assert result.ok  # the chain alone cannot tell
    assert result.head != recorded_head  # the recorded head can
