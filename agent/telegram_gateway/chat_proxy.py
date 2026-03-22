"""Chat Proxy - runs on host, bridges Telegram messages to coordinator.

Uses Redis Streams (XREADGROUP + ACK) for reliable message delivery.
Messages are never lost: unACKed messages are automatically redelivered.

Usage:
    from telegram_gateway.chat_proxy import ChatProxy

    proxy = ChatProxy(
        token="gov-xxx",
        gateway_url="http://localhost:40000",
        redis_url="redis://localhost:40079/0",
    )
    proxy.bind(chat_id=7848961760, project_id="amingClaw")

    # Option A: blocking loop
    proxy.listen(on_message=lambda msg: proxy.reply(handle(msg["text"])))

    # Option B: background thread + callback
    proxy.start(on_message=my_handler)
    # ... do other work ...
    proxy.stop()
"""

import hashlib
import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

CONSUMER_GROUP = "coordinator-group"
CONSUMER_NAME = "worker-1"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class ChatProxy:
    """Client-side proxy for coordinator <-> Telegram via Gateway + Redis Streams."""

    def __init__(self, token: str, gateway_url: str = "http://localhost:40000",
                 redis_url: str = "redis://localhost:40079/0"):
        self.token = token
        self.gateway_url = gateway_url.rstrip("/")
        self.redis_url = redis_url
        self.chat_id = None
        self._thread = None
        self._running = False
        self._redis = None
        self._stream_key = f"chat:inbox:{_token_hash(token)}"

    def bind(self, chat_id: int, project_id: str = "") -> dict:
        """Bind this coordinator to a Telegram chat via Gateway API."""
        self.chat_id = chat_id
        resp = requests.post(
            f"{self.gateway_url}/gateway/bind",
            json={"token": self.token, "chat_id": chat_id, "project_id": project_id},
            headers={"X-Gov-Token": self.token},
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            log.info("Bound to chat %d (project: %s)", chat_id, project_id)
        else:
            log.error("Bind failed: %s", result)
        return result

    def reply(self, text: str, chat_id: int = None) -> dict:
        """Send a reply to the Telegram user via Gateway API."""
        cid = chat_id or self.chat_id
        if not cid:
            raise ValueError("No chat_id bound. Call bind() first.")
        resp = requests.post(
            f"{self.gateway_url}/gateway/reply",
            json={"token": self.token, "chat_id": cid, "text": text},
            headers={"X-Gov-Token": self.token},
            timeout=10,
        )
        return resp.json()

    def listen(self, on_message) -> None:
        """Blocking: consume messages from Redis Stream with ACK."""
        self._connect_redis()
        self._ensure_group()

        # First: recover any unACKed messages from previous crash
        self._recover_pending(on_message)

        # Then: block-read new messages
        log.info("Listening on stream %s (group: %s)", self._stream_key, CONSUMER_GROUP)
        while True:
            try:
                entries = self._redis.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {self._stream_key: ">"},
                    count=10, block=30000,
                )
                if not entries:
                    continue
                for stream, messages in entries:
                    for msg_id, fields in messages:
                        try:
                            on_message(fields)
                            self._redis.xack(self._stream_key, CONSUMER_GROUP, msg_id)
                        except Exception:
                            log.exception("Error processing message %s", msg_id)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Stream read error")
                time.sleep(1)

    def start(self, on_message) -> None:
        """Non-blocking: start consuming in a background thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._consume_loop, args=(on_message,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def pending_count(self) -> int:
        """Check how many unACKed messages exist."""
        try:
            self._connect_redis()
            info = self._redis.xpending(self._stream_key, CONSUMER_GROUP)
            return info.get("pending", 0) if isinstance(info, dict) else 0
        except Exception:
            return 0

    def _connect_redis(self) -> None:
        if self._redis is None:
            import redis
            self._redis = redis.Redis.from_url(
                self.redis_url, decode_responses=True, socket_connect_timeout=5,
            )
            self._redis.ping()
            log.info("Redis connected: %s", self.redis_url)

    def _ensure_group(self) -> None:
        """Create consumer group if not exists."""
        try:
            self._redis.xgroup_create(self._stream_key, CONSUMER_GROUP, id="0", mkstream=True)
            log.info("Created consumer group %s on %s", CONSUMER_GROUP, self._stream_key)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _recover_pending(self, on_message) -> None:
        """Process any unACKed messages from a previous crash."""
        try:
            entries = self._redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {self._stream_key: "0"},  # "0" = read pending (unACKed)
                count=50,
            )
            if not entries:
                return
            for stream, messages in entries:
                if not messages:
                    continue
                log.info("Recovering %d pending messages", len(messages))
                for msg_id, fields in messages:
                    if not fields:
                        # Already ACKed but still in PEL, just ACK again
                        self._redis.xack(self._stream_key, CONSUMER_GROUP, msg_id)
                        continue
                    try:
                        on_message(fields)
                        self._redis.xack(self._stream_key, CONSUMER_GROUP, msg_id)
                    except Exception:
                        log.exception("Error recovering message %s", msg_id)
        except Exception:
            log.exception("Recovery failed")

    def _consume_loop(self, on_message) -> None:
        """Background thread: consume with reconnection."""
        backoff = 1
        while self._running:
            try:
                self._connect_redis()
                self._ensure_group()
                self._recover_pending(on_message)
                backoff = 1

                log.info("Consuming from %s", self._stream_key)
                while self._running:
                    entries = self._redis.xreadgroup(
                        CONSUMER_GROUP, CONSUMER_NAME,
                        {self._stream_key: ">"},
                        count=10, block=30000,
                    )
                    if not entries:
                        continue
                    for stream, messages in entries:
                        for msg_id, fields in messages:
                            try:
                                on_message(fields)
                                self._redis.xack(self._stream_key, CONSUMER_GROUP, msg_id)
                            except Exception:
                                log.exception("Error processing %s", msg_id)

            except Exception as e:
                if not self._running:
                    break
                log.warning("Connection lost (%s), reconnecting in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
