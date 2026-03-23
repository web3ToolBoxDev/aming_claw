"""Observability — Trace ID, structured logging, replay support.

Every message→coordinator→dev→eval→reply chain gets a trace_id.
All intermediate state is logged for debugging and replay.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def new_trace_id() -> str:
    """Generate a new trace ID for a message chain."""
    return f"trace-{int(time.time())}-{uuid.uuid4().hex[:8]}"


@dataclass
class TraceEntry:
    """A single event in a trace."""
    trace_id: str
    step: str           # message_received, coordinator_start, validator_check, etc.
    timestamp: float
    data: dict = field(default_factory=dict)
    duration_ms: float = 0


class TraceCollector:
    """Collects trace entries for a single message chain."""

    def __init__(self, trace_id: str = ""):
        self.trace_id = trace_id or new_trace_id()
        self.entries: list[TraceEntry] = []
        self._step_starts: dict[str, float] = {}

    def start_step(self, step: str, data: dict = None) -> None:
        """Mark the start of a trace step."""
        self._step_starts[step] = time.time()
        self.entries.append(TraceEntry(
            trace_id=self.trace_id,
            step=f"{step}_start",
            timestamp=time.time(),
            data=data or {},
        ))

    def end_step(self, step: str, data: dict = None) -> None:
        """Mark the end of a trace step."""
        start = self._step_starts.pop(step, time.time())
        duration = (time.time() - start) * 1000
        self.entries.append(TraceEntry(
            trace_id=self.trace_id,
            step=f"{step}_end",
            timestamp=time.time(),
            data=data or {},
            duration_ms=round(duration, 1),
        ))

    def log_event(self, step: str, data: dict = None) -> None:
        """Log a point-in-time event."""
        self.entries.append(TraceEntry(
            trace_id=self.trace_id,
            step=step,
            timestamp=time.time(),
            data=data or {},
        ))

    def save(self, project_id: str = "") -> str:
        """Save trace to file for replay."""
        trace_dir = Path(os.getenv("SHARED_VOLUME_PATH",
            os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
        ) / "codex-tasks" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)

        filepath = trace_dir / f"{self.trace_id}.json"
        trace_data = {
            "trace_id": self.trace_id,
            "project_id": project_id,
            "entries": [
                {
                    "step": e.step,
                    "timestamp": e.timestamp,
                    "duration_ms": e.duration_ms,
                    "data": e.data,
                }
                for e in self.entries
            ],
            "total_entries": len(self.entries),
            "saved_at": time.time(),
        }

        with open(filepath, "w") as f:
            json.dump(trace_data, f, ensure_ascii=False, indent=2)

        log.info("Trace saved: %s (%d entries)", self.trace_id, len(self.entries))
        return str(filepath)

    def summary(self) -> dict:
        """Get trace summary."""
        total_ms = 0
        steps = []
        for e in self.entries:
            if e.step.endswith("_end"):
                total_ms += e.duration_ms
                steps.append({"step": e.step.replace("_end", ""), "ms": e.duration_ms})

        return {
            "trace_id": self.trace_id,
            "total_steps": len(steps),
            "total_duration_ms": round(total_ms, 1),
            "steps": steps,
        }


def load_trace(trace_id: str) -> Optional[dict]:
    """Load a saved trace for replay/debugging."""
    trace_dir = Path(os.getenv("SHARED_VOLUME_PATH",
        os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
    ) / "codex-tasks" / "traces"

    filepath = trace_dir / f"{trace_id}.json"
    if not filepath.exists():
        return None

    with open(filepath) as f:
        return json.load(f)


def list_recent_traces(limit: int = 20) -> list[dict]:
    """List recent trace files."""
    trace_dir = Path(os.getenv("SHARED_VOLUME_PATH",
        os.path.join(os.path.dirname(__file__), "..", "shared-volume"))
    ) / "codex-tasks" / "traces"

    if not trace_dir.exists():
        return []

    files = sorted(trace_dir.glob("trace-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    traces = []
    for f in files[:limit]:
        try:
            with open(f) as fh:
                data = json.load(fh)
                traces.append({
                    "trace_id": data.get("trace_id", ""),
                    "project_id": data.get("project_id", ""),
                    "entries": data.get("total_entries", 0),
                    "saved_at": data.get("saved_at", 0),
                })
        except Exception:
            continue

    return traces
