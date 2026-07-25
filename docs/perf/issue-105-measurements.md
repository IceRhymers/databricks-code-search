# Issue #105 — batched writes: measurements

Recorded once, at the point step 7 of the execution plan committed it, against
local Postgres 16 (`codesearch-pg`) and a throwaway `git worktree` at the
branch point `732a7d7` (`origin/integration/indexer-performance`, pre-#105).
Both scripts are reproduced here in full so the numbers below are re-derivable,
not just asserted; they are throwaway measurement scripts, not part of the test
suite (which is where the gate-asserted numbers — test 17's statement count and
test 14/15/15b/18's parity assertions — actually live).

## 1. Statement count per file (AC 1: 3–7 → ≲0.05)

Gate-asserted by `tests/integration/test_store_batching.py`'s
`test_statement_count_per_file_meets_the_acceptance_criterion` (test 17), a real
`before_cursor_execute` listener over a 600-file first-time index:

```
statements/file at _BATCH_MAX_FILES=500, N=600: 0.0133 (issue AC: <= 0.05)
```

Matches §2.1's predicted arithmetic (`16 statements / 500 files ≈ 0.032` at a
realistic symbol/edge mix; `0.02` on the pure per-file-only shape this fixture
uses) — measured, not just predicted, and comfortably inside the ≲0.05 target.

## 2. Cross-version corpus parity (AC 3)

The check `tests/integration/test_store_batching.py`'s tests 14/15/15b/18
structurally cannot do: a diff against the *actual* pre-#105 implementation,
not just batched-against-batched. A 3-branch, 33-distinct-row fixture (5
content-deduped files, 2 divergent-content files × 3 branches, one
zero-symbols file, one file with a real edge, 18 branch-unique files) indexed
identically in a throwaway schema on both trees, then dumped as
`(repo_id, path, content_sha, lang, size, content, commit, sorted(branches))`
for `files`, `(path, name, kind, start_line, end_line)` for `symbols`, and the
9-field tuple for `reference_edges` (serial ids excluded):

```
$ git worktree add /tmp/dcs105-perf/base-732a7d7 732a7d7
$ cd /tmp/dcs105-perf/base-732a7d7 && uv run python parity_check.py before > before.json
$ cd <this branch> && uv run python parity_check.py after > after.json
$ diff before.json after.json && echo "IDENTICAL — empty diff"
IDENTICAL — empty diff
```

`files: 33, symbols: 32, edges: 3` on both sides, dict-equal. The parity
harness script (`parity_check.py`, reproduced below) feeds `index_repo` an
`items` list directly, so it is unaffected by #106's change of file source —
exactly the reasoning §3.4 requires for comparing against `732a7d7`, not
`17aeb4f`.

<details>
<summary><code>parity_check.py</code></summary>

```python
import json
import sys
from uuid import uuid4

from sqlalchemy import text

from app.db.client import create_db_engine
from app.db.models import Base
from indexer.languages import ExtractedEdge, ExtractedSymbol, FileExtraction, ParsedFile
from indexer.store import index_repo


def _pf(path, content):
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _sym(prefix, n):
    return ExtractedSymbol(f"{prefix}{n}", "function", 1, 2)


def _fixture_items(branch):
    items = []
    for i in range(5):
        content = f"def shared{i}():\n    return {i}\n"
        items.append(
            (_pf(f"shared{i}.py", content), FileExtraction(symbols=[_sym("shared", i)], edges=[]))
        )
    for i in range(2):
        magic = (sum(ord(c) for c in branch) * 31 + i) % 997
        content = f"def divergent{i}():\n    return {magic}\n"
        items.append(
            (
                _pf(f"divergent{i}.py", content),
                FileExtraction(symbols=[_sym("divergent", i)], edges=[]),
            )
        )
    items.append((_pf("nosymbols.py", "# just a comment\n"), FileExtraction(symbols=[], edges=[])))
    caller = _sym("caller", 0)
    items.append(
        (
            _pf(f"{branch}_withedges.py", "def caller0():\n    callee()\n"),
            FileExtraction(
                symbols=[caller],
                edges=[ExtractedEdge(kind="call", target="callee", line=2, enclosing=caller)],
            ),
        )
    )
    for i in range(6):
        content = f"def {branch}_only{i}():\n    return {i}\n"
        items.append(
            (
                _pf(f"{branch}_only{i}.py", content),
                FileExtraction(symbols=[_sym(f"{branch}_only", i)], edges=[]),
            )
        )
    return items


def main():
    label = sys.argv[1]
    schema = f"parity_{label}_{uuid4().hex[:8]}"
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()
        Base.metadata.create_all(bind=conn)
        conn.commit()

        index_repo(
            conn,
            name="acme/widgets",
            branch="a",
            is_default=True,
            head_sha="sha_a",
            items=_fixture_items("a"),
        )
        conn.commit()
        index_repo(
            conn,
            name="acme/widgets",
            branch="b",
            is_default=False,
            head_sha="sha_b",
            items=_fixture_items("b"),
        )
        conn.commit()
        index_repo(
            conn,
            name="acme/widgets",
            branch="c",
            is_default=False,
            head_sha="sha_c",
            items=_fixture_items("c"),
        )
        conn.commit()

        files = sorted(
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT repo_id, path, content_sha, lang, size, content, commit, "
                    "array_to_string((SELECT array_agg(x ORDER BY x) FROM unnest(branches) x), ',') "
                    "FROM files ORDER BY path, content_sha"
                )
            ).all()
        )
        symbols = sorted(
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT f.path, s.name, s.kind, s.start_line, s.end_line "
                    "FROM symbols s JOIN files f ON f.id = s.file_id"
                )
            ).all()
        )
        edges = sorted(
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT f.path, e.edge_kind, e.target_name, e.line, e.enclosing_name, e.enclosing_kind, "
                    "e.enclosing_start_line, e.enclosing_end_line "
                    "FROM reference_edges e JOIN files f ON f.id = e.file_id"
                )
            ).all()
        )
        print(json.dumps({"files": files, "symbols": symbols, "edges": edges}))
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


if __name__ == "__main__":
    main()
```

</details>

## 3. `db=` before/after (AC 2: ≥5x on a full index) — NOT verified against Lakebase

No dev Lakebase target was available in this environment, so per §3.4 this AC
is reported with both caveats and a labelled projection and is **not ticked**.
3000 first-time (full-path) files, `PhaseTimer` installed the same way
`indexer.job` installs it around `index_repo`, `db = wall − sweep` (no `parse`
term: `items` is a plain list already in hand, not lazily produced), three
runs per tree:

| Tree | Run 1 | Run 2 | Run 3 | Mean |
|---|---|---|---|---|
| `732a7d7` (before, unbatched) | 4.1906s | 4.1697s | 4.2105s | **4.2036s** |
| this branch (after, batched) | 0.6550s | 0.6622s | 0.6607s | **0.6593s** |

Measured local speedup: **4.2036 / 0.6593 ≈ 6.38×** — already past the issue's
5x threshold, even on loopback Postgres. Two caveats, both named up front
(§3.4), pushing in opposite directions:

- *Understating:* local Postgres over TCP loopback has ~0.05–0.15 ms round
  trips; Lakebase's are 1–2 ms. The protocol-chatter component of the win
  (statement count × round-trip latency) is understated here by roughly an
  order of magnitude relative to production.
- *Not understating:* the missing `symbols.file_id` index (§2.0 of the plan)
  means the per-file/per-batch `DELETE FROM symbols WHERE file_id = ...` is a
  sequential scan of the whole `symbols` table either way — CPU/IO-bound, not
  round-trip-bound, so it shows up in this local number at full weight and is
  not an artifact of the loopback environment.

**Projection** (labelled as such, not measured): unbatched, this fixture's
plain per-file symbol-only shape costs 4 statements/file (file-upsert,
symbols-delete, symbols-insert, edges-delete); batched, it costs 0.0133 (§1,
test 17's 600-file gate). Δround-trips/file ≈ 4 − 0.0133 ≈ 3.99. At a Lakebase
round trip of 1–2 ms, that projects an ADDITIONAL 4.0–8.0 ms/file of pure protocol
chatter saved over the local number, on top of whatever the CPU-bound
`symbols` scan component already contributes locally — i.e. the production win
is expected to be **larger** than 6.38×, not smaller, but this is a projection,
not a Lakebase measurement, and AC 2 is carried as an open item on epic #110
pending a real Lakebase run.

<details>
<summary><code>timing_check.py</code></summary>

```python
import sys
import time
from uuid import uuid4

from sqlalchemy import text

from app.db.client import create_db_engine
from app.db.models import Base
from indexer.languages import ExtractedSymbol, FileExtraction, ParsedFile
from indexer.store import index_repo
from indexer.timing import PhaseTimer, install_timer, reset_timer


def _pf(path, content):
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _fixture(n):
    items = []
    for i in range(n):
        content = f"def f{i}():\n    x = {i}\n    return x\n"
        items.append(
            (
                _pf(f"pkg/mod{i}.py", content),
                FileExtraction(symbols=[ExtractedSymbol(f"f{i}", "function", 1, 3)], edges=[]),
            )
        )
    return items


def main():
    label = sys.argv[1]
    n = int(sys.argv[2])
    schema = f"timing_{label}_{uuid4().hex[:8]}"
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()
        Base.metadata.create_all(bind=conn)
        conn.commit()

        items = _fixture(n)

        timer = PhaseTimer()
        token = install_timer(timer)
        try:
            wall_start = time.perf_counter()
            index_repo(
                conn,
                name="acme/bigrepo",
                branch="main",
                is_default=True,
                head_sha="sha1",
                items=items,
            )
            wall = time.perf_counter() - wall_start
        finally:
            reset_timer(token)

        sweep = timer.total("sweep")
        db = wall - sweep
        print(f"n={n} wall={wall:.4f}s sweep={sweep:.4f}s db(wall-sweep)={db:.4f}s")
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


if __name__ == "__main__":
    main()
```

</details>
