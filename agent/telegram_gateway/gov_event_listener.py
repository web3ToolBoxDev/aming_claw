"""Redis Pub/Sub listener for governance events.

Subscribes to gov:events:* channels and forwards relevant events
to Telegram via notifications.
"""

import json
import logging
import threading
import time

log = logging.getLogger(__name__)


# Event → Telegram message formatters
EVENT_FORMATTERS = {
    "node.status_changed": lambda p: (
        f"\u2705 [{p.get('project_id')}] {p.get('node_id')} \u2192 {p.get('to', p.get('new_status'))}"
        if p.get("to", p.get("new_status")) in ("t2_pass", "qa_pass")
        else f"\u274c [{p.get('project_id')}] {p.get('node_id')} \u2192 {p.get('to', p.get('new_status'))}"
        if p.get("to", p.get("new_status")) == "failed"
        else f"\U0001f504 [{p.get('project_id')}] {p.get('node_id')} \u2192 {p.get('to', p.get('new_status'))}"
    ),
    "gate.satisfied": lambda p: (
        f"\U0001f680 [{p.get('project_id')}] Gate {p.get('gate_id', 'unknown')} \u5df2\u6ee1\u8db3"
    ),
    "gate.blocked": lambda p: (
        f"\u26d4 [{p.get('project_id')}] Gate {p.get('gate_id', 'unknown')} \u88ab\u963b\u585e: {p.get('reason', '')}"
    ),
    "release.approved": lambda p: (
        f"\U0001f389 [{p.get('project_id')}] \u53d1\u5e03\u95e8\u7981\u901a\u8fc7!"
    ),
    "release.blocked": lambda p: (
        f"\u26d4 [{p.get('project_id')}] \u53d1\u5e03\u88ab\u963b\u585e: {p.get('reason', '')}"
    ),
    "baseline.applied": lambda p: (
        f"\U0001f4ca [{p.get('project_id')}] Baseline \u5df2\u5e94\u7528: {p.get('count', 0)} \u4e2a\u8282\u70b9"
    ),
    "role.registered": lambda p: (
        f"\U0001f464 [{p.get('project_id')}] \u65b0\u89d2\u8272\u6ce8\u518c: {p.get('role', '')} ({p.get('principal_id', '')})"
    ),
    "rollback.executed": lambda p: (
        f"\u26a0\ufe0f [{p.get('project_id')}] \u56de\u6eda\u6267\u884c: {p.get('reason', '')}"
    ),
}

# Events that should NOT notify (too noisy)
SILENT_EVENTS = {"memory.written", "task.updated"}


class GovEventListener:
    """Listens to governance events via Redis Pub/Sub and notifies Telegram."""

    def __init__(self, redis_url: str, notify_callback):
        """
        Args:
            redis_url: Redis connection URL.
            notify_callback: Function(text: str) to send Telegram notification.
        """
        self._redis_url = redis_url
        self._notify = notify_callback
        self._thread = None
        self._running = False
        self._redis = None

    def start(self) -> bool:
        """Start listening in a background thread. Returns True if started."""
        try:
            import redis
            self._redis = redis.Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                # No socket_timeout for subscribe — it blocks indefinitely
            )
            self._redis.ping()
        except Exception as e:
            log.warning("GovEventListener: Redis unavailable (%s), events disabled", e)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        log.info("GovEventListener: started")
        return True

    def stop(self) -> None:
        """Stop the listener thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("GovEventListener: stopped")

    def _listen_loop(self) -> None:
        """Main listen loop with reconnection."""
        backoff = 1
        while self._running:
            try:
                pubsub = self._redis.pubsub()
                pubsub.psubscribe("gov:events:*")
                log.info("GovEventListener: subscribed to gov:events:*")
                backoff = 1  # Reset on successful connect

                for msg in pubsub.listen():
                    if not self._running:
                        break
                    if msg["type"] not in ("pmessage", "message"):
                        continue
                    log.info("GovEventListener: received message type=%s channel=%s",
                             msg.get("type"), msg.get("channel"))
                    try:
                        data = json.loads(msg["data"])
                        self._handle_event(data)
                    except (json.JSONDecodeError, TypeError) as e:
                        log.warning("GovEventListener: failed to parse message: %s", e)
                        continue
                    except Exception as e:
                        log.exception("GovEventListener: error handling event: %s", e)

            except Exception as e:
                if not self._running:
                    break
                log.warning("GovEventListener: connection lost (%s), reconnecting in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle_event(self, data: dict) -> None:
        """Process a single governance event."""
        event = data.get("event", "")
        payload = data.get("payload", {})

        if event in SILENT_EVENTS:
            return

        formatter = EVENT_FORMATTERS.get(event)
        if formatter:
            try:
                text = formatter(payload)
                log.info("GovEventListener: sending notification: %s", text)
                self._notify(text)
                log.info("GovEventListener: notification sent successfully")
            except Exception:
                log.exception("Failed to format/send event %s", event)
        else:
            # Unknown event, log but don't notify
            log.debug("GovEventListener: unhandled event %s", event)
