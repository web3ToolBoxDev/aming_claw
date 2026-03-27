"""Event Bridge: Redis Pub/Sub → MCP notifications.

Subscribes to Redis gov:events:* and forwards events as MCP JSON-RPC
notifications to Claude Code via stdout.  Gracefully degrades if Redis
is unavailable (no crash, just no real-time events).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

# Events worth pushing to Claude Code (others are silently dropped)
NOTIFY_EVENTS = frozenset({
    "gate.blocked",
    "gate.satisfied",
    "task.created",
    "task.completed",
    "task.updated",
    "node.status_changed",
    "release.blocked",
    "release.approved",
    "rollback.executed",
})


class EventBridge:
    """Subscribe Redis gov:events:* → call notify_fn for each event."""

    def __init__(self, redis_url: str, notify_fn: Callable[[str, dict], None]):
        """
        Args:
            redis_url: Redis connection URL (e.g. redis://localhost:6379/0).
            notify_fn: Called with (event_name, payload) for each matching event.
                       This should write an MCP notification to stdout.
        """
        self._redis_url = redis_url
        self._notify = notify_fn
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._subscribe_loop,
            daemon=True,
            name="mcp-event-bridge",
        )
        self._thread.start()
        log.info("EventBridge started (redis=%s)", self._redis_url)

    def stop(self) -> None:
        self._running = False
        # Thread is daemon, will exit when main process exits.
        # Setting _running=False lets it exit gracefully on next iteration.

    def _subscribe_loop(self) -> None:
        """Retry loop: connect → subscribe → listen → reconnect on failure."""
        try:
            import redis as redis_lib
        except ImportError:
            log.warning("redis package not installed; EventBridge disabled.")
            return

        backoff = 1
        while self._running:
            try:
                r = redis_lib.from_url(self._redis_url, decode_responses=True)
                r.ping()
                backoff = 1  # Reset on successful connection

                pubsub = r.pubsub()
                pubsub.psubscribe("gov:events:*")
                log.info("EventBridge subscribed to gov:events:*")

                for raw_msg in pubsub.listen():
                    if not self._running:
                        break
                    if raw_msg.get("type") not in ("pmessage", "message"):
                        continue
                    data = raw_msg.get("data", "")
                    if not data:
                        continue
                    try:
                        payload = json.loads(data) if isinstance(data, str) else data
                    except (json.JSONDecodeError, TypeError):
                        continue

                    event_name = payload.get("event", "unknown")
                    if event_name in NOTIFY_EVENTS:
                        self._notify(event_name, payload.get("payload", payload))

            except Exception as exc:
                if not self._running:
                    break
                log.debug("EventBridge: %s, reconnecting in %ds", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
