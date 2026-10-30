"""
codebud/rag/indexer.py

Build and refresh a project's index (phase 9, B4).

index_project() selects the project's files (rag.files), skips those whose
content hash is unchanged since the last run, chunks the rest
(rag.chunker), embeds the chunks in batches and stores them (rag.store);
files no longer in the project are removed. Each file is committed on its
own, so an interrupted run keeps what it finished and the next run picks
up the rest.

Every chunk is embedded with a first line naming its location
("path:start-end symbol"), so the file and symbol name count towards
retrieval as well as the code itself.
"""

import hashlib
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from codebud import gitsafe, redact
from codebud.rag.chunker import Chunk, chunk_file
from codebud.rag.files import candidate_files, select_files
from codebud.rag.store import IndexStore, index_dir
from codebud.tools.embeddings import active_model, embed_documents

Progress = Callable[[int, int, str], None]  # (file number, total, path)


@dataclass
class IndexReport:
    indexed: int = 0  # files (re-)embedded
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0  # chunks embedded in this run
    skipped: Counter = field(default_factory=Counter)  # reason -> files
    seconds: float = 0.0


def project_root(start: Path | str | None = None) -> Path:
    """The git work tree containing ``start`` (default: cwd), else ``start``."""
    start = Path(start or ".").resolve()
    try:
        out = subprocess.run(
            gitsafe.git_argv(["rev-parse", "--show-toplevel"], cwd=start),
            cwd=start,
            env=gitsafe.git_environment(),  # the repository runs nothing (gitsafe.py)
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return start
    return Path(out.stdout.strip()) if out.returncode == 0 else start


def masked(chunk: Chunk) -> Chunk:
    """``chunk`` with its secrets masked, unless masking is off."""
    if not redact.enabled():
        return chunk
    text = redact.redact(chunk.text)
    return chunk if text == chunk.text else replace(chunk, text=text)


def document_text(chunk: Chunk) -> str:
    """The text embedded for ``chunk``: its location line, then its text."""
    where = f"{chunk.path}:{chunk.start_line}-{chunk.end_line}"
    if chunk.symbol:
        where += f" {chunk.symbol}"
    return f"{where}\n{chunk.text}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index_project(
    root: Path | str, rebuild: bool = False, progress: Progress | None = None
) -> IndexReport:
    """Create or refresh the index of ``root``; see the module docstring.

    Raises ModelMismatch if the index was built with another embedding
    model (before anything is embedded), and EmbeddingUnavailable if the
    model cannot be reached (files finished until then are kept).
    """
    started = time.monotonic()
    root = Path(root).resolve()
    if rebuild:
        drop_index(root)
    selection = select_files(root)
    report = IndexReport(skipped=selection.skipped)
    model = active_model()

    with IndexStore.open(root, create=True) as store:
        store.check_model(model)  # before embedding anything
        known = store.file_hashes()

        total = len(selection.files)
        for number, rel in enumerate(selection.files, 1):
            if progress:
                progress(number, total, rel)
            data = (root / rel).read_bytes()
            digest = _sha256(data)
            if known.get(rel) == digest:
                report.unchanged += 1
                continue
            # Secrets inside ordinary files (a key in config.py, aws.cfg)
            # are masked before the text is embedded, stored or put into a
            # prompt, like everything else codebud keeps (redact.py).
            chunks = [masked(c) for c in chunk_file(rel, data.decode("utf-8"))]
            vectors = embed_documents([document_text(c) for c in chunks]) if chunks else []
            if vectors:
                store.ensure_model(model, len(vectors[0]))
            store.replace_file(rel, digest, list(zip(chunks, vectors, strict=True)))
            report.indexed += 1
            report.chunks += len(chunks)

        gone = sorted(set(known) - set(selection.files))
        if gone:
            store.remove_files(gone)
        report.removed = len(gone)
        store.mark_checked()

    report.seconds = time.monotonic() - started
    return report


def index_status(root: Path | str) -> dict | None:
    """The index's stats plus files changed/new/deleted since; None if no index."""
    root = Path(root).resolve()
    store = IndexStore.open(root)
    if store is None:
        return None
    with store:
        status = store.stats()
        known = store.file_hashes()
    current = select_files(root).files
    changed = sum(
        1 for rel in current if rel in known and _sha256((root / rel).read_bytes()) != known[rel]
    )
    status["changed"] = changed
    status["new"] = sum(1 for rel in current if rel not in known)
    status["deleted"] = len(set(known) - set(current))
    return status


def quick_status(root: Path | str) -> dict | None:
    """Cheap staleness check by file times; None if ``root`` has no index.

    "changed" counts indexed files modified since the last index run (a
    touched but unchanged file counts too -- a refresh then embeds
    nothing), "deleted" indexed files that are gone, "new" project files
    not indexed yet, judged by name and size only. Nothing is read or
    hashed, so this can run before every chat prompt.
    """
    root = Path(root).resolve()
    store = IndexStore.open(root)
    if store is None:
        return None
    with store:
        known = set(store.file_hashes())
        checked = store.updated_ts()
    changed = deleted = 0
    for rel in known:
        try:
            if (root / rel).stat().st_mtime > checked:
                changed += 1
        except FileNotFoundError:
            deleted += 1
    new = sum(1 for rel in candidate_files(root) if rel not in known)
    return {"changed": changed, "new": new, "deleted": deleted}


def drop_index(root: Path | str) -> list[Path]:
    """Delete the index of ``root``; return the files removed ([] if none)."""
    directory = index_dir(root)
    if not directory.is_dir():
        return []
    removed = sorted(p for p in directory.iterdir() if p.is_file())
    shutil.rmtree(directory)
    return removed
