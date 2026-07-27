"""Unit tests for app.embed: batching, retry, dim-mismatch, and the lazy SDK import.

Every test injects a fake ``client`` (standing in for a ``WorkspaceClient``), so
``databricks.sdk`` is never imported here -- the whole point of the seam.
"""

from __future__ import annotations

import ast
import inspect
import sys
import threading
import time
from typing import Any

import pytest

from app.config import Settings
from app.embed import (
    EmbeddingCountMismatchError,
    EmbeddingDimMismatchError,
    databricks_embedder,
    get_embedder,
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
    # concurrency=1 pinned explicitly here: this test's own submission-order
    # assertion below would otherwise be the accidental tripwire for the
    # default -- passing it explicitly means it no longer is, so
    # test_default_concurrency_is_one below covers the default directly.
    client = _FakeClient(lambda texts: [[0.0, 0.0] for _ in texts])
    embed = databricks_embedder("ep", "m", client=client, dim=2, batch_size=2, concurrency=1)
    vectors = embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert client.api_client.batches == [["a", "b"], ["c", "d"], ["e"]]


@pytest.mark.unit
def test_default_concurrency_is_one() -> None:
    """The whole backward-compat guarantee rests on this default: every
    non-indexer caller (app/search/semantic.py, every other test in this file)
    relies on serial dispatch. Only get_embedder(cfg) is supposed to opt in
    to concurrency>1 (see databricks_embedder's docstring)."""
    assert inspect.signature(databricks_embedder).parameters["concurrency"].default == 1


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
@pytest.mark.parametrize("concurrency", [1, 4])
def test_stub_path_never_imports_databricks_sdk(concurrency: int) -> None:
    # batch_size=1 with two texts -> two batches, so concurrency=4 actually
    # reaches the pool branch (concurrent.futures is stdlib; nothing new to
    # import). At the module's default batch_size=64 this would be one batch
    # and the extension would pin nothing at any concurrency (round-3 finding).
    sys.modules.pop("databricks.sdk", None)
    client = _FakeClient(lambda texts: [[0.0, 0.0] for _ in texts])
    embed = databricks_embedder(
        "ep", "m", client=client, dim=2, batch_size=1, concurrency=concurrency
    )
    embed(["hello", "world"])
    assert "databricks.sdk" not in sys.modules


def _build_order_probe_client() -> tuple[Any, list[str]]:
    """Fake client where batch "0" blocks until batch "3" (the last batch)
    completes and releases it -- so batch 0 finishes LAST despite being
    submitted first. ``completion_order`` records the FAKE's own completion
    sequence, for T3 to prove this fixture is not passing vacuously.
    """
    release = threading.Event()
    completion_order: list[str] = []

    class _OrderedApiClient:
        def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
            assert method == "POST"
            batch = list(body["input"])
            text = batch[0]
            if text == "0":
                assert release.wait(timeout=5), "the last batch never released batch 0"
            completion_order.append(text)
            if text == "3":
                release.set()
            return {"data": [{"embedding": [float(text)]}]}

    class _Client:
        def __init__(self) -> None:
            self.api_client = _OrderedApiClient()

    return _Client(), completion_order


@pytest.mark.unit
def test_concurrent_batches_return_in_submission_order() -> None:
    """AC1: vectors come back in submission order even though batch 0 -- the
    one that determines index 0 of the result -- is the LAST to complete."""
    client, _ = _build_order_probe_client()
    embed = databricks_embedder("ep", "m", client=client, dim=1, batch_size=1, concurrency=4)
    vectors = embed(["0", "1", "2", "3"])
    assert vectors == [[0.0], [1.0], [2.0], [3.0]]


@pytest.mark.unit
def test_out_of_order_completion_is_recorded_out_of_order() -> None:
    """Proves test_concurrent_batches_return_in_submission_order isn't passing
    vacuously: the fake's own completion order genuinely differs from submission
    order (batch 0 finishes last, not first)."""
    client, completion_order = _build_order_probe_client()
    embed = databricks_embedder("ep", "m", client=client, dim=1, batch_size=1, concurrency=4)
    embed(["0", "1", "2", "3"])
    assert completion_order != ["0", "1", "2", "3"]
    assert completion_order[-1] == "0"


@pytest.mark.unit
def test_short_batch_under_concurrency_names_the_offending_batch() -> None:
    """AC2: a mismatch in a LATE batch under concurrency still names it."""

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        if batch == ["d"]:
            return []  # 0 vectors for 1 text: short by one
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder("ep", "m", client=client, dim=1, batch_size=1, concurrency=4)
    with pytest.raises(EmbeddingCountMismatchError, match=r"0 vectors for 1 texts \(batch 3"):
        embed(["a", "b", "c", "d"])


@pytest.mark.unit
def test_offset_names_the_texts_slice_at_a_non_degenerate_batch_size() -> None:
    """T4's ``batch_size=1`` fixture makes ``offset == ordinal`` trivially, so it
    cannot catch a broken ``offset = ordinal * batch_size``. This uses a real
    batch size (64) so the reported ``texts[192:256]`` only matches if the
    multiplication is right."""

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        if len(batch) == 64 and batch[0] == "192":
            return [[0.0] for _ in batch[:-1]]  # 63 vectors for 64 texts: short by one
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder("ep", "m", client=client, dim=1, batch_size=64, concurrency=4)
    texts = [str(i) for i in range(256)]
    with pytest.raises(
        EmbeddingCountMismatchError,
        match=r"63 vectors for 64 texts \(batch 3, texts\[192:256\]\)",
    ):
        embed(texts)


@pytest.mark.unit
def test_first_offending_batch_in_submission_order_wins() -> None:
    """A slow low-ordinal failure and a fast high-ordinal failure both occur;
    `pool.map` must raise the lowest-ordinal one (deterministic error under
    nondeterministic execution), not whichever failed first in wall clock."""

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        text = batch[0]
        if text == "1":
            time.sleep(0.2)
            raise RuntimeError("slow failure at ordinal 1")
        if text == "6":
            raise RuntimeError("fast failure at ordinal 6")
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder(
        "ep", "m", client=client, dim=1, batch_size=1, concurrency=8, max_retries=0
    )
    with pytest.raises(RuntimeError, match="slow failure at ordinal 1"):
        embed([str(i) for i in range(8)])


@pytest.mark.unit
def test_failure_cancels_queued_batches() -> None:
    """Structural fixture, NOT a scheduler-derived one (see the plan's stall
    sweep -- every wall-clock-derived constant fails somewhere). batch_size=1
    with 20 texts is load-bearing: at the module's default batch_size=64, 20
    texts is ONE batch, so this would take the serial path and pass vacuously
    (`started == 1 <= 3`) with no pool in either implementation.

    Batch "0" raises immediately; every other batch parks on an Event that is
    NEVER set. A parked worker structurally cannot pull the next queue item, so
    `started` is fixed by the code path (concurrency slots occupied, plus one
    more pulled by the freed failing worker before cancellation fires), not by
    scheduling. Measured (per the plan) at exactly 3 with zero variance across
    stall levels, while a submit()+serial-.result() refactor starts all 20.
    """
    never = threading.Event()
    started_lock = threading.Lock()
    started = {"n": 0}

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        text = batch[0]
        with started_lock:
            started["n"] += 1
        if text == "0":
            raise RuntimeError("endpoint down")
        never.wait(timeout=0.5)  # parks; never actually set -- bounds runtime only
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    texts = [str(i) for i in range(20)]
    embed = databricks_embedder(
        "ep", "m", client=client, dim=1, batch_size=1, concurrency=2, max_retries=0
    )
    with pytest.raises(RuntimeError, match="endpoint down"):
        embed(texts)
    assert started["n"] <= 3  # concurrency (2) + 1, structural, not statistical


@pytest.mark.unit
@pytest.mark.parametrize("concurrency", [0, -1, 1])
def test_concurrency_one_uses_the_calling_thread(concurrency: int) -> None:
    """The kill switch is a real no-pool path, and the clamp handles bad
    (<=0) values by degrading to serial rather than raising or spawning."""
    caller_thread = threading.current_thread()
    seen_threads: list[threading.Thread] = []

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        seen_threads.append(threading.current_thread())
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder(
        "ep", "m", client=client, dim=1, batch_size=1, concurrency=concurrency
    )
    embed(["a", "b", "c"])
    assert seen_threads == [caller_thread] * 3


@pytest.mark.unit
def test_single_batch_never_spawns_a_pool() -> None:
    """The query path (app/search/semantic.py: one text -> one batch) pays
    nothing even at a high configured concurrency."""
    caller_thread = threading.current_thread()
    seen_threads: list[threading.Thread] = []

    def vectors_fn(batch: list[str]) -> list[list[float]]:
        seen_threads.append(threading.current_thread())
        return [[0.0] for _ in batch]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder("ep", "m", client=client, dim=1, concurrency=8)
    embed(["only one text"])
    assert seen_threads == [caller_thread]


@pytest.mark.unit
def test_dim_mismatch_still_raises_under_concurrency() -> None:
    """Aggregate _assert_dims still runs over the flattened result under the
    pool path. batch_size=1 with two texts -> two batches, so this actually
    exercises the pool branch rather than degrading to serial."""
    client = _FakeClient(lambda texts: [[0.0, 0.0, 0.0] for _ in texts])  # dim 3, expect 2
    embed = databricks_embedder("ep", "m", client=client, dim=2, batch_size=1, concurrency=4)
    with pytest.raises(EmbeddingDimMismatchError):
        embed(["a", "b"])


@pytest.mark.unit
def test_embed_module_never_uses_as_completed() -> None:
    """AST-based, not a substring check: a substring check would fail on this
    very module's load-bearing explanatory comment about why as_completed must
    never appear here (it yields in completion order, defeating AC1)."""
    import app.embed as embed_module

    tree = ast.parse(inspect.getsource(embed_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "as_completed":
            pytest.fail("app.embed must never reference as_completed -- see module docstring")
        if isinstance(node, ast.Attribute) and node.attr == "as_completed":
            pytest.fail("app.embed must never reference as_completed -- see module docstring")
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "as_completed" for alias in node.names
        ):
            pytest.fail("app.embed must never import as_completed -- see module docstring")


@pytest.mark.unit
def test_get_embedder_threads_concurrency_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_databricks_embedder(endpoint: str, model: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return lambda texts: []

    monkeypatch.setattr("app.embed.databricks_embedder", fake_databricks_embedder)
    cfg = Settings(semantic_embedding_endpoint="/ep", semantic_embedding_concurrency=7)
    get_embedder(cfg)
    assert captured["concurrency"] == 7


@pytest.mark.unit
def test_empty_texts_under_concurrency() -> None:
    calls = {"n": 0}

    def vectors_fn(texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        return [[0.0] for _ in texts]

    client = _FakeClient(vectors_fn)
    embed = databricks_embedder("ep", "m", client=client, dim=1, concurrency=8)
    assert embed([]) == []
    assert calls["n"] == 0
