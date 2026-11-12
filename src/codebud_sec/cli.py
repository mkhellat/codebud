"""
codebud_sec/cli.py

The codebud-sec command (phase 10, step 5 plan, PR 7; D14a): codebud
for authorized security work, with an engagement open.

    codebud-sec chat|run|plan --engagement FILE [--evidence LOG] [--profile NAME] ...
    codebud-sec evidence add FILE... --engagement FILE [-m NOTE]
    codebud-sec evidence verify|show|close --engagement FILE
    codebud-sec pin [...]          (codebud integrity pin)

chat, run and plan open the engagement and then hand over to codebud
itself (codebud.cli.main) with a security profile -- security-pentest
unless --profile names another. While it runs:

- the engagement's scope guard and network fence are active
  (scope.ScopeGuard): every network target is judged, scanning steps
  are fenced to their targets, and a profile with engagement = true
  runs commands only now;
- every step is recorded in the engagement's evidence log before it runs
  and after it ends, refused steps too (codebud.safety step recorders);
  a step that cannot be recorded does not run. The log is created
  encrypted in codebud's data directory, which steps never see
  (evidence/<name>-<id>.jsonl), unless --evidence names another -- never
  one inside the project, which steps may write. Later sessions reopen
  it: its whole chain is checked (the key is unlocked for that), and the
  engagement file must be the one the log was opened with.
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

from codebud import crypto

from . import evidence
from .engagement import EngagementError, load
from .scope import ScopeGuard

PROG = "codebud-sec"
DEFAULT_PROFILE = "security-pentest"
SESSION_COMMANDS = ("chat", "run", "plan")
# Longer text in a step or its output is kept as its SHA-256, size and head.
_TEXT_LIMIT = 64 * 1024


def _fit(value):
    """``value`` for the log: long text as its hash, size and beginning."""
    if isinstance(value, str) and len(value) > _TEXT_LIMIT:
        raw = value.encode("utf-8", "replace")
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "head": value[: _TEXT_LIMIT // 4],
        }
    if isinstance(value, dict):
        return {k: _fit(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fit(v) for v in value]
    return value


class EvidenceRecorder:
    """A codebud.safety step recorder writing to an evidence log."""

    def __init__(self, log: evidence.EvidenceLog):
        self.log = log

    def __call__(self, event: str, step: dict, result: dict | None) -> None:
        data = {
            "event": event,
            "step": _fit({k: step.get(k) for k in ("id", "tool", "description", "args")}),
        }
        if result is not None:
            data["result"] = _fit({k: v for k, v in result.items() if k != "metadata"})
        self.log.append("warning" if event == "refused" else "step", data)


def evidence_path(engagement_file: Path, given: str | None) -> Path:
    """The evidence log: ``given``, or one per engagement file in codebud's
    data directory. EvidenceError for one inside the project, which steps
    may write."""
    from codebud import confine, paths

    if given:
        path = Path(given)
        real = Path(os.path.realpath(path))
        project = confine.project_root()
        if real == project or project in real.parents:
            raise evidence.EvidenceError(
                f"{given} is inside the project, where steps can write; "
                "keep the evidence log outside it"
            )
        return path
    real = os.path.realpath(engagement_file)
    tag = hashlib.sha256(real.encode()).hexdigest()[:12]
    return paths.data_dir() / "evidence" / f"{engagement_file.stem}-{tag}.jsonl"


def _open_log(engagement, path: Path) -> evidence.EvidenceLog:
    """Create the log, or reopen it: its whole chain checked, and the
    engagement file the same as when the log was opened."""
    if not path.exists():
        return evidence.EvidenceLog.create(path, engagement=engagement, encrypt=True)
    try:
        crypto.ensure_unlocked()
    except crypto.CryptoError as exc:
        raise evidence.EvidenceError(f"cannot check {path}: {exc}") from exc
    log = evidence.EvidenceLog.open(path)
    first = evidence.entries(path)[0].get("data", {})
    if first.get("engagement_sha256") != evidence.file_sha256(engagement.path):
        raise evidence.EvidenceError(
            f"{engagement.path} is not the engagement file {path} was opened with "
            "(it changed, or it is another one); a changed scope needs a new log "
            "(--evidence NEW)"
        )
    return log


def _fail(message: str) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return 2


def _session(args, rest: list[str]) -> int:
    from codebud.cli.main import main as run_codebud

    try:
        engagement = load(args.engagement)
    except EngagementError as exc:
        return _fail(str(exc))
    if not crypto.has_key():
        return _fail("the evidence log is encrypted: create a key first (codebud key init)")
    try:
        path = evidence_path(Path(args.engagement), args.evidence)
        log = _open_log(engagement, path)
    except evidence.EvidenceError as exc:
        return _fail(str(exc))
    if not engagement.in_window():
        print(
            f"{PROG}: the engagement window is not open now; active steps will be refused",
            file=sys.stderr,
        )
    print(f"{PROG}: engagement {engagement.header()}; evidence log {path}", file=sys.stderr)

    from codebud import safety

    guard, recorder = ScopeGuard(engagement), EvidenceRecorder(log)
    log.append("note", {"session": "start", "command": args.command, "profile": args.profile})
    guard.activate()
    safety.add_step_recorder(recorder)
    argv = ["codebud", "--profile", args.profile, args.command, *rest]
    saved, sys.argv = sys.argv, argv
    status = 0
    try:
        run_codebud()
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            status = exc.code or 0
        else:  # a message, which Python would have printed
            print(exc.code, file=sys.stderr)
            status = 1
    finally:
        sys.argv = saved
        safety.remove_step_recorder(recorder)
        guard.deactivate()
        log.append("note", {"session": "end", "status": status})
    return status


def _log_for(args) -> tuple[Path | None, object]:
    try:
        engagement = load(args.engagement)
        return evidence_path(Path(args.engagement), args.evidence), engagement
    except (EngagementError, evidence.EvidenceError) as exc:
        return None, str(exc)


def _unlock() -> str | None:
    try:
        crypto.ensure_unlocked()
    except crypto.CryptoError as exc:
        return str(exc)
    return None


def _evidence(args) -> int:
    path, engagement = _log_for(args)
    if path is None:
        return _fail(engagement)
    if args.action == "add":
        missing = [f for f in args.files if not Path(f).is_file()]
        if missing:
            return _fail(f"not a file: {', '.join(missing)}")
        try:
            log = _open_log(engagement, path)
            entry = log.append(
                "note", {"note": args.message or "", "added": args.files}, args.files
            )
        except evidence.EvidenceError as exc:
            return _fail(str(exc))
        for record in entry.get("artefacts", []):
            print(f"{record.get('sha256', '?')}  {record.get('path', '?')}")
        return 0
    if not path.exists():
        return _fail(f"no evidence log at {path}")
    if args.action == "close":
        try:
            evidence.EvidenceLog.open(path).close({"by": PROG})
        except evidence.EvidenceError as exc:
            return _fail(str(exc))
        print(f"{PROG}: closed {path}")
        return 0
    problem = _unlock()
    if problem:
        return _fail(problem)
    if args.action == "verify":
        result = evidence.verify(path, check_artefacts=args.files_too)
        if result.artefact_problems:
            return _fail("recorded files changed: " + "; ".join(result.artefact_problems))
        if not result.ok:
            return _fail(f"{path} does not verify: {result.error}")
        print(f"{path}: {result.entries} entries, chain intact; head {result.head}")
        print("Record the head hash outside the log: it shows any later change.")
        return 0
    for entry in evidence.entries(path):
        data = entry.get("data", {})
        what = data.get("event") or data.get("session") or data.get("note") or ""
        step = data.get("step", {}).get("args", {}) if isinstance(data.get("step"), dict) else {}
        detail = step.get("cmd") or step.get("path") or ""
        print(
            f"{entry.get('seq'):>5} {entry.get('time', '')} {entry.get('kind'):8} {what} {detail}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="codebud for authorized security work, with an engagement open."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def engagement_options(p):
        p.add_argument("--engagement", required=True, metavar="FILE", help="the engagement file")
        p.add_argument("--evidence", metavar="LOG", help="the evidence log (default: beside it)")

    for name in SESSION_COMMANDS:
        p = sub.add_parser(name, help=f"codebud {name} inside the engagement", add_help=False)
        engagement_options(p)
        p.add_argument("--profile", default=DEFAULT_PROFILE, help="a security profile")
    ev = sub.add_parser("evidence", help="the engagement's evidence log")
    ev_sub = ev.add_subparsers(dest="action", required=True)
    add = ev_sub.add_parser("add", help="record files (their SHA-256) in the log")
    add.add_argument("files", nargs="+")
    add.add_argument("-m", "--message", help="a note to go with them")
    engagement_options(add)
    for action, text in (
        ("verify", "check the hash chain"),
        ("show", "list the entries"),
        ("close", "end the log"),
    ):
        p = ev_sub.add_parser(action, help=text)
        engagement_options(p)
        if action == "verify":
            p.add_argument("--files", dest="files_too", action="store_true",
                           help="also check the recorded files' hashes")  # fmt: skip
    sub.add_parser("pin", help="codebud integrity pin", add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["pin"]:
        from codebud.cli.main import main as run_codebud

        saved, sys.argv = sys.argv, ["codebud", "integrity", "pin", *argv[1:]]
        try:
            run_codebud()
        finally:
            sys.argv = saved
        return 0
    args, rest = build_parser().parse_known_args(argv)
    if args.command in SESSION_COMMANDS:
        return _session(args, rest)
    if rest:
        return _fail(f"unknown arguments: {' '.join(rest)}")
    return _evidence(args)
