"""Unit tests for indexer.embed: batching, retry, dim-mismatch, and the lazy SDK import.

Every test injects a fake ``client`` (standing in for a ``WorkspaceClient``), so
``databricks.sdk`` is never imported here -- the whole point of the seam.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from indexer.embed import (
    EmbeddingCountMismatchError,
    EmbeddingDimMismatchError,
    databricks_embedder,
)


class _FakeApiClient:
    """Stands in for WorkspaceClient.api_client: records each POSTed batch and
    returns the gateway's OpenAI-shaped ``{"data": [{"embedding": [...]}]}`` dict."""

    def __init__(self, vectors_fn: Any) -> None:
        self._vectors_fn = vectors_fn
        self.batches: list[list[str]] = []

    def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        assert method == "POST"
        batch = list(body["input"])
        self.batches.append(batch)
        return {"data": [{"embedding": v} for v in self._vectors_fn(batch)]}


class _FakeClient:
    def __init__(self, vectors_fn: Any) -> None:
        self.api_client = _FakeApiClient(vectors_fn)


@pytest.mark.unit
def test_batching_splits_by_batch_size() -> None:
    client = _FakeClient(lambda texts: [[0.0, 0.0] for _ in texts])
    embed = databricks_embedder("ep", "m", client=client, dim=2, batch_size=2)
    vectors = embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert client.api_client.batches == [["a", "b"], ["c", "d"], ["e"]]


@pytest.mark.unit
def test_single_batch_when_under_batch_size() -> None:
    client = _FakeClient(lambda texts: [[0.0] for _ in texts])
    embed = databricks_embedder("ep", "m", client=client, dim=1, batch_size=10)
    embed(["a", "b"])
    assert client.api_client.batches == [["a", "b"]]


@pytest.mark.unit
def test_dim_mismatch_raises() -> None:
    client = _FakeClient(lambda texts: [[0.0, 0.0, 0.0] for _ in texts])  # dim 3, expect 2
    embed = databricks_embedder("ep", "m", client=client, dim=2)
    with pytest.raises(EmbeddingDimMismatchError):
        embed(["a"])


@pytest.mark.unit
def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient serving error")
        return [[0.1, 0.2] for _ in texts]

    client = _FakeClient(flaky)
    embed = databricks_embedder("ep", "m", client=client, dim=2, max_retries=2)
    assert embed(["a"]) == [[0.1, 0.2]]
    assert calls["n"] == 2


@pytest.mark.unit
def test_retries_exhausted_reraises() -> None:
    def always_fails(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("endpoint down")

    client = _FakeClient(always_fails)
    embed = databricks_embedder("ep", "m", client=client, dim=2, max_retries=1)
    with pytest.raises(RuntimeError, match="endpoint down"):
        embed(["a"])


@pytest.mark.unit
def test_short_batch_raises_count_mismatch() -> None:
    """A batch returning fewer vectors than texts must fail loudly, not misalign.

    The caller re-slices the flat result positionally, so silently accepting a short
    batch would attach every later file's embeddings to the WRONG file.
    """
    # Two texts in, one vector back.
    client = _FakeClient(lambda texts: [[0.0, 0.0] for _ in texts[:-1]])
    embed = databricks_embedder("ep", "m", client=client, dim=2, batch_size=10)
    with pytest.raises(EmbeddingCountMismatchError, match="1 vectors for 2 texts"):
        embed(["a", "b"])


@pytest.mark.unit
def test_count_mismatch_is_not_retried() -> None:
    """A count mismatch is a protocol violation, not a transient fault -- fail on attempt 1."""
    calls = {"n": 0}

    def short(texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        return [[0.0, 0.0] for _ in texts[:-1]]

    client = _FakeClient(short)
    embed = databricks_embedder("ep", "m", client=client, dim=2, batch_size=10, max_retries=3)
    with pytest.raises(EmbeddingCountMismatchError):
        embed(["a", "b"])
    assert calls["n"] == 1  # not retried


@pytest.mark.unit
def test_stub_path_never_imports_databricks_sdk() -> None:
    sys.modules.pop("databricks.sdk", None)
    client = _FakeClient(lambda texts: [[0.0, 0.0] for _ in texts])
    embed = databricks_embedder("ep", "m", client=client, dim=2)
    embed(["hello", "world"])
    assert "databricks.sdk" not in sys.modules
