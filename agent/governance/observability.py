"""Observability — trace_id propagation, structured logging, metrics.

Usage:
    from .observability import new_trace_id, structured_log, get_metrics

    trace_id = new_trace_id()
    structured_log("info", "message_forwarded", trace_id=trace_id, chat_id=123, duration_ms=12)
"""

import json
import logging
import time
import uuid
from collections import defaultdict
from threading import Lock

log = logging.getLogger(__name__)


def new_trace_id() -> str:
    """Generate a new trace ID."""
    return f"tr-{uuid.uuid4().hex[:12]}"


def structured_log(level: str, event: str, **fields) -> None:
    """Emit a structured log entry as JSON.

    Args:
        level: info, warning, error
        event: Event name (e.g., message_forwarded, verify_update)
        **fields: Additional fields (trace_id, chat_id, duration_ms, etc.)
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "event": event,
        **fields,
    }

    log_fn = getattr(log, level, log.info)
    log_fn(json.dumps(entry, ensure_ascii=False))


# --- Simple in-memory metrics ---

_counters: dict[str, int] = defaultdict(int)
_histograms: dict[str, list[float]] = defaultdict(list)
_lock = Lock()
_MAX_HISTOGRAM_SAMPLES = 1000


def inc_counter(name: str, value: int = 1) -> None:
    """Increment a counter metric."""
    with _lock:
        _counters[name] += value


def record_histogram(name: str, value: float) -> None:
    """Record a value to a histogram (e.g., latency)."""
    with _lock:
        h = _histograms[name]
        h.append(value)
        if len(h) > _MAX_HISTOGRAM_SAMPLES:
            _histograms[name] = h[-_MAX_HISTOGRAM_SAMPLES:]


def get_metrics() -> dict:
    """Get current metrics snapshot."""
    with _lock:
        result = {
            "counters": dict(_counters),
            "histograms": {},
        }
        for name, values in _histograms.items():
            if values:
                sorted_v = sorted(values)
                result["histograms"][name] = {
                    "count": len(sorted_v),
                    "min": sorted_v[0],
                    "max": sorted_v[-1],
                    "avg": sum(sorted_v) / len(sorted_v),
                    "p50": sorted_v[len(sorted_v) // 2],
                    "p95": sorted_v[int(len(sorted_v) * 0.95)],
                    "p99": sorted_v[int(len(sorted_v) * 0.99)],
                }
        return result


def reset_metrics() -> None:
    """Reset all metrics (for testing)."""
    with _lock:
        _counters.clear()
        _histograms.clear()


# --- Monitoring checks ---

def check_queue_health(redis_client, stream_keys: list[str]) -> list[dict]:
    """Check Redis Stream queue health."""
    alerts = []
    for key in stream_keys:
        try:
            length = redis_client._safe(lambda k=key: redis_client._client.xlen(k), 0)
            if length > 50:
                alerts.append({
                    "level": "warning",
                    "stream": key,
                    "message": f"Queue backlog: {length} messages",
                    "length": length,
                })
        except Exception:
            pass

        try:
            groups = redis_client._safe(
                lambda k=key: redis_client._client.xinfo_groups(k), []
            )
            for g in (groups or []):
                pending = g.get("pending", 0)
                if pending > 10:
                    alerts.append({
                        "level": "warning",
                        "stream": key,
                        "group": g.get("name"),
                        "message": f"Unacked messages: {pending}",
                        "pending": pending,
                    })
        except Exception:
            pass

    return alerts


def check_outbox_health(project_id: str) -> list[dict]:
    """Check outbox health for a project."""
    alerts = []
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM event_outbox WHERE delivered_at IS NULL AND dead_letter = 0 AND project_id = ?",
            (project_id,),
        ).fetchone()
        pending = row["cnt"] if row else 0
        if pending > 20:
            alerts.append({
                "level": "warning",
                "project_id": project_id,
                "message": f"Outbox backlog: {pending} undelivered events",
                "pending": pending,
            })

        row2 = conn.execute(
            "SELECT COUNT(*) as cnt FROM event_outbox WHERE dead_letter = 1 AND project_id = ?",
            (project_id,),
        ).fetchone()
        dead = row2["cnt"] if row2 else 0
        if dead > 0:
            alerts.append({
                "level": "error",
                "project_id": project_id,
                "message": f"Dead letters: {dead} events failed permanently",
                "dead_letters": dead,
            })
        conn.close()
    except Exception:
        pass
    return alerts
