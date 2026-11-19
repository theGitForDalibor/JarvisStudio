"""EventBroadcaster: In-memory Pub/Sub broker for live task execution events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis_studio.core.tasks.models import EventRecord

_logger = logging.getLogger(__name__)


class EventBroadcaster:
    """In-memory pub/sub broker distributing recorded EventRecords to live task subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord]]] = {}
        self._lock = asyncio.Lock()

    def broadcast(self, event: EventRecord) -> int:
        """Distribute event to all active subscriber queues for event.task_id."""
        task_id = event.task_id
        queues = self._subscribers.get(task_id, set())
        delivered = 0
        for q in list(queues):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                _logger.warning("Subscriber queue for task %s is full; dropped event %s", task_id, event.seq)
        return delivered

    @asynccontextmanager
    async def subscribe(
        self, task_id: str, queue_size: int = 500
    ) -> AsyncIterator[asyncio.Queue[EventRecord]]:
        """Context manager yielding a per-subscriber asyncio.Queue for task_id."""
        q: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=queue_size)
        async with self._lock:
            self._subscribers.setdefault(task_id, set()).add(q)
        try:
            yield q
        finally:
            async with self._lock:
                task_subs = self._subscribers.get(task_id)
                if task_subs is not None:
                    task_subs.discard(q)
                    if not task_subs:
                        self._subscribers.pop(task_id, None)

    async def get_subscriber_count(self, task_id: str) -> int:
        """Return count of active subscribers for task_id."""
        async with self._lock:
            return len(self._subscribers.get(task_id, ()))


_global_event_broadcaster: EventBroadcaster | None = None


def get_event_broadcaster() -> EventBroadcaster:
    """Return process-wide singleton EventBroadcaster."""
    global _global_event_broadcaster
    if _global_event_broadcaster is None:
        _global_event_broadcaster = EventBroadcaster()
    return _global_event_broadcaster


def set_event_broadcaster(broadcaster: EventBroadcaster | None) -> None:
    """Set process-wide singleton EventBroadcaster."""
    global _global_event_broadcaster
    _global_event_broadcaster = broadcaster


def reset_global_event_broadcaster() -> None:
    """Reset global EventBroadcaster."""
    global _global_event_broadcaster
    _global_event_broadcaster = None
