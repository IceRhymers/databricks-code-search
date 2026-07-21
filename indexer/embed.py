"""Query/index-time embedding seam (issue #14 Phase 1).

``EmbedFn`` is the pure contract the rest of the codebase depends on: texts in,
unit-normalized dim-D vectors out. :func:`databricks_embedder` is the real
implementation, backed by the workspace AI Gateway MLflow embeddings route
(``POST /ai-gateway/mlflow/v1/embeddings`` with ``{"model": ..., "input": ...}``,
OpenAI-shaped response) via ``databricks-sdk``'s raw API client -- workspace
auth included, no serving-endpoint name involved. The SDK import is LAZY (inside
the function body, not module scope) so the flag-off path and unit tests never import it -- mirrors
the ``from databricks.sdk import WorkspaceClient`` seam already used in
``app/db/client.py`` and ``indexer/job.py``. ``client`` is an injected SDK
client (or a fake in tests); when omitted, a real ``WorkspaceClient()`` is
constructed and the import happens.

Embeddings are computed OUTSIDE the index transaction (issue #14 A4): nothing
here opens a DB connection or is called while a lock is held.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.config import SEMANTIC_EMBEDDING_DIM, Settings

# texts -> unit-normalized dim-D vectors, one per input text, same order.
EmbedFn = Callable[[list[str]], list[list[float]]]


class EmbeddingDimMismatchError(RuntimeError):
    """Raised when an embedder returns a vector whose length != the configured dim."""


class EmbeddingCountMismatchError(RuntimeError):
    """Raised when an embedder returns a different NUMBER of vectors than texts sent.

    Load-bearing: callers (``indexer.job._precompute_chunk_writer``) re-slice the flat
    result positionally, so a short batch would silently shift every subsequent file's
    vectors -- writing chunks whose embedding belongs to a DIFFERENT file. That corruption
    is invisible (no error, plausible-looking results) and persists until the next
    re-index, so a count mismatch must fail loudly at the source rather than propagate.
    """


def _assert_dims(vectors: Sequence[list[float]], dim: int) -> None:
    for i, vector in enumerate(vectors):
        if len(vector) != dim:
            raise EmbeddingDimMismatchError(
                f"embedding at index {i} has dim {len(vector)}, expected {dim}"
            )


def _query_batch(
    client: Any, endpoint: str, model: str, batch: list[str], *, max_retries: int
) -> list[list[float]]:
    """Query one batch, retrying up to ``max_retries`` times (small, bounded).

    ``endpoint`` is the gateway route path (e.g. ``/ai-gateway/mlflow/v1/embeddings``),
    POSTed via the SDK's raw API client; the response is OpenAI-shaped
    (``{"data": [{"embedding": [...]}, ...]}``).
    """
    last_exc: Exception | None = None
    for _attempt in range(max_retries + 1):
        try:
            response = client.api_client.do("POST", endpoint, body={"model": model, "input": batch})
            vectors = [item["embedding"] for item in response["data"]]
            # Checked per batch, not just in aggregate: the caller re-slices the flat
            # result positionally, so a short batch misaligns every LATER file's vectors.
            # Catching it here names the offending batch instead of surfacing as a
            # confusing IndexError (or silent corruption) much further downstream.
            if len(vectors) != len(batch):
                raise EmbeddingCountMismatchError(
                    f"embedder returned {len(vectors)} vectors for {len(batch)} texts"
                )
            return vectors
        except EmbeddingCountMismatchError:
            raise  # a protocol violation, not a transient fault -- retrying cannot fix it
        except Exception as exc:  # narrow-then-reraise: bounded retry, no silent swallow
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def databricks_embedder(
    endpoint: str,
    model: str,
    *,
    client: Any | None = None,
    dim: int = SEMANTIC_EMBEDDING_DIM,
    batch_size: int = 64,
    timeout: float = 20.0,
    max_retries: int = 2,
) -> EmbedFn:
    """Build an :data:`EmbedFn` backed by the AI Gateway embeddings route ``endpoint``.

    ``endpoint`` is a workspace-relative API path (e.g.
    ``/ai-gateway/mlflow/v1/embeddings``); ``model`` names the embeddings model sent
    in each request body (e.g. ``system.ai.gte-large-en``). Batches ``texts`` by
    ``batch_size``, queries each batch with a bounded retry, and asserts every
    returned vector's length equals ``dim`` before returning -- a mismatch (e.g. a
    model swap to a different dim) raises :class:`EmbeddingDimMismatchError` instead
    of writing a bad vector.

    ``client`` is a test seam: when provided, ``databricks.sdk`` is never imported
    (the fake stands in for a ``WorkspaceClient``) and ``timeout`` is the injected
    client's concern. When omitted, the real ``WorkspaceClient`` is built with a
    ``Config`` carrying ``http_timeout_seconds=timeout`` (the raw API client has no
    per-call timeout).
    """
    if client is None:
        from databricks.sdk import WorkspaceClient  # lazy: see module docstring
        from databricks.sdk.config import Config as _SdkConfig

        client = WorkspaceClient(config=_SdkConfig(http_timeout_seconds=timeout))

    def embed(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vectors.extend(_query_batch(client, endpoint, model, batch, max_retries=max_retries))
        _assert_dims(vectors, dim)
        return vectors

    return embed


def get_embedder(cfg: Settings) -> EmbedFn:
    """Factory: build the real embedder from ``cfg`` (never called on the flag-off path).

    Callers (e.g. a process-scoped singleton, mirroring ``app/main.py``'s lazy
    ``get_engine()``) are responsible for caching; this just wires ``cfg``'s
    tunables into :func:`databricks_embedder`.
    """
    if not cfg.semantic_embedding_endpoint:
        raise RuntimeError(
            "semantic_embedding_endpoint is not configured; cannot build an embedder"
        )
    return databricks_embedder(
        cfg.semantic_embedding_endpoint,
        cfg.semantic_embedding_model,
        dim=cfg.semantic_embedding_dim,
        batch_size=cfg.semantic_embedding_batch_size,
        timeout=cfg.semantic_embedding_timeout_s,
    )
