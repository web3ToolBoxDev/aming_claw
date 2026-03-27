"""Executor — verify-update task executor with snapshot/rollback support.

Extends ExecutorWorker with a 'verify-update' task type that:
  1. Snapshots target files before applying changes.
  2. Rolls back files automatically on failure.
  3. Cleans up the snapshot on success.

Snapshot location:
  shared-volume/codex-tasks/state/snapshots/{task_id}/

Usage:
  from agent.executor import Executor
  e = Executor(project_id="aming-claw")
  e.run_once()
"""

import json
import logging
import os
import shutil
import sys
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from executor_worker import ExecutorWorker, GOVERNANCE_URL, WORKER_ID, WORKSPACE  # noqa: E402

log = logging.getLogger(__name__)

# Base directory for snapshots (overridable via env for tests)
_SHARED_VOLUME = os.getenv(
    "SHARED_VOLUME_PATH",
    str(Path(__file__).resolve().parents[1] / "shared-volume"),
)
SNAPSHOTS_BASE = os.path.join(_SHARED_VOLUME, "codex-tasks", "state", "snapshots")


class Executor(ExecutorWorker):
    """ExecutorWorker subclass with verify-update snapshot/rollback support."""

    # ------------------------------------------------------------------ #
    #  Snapshot helpers                                                    #
    # ------------------------------------------------------------------ #

    def _snapshot_files(self, file_paths: list, snapshot_dir: str) -> None:
        """Copy *file_paths* into *snapshot_dir* and write snapshot_manifest.json.

        Args:
            file_paths: Absolute or workspace-relative paths to snapshot.
            snapshot_dir: Directory that will hold the snapshots.
        """
        snap = Path(snapshot_dir)

        # Clear any stale snapshot to avoid mixing old/new state.
        if snap.exists():
            shutil.rmtree(snap)
            log.debug("Cleared existing snapshot dir: %s", snapshot_dir)
        snap.mkdir(parents=True, exist_ok=True)

        manifest: dict = {}  # snapshot_key → original_path
        for idx, raw_path in enumerate(file_paths):
            # Resolve path relative to workspace when not absolute
            orig = Path(raw_path)
            if not orig.is_absolute():
                orig = Path(self.workspace) / raw_path
            orig = orig.resolve()

            if not orig.exists():
                log.debug("Snapshot: skipping missing file %s", orig)
                manifest[f"file_{idx}"] = {
                    "original": str(orig),
                    "snapshot_name": None,
                    "exists": False,
                }
                continue

            snapshot_name = f"file_{idx}_{orig.name}"
            dest = snap / snapshot_name
            shutil.copy2(str(orig), str(dest))
            manifest[f"file_{idx}"] = {
                "original": str(orig),
                "snapshot_name": snapshot_name,
                "exists": True,
            }
            log.debug("Snapshot saved: %s → %s", orig, dest)

        manifest_path = snap / "snapshot_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.debug("Snapshot manifest written: %s (%d entries)", manifest_path, len(manifest))

    def _rollback_from_snapshot(self, snapshot_dir: str) -> None:
        """Restore files from *snapshot_dir* using snapshot_manifest.json.

        After restoring, the snapshot directory is removed.

        Args:
            snapshot_dir: Directory previously created by :meth:`_snapshot_files`.
        """
        snap = Path(snapshot_dir)
        manifest_path = snap / "snapshot_manifest.json"

        if not manifest_path.exists():
            log.error("Rollback aborted: snapshot manifest not found at %s", manifest_path)
            return

        try:
            manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Rollback aborted: cannot parse snapshot manifest: %s", exc)
            return

        log.debug("Rollback triggered from snapshot: %s (%d files)", snapshot_dir, len(manifest))

        for key, entry in manifest.items():
            original = entry.get("original")
            snapshot_name = entry.get("snapshot_name")
            existed = entry.get("exists", False)

            if not original:
                continue

            orig_path = Path(original)

            if not existed:
                # File did not exist before — remove it if it was created during the operation.
                if orig_path.exists():
                    orig_path.unlink()
                    log.debug("Rollback: removed newly-created file %s", orig_path)
                continue

            snap_file = snap / snapshot_name
            if not snap_file.exists():
                log.error("Rollback: snapshot file missing for %s (%s)", original, snapshot_name)
                continue

            orig_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(snap_file), str(orig_path))
            log.debug("Rollback: restored %s", orig_path)

        # Cleanup snapshot directory after successful rollback.
        try:
            shutil.rmtree(str(snap))
        except Exception as exc:
            log.warning("Rollback: failed to remove snapshot dir %s: %s", snapshot_dir, exc)

        log.error("Rollback complete: all files restored from snapshot %s", snapshot_dir)

    # ------------------------------------------------------------------ #
    #  verify-update execution                                            #
    # ------------------------------------------------------------------ #

    def _execute_verify_update(self, task: dict) -> dict:
        """Execute a verify-update task with pre/post snapshot/rollback.

        Flow:
          1. Determine target files from task metadata.
          2. Snapshot target files.
          3. Delegate to the normal AI execution (_execute_task equivalent).
          4a. On success: clean up snapshot, return result.
          4b. On failure (exception or status==failed): rollback, re-raise/return.

        Args:
            task: Task dict as returned by _claim_task().

        Returns:
            Outcome dict with ``{"status": "succeeded", "result": {...}}``.

        Raises:
            Exception: Re-raised after rollback when execution raises unexpectedly.
        """
        task_id = task.get("task_id", "unknown")
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        target_files: list = (
            metadata.get("target_files", [])
            or metadata.get("changed_files", [])
        )

        snapshot_dir = os.path.join(SNAPSHOTS_BASE, task_id)

        # --- 1. Snapshot before applying changes ---
        log.debug("verify-update: snapshotting %d file(s) for task %s", len(target_files), task_id)
        self._snapshot_files(target_files, snapshot_dir)

        try:
            # --- 2. Execute via the parent AI execution pipeline ---
            outcome = self._execute_task(task)
            status = outcome.get("status", "failed")

            if status != "succeeded":
                # Execution reported failure — rollback and surface the failure.
                log.debug("verify-update: execution returned status=%s, initiating rollback", status)
                self._rollback_from_snapshot(snapshot_dir)
                return outcome

            # --- 3. Success path: clean up snapshot ---
            try:
                if Path(snapshot_dir).exists():
                    shutil.rmtree(snapshot_dir)
                    log.debug("verify-update: snapshot cleaned up after success: %s", snapshot_dir)
            except Exception as cleanup_exc:
                log.warning("verify-update: snapshot cleanup failed: %s", cleanup_exc)

            return outcome

        except Exception:
            # --- 4. Unexpected exception: rollback then re-raise ---
            log.debug("verify-update: exception raised, initiating rollback for task %s", task_id)
            self._rollback_from_snapshot(snapshot_dir)
            raise

    # ------------------------------------------------------------------ #
    #  Override _execute_task to route verify-update tasks               #
    # ------------------------------------------------------------------ #

    def _execute_task(self, task: dict) -> dict:
        """Route verify-update tasks through snapshot/rollback logic."""
        task_type = task.get("type", "task")

        # Guard against recursive call: _execute_verify_update calls super()._execute_task
        # We distinguish by checking for the internal marker.
        if task_type == "verify-update" and not task.get("_snapshot_bypass"):
            # Mark the copy so the recursive call goes to the parent directly.
            task_copy = dict(task)
            task_copy["_snapshot_bypass"] = True
            return self._execute_verify_update(task_copy)

        # Delegate all other types (and the bypassed verify-update) to parent.
        return super()._execute_task(task)


# ------------------------------------------------------------------ #
#  Module-level convenience: create a default Executor instance       #
# ------------------------------------------------------------------ #

def make_executor(project_id: str = "aming-claw", **kwargs) -> Executor:
    """Create an :class:`Executor` with the given *project_id*.

    Keyword arguments are forwarded to :class:`Executor.__init__`.
    """
    return Executor(
        project_id=project_id,
        governance_url=kwargs.get("governance_url", GOVERNANCE_URL),
        worker_id=kwargs.get("worker_id", WORKER_ID),
        workspace=kwargs.get("workspace", WORKSPACE),
    )
