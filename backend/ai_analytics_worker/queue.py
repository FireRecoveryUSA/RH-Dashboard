"""AI Analytics Worker — claim deduplication / debounce queue (Phase 6).

Coalesces multiple change events for the same claim into a single refresh,
preventing unnecessary repeated rebuilds during a burst of updates to the
same claim. With ~98.6% of claims having 0-1 updates (Phase 0 audit) the
queue is mostly a pass-through, but it protects against the rare burst
scenario and against backfill + change-stream overlap.

Design:
- ``ClaimQueue`` is an in-process (asyncio) unbounded queue. It is NOT
  durable — the reconciliation safety net (Phase 7) covers any events lost
  on a crash between enqueue and refresh.
- ``enqueue(claim_id)`` records the current monotonic time for the claim.
  If the claim is already pending, the timer is **reset** — the claim waits
  another full debounce window. This is the coalescing mechanism: a burst
  of N updates to the same claim within ``debounce_seconds`` results in a
  single ``refresh_claim`` call.
- ``pop_ready()`` removes and returns all claim_ids that have been in the
  queue for at least ``debounce_seconds``.
- ``run_queue_consumer`` is the async loop that drains the queue, calls
  ``refresh_claim`` for each ready claim, and respects ``stop_event`` for
  graceful shutdown. It also yields to the event loop every
  ``max_claims_per_cycle`` claims (Section 1.1.5 starvation guard).

Lifecycle: created by ``run_worker`` (main.py), shared by the change-stream
listener (producer), the reconciliation loop (producer), and the queue
consumer (drainer). Closed when the worker shuts down.

Source: none (in-memory data structure).
Destination: ``ai_invoice_analytics`` and ``ai_analytics_worker_dead_letters``
(via ``refresh_claim`` called by the consumer).
Architectural constraints:
-- Never blocks the FastAPI event loop — all waits are ``await asyncio``.
-- Cancellable via ``stop_event`` — the consumer exits promptly.
-- One bad claim never stops the consumer — dead-lettered (via refresh_claim).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .claim_refresh import refresh_claim
from .config import worker_config

logger = logging.getLogger(__name__)


class ClaimQueue:
    """In-process claim deduplication and debounce queue.

    Coalesces multiple change events for the same claim into a single
    refresh. When a claim_id is enqueued, its debounce timer starts (or
    resets if already pending). The claim becomes "ready" after
    ``debounce_seconds`` have elapsed without a new enqueue for the same
    claim.

    The queue is unbounded and in-memory. It is not durable across restarts
    — the reconciliation safety net (Phase 7) covers lost events.

    Lifecycle: created by ``run_worker``, shared by producers (listener,
    reconciliation) and the consumer. ``close()`` is called during shutdown
    to wake the consumer.

    Inputs: ``enqueue(claim_id)`` calls from producers.
    Outputs: ``pop_ready()`` returns claim_ids ready for refresh.
    Dependencies: ``debounce_seconds`` (config), ``asyncio.Event`` for wakeup.
    Error behavior: never raises — ``enqueue`` returns False if closed.
    """

    def __init__(
        self,
        debounce_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the queue.

        Arguments:
            debounce_seconds: how long a claim_id must remain in the queue
                without being re-enqueued before it is "ready" to drain.
                0 means no debounce (items are ready immediately).
            clock: monotonic time source returning seconds as a float.
                Defaults to ``time.monotonic``. Injectable so tests can
                advance time deterministically instead of calling
                ``time.sleep`` — sleep-based tests are flaky on Windows,
                where the default timer granularity (~15.6ms) can exceed a
                short debounce window. Must be monotonic; a wall-clock
                source would break the window across NTP adjustments.
        """
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        # claim_id -> monotonic timestamp of the most recent enqueue.
        self._pending: Dict[int, float] = {}
        # claim_id -> source label of the most recent enqueue, used by the
        # consumer to label dead-letters with the originating pipeline
        # (change_stream / reconciliation / sync_integrity). A re-enqueue
        # overwrites the source so the last producer wins — consistent with
        # the timer-reset semantics, and the right call for audit purposes
        # since the most recent event is the one that triggered the refresh.
        self._sources: Dict[int, str] = {}
        # Set when items are added or the queue is closed, to wake the
        # consumer's ``asyncio.Event.wait()``.
        self._wakeup = asyncio.Event()
        self._closed = False

    def enqueue(self, claim_id: int, source: str = "change_stream") -> bool:
        """Add or refresh a claim_id in the queue.

        If the claim_id is already pending, its debounce timer is reset
        (the claim waits another full debounce window). This is the
        coalescing mechanism — a burst of N updates to the same claim
        within ``debounce_seconds`` results in a single refresh.

        Arguments:
            claim_id: the claim to enqueue for refresh.
            source: the originating pipeline label, recorded on the
                dead-letter audit trail by the consumer. One of
                ``"change_stream"`` / ``"reconciliation"`` /
                ``"sync_integrity"``. Defaults to ``"change_stream"`` for
                backward compatibility with existing callers that don't
                pass it explicitly.

        Returns:
            True if the claim_id was newly added.
            False if it was already pending (timer reset) or the queue
            is closed.
        """
        if self._closed:
            return False
        is_new = claim_id not in self._pending
        self._pending[claim_id] = self._clock()
        self._sources[claim_id] = source
        self._wakeup.set()
        return is_new

    def close(self) -> None:
        """Signal that no more items will be enqueued.

        Wakes up the consumer so it can drain remaining ready items and
        exit. Items still within their debounce window when the consumer
        shuts down are lost — the reconciliation safety net covers them.
        """
        self._closed = True
        self._wakeup.set()

    @property
    def closed(self) -> bool:
        """True if ``close()`` has been called."""
        return self._closed

    @property
    def size(self) -> int:
        """Number of claim_ids currently pending (not yet drained)."""
        return len(self._pending)

    def pop_ready_with_source(self) -> List[Tuple[int, str]]:
        """Remove and return ready (claim_id, source) pairs.

        Same semantics as ``pop_ready`` but returns the source label
        alongside each claim_id so the consumer can pass it through to
        ``refresh_claim`` for the dead-letter audit trail.

        Returns:
            List of ``(claim_id, source)`` tuples (arbitrary order).
            Empty if none are ready.
        """
        if not self._pending:
            return []
        now = self._clock()
        ready: List[Tuple[int, str]] = []
        for claim_id, enqueue_time in list(self._pending.items()):
            if now - enqueue_time >= self._debounce_seconds:
                ready.append((claim_id, self._sources.pop(claim_id, "change_stream")))
                del self._pending[claim_id]
        if not self._pending:
            # No pending items left — clear the wakeup so the consumer
            # blocks until the next enqueue or close.
            self._wakeup.clear()
        return ready

    def pop_ready(self) -> List[int]:
        """Remove and return all claim_ids that have passed the debounce window.

        A claim_id is "ready" if it has been in the queue for at least
        ``debounce_seconds`` without being re-enqueued. Ready claim_ids
        are removed from the queue and returned.

        Returns:
            List of ready claim_ids (arbitrary order). Empty if none are
            ready.
        """
        return [claim_id for claim_id, _ in self.pop_ready_with_source()]

    def seconds_until_next_ready(self) -> Optional[float]:
        """Return seconds until the next claim_id becomes ready.

        Returns:
            Seconds until the earliest pending claim_id passes its debounce
            window (clamped to >= 0), or None if the queue is empty.
        """
        if not self._pending:
            return None
        now = self._clock()
        min_remaining = float("inf")
        for enqueue_time in self._pending.values():
            remaining = self._debounce_seconds - (now - enqueue_time)
            if remaining < min_remaining:
                min_remaining = remaining
        return max(min_remaining, 0.0)

    @property
    def _wakeup_event(self) -> asyncio.Event:
        """Internal: the wakeup event used by ``run_queue_consumer``.

        Exposed so the consumer (in the same module) can clear and wait on
        it without a separate public API. Not intended for external use.
        """
        return self._wakeup


async def run_queue_consumer(
    ai_db: Any,
    db: Any,
    queue: ClaimQueue,
    stop_event: asyncio.Event,
    max_claims_per_cycle: Optional[int] = None,
) -> None:
    """Drain the claim queue and refresh each claim's projection.

    Continuously pops ready claim_ids from the queue and calls
    ``refresh_claim`` for each. Between pops, waits for either the debounce
    timer to expire (making new items ready), a new enqueue to wake it up,
    or ``stop_event`` to be set for graceful shutdown.

    The consumer also updates ``last_checkpoint_at`` on the worker state
    after each successful refresh, so the reconciliation safety net
    (Phase 7) knows the watermark up to which all claims have been
    processed.

    Arguments:
        ai_db: the RecoveryHub_AI Motor database handle (read-only).
        db: the dashboard-owned Motor database handle (writes).
        queue: the ``ClaimQueue`` to drain.
        stop_event: an ``asyncio.Event`` that the caller sets to request
            graceful shutdown.
        max_claims_per_cycle: max claims to process before yielding control
            to the event loop. Defaults to ``worker_config.max_claims_per_cycle``.

    Side effects:
        - Upserts projections into ``ai_invoice_analytics`` (via refresh_claim).
        - Records dead-letters for failing claims (via refresh_claim).
        - Updates ``last_checkpoint_at`` on ``ai_analytics_worker_state`` and
          on the in-memory ``worker_health`` after each successful refresh.
        - Increments ``worker_metrics`` throughput counters (via refresh_claim).

    Raises:
        ``asyncio.CancelledError``: if the caller cancels the task.
        Does not raise for data-level errors — those are dead-lettered
        inside ``refresh_claim``.
    """
    from datetime import UTC, datetime

    from .health import worker_health
    from .projection_repository import update_worker_state

    if max_claims_per_cycle is None:
        max_claims_per_cycle = worker_config.max_claims_per_cycle

    claims_since_yield = 0

    while not stop_event.is_set():
        ready = queue.pop_ready_with_source()
        if ready:
            for claim_id, source in ready:
                if stop_event.is_set():
                    break

                await refresh_claim(
                    ai_db=ai_db,
                    db=db,
                    claim_id=claim_id,
                    source_event_type=source,
                )

                # Update the checkpoint so reconciliation knows the
                # watermark up to which claims have been processed.
                await update_worker_state(
                    db,
                    worker_config.WORKER_NAME,
                    {"last_checkpoint_at": datetime.now(UTC)},
                )
                # Mirror onto the in-memory health state. The Mongo document
                # is what reconciliation reads after a restart; the in-memory
                # copy is what the Phase 8 /ready and /status endpoints read.
                # Writing only Mongo leaves those endpoints reporting null.
                worker_health.mark_checkpoint()

                claims_since_yield += 1
                if claims_since_yield >= max_claims_per_cycle:
                    await asyncio.sleep(0)
                    claims_since_yield = 0
            # INFO (not DEBUG): sync_integrity and reconciliation only log
            # their *enqueue* at INFO, so without this line an operator sees
            # "enqueued N divergent claims" and then silence at the default
            # log level, which reads as a stuck pipeline even though the
            # refreshes succeeded silently.
            logger.info(
                "Queue consumer: refreshed %d claim(s) this cycle (sources: %s).",
                len(ready),
                ", ".join(sorted({s for _, s in ready})),
            )
            continue

        # Nothing ready — check if we should exit.
        if queue.closed and queue.size == 0:
            return

        # Prepare to wait for: new enqueue (wakeup), debounce timer
        # expiry, or stop_event. Clear the wakeup first, then re-check
        # pop_ready to avoid a missed-wakeup race (asyncio is
        # single-threaded, so this sequence is atomic).
        queue._wakeup_event.clear()
        ready = queue.pop_ready_with_source()
        if ready:
            continue

        if queue.closed and queue.size == 0:
            return

        delay = queue.seconds_until_next_ready()
        if delay is not None:
            # Pending items exist — wait for debounce timer or wakeup.
            try:
                await asyncio.wait_for(
                    queue._wakeup_event.wait(),
                    timeout=delay + 0.001,  # tiny epsilon to avoid busy-spin
                )
            except asyncio.TimeoutError:
                pass  # Debounce timer expired — loop and pop_ready
        else:
            # No pending items — wait for enqueue or close, with a
            # short poll interval so stop_event is checked promptly.
            try:
                await asyncio.wait_for(
                    queue._wakeup_event.wait(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                pass  # Poll for stop_event / close

    logger.info("Queue consumer: stopped via stop_event.")
