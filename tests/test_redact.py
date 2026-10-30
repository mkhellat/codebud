"""
tests/test_redact.py

Secret masking before anything is stored (phase 8, Part B3; decision D12d:
on by default). Known token formats, private keys, URL passwords and
secret-looking assignments become [redacted] in saved sessions and the
step history; ordinary code stays untouched, and the live prompt is never
changed.
"""

import json

import pytest

from codebud.redact import REDACTED, redact, redact_data

# Fake secrets assembled at runtime so this file does not trip scanners.
OPENAI = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
ANTHROPIC = "sk-ant-" + "api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"
GITHUB = "ghp_" + "a" * 36
GITHUB_PAT = "github_pat_" + "11ABCDEFG0" + "x" * 20
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK = "xoxb-" + "1234567890-" + "abcdefghij"
GOOGLE = "AIza" + "S" * 35
JWT = (
    "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU"
)
PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"
    "-----END OPENSSH PRIVATE KEY-----"
)


@pytest.mark.parametrize("secret", [OPENAI, ANTHROPIC, GITHUB, GITHUB_PAT, AWS, SLACK, GOOGLE, JWT])
def test_known_token_formats_are_masked(secret):
    text = f"use {secret} for the call"
    assert redact(text) == f"use {REDACTED} for the call"


def test_private_key_blocks_are_masked_whole():
    text = f"key:\n{PEM}\ndone"
    assert redact(text) == f"key:\n{REDACTED}\ndone"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "Authorization: Bearer [redacted]",
        ),
        (
            "https://alice:hunter2pass@git.example.com/repo",
            "https://alice:[redacted]@git.example.com/repo",
        ),
        ('api_key = "q8w7e6r5t4y3u2i1"', 'api_key = "[redacted]"'),
        ("password: 'correct-horse-battery'", "password: '[redacted]'"),
        ("DB_PASSWORD=s3cr3tvalue99", "DB_PASSWORD=[redacted]"),
        ("export GITHUB_TOKEN=abcdef1234567890", "export GITHUB_TOKEN=[redacted]"),
    ],
)
def test_contextual_secrets_keep_their_name(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "code",
    [
        "token = get_token()",
        "password_hash = hash(password)",
        "def refresh_secret(self, api_key):",
        'secret = os.environ["APP_SECRET"]',
        "pip install scikit-learn  # sk-learn",
        'TOKEN_URL = "https://example.com/token"',
        "key = 'short'",
        "AKIA is the AWS key prefix",
    ],
)
def test_ordinary_code_is_left_alone(code):
    assert redact(code) == code


def test_redact_data_walks_nested_structures_and_keeps_types():
    data = {
        "step": {"tool": "command", "args": {"cmd": f"curl -H 'Authorization: Bearer {OPENAI}'"}},
        "result": {"stdout": f"key={GITHUB}\n", "returncode": 0, "lines": [GITHUB, 3, None]},
    }
    clean = redact_data(data)
    text = json.dumps(clean)
    assert OPENAI not in text and GITHUB not in text
    assert clean["result"]["returncode"] == 0
    assert clean["result"]["lines"][1:] == [3, None]
    assert data["result"]["stdout"] == f"key={GITHUB}\n"  # input not modified


# --- where it applies -------------------------------------------------------


def test_saved_sessions_are_masked_but_the_live_history_is_not():
    from codebud import paths, sessions

    history = [{"role": "user", "content": f"deploy with {OPENAI}"}]
    session = sessions.Session.create(name="masking")
    session.save(history, model="m", mode="ask")
    saved = (paths.data_dir() / "sessions" / f"{session.id}.json").read_text()
    session.close()
    assert OPENAI not in saved and REDACTED in saved
    assert history[0]["content"] == f"deploy with {OPENAI}"


def test_step_history_is_masked():
    from codebud import paths
    from codebud.memory import MemoryStore

    MemoryStore().add_snapshot(
        {"tool": "command", "args": {"cmd": f"echo {AWS}"}}, {"stdout": AWS, "returncode": 0}
    )
    stored = (paths.data_dir() / "memory" / "entries.jsonl").read_text()
    assert AWS not in stored and REDACTED in stored


def test_masking_can_be_turned_off(monkeypatch):
    from codebud import paths
    from codebud.memory import MemoryStore

    monkeypatch.setenv("CODEBUD_MASK_SECRETS", "0")
    MemoryStore().add_snapshot({"tool": "command", "args": {"cmd": "x"}}, {"stdout": AWS})
    assert AWS in (paths.data_dir() / "memory" / "entries.jsonl").read_text()


def test_a_project_config_cannot_turn_masking_off(tmp_path, monkeypatch):
    from codebud import settings

    monkeypatch.delenv("CODEBUD_MASK_SECRETS", raising=False)
    monkeypatch.setattr(settings, "_warned", set())
    (tmp_path / ".codebud").mkdir()
    (tmp_path / ".codebud" / "config.toml").write_text("mask_secrets = false\n")
    monkeypatch.chdir(tmp_path)
    assert settings.resolve()["CODEBUD_MASK_SECRETS"] == (True, "default")


# --- formats added after the security review (M4, 2026-10-29) ---------------

AGE_KEY = "AGE-SECRET-KEY-1" + "QQPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" + "QPZRY9X8GF2TVDW0S3JN54KH"
STRIPE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
STRIPE_RK = "rk_" + "test_" + "4eC39HqLyjWDarjtT1zdp7dc"
GITLAB = "glpat-" + "x" * 20
NPM = "npm_" + "a" * 36
HUGGINGFACE = "hf_" + "b" * 34
SENDGRID = "SG." + "c" * 22 + "." + "d" * 43
GOOGLE_OAUTH = "ya29." + "e" * 40
SLACK_HOOK = "https://hooks.slack.com/services/" + "T000/B000/" + "XXXXXXXX"


@pytest.mark.parametrize(
    "secret",
    [AGE_KEY, STRIPE, STRIPE_RK, GITLAB, NPM, HUGGINGFACE, SENDGRID, GOOGLE_OAUTH, SLACK_HOOK],
)
def test_more_token_formats_are_masked(secret):
    assert redact(f"use {secret} now") == f"use {REDACTED} now"


@pytest.mark.parametrize(
    "text, secret",
    [
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("password=hunter2hunter2", "hunter2hunter2"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("  db_password: s3cr3t-value-x", "s3cr3t-value-x"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("Authorization: token ghx0123456789abcdef", "ghx0123456789abcdef"),
        ("mysql -u root -pSuperS3cret db", "SuperS3cret"),
        ("curl -u alice:Pa55word https://x.example", "Pa55word"),
        ("postgres://user:p@ss@host:5432/db", "p@ss"),
        ("Endpoint=https;AccountName=x;AccountKey=abcdefghijklmnop123==;", "abcdefghijklmnop123"),
        ('{"auth": "dXNlcjpwYXNzd29yZDEyMw=="}', "dXNlcjpwYXNzd29yZDEyMw=="),
        ("db.example.com:5432:mydb:alice:Pa55w0rdLong", "Pa55w0rdLong"),
        ('secret = "contains spaces here"', "contains spaces here"),
        ("mytool --password=SuperS3cretVal", "SuperS3cretVal"),
    ],
)
def test_more_contextual_secrets_are_masked(text, secret):
    out = redact(text)
    assert secret not in out and REDACTED in out


def test_a_url_password_with_an_at_sign_is_masked_whole():
    assert redact("postgres://user:p@ss@host:5432/db") == "postgres://user:[redacted]@host:5432/db"


def test_pgp_and_truncated_private_keys_are_masked():
    pgp = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0x\n-----END PGP PRIVATE KEY BLOCK-----"
    assert redact(f"a\n{pgp}\nb") == f"a\n{REDACTED}\nb"
    head = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nzzzz"  # no END line
    assert redact(f"key:\n{head}") == f"key:\n{REDACTED}"


@pytest.mark.parametrize(
    "line",
    [
        "password = self.password",
        "max_tokens = 100000000",
        "secret_enabled = true",
        "token_file = /home/me/.config/tok",
        "password: ${DB_PASSWORD}",
        "api_key: {{ vault_api_key }}",
        "basic functionality works fine here",
        "The authorization step is described below",
        "pwd = os.getcwd()",
        "tokenizer = AutoTokenizer.from_pretrained(name)",
        "PWD=/home/desadm/Projects/codebud",
        "passenger_count = 12345678",
        "user@example.com wrote this",
    ],
)
def test_lines_that_only_look_secret_are_left_alone(line):
    assert redact(line) == line
