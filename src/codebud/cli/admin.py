"""
codebud/cli/admin.py

The housekeeping subcommands: doctor, sessions, index, skill, history,
models, config and version.
"""

import json
import os
import sys

from codebud.cli import common


def cmd_doctor(_args):
    from codebud.cli.doctor import run_doctor

    sys.exit(run_doctor())


def _delete_sessions(args) -> None:
    """codebud sessions --delete REF / --purge [--older-than DAYS]."""
    from codebud import sessions

    if args.delete:
        try:
            ids = [sessions.resolve(args.delete)]
        except sessions.SessionNotFoundError as exc:
            print(f"codebud: {exc}", file=sys.stderr)
            sys.exit(1)
        if any(s["id"] == ids[0] and s["in_use"] for s in sessions.list_sessions()):
            print(f"codebud: session {ids[0]} is open in a codebud chat.", file=sys.stderr)
            sys.exit(1)
        question = f"Delete session {args.delete}?"
    else:
        candidates, in_use = sessions.deletion_candidates(args.older_than)
        for session_id in in_use:
            print(f"(skipping {session_id}: open in a codebud chat)")
        ids = [c["id"] for c in candidates]
        # Sessions that cannot be read (corrupt, undecryptable): a purge of
        # everything takes them too; a purge by age cannot know theirs.
        unreadable = sessions.unreadable_sessions()
        if args.older_than is None:
            ids += [i for i in unreadable if not sessions._in_use(i)]
        else:
            for session_id in unreadable:
                print(f"(keeping {session_id}: it cannot be read, so its age is unknown)")
        if not ids:
            print("Nothing to delete.")
            return
        age = f" not used for {args.older_than}+ days" if args.older_than is not None else ""
        question = f"Delete {common.plural(len(ids), 'session')}{age}?"
    files = [path for session_id in ids for path in sessions.session_files(session_id)]
    if not common.confirm_deletion(files, question, args.yes):
        print("Kept.")
        sys.exit(1)
    try:
        sessions.delete(ids)
    except sessions.SessionInUseError as exc:  # opened between listing and deleting
        print(f"codebud: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted {common.plural(len(ids), 'session')}.")


def apply_retention() -> None:
    """Delete sessions older than session_retention_days, if the user set it.

    0 (the default) keeps sessions forever (decision D12e). Sessions open
    in a chat -- including one being resumed -- are never deleted.
    """
    from codebud import sessions
    from codebud.cli.display import print_status, print_warning

    raw = os.environ.get("CODEBUD_SESSION_RETENTION_DAYS", "0")
    try:
        days = int(raw)
    except ValueError:
        print_warning(f"Ignoring CODEBUD_SESSION_RETENTION_DAYS={raw!r}: not a number of days.")
        return
    if days <= 0:
        return
    candidates, _in_use = sessions.deletion_candidates(days)
    if not candidates:
        return
    ids = [c["id"] for c in candidates]
    sessions.delete(ids)
    print_status(
        f"Deleted {common.plural(len(ids), 'session')} not used for {days}+ days "
        f"(session_retention_days): {', '.join(ids)}"
    )


def cmd_sessions(args):
    """List saved chat sessions, most recently updated first."""
    from codebud import sessions
    from codebud.cli.display import _OUT_BOLD, _OUT_DIM, _OUT_GREEN, _OUT_RESET

    if args.delete or args.purge:
        _delete_sessions(args)
        return
    found = sessions.list_sessions()
    shown = found[: args.limit] if args.limit else found

    if args.json:
        print(json.dumps(shown, indent=2))
        return
    if not shown:
        print(f"{_OUT_DIM}No saved sessions yet. Start one with: codebud chat{_OUT_RESET}")
        return

    for s in shown:
        label = f"{s['id']}" + (f"  {_OUT_BOLD}{s['name']}{_OUT_RESET}" if s["name"] else "")
        busy = f"  {_OUT_GREEN}(open){_OUT_RESET}" if s["in_use"] else ""
        print(f"{label}{busy}")
        print(
            f"  {_OUT_DIM}updated {s['updated'][:19]}  ·  {s['turns']} entries  ·  "
            f"{s['model'] or '?'}{_OUT_RESET}"
        )
        if s["first_request"]:
            print(f"  {s['first_request'][:100]}")
    print(f"\n{_OUT_DIM}Resume with: codebud chat --resume <id or name>{_OUT_RESET}")


def cmd_index(args):
    """Build, refresh, inspect or delete the project index."""
    from codebud.cli.display import _OUT_BOLD, _OUT_DIM, _OUT_RESET
    from codebud.rag import indexer
    from codebud.rag.store import ModelMismatch
    from codebud.tools.embeddings import EmbeddingUnavailable

    root = indexer.project_root(args.path)
    from codebud import crypto
    from codebud.rag.store import is_sealed

    if is_sealed(root) and not (args.drop or args.rebuild):
        crypto.ensure_unlocked()  # reading or refreshing decrypts the index

    if args.status:
        status = indexer.index_status(root)
        if status is None:
            print(f"No index for {root}. Create one with: codebud index")
            return
        print(f"{_OUT_BOLD}Index of {status['root']}{_OUT_RESET}")
        print(f"  {_OUT_DIM}model         {_OUT_RESET} {status['model']} ({status['dim']}-dim)")
        print(
            f"  {_OUT_DIM}contents      {_OUT_RESET} "
            f"{common.plural(status['files'], 'file')}, {common.plural(status['chunks'], 'chunk')}"
        )
        keyword = "yes" if status["keyword_search"] else "no (SQLite lacks FTS5)"
        print(f"  {_OUT_DIM}keyword search{_OUT_RESET} {keyword}")
        print(f"  {_OUT_DIM}updated       {_OUT_RESET} {status['updated']}")
        print(f"  {_OUT_DIM}file          {_OUT_RESET} {status['path']}")
        encrypted = "yes (age)" if status["encrypted"] else "no"
        print(f"  {_OUT_DIM}encrypted     {_OUT_RESET} {encrypted}")
        changes = (status["changed"], status["new"], status["deleted"])
        if any(changes):
            print(
                f"  {_OUT_DIM}since then    {_OUT_RESET} "
                "{} changed, {} new, {} deleted -- refresh with: codebud index".format(*changes)
            )
        else:
            print(f"  {_OUT_DIM}since then    {_OUT_RESET} no changes")
        return

    if args.drop:
        directory = indexer.index_dir(root)
        if not directory.is_dir():
            print(f"No index for {root}.")
            return
        files = sorted(str(p) for p in directory.iterdir())
        if not common.confirm_deletion(files, f"Delete the index in {directory}?", args.yes):
            print("Kept.")
            sys.exit(1)
        removed = indexer.drop_index(root)
        print(f"Deleted the index ({common.plural(len(removed), 'file')}).")
        return

    show = not common.no_progress(args)

    def progress(number: int, total: int, path: str) -> None:
        if show:
            sys.stderr.write(f"\r\033[K  [{number}/{total}] {path}"[:200])
            sys.stderr.flush()

    try:
        report = indexer.index_project(root, rebuild=args.rebuild, progress=progress)
    except (EmbeddingUnavailable, ModelMismatch) as exc:
        if show:
            sys.stderr.write("\r\033[K")
        print(f"codebud: {exc}", file=sys.stderr)
        sys.exit(1)
    if show:
        sys.stderr.write("\r\033[K")

    if report.indexed or report.removed:
        print(
            f"Indexed {common.plural(report.indexed, 'file')} "
            f"({common.plural(report.chunks, 'chunk')}) in {report.seconds:.1f} s; "
            f"{report.unchanged} unchanged, {report.removed} removed."
        )
    else:
        print(f"Index is up to date ({common.plural(report.unchanged, 'file')}).")
    if report.skipped:
        skipped = ", ".join(f"{n} {reason}" for reason, n in sorted(report.skipped.items()))
        print(f"{_OUT_DIM}Skipped: {skipped}{_OUT_RESET}")


def cmd_skill(args):
    """Print codebud's SKILL.md, or install it where an agent finds skills."""
    from codebud import skill

    if args.skill_action == "show":
        try:
            kind = skill.kind_for_show(args.agent)
        except skill.SkillError as exc:
            print(f"codebud: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(skill.skill_text(kind))
        return
    try:
        directory = args.dir or skill.target_dir(args.agent)
        path = skill.install(directory, force=args.force)
    except skill.SkillError as exc:
        print(f"codebud: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Installed codebud's skill: {path}")


def cmd_history(args):
    """Show past step executions recorded by MemoryStore (in the data directory).

    This is persisted, cross-session execution history (MemoryStore), distinct
    from the in-session conversation history shown by /history in `chat`.
    """
    from codebud import memory
    from codebud.cli.display import _OUT_CYAN, _OUT_DIM, _OUT_GREEN, _OUT_RED, _OUT_RESET
    from codebud.memory import MemoryStore

    if args.purge:
        files = memory.history_files()
        if not files:
            print("Nothing to delete.")
            return
        if not common.confirm_deletion(files, "Delete the step history?", args.yes):
            print("Kept.")
            sys.exit(1)
        memory.purge()
        print("Deleted the step history.")
        return

    store = MemoryStore()
    entries = store.history[-args.limit :] if args.limit else store.history

    if args.json:
        print(json.dumps(entries, indent=2))
        return

    if not entries:
        print(f'{_OUT_DIM}No history yet. Run `codebud run "..."` to record some.{_OUT_RESET}')
        return

    for i, entry in enumerate(entries, 1):
        ts = entry.get("timestamp", "?")
        data = entry.get("data", {})
        step = data.get("step", {})
        result = data.get("result", {})
        tool = step.get("tool", "?")
        desc = step.get("description", "")
        rc = result.get("returncode")
        status_text = "ok" if rc == 0 else f"exit {rc}" if rc is not None else "?"
        status_color = _OUT_GREEN if rc == 0 else _OUT_RED if rc is not None else ""
        status = f"{status_color}{status_text:<8}{_OUT_RESET}"
        print(
            f"{_OUT_CYAN}[{i}]{_OUT_RESET} {_OUT_DIM}{ts}{_OUT_RESET}  {tool:<16} {status} {desc}"
        )


def cmd_models(_args):
    import requests as _req

    from codebud.cli.display import _OUT_BOLD, _OUT_GREEN, _OUT_RESET
    from codebud.llm_stub import ollama_proxies

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    selected = os.environ.get("OLLAMA_MODEL", "")
    try:
        r = _req.get(f"{base}/api/tags", timeout=5, proxies=ollama_proxies(base))
        r.raise_for_status()
        models = r.json().get("models", [])
    except Exception as exc:
        print(f"Cannot reach Ollama: {exc}", file=sys.stderr)
        print("Start it with: sudo systemctl start ollama", file=sys.stderr)
        sys.exit(1)

    if not models:
        print("No models downloaded. Pull one with:")
        print("  ollama pull qwen2.5-coder:3b-instruct-q4_K_M")
        return

    print(f"{_OUT_BOLD}{'NAME':<45} {'SIZE':>8}  {'SELECTED'}{_OUT_RESET}")
    for m in models:
        name = m.get("name", "")
        size_bytes = m.get("size", 0)
        size_gb = size_bytes / 1024**3
        marker = f"{_OUT_GREEN}<-- active{_OUT_RESET}" if name == selected else ""
        print(f"  {name:<43} {size_gb:>6.1f}G  {marker}")


def cmd_config(_args):
    from codebud import settings
    from codebud.cli.display import _OUT_BOLD, _OUT_DIM, _OUT_RESET

    print(f"{_OUT_BOLD}Codebud resolved configuration:{_OUT_RESET}")
    for env, (value, source) in settings.resolve().items():
        shown = settings.display_value(env, value)
        print(f"  {_OUT_DIM}{env:<25}{_OUT_RESET} = {shown:<34} {_OUT_DIM}({source}){_OUT_RESET}")

    from codebud import policy
    from codebud.paths import data_dir, project_policy_file, user_policy_file

    print(f"\n{_OUT_BOLD}Files:{_OUT_RESET}")
    project_config = settings.project_config_file()
    print(f"  {_OUT_DIM}{'config file':<24}{_OUT_RESET} = {settings.user_config_file()}")
    if project_config is not None:
        print(f"  {_OUT_DIM}{'project config file':<24}{_OUT_RESET} = {project_config}")
    print(f"  {_OUT_DIM}{'data directory':<24}{_OUT_RESET} = {data_dir()}")
    for name in policy.POLICY_FILES:
        print(f"  {_OUT_DIM}{name:<24}{_OUT_RESET} = {user_policy_file(name)}")
        project_file = project_policy_file(name)
        if project_file is not None:
            print(f"  {_OUT_DIM}{'  adjusted by':<24}{_OUT_RESET} = {project_file}")
    effective = policy.effective()
    if effective.project is not None:
        state = "trusted" if effective.trusted else "not trusted: only its narrowing applies"
        print(f"  {_OUT_DIM}{'project policy':<24}{_OUT_RESET} = {state}")


def cmd_trust(args):
    """Show what this project's .codebud/ policy changes; trust or revoke it."""
    from codebud import policy

    effective = policy.effective()
    if effective.project is None:
        print("This project has no .codebud/ command policy; yours applies as it is.")
        return
    if args.revoke:
        if policy.revoke(effective.project):
            print(f"No longer trusting {effective.project}: only its narrowing applies.")
        else:
            print(f"{effective.project} was not trusted.")
        return
    if not effective.widening:
        print(f"{effective.project} only narrows your policy; nothing to trust.")
        return
    state = "trusted" if effective.trusted else "not trusted (only its narrowing applies)"
    print(f"{effective.project}: {state}. It widens your policy with:")
    common.show_widening(effective)
    if effective.trusted:
        return
    if not args.yes:
        if not sys.stdin.isatty():
            print("codebud: not trusting without confirmation; re-run with --yes.", file=sys.stderr)
            sys.exit(1)
        if not input("Trust this project's policy? [y/N] ").strip().lower().startswith("y"):
            print("Not trusted.")
            sys.exit(1)
    policy.trust(effective.project)
    print("Trusted. Codebud asks again if its policy files change.")


def cmd_profiles(_args):
    from codebud import profiles
    from codebud.cli.display import _OUT_BOLD, _OUT_DIM, _OUT_RESET

    current = profiles.active().name
    for name in profiles.available():
        try:
            profile = profiles.get(name)
        except profiles.ProfileError as exc:
            print(f"  {name:<14} {_OUT_DIM}unusable: {exc}{_OUT_RESET}")
            continue
        mark = f"{_OUT_BOLD}*{_OUT_RESET}" if name == current else " "
        print(f"{mark} {name:<14} {profile.summary():<28} {profile.description}")
        print(f"  {'':<14} {_OUT_DIM}{profile.source}{_OUT_RESET}")
    print(f"\nYour own profiles: {profiles.profile_dir()}/<name>.toml")


def cmd_version(_args):
    import requests as _req

    from codebud.llm_stub import ollama_proxies

    model = os.environ.get("OLLAMA_MODEL", "(not set)")
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    # Try to get Ollama version
    try:
        r = _req.get(f"{base}/api/version", timeout=3, proxies=ollama_proxies(base))
        ollama_ver = r.json().get("version", "unknown") if r.ok else "unreachable"
    except Exception:
        ollama_ver = "unreachable"

    from codebud import __version__

    print(f"codebud  {__version__}")
    print(f"ollama   {ollama_ver}")
    print(f"model    {model}")
    print(f"python   {sys.version.split()[0]}")
