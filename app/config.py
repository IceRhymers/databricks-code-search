"""Runtime settings for the MCP server (pydantic-settings).

Config lives in one place so the tool layer never reads ``os.environ`` directly.
Two naming conventions co-exist on purpose:

* Tunables (timeouts, caps, limits) use the ``CODE_SEARCH_`` env prefix so they are
  namespaced and obviously ours.
* ``lakebase_endpoint`` reads the **unprefixed** ``LAKEBASE_ENDPOINT`` via a
  ``validation_alias`` because that is exactly the variable ``app/db/client.py`` already
  understands (and the same value the indexing job / DABs bundle export). Aliasing it here
  keeps a single deploy-time contract for the endpoint identifier.

``get_settings()`` is a process-cached accessor: settings are read once from the
environment and reused, matching the process-scoped engine singleton in ``app/main.py``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source of truth for the embedding vector width. Imported by
# app/db/semantic.py (the chunks.embedding column type) and by the 0004
# migration's DDL, so a model change never lets the two drift apart.
SEMANTIC_EMBEDDING_DIM = 1024


class Settings(BaseSettings):
    """Server configuration, read from the environment once per process."""

    model_config = SettingsConfigDict(env_prefix="CODE_SEARCH_", extra="ignore")

    # Lakebase endpoint identifier, threaded into create_db_engine(endpoint=...). Reads the
    # unprefixed LAKEBASE_ENDPOINT (client.py's own contract); None -> client.py falls back
    # to its own env lookup / raises if truly absent.
    lakebase_endpoint: str | None = Field(default=None, validation_alias="LAKEBASE_ENDPOINT")

    # Per-request DB-time bound, threaded into grep_search and the raw list_repos/get_file
    # SELECTs and the /ready probe. Bounds how long a single blocking worker can pin the pool.
    statement_timeout_ms: int = 5000

    # Aggregate content bytes grep pulls/scans per request (memory bound).
    max_content_bytes: int = 8 * 1024 * 1024

    # Per-request wall-clock bound on Python-side pattern matching in grep (regex module's
    # timeout=). Complements statement_timeout_ms (DB time) and max_content_bytes (bytes) as
    # the third leg of the per-request resource triangle.
    match_budget_ms: int = 2000

    # Default and hard-cap for search_code(limit): <=0 -> row_limit; > max -> max_row_limit.
    row_limit: int = 200
    max_row_limit: int = 1000

    # Gates the semantic_search code path. Default True: the target Lakebase project's
    # managed shared_preload_libraries including lakebase_vector,lakebase_text is a stated
    # project assumption (see docs/runbooks/semantic-enablement.md). Opt out with
    # CODE_SEARCH_SEMANTIC_ENABLED=0; flag-off is a true no-op (no DB/engine/chunks/
    # embedding/SDK access).
    semantic_enabled: bool = True

    # AI Gateway MLflow embeddings route (a workspace-relative API path, NOT a serving
    # endpoint name). app/embed.py POSTs {"model": ..., "input": ...} to it via the
    # SDK's raw API client. A working default is required: with the flag on by default,
    # an unset endpoint would raise at MCP query time.
    semantic_embedding_endpoint: str | None = "/ai-gateway/mlflow/v1/embeddings"

    # Embedding model identifier sent in the gateway request body. The gateway's default
    # embeddings model, 1024-dim (matches SEMANTIC_EMBEDDING_DIM; a unit test tripwires
    # a dim-changing model swap).
    semantic_embedding_model: str = "system.ai.gte-large-en"

    # Embedding vector width. Must match SEMANTIC_EMBEDDING_DIM (single source of truth);
    # a unit test ties the two together so a model swap with a different dim fails loudly.
    semantic_embedding_dim: int = SEMANTIC_EMBEDDING_DIM

    # Texts per embedding request, bounded by the serving endpoint's per-request input cap.
    semantic_embedding_batch_size: int = 64

    # Per-request embedding call timeout (seconds); kept low so a degraded endpoint fails
    # fast rather than holding a lock window open.
    semantic_embedding_timeout_s: float = 20.0

    # Hard ceiling on chunks embedded per repo per index run. Embeddings are buffered
    # outside the index transaction (no network call inside conn.begin()), so this bounds
    # in-process memory rather than a DB lock; exceeding it fails loudly.
    #
    # Sized against the ACTUAL buffer cost, not a round number: the vectors are held as
    # Python float lists, ~32 B per element (24 B float object + 8 B list pointer), so at
    # dim=1024 each chunk costs ~32 KB -- 8000 chunks is ~260 MB of vectors plus ~16 MB of
    # chunk text. A larger ceiling (e.g. 50k -> ~1.6 GB) would OOM the job container before
    # this loud check could ever fire, which would defeat the point of having a ceiling.
    # A repo that legitimately exceeds this needs a temp-table staging path, not a bigger
    # buffer.
    semantic_max_chunks_per_repo: int = 8000

    # Chunk size bound (tokens) fed to the embedding model. Distinct from MAX_FILE_BYTES,
    # which bounds file ingestion, not embedding-chunk granularity.
    semantic_chunk_max_tokens: int = 512


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-cached :class:`Settings` (read from the environment once)."""
    return Settings()
