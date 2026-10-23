"""Env-gated process crash and stall points for recovery tests.

A crash point kills the process at a named step, between two durable writes, so a
test can prove that the next start recovers. It is active only when both
``EARSHOT_TEST_MODE=1`` and ``EARSHOT_TEST_CRASH_AT=<name>`` are set; otherwise
``crash_point`` does nothing.

A stall point stops the process at a named step for good, alive but making no
progress (it reads nothing more), so a test can prove a bound holds against a peer
that is stopped, frozen or swapping. It is active only when both
``EARSHOT_TEST_MODE=1`` and ``EARSHOT_TEST_STALL_AT=<name>`` are set; otherwise
``stall_point`` does nothing.

A fault point raises ``InjectedFault`` (an ``OSError``) at a named step, so a test
can prove an error the code cannot otherwise be made to see is handled: one after a
commit, as a failed ``chmod`` or a lost commit answer raises. It is active only when
both ``EARSHOT_TEST_MODE=1`` and ``EARSHOT_TEST_FAULT_AT=<name>`` are set; otherwise
``fault_point`` does nothing.

Every name a call site may use is declared here.
"""

from __future__ import annotations

import os
import time

CRASH_EXIT_CODE = 137
CRASH_POINTS = frozenset(
    {
        # The frozen 0.1.x converge committed; the Alembic stamp has not run.
        "after_legacy_bootstrap",
        # An OTLP request's spans, traces, calls and counters are written; its
        # transaction has not committed (``CallStore.record_spans``).
        "before_otlp_commit",
        # The closer's claim committed (dirty cleared, revision pending, leased);
        # nothing is materialized yet (``Closer.claim``).
        "after_claim_commit",
        # A revision's incident and its finish committed together; the closer has
        # not returned yet (``Closer.finish``).
        "after_catalog_commit",
        # The closer's materializer child received a claim's snapshot and has not
        # answered: the child dies, the server does not (``MaterializerProcess``).
        "during_materialize",
        # The closer's materializer child has started and is not yet ready: it dies,
        # so the materializer cannot start (``MaterializerProcess``).
        "materializer_starting",
    }
)


def crash_point(name: str) -> None:
    """Exit immediately, without cleanup, if the test environment selects ``name``."""

    if name not in CRASH_POINTS:
        raise ValueError(f"undeclared crash point: {name!r}")
    if os.environ.get("EARSHOT_TEST_MODE") == "1" and (
        os.environ.get("EARSHOT_TEST_CRASH_AT") == name
    ):
        os._exit(CRASH_EXIT_CODE)


STALL_POINTS = frozenset(
    {
        # The closer's materializer child received a claim's snapshot and reads none
        # of its spans: the parent's sends block once the pipe is full
        # (``MaterializerProcess``).
        "materializer_reading_snapshot",
    }
)


def stall_point(name: str) -> None:
    """Stop making progress for good, if the test environment selects ``name``."""

    if name not in STALL_POINTS:
        raise ValueError(f"undeclared stall point: {name!r}")
    if os.environ.get("EARSHOT_TEST_MODE") == "1" and (
        os.environ.get("EARSHOT_TEST_STALL_AT") == name
    ):
        while True:
            time.sleep(3600)


FAULT_POINTS = frozenset(
    {
        # A revision's incident and its finish committed together; a step after the
        # commit raises (``Closer.finish``).
        "after_catalog_commit",
    }
)


class InjectedFault(OSError):
    """What a fault point raises."""


def fault_point(name: str) -> None:
    """Raise ``InjectedFault`` if the test environment selects ``name``."""

    if name not in FAULT_POINTS:
        raise ValueError(f"undeclared fault point: {name!r}")
    if os.environ.get("EARSHOT_TEST_MODE") == "1" and (
        os.environ.get("EARSHOT_TEST_FAULT_AT") == name
    ):
        raise InjectedFault(f"injected fault at {name}")
