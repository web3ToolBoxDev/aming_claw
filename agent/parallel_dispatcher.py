"""
parallel_dispatcher.py - Parallel task dispatcher across multiple workspaces.

Provides a worker-pool model where each registered workspace gets its own
worker thread. Tasks are routed to the appropriate workspace based on
explicit targeting or default assignment.

Architecture:
  ┌──────────────────────┐
  │  ParallelDispatcher  │  (main thread)
  │  poll pending queue  │
  └──────┬───────────────┘
         │ route task to workspace worker
         ▼
  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
  │ WorkerThread  │  │ WorkerThread  │  │ WorkerThread  │
  │  ws-abc123    │  │  ws-def456    │  │  ws-ghi789    │
  │  (project-a)  │  │  (project-b)  │  │  (project-c)  │
  └───────────────┘  └───────────────┘  └───────────────┘

Each worker:
  - Has a thread-safe queue (queue.Queue)
  - Processes tasks sequentially within its workspace
  - Supports max_concurrent via semaphore (future expansion)
  - Reports status to shared state
"""
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from utils import load_json, save_json, tasks_root, utc_iso
from workspace_registry import (
    get_workspace,
    list_workspaces,
    resolve_workspace_for_task,
    ensure_current_workspace_registered,
)


# ── Worker status persistence ────────────────────────────────────────────────

def _dispatcher_state_file() -> Path:
    return tasks_root() / "state" / "parallel_dispatcher.json"


def _load_dispatcher_state() -> Dict:
    p = _dispatcher_state_file()
    if not p.exists():
        return {"workers": {}, "updated_at": utc_iso()}
    try:
        return load_json(p)
    except Exception:
        return {"workers": {}, "updated_at": utc_iso()}


def _save_dispatcher_state(data: Dict) -> None:
    data["updated_at"] = utc_iso()
    save_json(_dispatcher_state_file(), data)


def get_dispatcher_status() -> Dict:
    """Return current dispatcher status for display."""
    return _load_dispatcher_state()


# ── Workspace Worker ─────────────────────────────────────────────────────────

class WorkspaceWorker:
    """A worker thread dedicated to a single workspace."""

    def __init__(
        self,
        workspace: Dict,
        task_processor: Callable[[Path, Dict], None],
        max_queue_size: int = 100,
    ):
        self.workspace = workspace
        self.ws_id = workspace["id"]
        self.ws_path = Path(workspace["path"])
        self.ws_label = workspace.get("label", self.ws_path.name)
        self.max_concurrent = workspace.get("max_concurrent", 1)
        self.task_processor = task_processor
        self.task_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current_task_id: Optional[str] = None
        self._tasks_completed = 0
        self._tasks_failed = 0
        self._started_at = ""

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_busy(self) -> bool:
        return self._current_task_id is not None

    @property
    def queue_size(self) -> int:
        return self.task_queue.qsize()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._started_at = utc_iso()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="worker-{}".format(self.ws_id),
            daemon=True,
        )
        self._thread.start()
        print("[dispatcher] worker started: {} ({})".format(self.ws_id, self.ws_label))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        print("[dispatcher] worker stopped: {} ({})".format(self.ws_id, self.ws_label))

    def enqueue(self, task_path: Path, task: Dict) -> bool:
        """Add a task to this worker's queue. Returns False if full."""
        try:
            self.task_queue.put_nowait((task_path, task))
            return True
        except queue.Full:
            return False

    def status(self) -> Dict:
        return {
            "ws_id": self.ws_id,
            "ws_label": self.ws_label,
            "ws_path": str(self.ws_path),
            "running": self.is_running,
            "busy": self.is_busy,
            "current_task_id": self._current_task_id,
            "queue_size": self.queue_size,
            "tasks_completed": self._tasks_completed,
            "tasks_failed": self._tasks_failed,
            "started_at": self._started_at,
        }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task_path, task = self.task_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            task_id = task.get("task_id", "unknown")
            self._current_task_id = task_id
            try:
                print("[worker-{}] processing task: {}".format(self.ws_id, task_id))
                self.task_processor(task_path, self.workspace)
                self._tasks_completed += 1
            except Exception as exc:
                self._tasks_failed += 1
                print("[worker-{}] task {} failed: {}".format(self.ws_id, task_id, exc))
            finally:
                self._current_task_id = None
                self.task_queue.task_done()


# ── Parallel Dispatcher ──────────────────────────────────────────────────────

class ParallelDispatcher:
    """Manages worker threads for all registered workspaces.

    Usage:
        dispatcher = ParallelDispatcher(task_processor=process_task_fn)
        dispatcher.start()
        # ... dispatcher.dispatch(task_path) ...
        dispatcher.stop()
    """

    def __init__(self, task_processor: Callable[[Path, Dict], None]):
        """
        Args:
            task_processor: Function(task_path, workspace_dict) that handles a task
                           within the context of the given workspace.
        """
        self.task_processor = task_processor
        self._workers: Dict[str, WorkspaceWorker] = {}
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        """Initialize workers for all active workspaces and start them."""
        ensure_current_workspace_registered()
        self._running = True
        self._sync_workers()
        self._persist_status()
        print("[dispatcher] started with {} workers".format(len(self._workers)))

    def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False
        with self._lock:
            for worker in self._workers.values():
                worker.stop()
            self._workers.clear()
        self._persist_status()
        print("[dispatcher] stopped")

    def refresh_workers(self) -> None:
        """Re-sync worker pool with current workspace registry."""
        if self._running:
            self._sync_workers()
            self._persist_status()

    def dispatch(self, task_path: Path) -> bool:
        """Route a pending task to the appropriate workspace worker.

        Returns True if dispatched, False if no suitable worker found.
        """
        try:
            task = load_json(task_path)
        except Exception as exc:
            print("[dispatcher] failed to read task {}: {}".format(task_path, exc))
            return False

        ws = resolve_workspace_for_task(task)
        if not ws:
            print("[dispatcher] no workspace found for task {}".format(
                task.get("task_id", "?")))
            return False

        ws_id = ws["id"]
        with self._lock:
            worker = self._workers.get(ws_id)
            if not worker or not worker.is_running:
                # Try to create worker on-the-fly
                self._sync_workers()
                worker = self._workers.get(ws_id)

            if not worker:
                print("[dispatcher] no worker for workspace {}".format(ws_id))
                return False

            if not worker.enqueue(task_path, task):
                print("[dispatcher] queue full for workspace {} ({})".format(
                    ws_id, worker.ws_label))
                return False

        return True

    def get_status(self) -> Dict:
        """Return status of all workers."""
        with self._lock:
            workers_status = {
                ws_id: worker.status()
                for ws_id, worker in self._workers.items()
            }
        return {
            "running": self._running,
            "worker_count": len(workers_status),
            "workers": workers_status,
            "updated_at": utc_iso(),
        }

    def get_worker_for_workspace(self, ws_id: str) -> Optional[WorkspaceWorker]:
        with self._lock:
            return self._workers.get(ws_id)

    def _sync_workers(self) -> None:
        """Create workers for new workspaces, remove workers for deleted ones."""
        workspaces = list_workspaces()
        ws_ids = {ws["id"] for ws in workspaces}

        with self._lock:
            # Remove workers for deactivated/deleted workspaces
            removed = [wid for wid in self._workers if wid not in ws_ids]
            for wid in removed:
                self._workers[wid].stop()
                del self._workers[wid]

            # Add workers for new workspaces
            for ws in workspaces:
                ws_id = ws["id"]
                if ws_id not in self._workers:
                    worker = WorkspaceWorker(
                        workspace=ws,
                        task_processor=self.task_processor,
                    )
                    worker.start()
                    self._workers[ws_id] = worker

    def _persist_status(self) -> None:
        """Save dispatcher status to state file."""
        try:
            _save_dispatcher_state(self.get_status())
        except Exception:
            pass


# ── Module-level singleton ───────────────────────────────────────────────────

_dispatcher_instance: Optional[ParallelDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(task_processor: Optional[Callable] = None) -> Optional[ParallelDispatcher]:
    """Get or create the global dispatcher singleton."""
    global _dispatcher_instance
    with _dispatcher_lock:
        if _dispatcher_instance is None and task_processor is not None:
            _dispatcher_instance = ParallelDispatcher(task_processor)
        return _dispatcher_instance


def shutdown_dispatcher() -> None:
    """Shut down the global dispatcher if running."""
    global _dispatcher_instance
    with _dispatcher_lock:
        if _dispatcher_instance:
            _dispatcher_instance.stop()
            _dispatcher_instance = None
