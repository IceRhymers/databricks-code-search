"""Ambient per-phase wall-clock accounting for one branch's indexing pipeline.

``indexer.job`` measures most of a branch's phases (resolve / download / parse /
embed / db) inline, but ``sweep`` runs deep inside
:func:`indexer.store.index_repo`, behind the injected ``index_fn`` seam and a
frozen ``IndexCounts`` return type. Threading a timer through that seam would
force every existing ``index_fn`` fake to grow a parameter, turning a log-only
change into a storage-contract change -- so the timer is carried *ambiently* in a
:class:`contextvars.ContextVar`, exactly mirroring ``indexer.job``'s ``_repo_ctx``
+ ``RepoLogFilter`` idiom for the same cross-module attribution problem.

Two properties are load-bearing:

* **Default-unset is a no-op.** :func:`record` with no installed timer does
  nothing and never raises, so ``index_repo`` stays callable directly (as
  ``tests/integration/test_store.py`` does) with no timer in sight.
* **Install/reset discipline.** ``ThreadPoolExecutor`` reuses worker threads and
  does NOT reset their context between tasks, so :func:`install_timer` must
  always be paired with :func:`reset_timer` in a ``finally`` -- a leaked timer
  would silently attribute one branch's sweep to the next branch's line.

The ``ContextVar`` does not cross a thread or process boundary: it isolates
concurrent worker threads from each other (which is the property this module
needs today), but a call to :func:`record` from a different thread or a
``ProcessPoolExecutor`` worker than the one that called :func:`install_timer`
reaches a *different* ambient timer (or none), not the installing thread's.
Relevant if a future phase's work is moved off the indexing worker thread --
e.g. issue #108's process-pool extraction, or issue #107's concurrent
embedding.

``_CLOCK`` is the module's single clock source. Every interval that appears on
an asserted log line reads it (via :func:`now`, or via
:attr:`PhaseTimer.clock` which captures it at construction), never a bare
``time.monotonic()`` -- mixing a fake clock with the real one makes the residual
``other=`` field meaningless. Tests patch ``indexer.timing._CLOCK`` before
driving ``run()``; that is the only injection point that reaches a
worker-constructed timer.

Stdlib only (``time`` + ``contextvars``): this module is imported by the
indexing hot path and adds no dependency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar, Token

# The single clock source for every duration on the phase-timing and repo
# `finished` log lines. Monotonic (never NTP-adjusted) and patched wholesale by
# tests -- do not read time.monotonic() directly anywhere those numbers are
# asserted against each other.
_CLOCK: Callable[[], float] = time.monotonic


def now() -> float:
    """Read the current clock, resolving ``_CLOCK`` at call time (test seam)."""
    return _CLOCK()


class PhaseTimer:
    """A per-branch accumulator of ``phase -> seconds``.

    Not thread-safe by design: one timer belongs to exactly one branch, which is
    indexed by exactly one worker thread. Cross-thread isolation comes from the
    ``ContextVar`` below, not from locking.
    """

    def __init__(self) -> None:
        # Captured at construction (not read per call) so one branch's
        # arithmetic can never straddle a clock swap mid-flight. There is no
        # constructor override for this: `record()` reaches this timer only
        # through the ambient ContextVar, always via the module-level
        # `_CLOCK`, so a per-instance clock would silently desync from the
        # sweep timing recorded through `now()` in indexer/store.py.
        self.clock: Callable[[], float] = _CLOCK
        self._totals: dict[str, float] = {}

    def add(self, phase: str, seconds: float) -> None:
        """Accumulate ``seconds`` into ``phase`` (phases are accrued, not set)."""
        self._totals[phase] = self._totals.get(phase, 0.0) + seconds

    def total(self, phase: str) -> float:
        """Seconds accrued to ``phase``; ``0.0`` for a phase that never ran."""
        return self._totals.get(phase, 0.0)


# Ambient per-thread timer. Default None = "nobody is measuring", which is the
# state every direct index_repo caller (and every non-worker thread) sees.
_timer_ctx: ContextVar[PhaseTimer | None] = ContextVar("phase_timer", default=None)


def install_timer(timer: PhaseTimer) -> Token[PhaseTimer | None]:
    """Make ``timer`` the ambient one for this context; reset the token in a ``finally``."""
    return _timer_ctx.set(timer)


def reset_timer(token: Token[PhaseTimer | None]) -> None:
    """Undo :func:`install_timer`. Mandatory -- see the module docstring."""
    _timer_ctx.reset(token)


def current_timer() -> PhaseTimer | None:
    """The ambient timer, or ``None`` when nothing is measuring."""
    return _timer_ctx.get()


def record(phase: str, seconds: float) -> None:
    """Accrue ``seconds`` to ``phase`` on the ambient timer, if there is one.

    A no-op when no timer is installed. Instrumentation must never be able to
    fail the work it measures.
    """
    timer = _timer_ctx.get()
    if timer is None:
        return
    timer.add(phase, seconds)
