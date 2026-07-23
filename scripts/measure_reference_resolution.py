"""Measure the ``reference_edges`` resolution distribution (#86, AC4).

Offline companion to :mod:`app.search.references`. Reuses :func:`build_candidate_count_select`
and :func:`classify_resolution` from that module -- the SAME join semantics and branch-scoping
predicate ``resolve_references``'s query 2 uses -- so this script's distribution agrees with the
live serve path BY CONSTRUCTION, rather than re-implementing the join and risking drift.

**Pinned: ``call`` edges are the primary headline metric**, the only number compared against
the prior-art baseline (28.8% unique / 33.4% ambiguous / 37.8% external). The ``import``-edge
distribution is reported separately, labeled informational -- it is expected to resolve
close to 0% (import targets are largely external/stdlib modules; see D3's exact-dotted-match
decision in ``docs/runbooks/reference-edges.md``) and is NOT compared against the call-edge
baseline.

``build_candidate_count_select`` has no per-request latency/timeout budget (unlike
``resolve_references``): it runs a correlated subquery per site, which is fine for a one-off
offline measurement but would be an unacceptable query shape to expose to a live caller.

Usage: ``uv run python scripts/measure_reference_resolution.py [--edge-kind call|import|both]
[--branch BRANCH] [--target NAME] [--use-resolver]``. Requires the standard ``PG*``/Lakebase
connection env (see ``app.db.client.create_db_engine``).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

from app.db.client import create_db_engine
from app.search.references import (
    build_candidate_count_select,
    classify_resolution,
    resolve_references,
)

# Prior-art comparison target from the epic's deep-dive probe (not a repo-sourced constant) --
# re-measured here, which is what actually satisfies AC4.
CALL_BASELINE_PCT = {"unique": 28.8, "ambiguous": 33.4, "unresolved": 37.8}

_BUCKETS = ("unique", "ambiguous", "unresolved")


# --------------------------------------------------------------------------- pure helpers


def bucket_counts(counts: Sequence[int]) -> dict[str, int]:
    """Classify each site's true candidate count into a resolution bucket via the SAME
    :func:`~app.search.references.classify_resolution` the resolver itself uses."""
    buckets = {bucket: 0 for bucket in _BUCKETS}
    for count in counts:
        buckets[classify_resolution(count)] += 1
    return buckets


def ambiguous_histogram(counts: Sequence[int]) -> dict[int, int]:
    """Candidate-count histogram restricted to ambiguous sites (count >= 2)."""
    histogram: dict[int, int] = {}
    for count in counts:
        if count >= 2:
            histogram[count] = histogram.get(count, 0) + 1
    return histogram


def format_distribution(
    label: str, buckets: dict[str, int], *, baseline: dict[str, float] | None = None
) -> str:
    total = sum(buckets.values())
    lines = [f"{label} (n={total}):"]
    for bucket in _BUCKETS:
        count = buckets[bucket]
        pct = (count / total * 100) if total else 0.0
        line = f"  {bucket:<11s} {count:>7d}  {pct:5.1f}%"
        if baseline is not None:
            line += f"   (baseline {baseline[bucket]:.1f}%)"
        lines.append(line)
    return "\n".join(lines)


def format_histogram(histogram: dict[int, int]) -> str:
    if not histogram:
        return "  (no ambiguous sites)"
    return "\n".join(
        f"  candidate_count={count:<4d} sites={histogram[count]}" for count in sorted(histogram)
    )


# ------------------------------------------------------------------------------ DB legs


def _fetch_counts(
    conn: Any, *, edge_kind: str, branch: str | None, target: str | None
) -> list[int]:
    """Every matching site's TRUE candidate count, via the shared count builder (D10).

    Runs in its own ``conn.begin()``/commit: a bare ``conn.execute()`` auto-begins an
    implicit transaction that stays open until explicitly closed, which would otherwise
    collide with ``resolve_references``'s own ``with conn.begin():`` on a later call
    reusing this same connection (``--use-resolver``).
    """
    stmt = build_candidate_count_select(edge_kind=edge_kind, branch=branch)
    with conn.begin():
        rows = conn.execute(stmt).all()
    if target is not None:
        rows = [row for row in rows if row.target_name == target]
    return [row.candidate_count for row in rows]


def _resolver_spot_check(
    conn: Any, *, edge_kind: str, branch: str | None, row_limit: int
) -> dict[str, int]:
    """Drive ``resolve_references`` directly over a ``row_limit``-bounded sample and bucket its
    OWN ``site.resolution`` field -- a live-path sanity check, not a full-corpus comparison
    (the live resolver's query 2 is window-bounded per name; this builder's isn't)."""
    result = resolve_references(conn, edge_kind=edge_kind, branch=branch, row_limit=row_limit)
    buckets = {bucket: 0 for bucket in _BUCKETS}
    for site in result.sites:
        buckets[site.resolution] += 1
    return buckets


# --------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-kind", choices=("call", "import", "both"), default="both")
    parser.add_argument(
        "--branch", default=None, help="branch scope (default: each repo's default branch)"
    )
    parser.add_argument("--target", default=None, help="restrict to one target_name (debugging)")
    parser.add_argument(
        "--use-resolver",
        action="store_true",
        help="also spot-check via resolve_references directly (bounded sample, not full corpus)",
    )
    parser.add_argument("--resolver-row-limit", type=int, default=500)
    args = parser.parse_args(argv)

    kinds = ["call", "import"] if args.edge_kind == "both" else [args.edge_kind]

    engine = create_db_engine()
    with engine.connect() as conn:
        for kind in kinds:
            counts = _fetch_counts(conn, edge_kind=kind, branch=args.branch, target=args.target)
            buckets = bucket_counts(counts)
            baseline = CALL_BASELINE_PCT if kind == "call" else None
            label = (
                "call edges -- HEADLINE AC4 metric"
                if kind == "call"
                else "import edges -- informational, expected ~0% resolution (validates D3)"
            )
            print(format_distribution(label, buckets, baseline=baseline))
            print("ambiguous candidate-count histogram:")
            print(format_histogram(ambiguous_histogram(counts)))
            print()

            if args.use_resolver:
                sample_buckets = _resolver_spot_check(
                    conn, edge_kind=kind, branch=args.branch, row_limit=args.resolver_row_limit
                )
                print(
                    format_distribution(
                        f"{kind} edges -- resolve_references spot check "
                        f"(row_limit={args.resolver_row_limit}, bounded sample)",
                        sample_buckets,
                    )
                )
                print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
