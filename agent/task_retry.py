"""
task_retry.py - Task retry/re-develop functionality after rejection.

Provides:
- retry_task: Core retry logic (state reset, history tracking, prompt enhancement)
- build_retry_summary: Generate enhanced prompt with rejection context
- MAX_RETRY_ITERATIONS: Configurable iteration limit via environment variable

Lifecycle:
  rejected (results/) -> retry -> pending (pending/) -> executor picks up
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from typing import Dict, Optional, Tuple

from utils import load_json, save_json, task_file, tasks_root, utc_iso
from task_state import (
    append_task_event,
    update_task_runtime,
)
from git_rollback import pre_task_checkpoint


# ── Constants ────────────────────────────────────────────────────────────────

_SUMMARY_MAX_CHARS = 1000


def get_max_retry_iterations() -> int:
    """Read MAX_RETRY_ITERATIONS from env. 0 means unlimited (use default=3)."""
    raw = os.getenv("MAX_RETRY_ITERATIONS", "3").strip()
    try:
        val = int(raw)
    except ValueError:
        return 3
    if val <= 0:
        return 3
    return val


# ── Rejection history helpers ────────────────────────────────────────────────

def _build_rejection_record(acceptance: Dict, iteration: int) -> Dict:
    """Build a single rejection history entry from acceptance fields."""
    return {
        "reason": str(acceptance.get("reason") or "(未提供)"),
        "rejected_at": str(acceptance.get("rejected_at") or ""),
        "rejected_by": acceptance.get("rejected_by"),
        "iteration": iteration,
    }


def _append_rejection_history(acceptance: Dict, iteration: int) -> None:
    """Append current rejection info to rejection_history array."""
    history = acceptance.get("rejection_history")
    if not isinstance(history, list):
        history = []
    record = _build_rejection_record(acceptance, iteration)
    history.append(record)
    acceptance["rejection_history"] = history


# ── Enhanced prompt builder ──────────────────────────────────────────────────

def build_retry_summary(task: Dict, extra_instruction: str = "") -> str:
    """Generate enhanced prompt context from rejection history.

    Returns a structured summary block to be prepended to the original prompt.
    Includes: original task description, all rejection reasons, user supplements.
    """
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    history = acceptance.get("rejection_history")
    if not isinstance(history, list):
        history = []
    iteration_count = int(acceptance.get("iteration_count") or 1)

    lines = []
    lines.append("=" * 40)
    lines.append("上一轮验收未通过摘要（第{}轮重新开发）".format(iteration_count + 1))
    lines.append("=" * 40)

    # Original task description
    original_text = str(task.get("text") or "").strip()
    if original_text:
        lines.append("\n原始任务描述:\n{}".format(original_text[:500]))

    # All rejection reasons (accumulated)
    if history:
        lines.append("\n历史验收不通过记录:")
        # Limit to recent entries to control length
        recent = history[-5:] if len(history) > 5 else history
        for rec in recent:
            lines.append("  第{}轮 - 验收未通过原因: {}".format(
                rec.get("iteration", "?"),
                str(rec.get("reason", "(未提供)"))[:300],
            ))
    else:
        # Single rejection (first time retry)
        reason = str(acceptance.get("reason") or "(未提供)")
        lines.append("\n验收未通过原因: {}".format(reason[:500]))

    # Last execution output summary
    executor = task.get("executor") if isinstance(task.get("executor"), dict) else {}
    last_msg = str(executor.get("last_message") or "").strip()
    if last_msg:
        lines.append("\n上轮执行产出摘要:\n{}".format(last_msg[:400]))

    # User supplement
    if extra_instruction:
        lines.append("\n用户补充说明: {}".format(extra_instruction.strip()[:300]))

    lines.append("=" * 40)

    summary = "\n".join(lines)
    # Enforce max length
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[:_SUMMARY_MAX_CHARS] + "\n...(摘要已截断)"
    return summary


# ── Core retry logic ─────────────────────────────────────────────────────────

def retry_task(
    task: Dict,
    user_id: int,
    extra_instruction: str = "",
) -> Tuple[bool, str, Optional[Dict]]:
    """Retry a rejected task: validate, update state, move file, return enhanced task.

    Args:
        task: The task dict loaded from results/{task_id}.json (must have _stage="results")
        user_id: User who triggered the retry
        extra_instruction: Optional user supplement for the retry

    Returns:
        (success, message, updated_task_or_None)
    """
    task_id = str(task.get("task_id") or "")
    task_code = str(task.get("task_code") or "-")
    status = str(task.get("status") or "")
    stage = str(task.get("_stage") or "")

    # ── AC-2: State validation ──
    if status == "accepted":
        return False, "任务已验收通过并归档，无法重新开发", None
    if status != "rejected":
        return False, "只能对验收拒绝的任务重新开发（当前状态: {}）".format(status), None
    if stage != "results":
        return False, "任务不在results阶段，无法重新开发（当前stage: {}）".format(stage), None

    # ── AC-7: Iteration limit ──
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    current_iteration = int(acceptance.get("iteration_count") or 1)
    max_iterations = get_max_retry_iterations()
    if current_iteration >= max_iterations:
        return False, "已达最大迭代次数({})，请手动创建新任务".format(max_iterations), None

    # ── AC-4: Record rejection history ──
    _append_rejection_history(acceptance, current_iteration)
    new_iteration = current_iteration + 1
    acceptance["iteration_count"] = new_iteration

    # ── AC-3: Build enhanced prompt ──
    retry_summary = build_retry_summary(task, extra_instruction)
    original_text = str(task.get("text") or "").strip()
    enhanced_text = "{}\n\n{}".format(retry_summary, original_text)

    # ── AC-5: Git checkpoint handling ──
    git_checkpoint_msg = ""
    try:
        ckpt = pre_task_checkpoint(task_id=task_id)
        if ckpt.get("checkpoint_commit"):
            task["_git_checkpoint"] = ckpt["checkpoint_commit"]
            git_checkpoint_msg = "新检查点: {}".format(ckpt["checkpoint_commit"][:12])
        if ckpt.get("error"):
            git_checkpoint_msg = "Git检查点警告: {}".format(ckpt["error"][:200])
    except Exception as exc:
        git_checkpoint_msg = "Git检查点异常: {}".format(str(exc)[:200])

    # ── AC-4: Reset task state ──
    # Clear execution artifacts but preserve history
    task["status"] = "pending"
    task["updated_at"] = utc_iso()
    task["_retry_enhanced_text"] = enhanced_text
    task["acceptance"] = acceptance
    # Clear previous execution products
    task.pop("executor", None)
    task.pop("completed_at", None)
    task.pop("error", None)
    task.pop("execution_status", None)
    task.pop("_stage", None)
    task.pop("_task_ref", None)

    # ── Move task file from results/ to pending/ ──
    result_path = task_file("results", task_id)
    pending_path = task_file("pending", task_id)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        save_json(result_path, task)
        shutil.move(str(result_path), str(pending_path))
    else:
        save_json(pending_path, task)

    # ── Update runtime state ──
    update_task_runtime(task, status="pending", stage="pending")

    # ── AC-8: Event log ──
    append_task_event(task_id, "task_retry", {
        "iteration": new_iteration,
        "reason_summary": str(acceptance.get("reason") or "")[:300],
        "triggered_by": user_id,
        "extra_instruction": extra_instruction[:200] if extra_instruction else "",
        "git_checkpoint": git_checkpoint_msg,
    })

    msg = (
        "任务 [{code}] {task_id} 已重新提交开发（第{iter}轮）\n"
        "状态: pending\n"
        "增强prompt已注入拒绝原因摘要"
    ).format(code=task_code, task_id=task_id, iter=new_iteration)
    if git_checkpoint_msg:
        msg += "\n{}".format(git_checkpoint_msg)

    return True, msg, task
