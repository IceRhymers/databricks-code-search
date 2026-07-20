"""Serverless indexing job entry point (``code-search-index``).

The repo set comes from the central ``config.yaml``, read from the workspace at
``--config`` on every run and resolved to canonical ``org/repo`` names by
:func:`indexer.resolve.resolve_repos`. Resolution completes in full -- config
read, enumeration, filtering, dedup, ceiling -- before the first tarball is
fetched, so a bad config exits non-zero having indexed nothing and without ever
opening a database connection.

Orchestrates, per resolved repo: resolve HEAD -> download the tarball by that
immutable SHA -> extract -> parse text files -> extract symbols -> atomic upsert +
mark-and-sweep via :func:`indexer.store.index_repo`. Each repo is isolated; the
process exits non-zero if any repo fails.

When ``cfg.semantic_enabled`` (issue #14), each repo's files are also chunked and
embedded -- but OUTSIDE ``index_repo``'s transaction (A4): the embedder is called
here, up front, and only a ``chunk_writer`` closure over the precomputed vectors
is handed to ``index_repo``, which writes them (pure DML, no network) inside the
same per-file loop as symbols. Flag-off: no chunking, no embedder, no import of
``indexer.embed``'s lazy ``databricks-sdk`` dependency.

Logging is INFO only. The GitHub token is read via an injected client and is never
logged, and this module never lowers root/SDK/httpx log levels (see the redaction
test + source-level tripwire).
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.db.client import create_db_engine
from indexer.chunk_store import write_chunks
from indexer.embed import EmbeddingCountMismatchError, EmbedFn, get_embedder
from indexer.fetch import download_tarball, extract_tarball, resolve_ref
from indexer.languages import Chunk, IndexCounts, ParsedFile
from indexer.parse import iter_chunks, iter_source_files
from indexer.repo_config import RepoConfig, load_config, normalize_repo
from indexer.resolve import MAX_REPOS, resolve_repos
from indexer.store import ChunkWriter, index_repo
from indexer.symbols import extract_symbols

logger = logging.getLogger("indexer.job")


def read_github_token(client: Any, scope: str, key: str) -> str:
    """Read + base64-decode the GitHub token from Databricks secrets.

    ``client`` is an injected ``WorkspaceClient`` (or a fake in tests). Nothing
    here is logged — the token never touches a log record.
    """
    secret = client.secrets.get_secret(scope, key)
    return base64.b64decode(secret.value).decode()


def run(
    *,
    config_path: str,
    scope: str,
    key: str,
    endpoint: str | None,
    database: str | None,
    workspace_client: Any | None = None,
    http_client: httpx.Client | None = None,
    engine: Any | None = None,
    index_fn: Callable[..., IndexCounts] = index_repo,
    cfg: Settings | None = None,
    embed_fn: EmbedFn | None = None,
    config_loader: Callable[[Any, str], RepoConfig] = load_config,
    max_repos: int = MAX_REPOS,
) -> int:
    """Index every configured repo and return a process exit code (0 = all ok).

    Boundaries are injectable for tests: ``workspace_client`` (secret + config
    read), ``http_client`` (GitHub HTTP), ``engine`` (DB), ``index_fn`` (the
    store), ``embed_fn`` (issue #14 semantic chunking), and ``config_loader``
    (so orchestration tests need no SDK fake). ``cfg`` defaults to the
    process-cached :func:`app.config.get_settings`.

    ``resolve_repos`` is deliberately **not** injectable: the existing
    ``httpx.MockTransport`` seam already lets tests drive enumeration outcomes
    through the real resolver, and a fake one would let the fail-fast contract
    pass without exercising the wiring it exists to prove.
    """
    if cfg is None:
        cfg = get_settings()

    if workspace_client is None:
        from databricks.sdk import WorkspaceClient

        workspace_client = WorkspaceClient()

    try:
        config = config_loader(workspace_client, config_path)
    except Exception:
        # Broad by design, matching the resolution handler below.
        # read_workspace_config wraps every SDK failure in ConfigError, but
        # parse_config only converts UnicodeDecodeError, yaml.YAMLError, and
        # ValidationError -- anything else out of yaml.safe_load would otherwise
        # escape as a raw traceback, degrading diagnosability at exactly the
        # point that motivated loading the config through the SDK.
        # exc_info is load-bearing, not stylistic: the chained SDK exception
        # carries the 403/404 that distinguishes "never synced" from "no read
        # permission". The message stays load-phase-specific so this and the
        # resolution failure below remain distinguishable in the log.
        logger.error("could not load config from %s", config_path, exc_info=True)
        return 1

    token = read_github_token(workspace_client, scope, key)

    owns_http = http_client is None
    if http_client is None:
        http_client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=60.0)

    # Resolve BEFORE opening a database connection: a bad config must cost
    # nothing but the enumeration calls it already made.
    try:
        entries = resolve_repos(config, http_client, max_repos=max_repos)
    except Exception:
        # Broad by design. normalize_repo raises a bare ValueError, and
        # enumeration can raise httpx errors, RateLimitError, EmptyConfigError,
        # or RepoCeilingError; a named tuple would let a ValueError escape and
        # break main()'s exit-code contract.
        logger.error("could not resolve repos from %s", config_path, exc_info=True)
        if owns_http:
            http_client.close()
        return 1

    owns_engine = engine is None
    if engine is None:
        engine = create_db_engine(endpoint=endpoint, database=database)

    # Lazy embedder: built (and databricks-sdk imported) only when semantic
    # search is enabled and no fake was injected (issue #14 A1).
    #
    # Degrade rather than abort: get_embedder raises when semantic_embedding_endpoint is
    # unset, and letting that propagate would kill the WHOLE indexing run -- including every
    # repo that has nothing to do with semantic search. Semantic is an additive layer, so a
    # semantic misconfiguration must not cost us the core index.
    if cfg.semantic_enabled and embed_fn is None:
        try:
            embed_fn = get_embedder(cfg)
        except Exception:
            logger.warning(
                "semantic enabled but the embedder could not be built; indexing the core "
                "corpus WITHOUT chunks (semantic results will be stale until fixed)",
                exc_info=True,
            )
            embed_fn = None

    failures = 0
    try:
        for entry in entries:
            try:
                counts = _index_one(
                    entry,
                    http_client=http_client,
                    engine=engine,
                    index_fn=index_fn,
                    cfg=cfg,
                    embed_fn=embed_fn,
                )
                logger.info(
                    "indexed %s: files=%d symbols=%d swept=%d",
                    entry,
                    counts.files,
                    counts.symbols,
                    counts.swept,
                )
            except Exception:
                failures += 1
                logger.exception("failed to index %s", entry)
    finally:
        if owns_http:
            http_client.close()
        if owns_engine:
            engine.dispose()

    logger.info("indexing complete: %d/%d repos ok", len(entries) - failures, len(entries))
    return 1 if failures else 0


def _precompute_chunk_writer(
    files: list[ParsedFile], embed_fn: EmbedFn, max_chunks_per_repo: int
) -> ChunkWriter:
    """Chunk + embed every file up front (issue #14 A4: no network inside conn.begin()).

    Returns a :data:`indexer.store.ChunkWriter` closure over the precomputed
    ``(chunk_index, content, embedding)`` triples, keyed by file path, so
    ``index_repo`` can write them per file without ever calling the embedder
    itself. Raises ``ValueError`` if the repo's total chunk count exceeds
    ``max_chunks_per_repo`` (the documented hard ceiling, not a streaming bound
    -- see ``app.config.semantic_max_chunks_per_repo``).
    """
    per_file: dict[str, list[Chunk]] = {pf.path: list(iter_chunks(pf)) for pf in files}
    total = sum(len(chunks) for chunks in per_file.values())
    if total > max_chunks_per_repo:
        raise ValueError(
            f"repo has {total} chunks, exceeding semantic_max_chunks_per_repo={max_chunks_per_repo}"
        )

    all_texts = [c.content for chunks in per_file.values() for c in chunks]
    all_vectors = embed_fn(all_texts) if all_texts else []
    # Re-checked HERE, not just inside databricks_embedder: embed_fn is a public injection
    # point on run(), and the positional re-slicing below is what actually depends on the
    # invariant. A short result would raise IndexError further down; a long one would
    # silently attach the wrong file's vectors. Guard where the assumption is used.
    if len(all_vectors) != len(all_texts):
        raise EmbeddingCountMismatchError(
            f"embedder returned {len(all_vectors)} vectors for {len(all_texts)} chunk texts"
        )

    by_path: dict[str, list[tuple[int, str, list[float]]]] = {}
    i = 0
    for path, chunks in per_file.items():
        if not chunks:
            continue
        by_path[path] = [
            (c.chunk_index, c.content, all_vectors[i + j]) for j, c in enumerate(chunks)
        ]
        i += len(chunks)

    def chunk_writer(conn: Any, repo_id: int, file_id: int, pf: ParsedFile) -> None:
        write_chunks(conn, file_id=file_id, chunks=by_path.get(pf.path, []))

    return chunk_writer


def _index_one(
    entry: str,
    *,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
) -> IndexCounts:
    """Run the full fetch -> parse -> symbols -> store pipeline for one repo entry."""
    name = normalize_repo(entry)
    org, repo = name.split("/", 1)
    default_branch, head_sha = resolve_ref(http_client, org, repo)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tar_path = download_tarball(http_client, org, repo, head_sha, tmp_path)
        root = extract_tarball(tar_path, tmp_path / "extracted")

        chunk_writer: ChunkWriter | None = None
        if cfg.semantic_enabled and embed_fn is not None:
            # Chunking/embedding needs the full file list up front -- unlike the
            # lazy items generator below, it cannot stream through index_repo's
            # open transaction (A4).
            files = list(iter_source_files(root))
            try:
                chunk_writer = _precompute_chunk_writer(
                    files, embed_fn, cfg.semantic_max_chunks_per_repo
                )
            except Exception:
                # The semantic layer is ADDITIVE: a chunk-ceiling breach, a downed embedder,
                # or a dim/count mismatch must not cost this repo its core index. Letting it
                # propagate would skip files/symbols AND the mark-and-sweep, silently leaving
                # the repo stale -- a worse outcome than stale chunks. Chunks catch up on the
                # next successful run; the failure is logged with a traceback, never swallowed
                # silently.
                logger.warning(
                    "semantic precompute failed for %s; indexing core corpus without chunks",
                    name,
                    exc_info=True,
                )
                chunk_writer = None
            items = ((pf, extract_symbols(pf)) for pf in files)
        else:
            # Lazy generator: files stream through the open transaction (bounded memory).
            items = ((pf, extract_symbols(pf)) for pf in iter_source_files(root))

        with engine.connect() as conn:
            return index_fn(
                conn,
                name=name,
                default_branch=default_branch,
                head_sha=head_sha,
                items=items,
                chunk_writer=chunk_writer,
            )


def _positive_int(raw: str) -> int:
    """argparse type for ``--max_repos``: an int >= 1.

    ``--max_repos=0`` is well-defined but makes every non-empty resolution raise
    ``RepoCeilingError``, so reject it at the boundary rather than at 2am.
    """
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Capture the serverless interpreter version in the run log (M2 live gate).
    logger.info("code-search-index starting on Python %s", sys.version)

    parser = argparse.ArgumentParser(description="Index GitHub repos into the code search DB.")
    parser.add_argument("--config", required=True, help="Workspace path to config.yaml.")
    # Underscore, NOT hyphen: python_wheel_task emits each named_parameters key
    # verbatim as `--<key>=<value>`, and resources/job.yml spells it max_repos.
    parser.add_argument(
        "--max_repos",
        type=_positive_int,
        default=MAX_REPOS,
        help="Safety ceiling on the resolved repo count.",
    )
    parser.add_argument("--scope", required=True, help="Databricks secret scope for the GH token.")
    parser.add_argument("--key", required=True, help="Secret key within the scope.")
    parser.add_argument("--endpoint", default=None, help="Lakebase endpoint identifier.")
    parser.add_argument("--database", default=None, help="Postgres database name.")
    args = parser.parse_args()

    exit_code = run(
        config_path=args.config,
        max_repos=args.max_repos,
        scope=args.scope,
        key=args.key,
        endpoint=args.endpoint,
        database=args.database,
    )
    # A serverless python_wheel_task entry point signals success by returning
    # normally; any raised SystemExit (even code 0) is reported as a workload
    # failure. So only exit non-zero to fail the task when a repo failed.
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
