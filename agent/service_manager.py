"""
manager.py - Outer management service for aming-claw.

Responsibilities:
  1. Monitor coordinator + executor processes (auto-restart on crash)
  2. Read control signals from state/manager_signal.json:
       restart  - kill + re-launch all services
       reinit   - git pull + restart
  3. Write live status to state/manager_status.json (used by /mgr_status)

Signal file protocol (written by coordinator.py, read here):
  {
    "action":       "restart" | "reinit",
    "args":         {},
    "requested_by": <user_id>,
    "requested_at": "<iso>",
    "request_id":   "mgr-<ms>"
  }
After processing, the signal file is deleted (acked).
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Ensure common/agent_config are importable from this directory
sys.path.insert(0, str(Path(__file__).parent))

from utils import load_json, save_json, tasks_root, utc_iso  # noqa: E402

POLL_SEC = float(os.getenv("MANAGER_POLL_SEC", "5"))
MANAGER_SINGLETON_PORT = int(os.getenv("MANAGER_SINGLETON_PORT", "39103"))

# Reload constants
RELOAD_WAIT_TIMEOUT_SEC = int(os.getenv("RELOAD_WAIT_TIMEOUT_SEC", "120"))
RELOAD_POLL_SEC = float(os.getenv("RELOAD_POLL_SEC", "2"))

# Module-level start time for uptime tracking
_START_TIME: float = time.time()


# ── File paths ────────────────────────────────────────────────────────────────

def _signal_path() -> Path:
    return tasks_root() / "state" / "manager_signal.json"


def _status_path() -> Path:
    return tasks_root() / "state" / "manager_status.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _scripts_dir() -> Path:
    return _repo_root() / "scripts"


# ── Signal helpers ────────────────────────────────────────────────────────────

def read_signal() -> Optional[Dict]:
    p = _signal_path()
    if not p.exists():
        return None
    try:
        return load_json(p)
    except Exception:
        return None


def clear_signal() -> None:
    try:
        _signal_path().unlink(missing_ok=True)
    except Exception:
        pass


# ── Status ────────────────────────────────────────────────────────────────────

def _count_processes(keyword: str) -> int:
    """Count running python processes whose command line contains keyword."""
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                (
                    "Get-CimInstance Win32_Process"
                    " | Where-Object {{ $_.CommandLine -like '*{}*' }}"
                    " | Measure-Object"
                    " | Select-Object -ExpandProperty Count"
                ).format(keyword),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return int((proc.stdout or "0").strip() or "0")
    except Exception:
        return -1


def get_service_status() -> Dict[str, str]:
    coord_n = _count_processes("coordinator.py")
    exec_n = _count_processes("executor.py")
    mgr_n = _count_processes("manager.py")
    return {
        "coordinator": "running" if coord_n > 0 else ("unknown" if coord_n < 0 else "stopped"),
        "executor":    "running" if exec_n > 0  else ("unknown" if exec_n < 0  else "stopped"),
        "manager":     "running" if mgr_n > 0   else ("unknown" if mgr_n < 0   else "stopped"),
    }


def write_status(services: Dict[str, str]) -> None:
    try:
        p = _status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        save_json(p, {
            "pid": os.getpid(),
            "updated_at": utc_iso(),
            "services": services,
        })
    except Exception as exc:
        print("[manager] write_status error: {}".format(exc))


# ── Active / queued task counters ────────────────────────────────────────────

def _count_active_tasks() -> int:
    """Return number of tasks currently in 'processing' state via task_state."""
    try:
        import task_state  # imported lazily to avoid circular deps at module load
        active_list = task_state.list_active_tasks()
        return sum(
            1 for t in active_list
            if (t.get("status") or "") == "processing"
        )
    except Exception:
        return 0


def _count_queued_tasks() -> int:
    """Return total number of tasks queued across all workspaces."""
    try:
        import workspace_queue  # imported lazily
        data = load_json(tasks_root() / "state" / "workspace_task_queue.json")
        if not isinstance(data, dict):
            return 0
        queues: Dict[str, List] = data.get("queues") or {}
        return sum(len(v) for v in queues.values() if isinstance(v, list))
    except Exception:
        return 0


# ── New public API ─────────────────────────────────────────────────────────────

def status() -> Dict:
    """Return structured status dict with pid, uptime, active/queued task counts.

    Returns:
        {
            "pid":      <int>,
            "uptime_sec": <float>,
            "active_tasks": <int>,   # tasks currently processing
            "queued_tasks": <int>,   # tasks waiting in workspace queue
            "services": {...},       # coordinator/executor/manager process status
        }
    """
    return {
        "pid": os.getpid(),
        "uptime_sec": round(time.time() - _START_TIME, 1),
        "active_tasks": _count_active_tasks(),
        "queued_tasks": _count_queued_tasks(),
        "services": get_service_status(),
    }


def reload(callback: Optional[Callable[[bool, str], None]] = None) -> bool:
    """Gracefully restart the executor process without losing the task queue.

    Algorithm:
      1. Poll active_tasks until it reaches 0 (timeout: RELOAD_WAIT_TIMEOUT_SEC).
      2. When active == 0, call run_restart() to restart all services.
      3. Invoke optional callback(success: bool, message: str) when done.

    Args:
        callback: Optional callable(success, message) called after reload.
                  Runs on the same thread that calls reload().

    Returns:
        True if restart succeeded, False on timeout or restart failure.
    """
    deadline = time.time() + RELOAD_WAIT_TIMEOUT_SEC

    # Wait for active tasks to drain
    while True:
        active = _count_active_tasks()
        if active == 0:
            break
        if time.time() >= deadline:
            msg = "[manager] reload: timed out waiting for {} active task(s) to finish".format(active)
            print(msg)
            if callback is not None:
                try:
                    callback(False, msg)
                except Exception as cb_exc:
                    print("[manager] reload callback error: {}".format(cb_exc))
            return False
        print("[manager] reload: waiting for {} active task(s)…".format(active))
        time.sleep(RELOAD_POLL_SEC)

    print("[manager] reload: no active tasks — restarting services")
    ok = run_restart()
    msg = "[manager] reload: restart {}".format("succeeded" if ok else "failed")
    print(msg)

    if callback is not None:
        try:
            callback(ok, msg)
        except Exception as cb_exc:
            print("[manager] reload callback error: {}".format(cb_exc))

    return ok


def reload_async(
    callback: Optional[Callable[[bool, str], None]] = None
) -> threading.Thread:
    """Non-blocking variant of reload(); runs in a daemon thread.

    Returns the Thread object so the caller can join() if needed.
    """
    t = threading.Thread(target=reload, kwargs={"callback": callback}, daemon=True)
    t.start()
    return t


# ── Service restart helpers ───────────────────────────────────────────────────

def _run_ps1(script: Path, extra_args: list = None) -> bool:
    args = ["powershell", "-NoProfile", "-File", str(script), "-BypassMutex", "-HardRestart"]
    if extra_args:
        args += extra_args
    print("[manager] running: {}".format(" ".join(str(a) for a in args)))
    try:
        result = subprocess.run(args, timeout=180)
        ok = result.returncode == 0
        print("[manager] script exited rc={}".format(result.returncode))
        return ok
    except subprocess.TimeoutExpired:
        print("[manager] script timed out")
        return False
    except Exception as exc:
        print("[manager] script error: {}".format(exc))
        return False


def run_restart() -> bool:
    script = _scripts_dir() / "restart-all.ps1"
    if not script.exists():
        print("[manager] restart-all.ps1 not found: {}".format(script))
        return False
    return _run_ps1(script, ["-SkipChecks", "-NoHealthWait"])


def run_reinit() -> bool:
    """git pull in repo root, then restart all services."""
    repo = _repo_root()
    print("[manager] git pull in {}".format(repo))
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "pull"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            print("[manager] git pull ok: {}".format((proc.stdout or "").strip()[:300]))
        else:
            # Not fatal — repo may be in detached HEAD or dirty; still restart
            print("[manager] git pull rc={}: {}".format(
                proc.returncode, (proc.stderr or proc.stdout or "").strip()[:300]
            ))
    except Exception as exc:
        print("[manager] git pull exception: {}".format(exc))
    return run_restart()


# ── Signal processing ─────────────────────────────────────────────────────────

def process_signal(sig: Dict) -> None:
    action = str(sig.get("action") or "")
    print("[manager] signal: action={} request_id={}".format(action, sig.get("request_id", "")))

    if action == "restart":
        run_restart()
    elif action == "reinit":
        run_reinit()
    else:
        print("[manager] unknown signal action: {!r}".format(action))


# ── Singleton lock ────────────────────────────────────────────────────────────

def acquire_singleton_lock() -> Optional[socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", MANAGER_SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    lock = acquire_singleton_lock()
    if lock is None:
        print("[manager] another manager instance is already running; exit")
        return

    print("[manager] started (pid={}, poll={}s)".format(os.getpid(), POLL_SEC))

    while True:
        try:
            sig = read_signal()
            if sig:
                clear_signal()
                process_signal(sig)

            services = get_service_status()
            write_status(services)

        except KeyboardInterrupt:
            print("[manager] stopped by keyboard")
            return
        except Exception as exc:
            print("[manager] loop error: {}".format(exc))

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    run()
