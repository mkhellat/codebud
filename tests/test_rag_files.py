"""
tests/test_rag_files.py

Which files the project index reads (phase 9, B2): git's view of the
project when there is one, a filtered walk otherwise; secrets, binaries,
huge, generated and ignored files never.
"""

import os
import subprocess

import pytest

from codebud.rag import files
from codebud.rag.files import select_files


def _write(root, rel, content="x = 1\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def test_git_repo_uses_tracked_and_new_unignored_files(repo):
    _write(repo, "src/a.py")
    _write(repo, ".gitignore", "build/\n*.log\n")
    _write(repo, "build/out.py")
    _write(repo, "run.log", "log\n")
    _git(repo, "add", "src/a.py", ".gitignore")
    _write(repo, "src/new.py")  # untracked but not ignored
    _write(repo, ".git/info/exclude", "notes/\n")
    _write(repo, "notes/plan.md", "# plan\n")

    assert select_files(repo).files == [".gitignore", "src/a.py", "src/new.py"]


def test_deleted_but_still_tracked_file_is_skipped(repo):
    _write(repo, "gone.py")
    _git(repo, "add", "gone.py")
    (repo / "gone.py").unlink()
    assert select_files(repo).files == []


def test_walk_outside_git_skips_dot_dirs_and_tool_caches(tmp_path):
    root = tmp_path / "plain"
    _write(root, "main.py")
    _write(root, "pkg/util.py")
    for skipped in (".venv/lib.py", "node_modules/m.js", "__pycache__/c.py", ".hidden/h.py"):
        _write(root, skipped)
    assert select_files(root).files == ["main.py", "pkg/util.py"]


SECRETS = [
    ".env",
    ".env.local",
    "deploy/server.pem",
    "tls.key",
    "id_ed25519",
    "id_rsa.pub",
    "cert.p12",
    ".netrc",
    ".pypirc",
    "credentials.json",
    "db_secrets.yaml",
    "vault.kdbx",
    "backup.gpg",
    ".ssh/config",
    ".aws/config",
    ".gnupg/gpg.conf",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_never_indexed_even_when_tracked(repo, secret):
    _write(repo, secret, "token = abc\n")
    _write(repo, "ok.py")
    _git(repo, "add", "-f", ".")
    selection = select_files(repo)
    assert secret not in selection.files
    assert "ok.py" in selection.files
    assert selection.skipped["secret"] == 1


def test_codebudignore_patterns(repo):
    _write(repo, ".codebudignore", "# comment\nvendor/\n*.generated.py\nsrc/skip_me.py\n")
    for rel in ("vendor/lib.py", "src/x.generated.py", "src/skip_me.py", "src/keep.py"):
        _write(repo, rel)
    selected = select_files(repo).files
    assert "src/keep.py" in selected
    assert not {"vendor/lib.py", "src/x.generated.py", "src/skip_me.py"} & set(selected)


def test_binary_large_generated_and_symlinked_files_are_skipped(repo, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 100)
    _write(repo, "image.png", b"\x89PNG\r\n\x00\x00binary")
    _write(repo, "latin1.txt", "caf\xe9\n".encode("latin-1"))
    _write(repo, "big.py", "x = 1\n" * 50)
    _write(repo, "uv.lock", "lock\n")
    _write(repo, "app.min.js", "a=1\n")
    _write(repo, "real.py")
    os.symlink(repo / "real.py", repo / "link.py")
    _git(repo, "add", ".")

    selection = select_files(repo)
    assert selection.files == ["real.py"]
    assert selection.skipped == {"binary": 2, "too large": 1, "generated": 2, "symlink": 1}


# --- secret files the security review found indexed (M3, 2026-10-29) --------


@pytest.mark.parametrize(
    "rel",
    [
        ".envrc",
        ".git-credentials",
        ".vault-token",
        ".bash_history",
        ".zsh_history",
        ".python_history",
        "psql_history",
        ".htpasswd",
        ".s3cfg",
        ".boto",
        ".dockercfg",
        "prod.tfvars",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "aws_credentials",
        ".password-store/web/site.gpg",
        ".azure/accessTokens.json",
        ".terraform/terraform.tfstate",
    ],
)
def test_more_secret_files_are_never_indexed(tmp_path, rel):
    _write(tmp_path, "main.py")
    _write(tmp_path, rel, "token = abcdefghijklmnop\n")
    selection = select_files(tmp_path)
    assert rel not in selection.files
    assert "main.py" in selection.files


@pytest.mark.parametrize("rel", ["history.py", "src/env_setup.py", "docs/environment.md"])
def test_ordinary_files_with_similar_names_are_indexed(tmp_path, rel):
    _write(tmp_path, rel)
    assert rel in select_files(tmp_path).files
