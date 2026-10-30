"""
codebud/crypto.py

Encryption at rest with age (phase 10, D14b: age with a passphrase).

A passphrase protects one age X25519 key, kept in the config directory
($XDG_CONFIG_HOME/codebud/keys), never beside the data it encrypts:

    keys/identity.age   the secret key, encrypted with the passphrase
    keys/recipient.txt  the public key (age1...)

Writing needs only the public key, so codebud can save data without
asking for the passphrase; reading needs the key unlocked, which costs
one passphrase (scrypt, about a second) per run rather than one per file.
The unlocked key lives only in this process's memory. The passphrase is
asked for on the terminal; for unattended runs it may come from
CODEBUD_PASSPHRASE instead (`codebud doctor` warns while it is set).

Whole files (a session) are ordinary age files. Append-only files (the
execution history, the evidence log) cannot be appended to as one age
file, so each line is encrypted on its own and stored as
``age:<base64>`` -- still one line, still appendable.

If the passphrase is lost, the data cannot be recovered: that is the point.

The optional dependency pyrage provides age (pip install 'codebud[crypto]').
The age command-line tool is not used: it reads passphrases only from a
terminal.
"""

import base64
import binascii
import getpass
import os
import sys
from pathlib import Path

from codebud import paths
from codebud.fsutil import atomic_write_bytes, private_dir

IDENTITY_FILE = "identity.age"
RECIPIENT_FILE = "recipient.txt"
LINE_PREFIX = "age:"
PASSPHRASE_ENV = "CODEBUD_PASSPHRASE"
ENCRYPT_ENV = "CODEBUD_ENCRYPT"
_MAGIC = b"age-encryption.org/v1\n"

_identity = None  # the unlocked secret key, for this process only
_env_passphrase: str | None = None  # CODEBUD_PASSPHRASE, taken out of the environment


class CryptoError(Exception):
    """Encryption that is unavailable, locked, or fails."""


def _pyrage():
    try:
        import pyrage
    except ImportError as exc:
        raise CryptoError(
            "encryption needs the pyrage module: pip install 'codebud[crypto]'"
        ) from exc
    return pyrage


def available() -> bool:
    """Whether age encryption (pyrage) is installed."""
    try:
        _pyrage()
    except CryptoError:
        return False
    return True


def key_dir() -> Path:
    return paths.config_dir() / "keys"


def has_key() -> bool:
    return (key_dir() / IDENTITY_FILE).is_file() and (key_dir() / RECIPIENT_FILE).is_file()


def _require_key() -> None:
    if not has_key():
        raise CryptoError(f"no encryption key yet in {key_dir()}")


def create_key(passphrase: str) -> str:
    """Generate the key pair, seal it with ``passphrase``; return the public key.

    Refuses to replace an existing key: data encrypted to it would be lost.
    The new key is left unlocked.
    """
    global _identity
    pyrage = _pyrage()
    if not passphrase:
        raise CryptoError("the passphrase must not be empty")
    if (key_dir() / IDENTITY_FILE).exists() or (key_dir() / RECIPIENT_FILE).exists():
        raise CryptoError(f"an encryption key already exists in {key_dir()}")
    _harden_process()
    identity = pyrage.x25519.Identity.generate()
    recipient = str(identity.to_public())
    private_dir(key_dir())
    atomic_write_bytes(
        key_dir() / IDENTITY_FILE, pyrage.passphrase.encrypt(str(identity).encode(), passphrase)
    )
    atomic_write_bytes(key_dir() / RECIPIENT_FILE, (recipient + "\n").encode())
    _identity = identity
    return recipient


def recipient() -> str:
    """The public key data is encrypted to.

    While the key is unlocked it is derived from the secret key itself;
    otherwise it is read from recipient.txt, which unlock() checks.
    """
    if _identity is not None:
        return str(_identity.to_public())
    _require_key()
    return (key_dir() / RECIPIENT_FILE).read_text().strip()


def _open_identity(passphrase: str):
    pyrage = _pyrage()
    _require_key()
    _harden_process()  # the passphrase and the secret key are about to be in memory
    sealed = (key_dir() / IDENTITY_FILE).read_bytes()
    try:
        secret = pyrage.passphrase.decrypt(sealed, passphrase)
    except pyrage.DecryptError as exc:
        raise CryptoError("wrong passphrase (or a damaged key file)") from exc
    return pyrage.x25519.Identity.from_str(secret.decode())


def unlock(passphrase: str) -> None:
    """Unlock the secret key for this process.

    Also checks that recipient.txt -- the public key that writes use while
    the key is locked -- belongs to this key. If someone replaced it, data
    saved since then went to their key, not yours: that is refused loudly.
    """
    global _identity
    identity = _open_identity(passphrase)
    _check_recipient(identity)
    _identity = identity


def _check_recipient(identity) -> None:
    expected = str(identity.to_public())
    stored = (key_dir() / RECIPIENT_FILE).read_text().strip()
    if stored != expected:
        raise CryptoError(
            f"{key_dir() / RECIPIENT_FILE} does not belong to your key: it holds "
            f"{stored[:16]}..., your key's public key is {expected}. Data saved "
            "while it was wrong was encrypted to that other key, which you may not "
            "be able to read. Find out how it changed, then put your public key "
            "back in that file."
        )


def _harden_process() -> None:
    """Keep secrets in this process's memory from reaching the disk or others.

    Called before the secret key or a passphrase is held in memory: no core
    dump (systemd-coredump would otherwise store one, key and decrypted
    data included, if codebud crashed), and on Linux the process is made
    non-dumpable, which also stops other processes of the same user from
    attaching to it or reading /proc/<pid>/mem. Both last until codebud
    exits; best effort where the platform lacks them.
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            pr_set_dumpable = 4
            ctypes.CDLL(None, use_errno=True).prctl(pr_set_dumpable, 0, 0, 0, 0)
        except (OSError, AttributeError):
            pass


def take_env_passphrase() -> None:
    """Move CODEBUD_PASSPHRASE out of the environment into this process.

    The CLI calls this first thing, so nothing codebud starts -- command
    steps, git, editors -- can inherit the passphrase.
    """
    global _env_passphrase
    value = os.environ.pop(PASSPHRASE_ENV, None)
    if value:
        _harden_process()
        _env_passphrase = value


def passphrase_from_env() -> str | None:
    """CODEBUD_PASSPHRASE, for unattended runs; None when it was not given."""
    return _env_passphrase or os.environ.get(PASSPHRASE_ENV) or None


def _terminal_available() -> bool:
    """Whether a passphrase can be typed without echo (a controlling terminal)."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def ask_passphrase(prompt: str) -> str:
    """Read a passphrase from the terminal without echo.

    Without a terminal, getpass would fall back to reading stdin *with*
    echo; refuse instead.
    """
    if not _terminal_available():
        raise CryptoError(
            f"the passphrase is needed and there is no terminal to ask on; "
            f"set {PASSPHRASE_ENV} for unattended runs"
        )
    return getpass.getpass(prompt)


def ensure_unlocked(ask=None, tries: int = 3) -> None:
    """Unlock the key unless it already is.

    The passphrase comes from CODEBUD_PASSPHRASE when it is set (one try:
    nobody is there to retype it), otherwise ``ask`` prompts on the
    terminal, up to ``tries`` times.
    """
    if _identity is not None:
        return
    _require_key()
    from_env = passphrase_from_env()
    if from_env is not None:
        try:
            unlock(from_env)
        except CryptoError as exc:
            raise CryptoError(f"{PASSPHRASE_ENV}: {exc}") from exc
        return
    ask = ask or ask_passphrase
    for attempt in range(1, tries + 1):
        try:
            typed = ask("Passphrase for codebud's encryption key: ")
        except (EOFError, OSError) as exc:  # no terminal to ask on
            raise CryptoError(
                f"the encrypted data needs the passphrase, and there is no terminal to "
                f"ask on; set {PASSPHRASE_ENV} for unattended runs"
            ) from exc
        try:
            unlock(typed)
            return
        except CryptoError:
            if attempt == tries:
                raise


def lock() -> None:
    """Forget the unlocked key."""
    global _identity
    _identity = None


def is_unlocked() -> bool:
    return _identity is not None


def change_passphrase(old: str, new: str) -> None:
    """Re-seal the same key with a new passphrase; the data stays readable."""
    if not new:
        raise CryptoError("the passphrase must not be empty")
    identity = _open_identity(old)
    _check_recipient(identity)
    sealed = _pyrage().passphrase.encrypt(str(identity).encode(), new)
    atomic_write_bytes(key_dir() / IDENTITY_FILE, sealed)


def is_encrypted(data: bytes) -> bool:
    return data.startswith(_MAGIC)


def encrypt(data: bytes) -> bytes:
    """Encrypt ``data`` to the public key; no passphrase needed."""
    pyrage = _pyrage()
    public = pyrage.x25519.Recipient.from_str(recipient())
    return pyrage.encrypt(data, [public])


def decrypt(data: bytes) -> bytes:
    """Decrypt ``data`` with the unlocked key."""
    pyrage = _pyrage()
    if _identity is None:
        raise CryptoError("the encryption key is locked; unlock it with the passphrase first")
    try:
        return pyrage.decrypt(data, [_identity])
    except pyrage.DecryptError as exc:
        raise CryptoError(f"cannot decrypt: {exc}") from exc


def is_encrypted_line(line: str) -> bool:
    return line.startswith(LINE_PREFIX)


def encrypt_line(text: str) -> str:
    """Encrypt one line of an append-only file; the result is one line."""
    return LINE_PREFIX + base64.b64encode(encrypt(text.encode())).decode("ascii")


def decrypt_line(line: str) -> str:
    if not is_encrypted_line(line):
        raise CryptoError("not an encrypted line")
    try:
        sealed = base64.b64decode(line[len(LINE_PREFIX) :].strip(), validate=True)
    except binascii.Error as exc:
        raise CryptoError(f"damaged encrypted line: {exc}") from exc
    return decrypt(sealed).decode()


def enabled() -> bool:
    """Whether codebud encrypts what it stores (CODEBUD_ENCRYPT / encrypt = true)."""
    from codebud import settings

    # A value that is not a clear "no" keeps data encrypted (the safe side).
    return settings.flag(ENCRYPT_ENV, False, unknown=True)


def check_ready() -> None:
    """Raise CryptoError, with the fix, if encryption is on but cannot work."""
    if not enabled():
        return
    if not available():
        raise CryptoError(
            "encryption at rest is on, but pyrage is not installed: "
            "pip install 'codebud[crypto]' (or turn it off: encrypt = false)"
        )
    if not has_key():
        raise CryptoError(
            "encryption at rest is on, but there is no key yet: create one with: codebud key init"
        )


def seal_text(text: str) -> bytes:
    """``text`` as it is stored: encrypted when encryption is on."""
    return encrypt(text.encode()) if enabled() else text.encode()


def seal_line(text: str) -> str:
    """One line of an append-only file as it is stored."""
    return encrypt_line(text) if enabled() else text


def write_file(path, data: bytes) -> None:
    """Encrypt ``data`` and replace ``path`` with it, atomically, 0600."""
    atomic_write_bytes(path, encrypt(data))


def read_file(path) -> bytes:
    return decrypt(Path(os.fspath(path)).read_bytes())
