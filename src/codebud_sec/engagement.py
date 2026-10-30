"""
codebud_sec/engagement.py

The engagement file (phase 10; D14d settled 2026-10-28). A security
engagement is authorized, scoped work: incident response on hosts the
operator owns, or a penetration test the operator is contracted to run.
The engagement file records that authorization so every action can be
checked against it and logged with it.

It is a TOML file whose path is passed on the command line
(``--engagement acme-2026.toml``). It names the client and ticket, the
in-scope and out-of-scope targets (CIDRs, hosts, domains), and the time
window the work is authorized for.

Scope handling is **warn, do not block** (D14): a target outside the scope
or the window raises a clear, logged warning and asks for confirmation,
but is allowed. The engagement file and every action are recorded
regardless, so the record is complete even when the operator proceeds.
Nothing here grants access; it only states, and helps keep to, what was
already authorized.
"""

import ipaddress
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

_STRING_KEYS = ("client", "ticket", "operator", "notes")
_LIST_KEYS = ("in_scope", "out_of_scope")
_TIME_KEYS = ("starts", "ends")
_KEYS = (*_STRING_KEYS, *_LIST_KEYS, *_TIME_KEYS)


class EngagementError(Exception):
    """An engagement file that does not exist or cannot be parsed."""


@dataclass(frozen=True)
class Target:
    """One scope entry: a CIDR network, or a host / domain name."""

    raw: str
    network: ipaddress._BaseNetwork | None = None  # set when raw is a CIDR/IP

    @classmethod
    def parse(cls, raw: str) -> "Target":
        # A trailing dot is a fully qualified name ("prod.example.com."):
        # strip it as matches() does, or the entry silently matches nothing.
        text = raw.strip().lower().rstrip(".")
        try:
            return cls(text, ipaddress.ip_network(text, strict=False))
        except ValueError:
            return cls(text, None)  # a host or domain name

    def matches(self, target: str) -> bool:
        """Whether ``target`` (an IP, host or domain) falls under this entry."""
        text = target.strip().lower().rstrip(".")
        if self.network is not None:
            try:
                return ipaddress.ip_address(text) in self.network
            except ValueError:
                return False  # a name is never inside a CIDR
        if self.network is None:
            try:
                ipaddress.ip_address(text)
                return False  # an IP is never matched by a name entry
            except ValueError:
                pass
        # Name entry: exact, or a subdomain of it (example.com covers a.example.com).
        return text == self.raw or text.endswith("." + self.raw)


@dataclass(frozen=True)
class Engagement:
    path: Path
    client: str = ""
    ticket: str = ""
    operator: str = ""
    notes: str = ""
    in_scope: tuple[Target, ...] = ()
    out_of_scope: tuple[Target, ...] = ()
    starts: datetime | None = None
    ends: datetime | None = None

    def header(self) -> str:
        """A one-line summary for the evidence log header."""
        who = self.client or "(no client)"
        ref = f" {self.ticket}" if self.ticket else ""
        return f"{who}{ref} ({len(self.in_scope)} in scope)"

    def in_window(self, when: datetime | None = None) -> bool:
        now = when or datetime.now(timezone.utc)
        if self.starts and now < self.starts:
            return False
        if self.ends and now > self.ends:
            return False
        return True

    def covers(self, target: str) -> bool:
        """Whether ``target`` is in scope: listed in, and not excluded."""
        if any(t.matches(target) for t in self.out_of_scope):
            return False
        return any(t.matches(target) for t in self.in_scope)

    def out_of_scope_reason(self, target: str, when: datetime | None = None) -> str | None:
        """Why acting on ``target`` is outside the engagement, or None.

        Warn-only: the caller shows this and confirms; it does not block.
        """
        if not self.covers(target):
            return f"{target} is not in the engagement scope ({self.path.name})"
        if not self.in_window(when):
            return f"the engagement window ({self.path.name}) is not open now"
        return None


def _targets(values: list, key: str, path: Path) -> tuple[Target, ...]:
    if not all(isinstance(v, str) for v in values):
        raise EngagementError(f"{path}: every entry of '{key}' must be a string")
    return tuple(Target.parse(v) for v in values)


def _time(value, key: str, path: Path) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise EngagementError(f"{path}: '{key}' is not a valid date/time: {exc}") from exc
    else:
        raise EngagementError(f"{path}: '{key}' must be a date/time")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load(path: str | Path) -> Engagement:
    """Read and validate the engagement file at ``path``."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text())
    except OSError as exc:
        raise EngagementError(f"cannot read engagement file {path}: {exc}") from exc
    except ValueError as exc:  # TOMLDecodeError is a ValueError
        raise EngagementError(f"engagement file {path} is not valid TOML: {exc}") from exc

    unknown = [k for k in data if k not in _KEYS]
    if unknown:
        raise EngagementError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
    for key in _STRING_KEYS:
        if key in data and not isinstance(data[key], str):
            raise EngagementError(f"{path}: '{key}' must be a string")
    for key in _LIST_KEYS:
        if key in data and not isinstance(data[key], list):
            raise EngagementError(f"{path}: '{key}' must be a list of strings")
    if not data.get("in_scope"):
        raise EngagementError(f"{path}: 'in_scope' is required and must list at least one target")

    return Engagement(
        path=path,
        client=data.get("client", ""),
        ticket=data.get("ticket", ""),
        operator=data.get("operator", ""),
        notes=data.get("notes", ""),
        in_scope=_targets(data["in_scope"], "in_scope", path),
        out_of_scope=_targets(data.get("out_of_scope", []), "out_of_scope", path),
        starts=_time(data["starts"], "starts", path) if "starts" in data else None,
        ends=_time(data["ends"], "ends", path) if "ends" in data else None,
    )
