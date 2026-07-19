"""Serverless indexing job entry point (``code-search-index``).

Orchestrates, per configured repo: resolve HEAD -> download the tarball by that
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
import re
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
from indexer.store import ChunkWriter, index_repo
from indexer.symbols import extract_symbols

logger = logging.getLogger("indexer.job")

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}


def normalize_repo(entry: str) -> str:
    """Normalize a repo entry to canonical ``org/repo`` (the ``repos.name`` key).

    Accepts ``https://github.com/org/repo(.git)``, ``git@github.com:org/repo.git``,
    and bare ``org/repo``. Rejects other hosts, empty input, and anything that is
    not exactly two ``org/repo`` segments.
    """
    raw = entry.strip()
    if not raw:
        raise ValueError("empty repo entry")

    slug = raw
    if slug.startswith("git@"):
        # git@github.com:org/repo.git
        host, _, path = slug[len("git@") :].partition(":")
        if host not in _GITHUB_HOSTS:
            raise ValueError(f"unsupported host in repo entry: {entry!r}")
        slug = path
    elif "://" in slug:
        # https://github.com/org/repo(.git)
        scheme_host, _, path = slug.partition("://")[2].partition("/")
        if scheme_host not in _GITHUB_HOSTS:
            raise ValueError(f"unsupported host in repo entry: {entry!r}")
        slug = path

    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    slug = slug.strip("/")

    if not _REPO_RE.match(slug):
        raise ValueError(f"could not parse a github org/repo from {entry!r}")
    # Reject degenerate `.`/`..` segments GitHub itself rejects; keeps them out of
    # the API URL path and the tarball dest even though the host is fixed.
    if any(part in {".", ".."} for part in slug.split("/")):
        raise ValueError(f"invalid org/repo segment in {entry!r}")
    return slug


def read_github_token(client: Any, scope: str, key: str) -> str:
    """Read + base64-decode the GitHub token from Databricks secrets.

    ``client`` is an injected ``WorkspaceClient`` (or a fake in tests). Nothing
    here is logged — the token never touches a log record.
    """
    secret = client.secrets.get_secret(scope, key)
    return base64.b64decode(secret.value).decode()


def _split_repos(repos: str) -> list[str]:
    """Split the ``--repos`` value on commas/whitespace (matches ``repos_to_index``)."""
    return [tok for tok in re.split(r"[,\s]+", repos.strip()) if tok]


def run(
    *,
    repos: str,
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
) -> int:
    """Index every configured repo and return a process exit code (0 = all ok).

    Boundaries are injectable for tests: ``workspace_client`` (secret read),
    ``http_client`` (GitHub HTTP), ``engine`` (DB), ``index_fn`` (the store), and
    ``embed_fn`` (issue #14 semantic chunking; a fake in tests). ``cfg`` defaults
    to the process-cached :func:`app.config.get_settings`.
    """
    entries = _split_repos(repos)
    if not entries:
        logger.info("no repos configured; nothing to index")
        return 0

    if cfg is None:
        cfg = get_settings()

    if workspace_client is None:
        from databricks.sdk import WorkspaceClient

        workspace_client = WorkspaceClient()
    token = read_github_token(workspace_client, scope, key)

    owns_http = http_client is None
    if http_client is None:
        http_client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=60.0)

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Capture the serverless interpreter version in the run log (M2 live gate).
    logger.info("code-search-index starting on Python %s", sys.version)

    parser = argparse.ArgumentParser(description="Index GitHub repos into the code search DB.")
    parser.add_argument("--repos", default="", help="Comma/space-separated org/repo entries.")
    parser.add_argument("--scope", required=True, help="Databricks secret scope for the GH token.")
    parser.add_argument("--key", required=True, help="Secret key within the scope.")
    parser.add_argument("--endpoint", default=None, help="Lakebase endpoint identifier.")
    parser.add_argument("--database", default=None, help="Postgres database name.")
    args = parser.parse_args()

    exit_code = run(
        repos=args.repos,
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
