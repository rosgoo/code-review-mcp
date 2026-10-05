import asyncio
import json
from collections.abc import Mapping


class ReviewHub:
    """In-memory, per-review wake-ups for submit waits and SSE fan-out.

    A submit sets the review's event until a waiter consumes it, so a submit that lands
    between two waits still wakes the next one. State is lost when the process exits.
    """

    def __init__(self) -> None:
        self._submit_events: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    def _submit_event(self, review_id: str) -> asyncio.Event:
        return self._submit_events.setdefault(review_id, asyncio.Event())

    def notify_submit(self, review_id: str) -> None:
        self._submit_event(review_id).set()

    async def wait_for_submit(self, review_id: str, timeout: float) -> bool:
        """Return True once a submit is pending for the review (consuming it), False on timeout."""
        event = self._submit_event(review_id)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        event.clear()
        return True

    def subscribe(self, review_id: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.setdefault(review_id, set()).add(queue)
        return queue

    def unsubscribe(self, review_id: str, queue: asyncio.Queue[str]) -> None:
        subscribers = self._subscribers.get(review_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[review_id]

    def subscriber_count(self, review_id: str) -> int:
        return len(self._subscribers.get(review_id, ()))

    def publish(
        self, review_id: str, event_type: str, payload: Mapping[str, object] | None = None
    ) -> None:
        message = json.dumps({"type": event_type, "review_id": review_id, **(payload or {})})
        for queue in self._subscribers.get(review_id, ()):
            queue.put_nowait(message)
