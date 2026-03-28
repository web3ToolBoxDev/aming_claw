"""Service Manager — Lifecycle management for the Executor subprocess.

Provides start/stop/reload and status inspection for the governance Executor process.
Intended for use by the bot layer (e.g. Telegram bot) for operational control.

Public interfaces
-----------------
start()                     — spawn the executor process
stop()                      — terminate the executor process
reload(callback=None)       — graceful restart: waits for active tasks to finish
                              (timeout 120 s), then stop→start; fires *callback* when done
status() -> dict            — structured snapshot: PID, uptime_s, active_tasks, queued_tasks

Design notes
------------
* The executor is launched as a child subprocess via ``subprocess.Popen``.
* Active / queued task counts are obtained by querying the Governance API
  (same endpoint the executor worker itself uses).
* ``reload()`` blocks the *calling* thread but does not hold the GIL; it polls
  the API on a configurable interval and respects the 120 s timeout.
* A reload callback is called in the *same* thread after the new process has
  been confirmed running, so it can safely call ``send_text`` / Telegram helpers.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    import requests  # noqa: F401 — imported here so tests can patch service_manager.requests.get
except ImportError:  # pragma: no cover — requests may be absent in minimal test envs
    requests = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (all overridable via environment variables)
# ---------------------------------------------------------------------------

GOVERNANCE_URL: str = os.getenv("GOVERNANCE_URL", "http://localhost:40006")
PROJECT_ID: str = os.getenv("EXECUTOR_PROJECT_ID", "aming-claw")

_RELOAD_TIMEOUT: int = int(os.getenv("SERVICE_RELOAD_TIMEOUT", "120"))
_POLL_INTERVAL: float = float(os.getenv("SERVICE_POLL_INTERVAL", "2"))

_agent_dir = str(Path(__file__).resolve().parent)


# ---------------------------------------------------------------------------
# ServiceManager
# ---------------------------------------------------------------------------


class ServiceManager:
    """Manages the lifecycle of the Executor subprocess.

    Args:
        project_id: Governance project identifier used when querying task counts.
        governance_url: Base URL of the Governance HTTP API.
        executor_cmd: Command list passed to ``subprocess.Popen``. Defaults to
            ``[sys.executable, "-m", "agent.executor_worker", "--project", <project_id>]``.
        reload_timeout: Seconds to wait for active tasks to drain before a reload
            forcefully proceeds. Default: 120.
        poll_interval: Seconds between active-task polls during reload. Default: 2.
    """

    def __init__(
        self,
        project_id: str = PROJECT_ID,
        governance_url: str = GOVERNANCE_URL,
        executor_cmd: Optional[list] = None,
        reload_timeout: int = _RELOAD_TIMEOUT,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.project_id = project_id
        self.governance_url = governance_url.rstrip("/")
        self.reload_timeout = reload_timeout
        self.poll_interval = poll_interval

        self._executor_cmd: list = executor_cmd or [
            sys.executable, "-m", "agent.executor_worker",
            "--project", self.project_id,
        ]

        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None
        self._lock = threading.Lock()

        # Monitor-thread state
        self._running: bool = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.restart_count: int = 0
        self.last_crash_at: Optional[float] = None   # wall-clock (time.time())
        self._restart_times: list = []               # monotonic timestamps for circuit-breaker window
        self._circuit_breaker_tripped: bool = False

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Spawn the executor subprocess if it is not already running.

        Returns:
            ``True`` if a new process was started, ``False`` if one was already
            alive.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                log.info("ServiceManager.start: executor already running (PID %d)", self._process.pid)
                # Ensure monitor thread is running even if process was already up
                self._ensure_monitor_running()
                return False

            log.info("ServiceManager.start: launching executor %s", self._executor_cmd)
            self._process = subprocess.Popen(
                self._executor_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._start_time = time.monotonic()
            log.info("ServiceManager.start: executor started (PID %d)", self._process.pid)

        # Start monitor thread outside the lock to avoid re-entrant lock issues
        self._ensure_monitor_running()
        return True

    def stop(self) -> bool:
        """Terminate the executor subprocess gracefully (SIGTERM, then SIGKILL after 5 s).

        Returns:
            ``True`` if a running process was stopped, ``False`` if none was
            running.
        """
        self._running = False  # Signal monitor loop to exit
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        """Internal stop — caller must hold ``self._lock``."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            log.info("ServiceManager.stop: no running executor to stop")
            self._process = None
            self._start_time = None
            return False

        log.info("ServiceManager.stop: terminating executor (PID %d)", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("ServiceManager.stop: executor did not exit after SIGTERM; sending SIGKILL")
            proc.kill()
            proc.wait(timeout=5)

        self._process = None
        self._start_time = None
        log.info("ServiceManager.stop: executor stopped")
        return True

    # ------------------------------------------------------------------
    # reload
    # ------------------------------------------------------------------

    def reload(self, callback: Optional[Callable[[dict], None]] = None) -> dict:
        """Gracefully restart the executor.

        Workflow:
        1. Poll Governance API until ``active_tasks == 0`` *or* *reload_timeout*
           seconds elapse (whichever comes first).
        2. Stop the current executor process.
        3. Start a new executor process.
        4. Call *callback(status_dict)* if provided.

        Args:
            callback: Optional callable invoked after the new process is running.
                Receives the result of :meth:`status` as its sole argument.  Use
                this hook to send a Telegram notification, for example.

        Returns:
            A dict describing the outcome::

                {
                    "success": True,
                    "waited_s": 12.4,
                    "timed_out": False,
                    "pid": 12345,
                }
        """
        log.info("ServiceManager.reload: initiating graceful reload (timeout=%ds)", self.reload_timeout)

        waited = 0.0
        timed_out = False

        # --- Phase 1: drain active tasks ---
        deadline = time.monotonic() + self.reload_timeout
        while True:
            active = self._get_active_task_count()
            if active == 0:
                log.info("ServiceManager.reload: active_tasks=0, proceeding immediately")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "ServiceManager.reload: timeout reached (%ds); proceeding despite active_tasks=%d",
                    self.reload_timeout, active,
                )
                timed_out = True
                break
            sleep_for = min(self.poll_interval, remaining)
            log.debug("ServiceManager.reload: active_tasks=%d, waiting %.1fs …", active, sleep_for)
            time.sleep(sleep_for)
            waited = time.monotonic() - (deadline - self.reload_timeout)

        if not timed_out:
            waited = time.monotonic() - (deadline - self.reload_timeout)

        # --- Phase 2: stop then start ---
        with self._lock:
            self._stop_locked()
            self._process = subprocess.Popen(
                self._executor_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._start_time = time.monotonic()
            new_pid = self._process.pid
            log.info("ServiceManager.reload: executor restarted (PID %d)", new_pid)

        result = {
            "success": True,
            "waited_s": round(waited, 2),
            "timed_out": timed_out,
            "pid": new_pid,
        }

        # --- Phase 3: fire callback ---
        if callback is not None:
            try:
                callback(self.status())
            except Exception as exc:
                log.error("ServiceManager.reload: callback raised %s", exc)

        return result

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a structured snapshot of the service state.

        Returns:
            A dict with the following keys:

            * ``pid``          — int or ``None`` if not running
            * ``running``      — bool
            * ``uptime_s``     — float seconds since last start, or ``None``
            * ``active_tasks`` — int queried from Governance API
            * ``queued_tasks`` — int queried from Governance API
        """
        with self._lock:
            proc = self._process
            if proc is not None and proc.poll() is not None:
                # Process has exited since we last checked — clean up references.
                self._process = None
                self._start_time = None
                proc = None

            pid = proc.pid if proc is not None else None
            running = proc is not None
            uptime_s = (
                round(time.monotonic() - self._start_time, 2)
                if self._start_time is not None and running
                else None
            )

        active, queued = self._get_task_counts()

        # Determine health state
        if self._circuit_breaker_tripped:
            health = "crash_loop"
        elif self.restart_count > 0:
            health = "degraded"
        else:
            health = "healthy"

        return {
            "pid": pid,
            "running": running,
            "uptime_s": uptime_s,
            "active_tasks": active,
            "queued_tasks": queued,
            "restart_count": self.restart_count,
            "last_crash_at": self.last_crash_at,
            "health": health,
            "circuit_breaker": self._circuit_breaker_tripped,
        }

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    _CIRCUIT_BREAKER_MAX = 5
    _CIRCUIT_BREAKER_WINDOW = 300  # seconds

    def _ensure_monitor_running(self) -> None:
        """Start monitor thread if it is not already alive."""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="executor-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            log.info("ServiceManager: monitor thread started")

    def _monitor_loop(self) -> None:
        """Background thread: checks every 10 s if executor is alive; restarts if dead.

        Circuit breaker: if the executor has been restarted
        ``_CIRCUIT_BREAKER_MAX`` or more times within the last
        ``_CIRCUIT_BREAKER_WINDOW`` seconds, stop trying and log an error.
        """
        while self._running:
            time.sleep(10)
            if not self._running:
                break

            with self._lock:
                proc = self._process
                if proc is None:
                    # Nothing to monitor
                    if self._circuit_breaker_tripped:
                        log.error(
                            "ServiceManager._monitor_loop: circuit breaker is tripped; "
                            "executor will not be restarted automatically"
                        )
                    continue

                if proc.poll() is None:
                    # Still alive — nothing to do
                    continue

                # ---- Process has died ----
                if self._circuit_breaker_tripped:
                    log.error(
                        "ServiceManager._monitor_loop: executor died but circuit breaker is tripped; "
                        "will not restart"
                    )
                    self._process = None
                    self._start_time = None
                    continue

                dead_pid = proc.pid
                log.warning(
                    "ServiceManager._monitor_loop: executor process (PID %d) died; "
                    "preparing restart …",
                    dead_pid,
                )
                self.last_crash_at = time.time()
                self._process = None
                self._start_time = None

            # Clear pycache outside the lock (I/O operation)
            self._clear_pycache()

            with self._lock:
                # Update circuit-breaker state
                now = time.monotonic()
                self._restart_times.append(now)
                self._restart_times = [
                    t for t in self._restart_times
                    if now - t < self._CIRCUIT_BREAKER_WINDOW
                ]
                self.restart_count = len(self._restart_times)

                if self.restart_count >= self._CIRCUIT_BREAKER_MAX:
                    log.error(
                        "ServiceManager._monitor_loop: circuit breaker tripped — %d restarts "
                        "within %ds; stopping automatic restarts",
                        self._CIRCUIT_BREAKER_MAX,
                        self._CIRCUIT_BREAKER_WINDOW,
                    )
                    self._circuit_breaker_tripped = True
                    continue

                # Restart the executor
                log.info(
                    "ServiceManager._monitor_loop: restarting executor (restart #%d of %d)",
                    self.restart_count,
                    self._CIRCUIT_BREAKER_MAX - 1,
                )
                try:
                    self._process = subprocess.Popen(
                        self._executor_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self._start_time = time.monotonic()
                    log.info(
                        "ServiceManager._monitor_loop: executor restarted (PID %d)",
                        self._process.pid,
                    )
                except Exception as exc:
                    log.error(
                        "ServiceManager._monitor_loop: failed to restart executor: %s", exc
                    )

    def _clear_pycache(self) -> None:
        """Walk the workspace and remove every ``__pycache__`` directory found."""
        workspace = str(Path(__file__).resolve().parent.parent)
        removed = 0
        for dirpath, dirnames, _ in os.walk(workspace):
            if "__pycache__" in dirnames:
                cache_path = os.path.join(dirpath, "__pycache__")
                try:
                    shutil.rmtree(cache_path)
                    removed += 1
                    # Prevent os.walk from descending into the (now-deleted) dir
                    dirnames.remove("__pycache__")
                except Exception as exc:
                    log.debug(
                        "ServiceManager._clear_pycache: could not remove %s: %s",
                        cache_path, exc,
                    )
        if removed:
            log.info("ServiceManager._clear_pycache: removed %d __pycache__ dir(s)", removed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_task_count(self) -> int:
        """Return the number of currently active (claimed/processing) tasks."""
        active, _ = self._get_task_counts()
        return active

    def _get_task_counts(self) -> tuple[int, int]:
        """Query the Governance API and return (active_count, queued_count).

        Returns ``(0, 0)`` on any network or parse error so that callers degrade
        gracefully.
        """
        try:
            url = f"{self.governance_url}/api/task/{self.project_id}/list"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            tasks: list = data.get("tasks", [])

            active = sum(1 for t in tasks if t.get("status") in ("claimed", "processing", "running"))
            queued = sum(1 for t in tasks if t.get("status") in ("queued", "pending"))
            return active, queued

        except Exception as exc:
            log.debug("ServiceManager._get_task_counts: API error — %s", exc)
            return 0, 0


# ---------------------------------------------------------------------------
# Module-level singleton convenience
# ---------------------------------------------------------------------------

_default_manager: Optional[ServiceManager] = None
_default_lock = threading.Lock()


def get_manager(
    project_id: str = PROJECT_ID,
    governance_url: str = GOVERNANCE_URL,
) -> ServiceManager:
    """Return the module-level singleton :class:`ServiceManager`.

    Creates it on first call with the supplied parameters.  Subsequent calls
    return the same instance regardless of parameters.
    """
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = ServiceManager(
                project_id=project_id,
                governance_url=governance_url,
            )
    return _default_manager
