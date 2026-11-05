"""
tests/test_release_sign.py

scripts/release-sign with a throwaway key in a private GNUPGHOME, so the
user's own keyring is never touched.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release-sign"

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("sha256sum") is None,
    reason="needs gpg and sha256sum",
)


@pytest.fixture
def gnupg():
    """A private GNUPGHOME with one passphrase-less test key; yields its env.

    The home is a short directory under /tmp, not pytest's tmp_path: gpg-agent
    puts its socket in GNUPGHOME, and on macOS (no /run/user to fall back to)
    tmp_path is too long for a Unix socket path (104 bytes there).
    """
    home = tempfile.mkdtemp(prefix="cb-gpg-", dir="/tmp" if os.path.isdir("/tmp") else None)
    env = {**os.environ, "GNUPGHOME": home}
    try:
        made = subprocess.run(
            ["gpg", "--batch", "--passphrase", "", "--quick-gen-key",
             "Codebud Test <test@example.invalid>", "ed25519", "sign", "never"],
            env=env, capture_output=True, text=True,
        )  # fmt: skip
        assert made.returncode == 0, f"gpg could not make a test key:\n{made.stderr}"
        yield env
    finally:
        subprocess.run(["gpgconf", "--kill", "all"], env=env, capture_output=True)
        shutil.rmtree(home, ignore_errors=True)


def _sign(tarball: Path, env: dict, *key: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT), str(tarball), *key],
        cwd=tarball.parent, env=env, capture_output=True, text=True,
    )  # fmt: skip


@pytest.fixture
def tarball(tmp_path):
    path = tmp_path / "codebud-1.2.3.tar.gz"
    path.write_bytes(b"not really a tarball\n")
    return path


def test_signs_and_writes_checksum(gnupg, tarball):
    result = _sign(tarball, gnupg, "test@example.invalid")
    assert result.returncode == 0, result.stderr
    verify = subprocess.run(
        ["gpg", "--verify", f"{tarball}.sig", str(tarball)], env=gnupg, capture_output=True
    )
    assert verify.returncode == 0
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert Path(f"{tarball}.sha256").read_text() == f"{digest}  {tarball.name}\n"


def test_signature_no_longer_verifies_after_tampering(gnupg, tarball):
    assert _sign(tarball, gnupg, "test@example.invalid").returncode == 0
    tarball.write_bytes(b"tampered\n")
    verify = subprocess.run(
        ["gpg", "--verify", f"{tarball}.sig", str(tarball)], env=gnupg, capture_output=True
    )
    assert verify.returncode != 0


def test_without_a_key_it_refuses(gnupg, tarball, tmp_path):
    # An empty global git config, so no user.signingkey is set.
    env = {**gnupg, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull}
    result = _sign(tarball, env)
    assert result.returncode == 1
    assert "no signing key" in result.stderr
    assert not Path(f"{tarball}.sig").exists()


def test_missing_tarball_is_reported(gnupg, tmp_path):
    result = _sign(tmp_path / "codebud-9.9.9.tar.gz", gnupg, "test@example.invalid")
    assert result.returncode == 1
    assert "not found" in result.stderr
