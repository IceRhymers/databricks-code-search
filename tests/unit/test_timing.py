"""Unit tests for indexer.timing: the ambient per-phase accumulator.

Two properties the whole instrumentation rests on are pinned here, away from the
job pipeline that consumes them: ``record`` is a no-op when nobody is measuring
(so ``index_repo`` stays callable directly), and one thread's totals are
invisible to another (so fan-out cannot cross-attribute a phase).
"""

from __future__ import annotations

import threading

import pytest

from indexer.timing import PhaseTimer, current_timer, install_timer, record, reset_timer


@pytest.mark.unit
def test_timer_accumulates_and_is_a_noop_when_unset() -> None:
    """Default-unset must be silent, not merely harmless.

    ``tests/integration/test_store.py`` calls ``index_repo`` directly with no
    timer in context; the sweep hook in ``store.py`` runs there regardless, so
    ``record`` with nothing installed has to do nothing AND not raise.
    """
    assert current_timer() is None
    record("sweep", 1.0)  # must not raise
    assert current_timer() is None

    timer = PhaseTimer()
    assert timer.total("sweep") == 0.0  # an unrecorded phase reads as zero, not KeyError

    token = install_timer(timer)
    try:
        assert current_timer() is timer
        record("sweep", 1.5)
        record("sweep", 0.25)  # accrues, never overwrites
        record("parse", 2.0)
    finally:
        reset_timer(token)

    assert timer.total("sweep") == 1.75
    assert timer.total("parse") == 2.0
    assert timer.total("download") == 0.0

    # The reset really uninstalled it: a later record goes nowhere.
    assert current_timer() is None
    record("sweep", 100.0)
    assert timer.total("sweep") == 1.75


@pytest.mark.unit
def test_timer_is_isolated_per_thread() -> None:
    """Each thread starts with a fresh context, so timers cannot cross-attribute.

    This is what lets one ``PhaseTimer`` per branch be correct while four worker
    threads index four repos concurrently.
    """
    totals: dict[str, float] = {}
    start = threading.Barrier(2)

    def worker(label: str, amount: float) -> None:
        # A new thread inherits no context: nothing is installed here yet.
        assert current_timer() is None
        timer = PhaseTimer()
        token = install_timer(timer)
        try:
            start.wait(timeout=5)  # force real overlap, not sequential execution
            record("db", amount)
            start.wait(timeout=5)
            totals[label] = timer.total("db")
        finally:
            reset_timer(token)

    threads = [
        threading.Thread(target=worker, args=("a", 3.0)),
        threading.Thread(target=worker, args=("b", 7.0)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert totals == {"a": 3.0, "b": 7.0}
    assert current_timer() is None  # and nothing leaked back to the main thread
