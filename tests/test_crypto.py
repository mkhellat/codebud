"""
tests/test_crypto.py

Encryption at rest (phase 10, D14b: age with a passphrase). A passphrase
protects one age X25519 key kept in the config directory, away from the
data; writing needs only the public key, reading needs the key unlocked.
Skipped when pyrage is not installed.

Passphrase operations take about a second each (age's scrypt), so only
the tests of the passphrase itself use one; the rest start from a key that
is already unlocked.
"""

import os
import stat

import pytest

pytest.importorskip("pyrage")

from codebud import crypto, paths  # noqa: E402


@pytest.fixture
def unlocked(monkeypatch):
    """A key pair on disk and unlocked, without paying for the passphrase."""
    from pyrage import x25519

    identity = x25519.Identity.generate()
    keys = paths.config_dir() / "keys"
    keys.mkdir(parents=True)
    (keys / crypto.RECIPIENT_FILE).write_text(str(identity.to_public()) + "\n")
    (keys / crypto.IDENTITY_FILE).write_bytes(b"not used by these tests")
    monkeypatch.setattr(crypto, "_identity", identity)
    return identity


# --- the key and its passphrase ---------------------------------------------


def test_create_unlock_and_round_trip():
    assert not crypto.has_key()
    recipient = crypto.create_key("correct horse")
    assert recipient.startswith("age1")
    assert crypto.has_key()
    assert crypto.recipient() == recipient

    sealed = crypto.encrypt(b"secret")  # the public key is enough to write
    crypto.lock()
    with pytest.raises(crypto.CryptoError, match="locked"):
        crypto.decrypt(sealed)
    crypto.unlock("correct horse")
    assert crypto.decrypt(sealed) == b"secret"


def test_key_files_are_private_and_outside_the_data_dir():
    crypto.create_key("pw")
    keys = crypto.key_dir()
    assert paths.data_dir() not in keys.parents
    assert stat.S_IMODE(os.stat(keys).st_mode) == 0o700
    for name in (crypto.IDENTITY_FILE, crypto.RECIPIENT_FILE):
        assert stat.S_IMODE(os.stat(keys / name).st_mode) == 0o600
    # The secret key is never on disk in the clear.
    assert b"AGE-SECRET-KEY" not in (keys / crypto.IDENTITY_FILE).read_bytes()


def test_wrong_passphrase_is_refused():
    crypto.create_key("right")
    crypto.lock()
    with pytest.raises(crypto.CryptoError, match="passphrase"):
        crypto.unlock("wrong")
    assert not crypto.is_unlocked()


def test_create_refuses_to_replace_a_key(unlocked):
    with pytest.raises(crypto.CryptoError, match="already exists"):
        crypto.create_key("other")


def test_create_refuses_an_empty_passphrase():
    with pytest.raises(crypto.CryptoError, match="empty"):
        crypto.create_key("")


def test_change_passphrase_keeps_the_key():
    crypto.create_key("old")
    sealed = crypto.encrypt(b"kept")
    crypto.change_passphrase("old", "new")
    crypto.lock()
    with pytest.raises(crypto.CryptoError):
        crypto.unlock("old")
    crypto.unlock("new")
    assert crypto.decrypt(sealed) == b"kept"


def test_no_key_yet():
    with pytest.raises(crypto.CryptoError, match="no encryption key"):
        crypto.encrypt(b"x")
    with pytest.raises(crypto.CryptoError, match="no encryption key"):
        crypto.unlock("pw")


# --- data -------------------------------------------------------------------


def test_is_encrypted(unlocked):
    assert crypto.is_encrypted(crypto.encrypt(b"x"))
    assert not crypto.is_encrypted(b'{"plain": "json"}')


def test_decrypt_with_another_key_fails(unlocked, monkeypatch):
    from pyrage import x25519

    sealed = crypto.encrypt(b"x")
    monkeypatch.setattr(crypto, "_identity", x25519.Identity.generate())
    with pytest.raises(crypto.CryptoError, match="cannot decrypt"):
        crypto.decrypt(sealed)


def test_lines_round_trip_and_stay_one_line(unlocked):
    line = crypto.encrypt_line('{"step": "ls -la"}')
    assert line.startswith(crypto.LINE_PREFIX)
    assert "\n" not in line and "ls -la" not in line
    assert crypto.is_encrypted_line(line)
    assert not crypto.is_encrypted_line('{"step": "ls"}')
    assert crypto.decrypt_line(line) == '{"step": "ls -la"}'


def test_decrypt_line_rejects_a_plain_line(unlocked):
    with pytest.raises(crypto.CryptoError, match="not an encrypted line"):
        crypto.decrypt_line('{"plain": 1}')


def test_file_round_trip_is_private(unlocked, tmp_path):
    path = tmp_path / "session.json.age"
    crypto.write_file(path, b'{"turns": []}')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert crypto.is_encrypted(path.read_bytes())
    assert crypto.read_file(path) == b'{"turns": []}'


# --- the public key file (audit M4) -----------------------------------------


def test_unlock_refuses_a_replaced_public_key(monkeypatch):
    from pyrage import x25519

    crypto.create_key("pw")
    crypto.lock()
    stranger = str(x25519.Identity.generate().to_public())
    (crypto.key_dir() / crypto.RECIPIENT_FILE).write_text(stranger + "\n")
    with pytest.raises(crypto.CryptoError, match="does not belong to your key"):
        crypto.unlock("pw")
    assert not crypto.is_unlocked()


def test_while_unlocked_writes_use_the_key_not_the_file(unlocked):
    from pyrage import x25519

    stranger = x25519.Identity.generate()
    (crypto.key_dir() / crypto.RECIPIENT_FILE).write_text(str(stranger.to_public()) + "\n")
    sealed = crypto.encrypt(b"mine")
    assert crypto.decrypt(sealed) == b"mine"  # encrypted to my key, not the stranger's


# --- age refusing a correct passphrase on a slow moment ---------------------------


def _excessive_then(real, times):
    """pyrage.passphrase.decrypt that refuses ``times`` times the way age
    does when the machine is slower than when the key was sealed, then
    works."""
    import pyrage

    calls = []

    def decrypt(data, passphrase):
        calls.append(1)
        if len(calls) <= times:
            raise pyrage.DecryptError("Excessive work parameter for passphrase.")
        return real(data, passphrase)

    return decrypt, calls


def test_a_slow_moment_is_retried(monkeypatch):
    """age calibrates its passphrase work factor to the machine's speed when
    sealing, and refuses one far above the current speed when opening: on a
    loaded machine a correct passphrase was reported as wrong (seen on
    macOS CI)."""
    import pyrage

    crypto.create_key("correct horse")
    crypto.lock()
    decrypt, calls = _excessive_then(pyrage.passphrase.decrypt, 2)
    monkeypatch.setattr(pyrage.passphrase, "decrypt", decrypt)
    crypto.unlock("correct horse")
    assert len(calls) == 3


def test_a_persistent_refusal_is_not_called_a_wrong_passphrase(monkeypatch):
    import pyrage

    crypto.create_key("correct horse")
    crypto.lock()
    decrypt, _ = _excessive_then(pyrage.passphrase.decrypt, 99)
    monkeypatch.setattr(pyrage.passphrase, "decrypt", decrypt)
    with pytest.raises(crypto.CryptoError) as exc:
        crypto.unlock("correct horse")
    assert "wrong passphrase" not in str(exc.value)
    assert "slower" in str(exc.value)


def test_a_wrong_passphrase_is_still_wrong_and_not_retried(monkeypatch):
    import pyrage

    crypto.create_key("correct horse")
    crypto.lock()
    calls = []
    real = pyrage.passphrase.decrypt
    monkeypatch.setattr(pyrage.passphrase, "decrypt", lambda d, p: calls.append(1) or real(d, p))
    with pytest.raises(crypto.CryptoError, match="wrong passphrase"):
        crypto.unlock("wrong horse")
    assert len(calls) == 1
