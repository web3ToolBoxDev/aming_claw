"""Message Worker — consumes Telegram messages from Redis Stream.

Three-tier consumption model:
  1. Interactive Session: ChatProxy in Claude Code (highest priority, takes worker lock)
  2. Message Worker: This script (background, blocked by interactive session)
  3. Cron Fallback: Scheduled task checks XPENDING every 5 min

Worker lock ensures single consumer per coordinator.
"""

import hashlib
import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("message_worker")

CONSUMER_GROUP = "coordinator-group"
CONSUMER_NAME = "worker-1"
WORKER_LOCK_TTL = 60  # seconds
BLOCK_TIMEOUT_MS = 30000  # 30s block read


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class MessageWorker:
    """Consumes messages from Redis Stream with worker lock and lease renewal."""

    def __init__(self, token: str, gateway_url: str, redis_url: str,
                 governance_url: str = "", lease_id: str = ""):
        self.token = token
        self.gateway_url = gateway_url.rstrip("/")
        self.redis_url = redis_url
        self.governance_url = governance_url.rstrip("/") if governance_url else ""
        self.lease_id = lease_id
        self._redis = None
        self._running = False

        th = _token_hash(token)
        self.stream_key = f"chat:inbox:{th}"
        self.worker_lock_key = f"worker:{th}:owner"
        self.worker_id = f"mw-{os.getpid()}-{int(time.time())}"

    def run(self) -> None:
        """Main loop: acquire lock → consume → renew lease."""
        self._connect()
        self._running = True
        log.info("Worker %s starting for stream %s", self.worker_id, self.stream_key)

        while self._running:
            if not self._acquire_lock():
                log.info("Another consumer active, standing by...")
                time.sleep(30)
                continue

            try:
                self._ensure_group()
                self._recover_pending()
                self._consume_loop()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error("Consume error: %s", e)
                time.sleep(3)
            finally:
                self._release_lock()

    def stop(self) -> None:
        self._running = False

    def _connect(self) -> None:
        import redis
        self._redis = redis.Redis.from_url(
            self.redis_url, decode_responses=True, socket_connect_timeout=5,
        )
        self._redis.ping()
        log.info("Redis connected: %s", self.redis_url)

    def _acquire_lock(self) -> bool:
        """Try to acquire worker lock (NX + EX)."""
        result = self._redis.set(
            self.worker_lock_key, self.worker_id, nx=True, ex=WORKER_LOCK_TTL,
        )
        return bool(result)

    def _release_lock(self) -> None:
        """Release worker lock only if we own it."""
        current = self._redis.get(self.worker_lock_key)
        if current == self.worker_id:
            self._redis.delete(self.worker_lock_key)

    def _renew_lock(self) -> bool:
        """Renew worker lock TTL."""
        current = self._redis.get(self.worker_lock_key)
        if current == self.worker_id:
            self._redis.expire(self.worker_lock_key, WORKER_LOCK_TTL)
            return True
        return False

    def _ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(self.stream_key, CONSUMER_GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _recover_pending(self) -> None:
        """Process unACKed messages from previous crash."""
        entries = self._redis.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {self.stream_key: "0"}, count=50,
        )
        if not entries:
            return
        for stream, messages in entries:
            for msg_id, fields in messages:
                if not fields:
                    self._redis.xack(self.stream_key, CONSUMER_GROUP, msg_id)
                    continue
                log.info("Recovering pending: %s", msg_id)
                self._process_message(fields)
                self._redis.xack(self.stream_key, CONSUMER_GROUP, msg_id)

    def _consume_loop(self) -> None:
        """Block-read loop with periodic lock/lease renewal."""
        last_renew = time.time()

        while self._running:
            entries = self._redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {self.stream_key: ">"}, count=10, block=BLOCK_TIMEOUT_MS,
            )

            # Renew lock and lease every 30s
            now = time.time()
            if now - last_renew >= 30:
                if not self._renew_lock():
                    log.warning("Lost worker lock, yielding")
                    return
                self._renew_lease()
                last_renew = now

            if not entries:
                continue

            for stream, messages in entries:
                for msg_id, fields in messages:
                    try:
                        self._process_message(fields)
                        self._redis.xack(self.stream_key, CONSUMER_GROUP, msg_id)
                    except Exception:
                        log.exception("Error processing %s", msg_id)

    def _process_message(self, fields: dict) -> None:
        """Process a single message. Override for custom logic."""
        chat_id = fields.get("chat_id", "")
        text = fields.get("text", "")

        if not chat_id or not text:
            return

        log.info("Processing: [%s] %s", chat_id, text[:80])

        # Default: echo back with status
        reply_text = f"[Worker] 收到: {text[:200]}"

        # Try governance query if it looks like a status request
        if any(kw in text for kw in ["状态", "status", "节点", "node"]):
            reply_text = self._query_status(text)

        self._reply(int(chat_id), reply_text)

    def _reply(self, chat_id: int, text: str) -> None:
        """Send reply via Gateway API."""
        import requests
        try:
            requests.post(
                f"{self.gateway_url}/gateway/reply",
                json={"token": self.token, "chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            log.error("Reply failed: %s", e)

    def _query_status(self, text: str) -> str:
        """Query governance for project status."""
        if not self.governance_url:
            return f"[Worker] 收到: {text[:200]}"
        import requests
        try:
            resp = requests.get(
                f"{self.governance_url}/api/wf/amingClaw/summary",
                headers={"X-Gov-Token": self.token},
                timeout=5,
            )
            data = resp.json()
            total = data.get("total_nodes", 0)
            by_status = data.get("by_status", {})
            lines = [f"amingClaw ({total} 节点):"]
            for s, c in by_status.items():
                lines.append(f"  {s}: {c}")
            return "\n".join(lines)
        except Exception:
            return f"[Worker] 收到: {text[:200]}"

    def _renew_lease(self) -> None:
        """Renew agent lease via governance API."""
        if not self.lease_id or not self.governance_url:
            return
        import requests
        try:
            requests.post(
                f"{self.governance_url}/api/agent/heartbeat",
                json={"lease_id": self.lease_id, "status": "processing"},
                timeout=5,
            )
        except Exception:
            pass


def main():
    """CLI entry point."""
    token = os.environ.get("GOV_COORDINATOR_TOKEN", "")
    gateway_url = os.environ.get("GATEWAY_URL", "http://localhost:40000")
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:40079/0")
    governance_url = os.environ.get("GOVERNANCE_URL", "http://localhost:40000")

    if not token:
        print("Error: GOV_COORDINATOR_TOKEN env var required")
        sys.exit(1)

    worker = MessageWorker(
        token=token,
        gateway_url=gateway_url,
        redis_url=redis_url,
        governance_url=governance_url,
    )
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
