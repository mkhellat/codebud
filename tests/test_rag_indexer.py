"""
tests/test_rag_indexer.py

Building and refreshing a project index (phase 9, B4): incremental by
content hash, batched embedding with the model's document prefix, one
model per index, resumable after an interruption.
"""

import pytest

from codebud.rag import indexer
from codebud.rag.store import IndexStore, ModelMismatch, index_dir
from codebud.tools import embeddings


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    )
    (root / "README.md").write_text("# Title\nintro\n## Usage\nrun it\n")
    return root


@pytest.fixture
def embed_calls(monkeypatch):
    """Count texts sent for embedding (the stub still computes them)."""
    calls = []
    real = embeddings.embed_documents

    def counting(texts):
        calls.append(list(texts))
        return real(texts)

    monkeypatch.setattr(indexer, "embed_documents", counting)
    return calls


def _texts(calls):
    return [t for batch in calls for t in batch]


def test_first_run_indexes_every_file(project, embed_calls):
    report = indexer.index_project(project)
    assert (report.indexed, report.unchanged, report.removed) == (2, 0, 0)
    assert report.chunks == 4  # alpha, beta, Title, Usage
    with IndexStore.open(project) as store:
        stats = store.stats()
    assert (stats["files"], stats["chunks"]) == (2, 4)
    assert stats["model"] == "stub"  # tests embed with the offline stub


def test_chunks_are_embedded_with_their_location(project, embed_calls):
    indexer.index_project(project)
    assert "pkg/a.py:1-2 alpha\ndef alpha():\n    return 1" in _texts(embed_calls)


def test_second_run_embeds_nothing_when_nothing_changed(project, embed_calls):
    indexer.index_project(project)
    embed_calls.clear()
    report = indexer.index_project(project)
    assert (report.indexed, report.unchanged, report.chunks) == (0, 2, 0)
    assert embed_calls == []


def test_changed_and_deleted_files_are_updated(project, embed_calls):
    indexer.index_project(project)
    embed_calls.clear()
    (project / "pkg" / "a.py").write_text("def gamma():\n    return 3\n")
    (project / "README.md").unlink()

    report = indexer.index_project(project)
    assert (report.indexed, report.unchanged, report.removed) == (1, 0, 1)
    assert [t.split("\n")[0] for t in _texts(embed_calls)] == ["pkg/a.py:1-2 gamma"]
    with IndexStore.open(project) as store:
        assert set(store.file_hashes()) == {"pkg/a.py"}


def test_status_counts_changes_since_the_last_run(project):
    assert indexer.index_status(project) is None
    indexer.index_project(project)
    assert indexer.index_status(project)["changed"] == 0

    (project / "pkg" / "a.py").write_text("x = 1\n")
    (project / "new.py").write_text("y = 2\n")
    (project / "README.md").unlink()
    status = indexer.index_status(project)
    assert (status["changed"], status["new"], status["deleted"]) == (1, 1, 1)


def test_an_index_from_another_model_is_refused_before_embedding(project, embed_calls):
    with IndexStore.open(project, create=True) as store:
        store.ensure_model("other-model", 16)
    with pytest.raises(ModelMismatch):
        indexer.index_project(project)
    assert embed_calls == []

    report = indexer.index_project(project, rebuild=True)
    assert report.indexed == 2


def test_interrupted_run_keeps_finished_files(project, monkeypatch):
    real = embeddings.embed_documents

    def fail_on_second_file(texts):
        if any(t.startswith("pkg/a.py") for t in texts):
            raise embeddings.EmbeddingUnavailable("gone")
        return real(texts)

    monkeypatch.setattr(indexer, "embed_documents", fail_on_second_file)
    with pytest.raises(embeddings.EmbeddingUnavailable):
        indexer.index_project(project)
    with IndexStore.open(project) as store:
        assert set(store.file_hashes()) == {"README.md"}  # pkg/a.py is retried next time


def test_progress_is_reported_per_file(project):
    seen = []
    indexer.index_project(project, progress=lambda i, n, path: seen.append((i, n, path)))
    assert seen == [(1, 2, "README.md"), (2, 2, "pkg/a.py")]


def test_drop_removes_the_whole_index(project):
    indexer.index_project(project)
    removed = indexer.drop_index(project)
    assert removed and all(not p.exists() for p in removed)
    assert not index_dir(project).exists()
    assert indexer.drop_index(project) == []


def test_documents_are_batched_and_prefixed_for_nomic(monkeypatch):
    monkeypatch.delenv("CODEBUD_EMBED_STUB", raising=False)
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setattr(embeddings, "BATCH_SIZE", 2)
    sent = []

    class Reply:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[1.0, 0.0]] * self.n}

    def fake_post(url, json, **kwargs):
        sent.append(json["input"])
        return Reply(len(json["input"]))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)
    vectors = embeddings.embed_documents(["a", "b", "c"])
    assert len(vectors) == 3
    assert sent == [["search_document: a", "search_document: b"], ["search_document: c"]]

    sent.clear()
    embeddings.embed_query("where?")
    assert sent == [["search_query: where?"]]


def test_short_reply_from_ollama_is_an_error(monkeypatch):
    monkeypatch.delenv("CODEBUD_EMBED_STUB", raising=False)

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[1.0]]}

    monkeypatch.setattr(embeddings.requests, "post", lambda *a, **k: Reply())
    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.embed_documents(["a", "b"])


def test_qwen3_embedding_gets_an_instruction_on_queries_only(monkeypatch):
    monkeypatch.delenv("CODEBUD_EMBED_STUB", raising=False)
    monkeypatch.setenv("EMBED_MODEL", "qwen3-embedding:0.6b")
    sent = []

    class Reply:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[1.0]] * self.n}

    def fake_post(url, json, **kwargs):
        sent.append(json["input"])
        return Reply(len(json["input"]))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)
    embeddings.embed_documents(["def f(): pass"])
    embeddings.embed_query("where is f?")
    assert sent[0] == ["def f(): pass"]
    assert sent[1][0].startswith("Instruct: ") and sent[1][0].endswith("\nQuery: where is f?")


def test_default_model_gets_no_task_prefix(monkeypatch):
    monkeypatch.delenv("CODEBUD_EMBED_STUB", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    sent = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[1.0]]}

    monkeypatch.setattr(
        embeddings.requests, "post", lambda url, json, **k: sent.append(json) or Reply()
    )
    embeddings.embed_query("where is f?")
    assert sent[0]["model"] == "all-minilm"
    assert sent[0]["input"] == ["where is f?"]


# --- secrets inside ordinary files are masked before indexing (M3) ----------


def test_secrets_in_ordinary_files_are_masked_in_the_index(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    key = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
    (root / "aws.cfg").write_text(f"[default]\naws_secret_access_key = {key}\n")
    indexer.index_project(root)
    with IndexStore.open(root) as store:
        texts = [store.chunk(i).text for i, _ in store.vectors()]
    assert texts and all(key not in t for t in texts)
    assert any("aws_secret_access_key = [redacted]" in t for t in texts)


def test_masking_off_keeps_the_index_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBUD_MASK_SECRETS", "0")
    root = tmp_path / "proj"
    root.mkdir()
    key = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
    (root / "aws.cfg").write_text(f"aws_secret_access_key = {key}\n")
    indexer.index_project(root)
    with IndexStore.open(root) as store:
        texts = [store.chunk(i).text for i, _ in store.vectors()]
    assert any(key in t for t in texts)
