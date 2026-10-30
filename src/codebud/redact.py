"""
codebud/redact.py

Mask secrets before codebud stores anything (phase 8, Part B3; decision
D12d: on by default, CODEBUD_MASK_SECRETS=0 turns it off).

redact(text) replaces, with "[redacted]":

- well-known token formats: OpenAI/Anthropic "sk-...", Stripe
  "sk_live_..."/"rk_live_...", GitHub "ghp_..." and "github_pat_...",
  GitLab "glpat-...", npm "npm_...", Hugging Face "hf_...", AWS
  "AKIA...", Slack "xox..." and webhook URLs, Google "AIza..." and OAuth
  "ya29....", SendGrid "SG....", JWTs, and codebud's own age keys
  "AGE-SECRET-KEY-...";
- private-key blocks (PEM, OpenSSH, PGP), whole -- and to the end of the
  text when the END line is missing (a key shown with `head`);
- the credential in "Authorization:" headers and after "Bearer", the
  password in "scheme://user:password@host" (passwords containing "@"
  included), "--password=..." style options, `mysql -pSECRET`,
  `curl -u user:pass`, Azure "AccountKey=", Docker config "auth" values
  and .pgpass lines;
- the value assigned to a secret-looking name: quoted literals in code,
  YAML and JSON (api_key = "...", password: '...'), and unquoted values
  in INI/YAML/.env lines (aws_secret_access_key = ..., password: ...).

Names stay, so the text still reads. Ordinary code such as
`token = get_token()` or `password = self.password` is left alone: a
secret-looking name alone is not enough, its value must be a literal --
not a call, an attribute, a number, a boolean, a ${...}/{{...}} reference
or a path. Values shorter than 8 characters are not masked (too many
ordinary words would be). Only what is written to disk is masked
(sessions, step history); prompts sent to the model are not.
"""

import re
from typing import Any

REDACTED = "[redacted]"

_NAME = (
    r"(?:pass(?:word|wd|phrase)|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credentials?|auth[_-]?key)"
)

# An unquoted value that is a literal, not code: no call, attribute path,
# number, boolean, ${...}/{{...}}/<...>/%(...) reference or path.
_NOT_CODE = (
    r"(?![$<{%/~`]|\.\.?/"
    r"|\d+[ \t]*(?:#.*)?$"
    r"|(?:true|false|null|none|yes|no|on|off)[ \t]*(?:#.*)?$"
    r"|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+[ \t]*(?:#.*)?$)"
)

# (pattern, replacement); applied in order, whole blocks and tokens first.
_RULES: list[tuple[re.Pattern, str]] = [
    # Private-key blocks (PEM, OpenSSH, PGP), to END -- or to the end of the
    # text when a truncated listing has no END line.
    (
        re.compile(
            r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----.*?(?:-----END \1-----|\Z)",
            re.DOTALL,
        ),
        REDACTED,
    ),
    (re.compile(r"\bAGE-SECRET-KEY-(?:PQ-)?1[0-9A-Z]{20,}"), REDACTED),
    (re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), REDACTED),
    # Authorization: <scheme> <credential> (Basic, Token, Digest, ...).
    (
        re.compile(r"(?i)(\bauthorization\s*:\s*(?:[a-z]+\s+)?)(?!\[redacted\])[^\s\"']{8,}"),
        rf"\g<1>{REDACTED}",
    ),
    (re.compile(r"(?i)(\bbearer\s+)(?!\[redacted\])[A-Za-z0-9._~+/=-]{20,}"), rf"\g<1>{REDACTED}"),
    # scheme://user:password@host -- up to the last "@" before the host, so
    # a password containing "@" is masked whole.
    (
        re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/]+(@)(?=[^\s@/]+(?:[/:?#\s]|$))"),
        rf"\g<1>{REDACTED}\g<2>",
    ),
    # --password=... / --token ... style command-line options.
    (
        re.compile(
            r"(?i)(--(?:password|passwd|passphrase|token|secret|api-key|access-key)[= ])"
            r"(?!\[redacted\])(?!-)\S+"
        ),
        rf"\g<1>{REDACTED}",
    ),
    # mysql -pSECRET (the password is glued to -p).
    (
        re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]*?\s-p)(?!\s)\S+"),
        rf"\g<1>{REDACTED}",
    ),
    # curl -u user:password
    (
        re.compile(r"(\bcurl\b[^\n]*?\s(?:-u|--user)\s+[^\s:]+:)\S+"),
        rf"\g<1>{REDACTED}",
    ),
    (re.compile(r"(?i)(\b(?:AccountKey|SharedAccessKey)=)[^;\s\"']{8,}"), rf"\g<1>{REDACTED}"),
    # Docker config.json: "auth": "base64(user:pass)"
    (re.compile(r'("auth"\s*:\s*")[A-Za-z0-9+/=]{8,}(")'), rf"\g<1>{REDACTED}\g<2>"),
    # .pgpass: host:port:database:user:password
    (
        re.compile(r"(?m)^([^:\s#]+:(?:\d+|\*):[^:\s]+:[^:\s]+:)(\S+)$"),
        rf"\g<1>{REDACTED}",
    ),
    # name = "literal" / name: 'literal' (code, YAML, JSON); 8+ characters.
    (
        re.compile(rf"""(?i)(\b\w*{_NAME}\w*["']?\s*[:=]\s*)(["'])(?!https?://)[^"'\n]{{8,}}?\2"""),
        rf"\g<1>\g<2>{REDACTED}\g<2>",
    ),
    # name = value / name: value, unquoted, at the start of a line (INI,
    # YAML, .env, simple assignments); any case.
    (
        re.compile(
            rf"""(?im)^([ \t]*(?:export[ \t]+)?[\w.-]*{_NAME}[\w.-]*[ \t]*[:=][ \t]*)"""
            rf"""{_NOT_CODE}(?!\[redacted\])([^\s#"'`()\[\]{{}},;]{{8,}})(?=[ \t]*(?:#.*)?$)"""
        ),
        rf"\g<1>{REDACTED}",
    ),
    # NAME=value, environment style (UPPER_CASE names), anywhere on a line.
    (
        re.compile(
            r"(\b[A-Z0-9_]*(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_KEY|ACCESS_KEY"
            r"|PRIVATE_KEY)[A-Z0-9_]*=)(?![\"'$])(?!\[redacted\])[^\s\"']{8,}"
        ),
        rf"\g<1>{REDACTED}",
    ),
]


def enabled() -> bool:
    """Whether masking is on: CODEBUD_MASK_SECRETS, default on."""
    from codebud import settings

    # A value that is not a clear "no" keeps masking on (the safe side).
    return settings.flag("CODEBUD_MASK_SECRETS", True, unknown=True)


def redact(text: str) -> str:
    """``text`` with every secret it contains replaced by [redacted]."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_data(data: Any) -> Any:
    """A copy of ``data`` (dicts, lists, strings) with every string redacted."""
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        return {key: redact_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    return data


def for_storage(data: Any) -> Any:
    """``data`` as it should be written to disk: redacted unless masking is off."""
    return redact_data(data) if enabled() else data
