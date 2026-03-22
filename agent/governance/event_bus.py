"""Internal event bus for the governance service.

Supports synchronous in-process subscriptions AND Redis Pub/Sub for cross-container events.
When Redis is available, all published events are also forwarded to Redis channels.
External consumers (e.g., Telegram Gateway) subscribe via Redis Pub/Sub.
"""

import json
import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger(__name__)

# Well-known event names
EVENTS = [
    "node.status_changed",
    "node.created",
    "node.deleted",
    "gate.satisfied",
    "gate.blocked",
    "release.blocked",
    "release.approved",
    "role.registered",
    "role.expired",
    "role.missing",
    "rollback.executed",
    "task.created",
    "task.updated",
    "memory.written",
    "baseline.applied",
]

# Redis channel prefix
REDIS_CHANNEL_PREFIX = "gov:events"


class EventBus:
    """Synchronous event bus with optional Redis Pub/Sub bridge."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._history: list[dict] = []  # Recent events for debugging
        self._max_history = 1000
        self._redis_bridge_enabled = False

    def enable_redis_bridge(self) -> None:
        """Enable forwarding events to Redis Pub/Sub.

        Called during server startup after Redis connects.
        """
        self._redis_bridge_enabled = True
        log.info("EventBus: Redis Pub/Sub bridge enabled")

    def subscribe(self, event: str, callback: Callable) -> None:
        """Subscribe to an event.

        Args:
            event: Event name (e.g., "node.status_changed").
            callback: Function(payload: dict) to call when event fires.
        """
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Remove a subscription."""
        subs = self._subscribers.get(event, [])
        if callback in subs:
            subs.remove(callback)

    def publish(self, event: str, payload: dict) -> None:
        """Publish an event to in-process subscribers AND Redis.

        Args:
            event: Event name.
            payload: Event data dict.
        """
        # Record in history
        entry = {"event": event, "payload": payload}
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Dispatch to in-process subscribers
        for callback in self._subscribers.get(event, []):
            try:
                callback(payload)
            except Exception:
                log.exception("Event subscriber error for %s", event)

        # Also dispatch to wildcard subscribers
        for callback in self._subscribers.get("*", []):
            try:
                callback(payload)
            except Exception:
                log.exception("Wildcard subscriber error for %s", event)

        # Forward to Redis Pub/Sub (fire-and-forget)
        if self._redis_bridge_enabled:
            self._publish_to_redis(event, payload)

    def _publish_to_redis(self, event: str, payload: dict) -> None:
        """Forward event to Redis Pub/Sub channels."""
        try:
            from .redis_client import get_redis
            r = get_redis()
            if not r.available:
                return

            message = {
                "event": event,
                "payload": payload,
            }

            # Publish to project-specific channel if project_id in payload
            project_id = payload.get("project_id")
            if project_id:
                channel = f"{REDIS_CHANNEL_PREFIX}:{project_id}"
                r.publish(channel, message)
            else:
                # No project_id, publish to global channel
                r.publish(f"{REDIS_CHANNEL_PREFIX}:global", message)

        except Exception:
            log.exception("Failed to publish event to Redis: %s", event)

    def recent_events(self, limit: int = 50) -> list[dict]:
        """Get recent event history for debugging."""
        return self._history[-limit:]

    def clear(self) -> None:
        """Clear all subscriptions and history."""
        self._subscribers.clear()
        self._history.clear()


# Global event bus instance
_bus = EventBus()


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    return _bus


def publish(event: str, payload: dict) -> None:
    """Convenience: publish to the global event bus."""
    _bus.publish(event, payload)


def subscribe(event: str, callback: Callable) -> None:
    """Convenience: subscribe on the global event bus."""
    _bus.subscribe(event, callback)
