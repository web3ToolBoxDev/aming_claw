"""
task_accept.py - Task acceptance documents, finalization, and notification helpers.

Contains:
- json_sha256, write_run_log, acceptance_root
- build_acceptance_cases, write_acceptance_documents, to_pending_acceptance
- finalize_codex_task, finalize_pipeline_task
- task_inline_keyboard, build_task_summary, acceptance_notice_text
- run_post_acceptance_tests
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from utils import save_json, tasks_root, utc_iso


def json_sha256(data: Dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_run_log(task_id: str, data: Dict) -> Path:
    log_path = tasks_root() / "logs" / (task_id + ".run.json")
    save_json(log_path, data)
    return log_path


def acceptance_root() -> Path:
    p = tasks_root() / "acceptance"
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_acceptance_cases(task: Dict, result: Dict) -> List[Dict]:
    exec_status = str(result.get("execution_status") or result.get("status") or "unknown")
    executor = result.get("executor") or {}
    changed = executor.get("git_changed_files")
    changed_count = len(changed) if isinstance(changed, list) else 0
    task_text = str(task.get("text") or "").strip()
    summary = str(executor.get("last_message") or result.get("error") or "").strip()
    return [
        {
            "case_id": "AC-000",
            "title": "任务描述可测试",
            "steps": ["读取任务文本", "确认目标、范围、约束可用于验收"],
            "expected": "任务目标清晰，具备可验证结果",
            "actual": (task_text or "(空任务文本)")[:600],
            "status": "passed" if bool(task_text) else "failed",
        },
        {
            "case_id": "AC-001",
            "title": "任务执行结果可核对",
            "steps": ["查看 result JSON", "核对任务目标与执行摘要是否一致"],
            "expected": "执行摘要完整，且能说明任务处理结果",
            "actual": (summary or "(无执行摘要)")[:600],
            "status": "passed" if exec_status == "completed" else "failed",
        },
        {
            "case_id": "AC-002",
            "title": "运行日志可追溯",
            "steps": ["查看 runlog 文件", "核对命令、耗时、returncode、stdout/stderr"],
            "expected": "runlog 字段完整，可用于审计",
            "actual": "runlog_file={}".format(str(executor.get("runlog_file") or "")),
            "status": "passed" if bool(executor.get("runlog_file")) else "failed",
        },
        {
            "case_id": "AC-003",
            "title": "变更范围可核验",
            "steps": ["检查 git_changed_files", "确认变更是否符合任务范围"],
            "expected": "存在合理变更，或任务类型允许无代码变更",
            "actual": "git_changed_files_count={}".format(changed_count),
            "status": "passed" if (changed_count > 0 or task.get("action") != "codex") else "failed",
        },
        {
            "case_id": "UAT-001",
            "title": "用户验收确认",
            "steps": ["业务方查看结果、日志、测试用例", "业务方执行 /accept 或 /reject"],
            "expected": "用户明确给出通过或拒绝结论",
            "actual": "等待用户执行验收命令",
            "status": "pending",
        },
    ]


def write_acceptance_documents(task: Dict, result: Dict) -> Dict:
    root = acceptance_root()
    task_id = str(task.get("task_id") or "")
    md_path = root / (task_id + ".acceptance.md")
    case_path = root / (task_id + ".cases.json")
    executor = result.get("executor") or {}
    exec_status = str(result.get("execution_status") or result.get("status") or "unknown")
    cases = build_acceptance_cases(task, result)
    save_json(case_path, {"task_id": task_id, "generated_at": utc_iso(), "cases": cases})
    case_lines = []
    for c in cases:
        steps = c.get("steps") if isinstance(c.get("steps"), list) else []
        case_lines.append(
            "| {id} | {title} | {steps} | {expected} | {actual} | {status} |".format(
                id=c.get("case_id", ""),
                title=str(c.get("title", "")).replace("|", "/"),
                steps="; ".join(str(s).replace("|", "/") for s in steps)[:180],
                expected=str(c.get("expected", "")).replace("|", "/"),
                actual=str(c.get("actual", "")).replace("|", "/")[:120],
                status=c.get("status", ""),
            )
        )
    doc = (
        "# 任务测试与验收文档\n\n"
        "## 基本信息\n"
        "- task_id: {task_id}\n"
        "- task_code: {task_code}\n"
        "- action: {action}\n"
        "- execution_status: {execution_status}\n"
        "- generated_at: {generated_at}\n\n"
        "## 任务内容\n"
        "{task_text}\n\n"
        "## 执行摘要\n"
        "{summary}\n\n"
        "## 运行环境\n"
        "- workspace: {workspace}\n"
        "- elapsed_ms: {elapsed_ms}\n"
        "- returncode: {returncode}\n\n"
        "## 功能测试/验收用例\n"
        "| 用例ID | 标题 | 步骤 | 预期结果 | 实际结果 | 结论 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "{case_table}\n\n"
        "## 验收门禁规则\n"
        "- 规则1: 任务完成后必须先进入待验收，禁止直接归档\n"
        "- 规则2: 仅在用户执行 `/accept {task_code}` 后才允许归档\n"
        "- 规则3: 用户执行 `/reject {task_code} <原因>` 后任务保留在结果区，可继续迭代\n"
        "- 规则4: 可随时通过 `/status {task_code}` 查询当前状态与验收标识\n\n"
        "## 证据清单\n"
        "- result_file: `shared-volume/codex-tasks/results/{task_id}.json`\n"
        "- runlog_file: `{runlog_file}`\n"
        "- cases_file: `{cases_file}`\n\n"
        "## 验收结论\n"
        "- 当前状态: 待用户验收\n"
        "- 通过命令: /accept {task_code}\n"
        "- 拒绝命令: /reject {task_code} <原因>\n"
    ).format(
        task_id=task_id,
        task_code=result.get("task_code", "-"),
        action=task.get("action", "codex"),
        execution_status=exec_status,
        generated_at=utc_iso(),
        task_text=task.get("text", ""),
        summary=(executor.get("last_message") or result.get("error") or "(见结果文件)")[:1200],
        workspace=executor.get("workspace", ""),
        elapsed_ms=executor.get("elapsed_ms", 0),
        returncode=executor.get("returncode", ""),
        case_table="\n".join(case_lines),
        runlog_file=str(executor.get("runlog_file") or ""),
        cases_file=str(case_path),
    )
    md_path.write_text(doc, encoding="utf-8")
    return {"doc_file": str(md_path), "cases_file": str(case_path)}


def to_pending_acceptance(task: Dict, result: Dict) -> Dict:
    exec_status = str(result.get("status") or "unknown")
    result["execution_status"] = exec_status
    result["status"] = "pending_acceptance"
    result["updated_at"] = utc_iso()
    generate_docs = os.getenv("TASK_GENERATE_ACCEPTANCE_FILES", "1").strip().lower() in {"1", "true", "yes"}
    docs = write_acceptance_documents(task, result) if generate_docs else {"doc_file": "", "cases_file": ""}
    acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else {}
    # Preserve iteration_count from retry; default to 1 for first execution
    if "iteration_count" not in acceptance:
        acceptance["iteration_count"] = 1
    acceptance.update(
        {
            "state": "pending",
            "acceptance_required": True,
            "archive_allowed": False,
            "gate_rule": "only_after_user_accept",
            "updated_at": utc_iso(),
            "doc_file": docs.get("doc_file", ""),
            "cases_file": docs.get("cases_file", ""),
        }
    )
    result["acceptance"] = acceptance
    return result


def task_inline_keyboard(task_code: str, task_id: str) -> Dict:
    return {
        "inline_keyboard": [
            [
                {"text": "查看状态", "callback_data": "status:{}".format(task_code or task_id)},
                {"text": "验收通过", "callback_data": "accept:{}".format(task_code or task_id)},
                {"text": "验收拒绝", "callback_data": "reject:{}".format(task_code or task_id)},
            ],
            [
                {"text": "查看事件", "callback_data": "events:{}".format(task_code or task_id)},
            ],
        ]
    }


def finalize_codex_task(task: Dict, processing: Path, run: Dict, status: str, error: Optional[str] = None) -> Dict:
    result = {
        **task,
        "status": status,
        "completed_at": utc_iso(),
        "updated_at": utc_iso(),
        "executor": {
            "action": task.get("action", "codex"),
            "elapsed_ms": run.get("elapsed_ms"),
            "returncode": run.get("returncode"),
            "last_message": run.get("last_message", ""),
            "workspace": run.get("workspace"),
            "git_changed_files": run.get("git_changed_files"),
            "noop_reason": run.get("noop_reason"),
            "attempt_count": run.get("attempt_count", 1),
            "attempt_tag": run.get("attempt_tag", ""),
        },
    }
    if error:
        result["error"] = error

    # Save once to processing to preserve current behavior, then hash and cross-link.
    save_json(processing, result)
    result_hash = json_sha256(result)

    run_data = {
        "task_id": task["task_id"],
        "status": status,
        "updated_at": utc_iso(),
        "returncode": run.get("returncode"),
        "elapsed_ms": run.get("elapsed_ms"),
        "cmd": run.get("cmd"),
        "timeout_retries": run.get("timeout_retries", 0),
        "workspace": run.get("workspace"),
        "git_changed_files": run.get("git_changed_files"),
        "noop_reason": run.get("noop_reason"),
        "attempt_count": run.get("attempt_count", 1),
        "attempt_tag": run.get("attempt_tag", ""),
        "noop_retry_last_reason": run.get("noop_retry_last_reason"),
        "stdout": run.get("stdout", ""),
        "stderr": run.get("stderr", ""),
        "last_message": run.get("last_message", ""),
        "error": error,
        "result_sha256": result_hash,
    }
    run_log_path = write_run_log(task["task_id"], run_data)
    runlog_hash = json_sha256(run_data)

    result["executor"]["runlog_file"] = str(run_log_path)
    result["executor"]["runlog_sha256"] = runlog_hash
    result["executor"]["result_sha256"] = result_hash
    save_json(processing, result)
    return result


def finalize_pipeline_task(
    task: Dict, processing: Path, stage_results: List[Dict], status: str,
    error: Optional[str] = None, stages_model_info: Optional[List[Dict]] = None
) -> Dict:
    """Write pipeline result JSON + runlog with per-stage details."""
    last_run = stage_results[-1]["run"] if stage_results else {}
    total_ms = sum((sr["run"].get("elapsed_ms") or 0) for sr in stage_results)

    # Collect all changed files across stages (deduplicated, order preserved)
    seen: set = set()
    all_changed: List[str] = []
    for sr in stage_results:
        for f in (sr["run"].get("git_changed_files") or []):
            if f not in seen:
                seen.add(f)
                all_changed.append(f)

    stage_summary = [
        {
            "stage": sr["stage"],
            "backend": sr["backend"],
            "model": sr.get("model", ""),
            "provider": sr.get("provider", ""),
            "stage_index": sr["stage_index"],
            "returncode": sr["run"].get("returncode"),
            "elapsed_ms": sr["run"].get("elapsed_ms"),
            "noop_reason": sr["run"].get("noop_reason"),
            "attempt_count": sr["run"].get("attempt_count", 1),
            "last_message_preview": (sr["run"].get("last_message") or "")[:600],
        }
        for sr in stage_results
    ]

    result = {
        **task,
        "status": status,
        "completed_at": utc_iso(),
        "updated_at": utc_iso(),
        "executor": {
            "action": "pipeline",
            "elapsed_ms": total_ms,
            "returncode": last_run.get("returncode"),
            "last_message": (last_run.get("last_message") or "").strip(),
            "workspace": last_run.get("workspace"),
            "git_changed_files": all_changed or last_run.get("git_changed_files"),
            "noop_reason": last_run.get("noop_reason"),
            "attempt_count": sum(sr["run"].get("attempt_count", 1) for sr in stage_results),
            "attempt_tag": "pipeline",
            "stage_count": len(stage_results),
            "stages": stage_summary,
        },
    }
    if stages_model_info:
        result["stages_model_info"] = stages_model_info
    if error:
        result["error"] = error

    save_json(processing, result)
    result_hash = json_sha256(result)

    run_data = {
        "task_id": task["task_id"],
        "status": status,
        "updated_at": utc_iso(),
        "action": "pipeline",
        "returncode": last_run.get("returncode"),
        "elapsed_ms": total_ms,
        "workspace": last_run.get("workspace"),
        "git_changed_files": all_changed or last_run.get("git_changed_files"),
        "noop_reason": last_run.get("noop_reason"),
        "error": error,
        "result_sha256": result_hash,
        "stages": stage_summary,
        "stage_details": [
            {
                "stage": sr["stage"],
                "backend": sr["backend"],
                "model": sr.get("model", ""),
                "provider": sr.get("provider", ""),
                "stage_index": sr["stage_index"],
                "returncode": sr["run"].get("returncode"),
                "elapsed_ms": sr["run"].get("elapsed_ms"),
                "stdout": (sr["run"].get("stdout") or "")[-6000:],
                "stderr": (sr["run"].get("stderr") or "")[-2000:],
                "last_message": (sr["run"].get("last_message") or "")[-6000:],
                "cmd": sr["run"].get("cmd"),
                "noop_reason": sr["run"].get("noop_reason"),
                "attempt_count": sr["run"].get("attempt_count", 1),
            }
            for sr in stage_results
        ],
    }
    run_log_path = write_run_log(task["task_id"], run_data)
    runlog_hash = json_sha256(run_data)

    result["executor"]["runlog_file"] = str(run_log_path)
    result["executor"]["runlog_sha256"] = runlog_hash
    result["executor"]["result_sha256"] = result_hash
    save_json(processing, result)
    return result


def build_task_summary(result: Dict) -> str:
    executor = result.get("executor") or {}
    summary = (executor.get("last_message") or "").strip()
    if summary:
        return summary
    noop_reason = (executor.get("noop_reason") or "").strip()
    if noop_reason:
        return "失败原因: {}".format(noop_reason)
    error = (result.get("error") or "").strip()
    if error:
        return "错误: {}".format(error)
    return "(见日志文件)"


def acceptance_notice_text(result: Dict, task_id: str, task_code: str, *, detailed: bool) -> str:
    execution_status = str(result.get("execution_status") or result.get("status") or "unknown")
    elapsed = (result.get("executor") or {}).get("elapsed_ms", 0)
    summary = build_task_summary(result)
    acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else {}
    iteration = int(acceptance.get("iteration_count") or 1)
    iteration_tag = "（第{}轮迭代）".format(iteration) if iteration > 1 else ""
    if execution_status == "failed":
        if detailed:
            return (
                "任务 [{code}] {task_id} 执行失败{iter}，等待验收。\n"
                "状态: pending_acceptance\n"
                "执行结果: failed\n"
                "耗时: {elapsed} ms\n"
                "失败摘要:\n{summary}\n\n"
                "通过: /accept {code}\n"
                "拒绝: /reject {code} <原因>"
            ).format(code=task_code, task_id=task_id, elapsed=elapsed, summary=summary[:800], iter=iteration_tag)
        return (
            "任务 [{code}] {task_id} 执行失败{iter}，等待验收。\n"
            "状态: pending_acceptance\n"
            "执行结果: failed\n"
            "失败摘要: {summary}\n"
            "通过: /accept {code}\n"
            "拒绝: /reject {code} <原因>"
        ).format(code=task_code, task_id=task_id, summary=summary[:300], iter=iteration_tag)
    if detailed:
        return (
            "任务 [{code}] {task_id} 执行完成{iter}，等待验收。\n"
            "状态: pending_acceptance\n"
            "执行结果: {execution_status}\n"
            "耗时: {elapsed} ms\n"
            "摘要:\n{summary}\n\n"
            "通过: /accept {code}\n"
            "拒绝: /reject {code} <原因>"
        ).format(
            code=task_code,
            task_id=task_id,
            execution_status=execution_status,
            elapsed=elapsed,
            summary=summary[:800],
            iter=iteration_tag,
        )
    return (
        "任务 [{code}] {task_id} 已处理完成{iter}，等待验收。\n"
        "状态: pending_acceptance\n"
        "执行结果: {execution_status}\n"
        "概要: {summary}\n"
        "通过: /accept {code}\n"
        "拒绝: /reject {code} <原因>"
    ).format(
        code=task_code,
        task_id=task_id,
        execution_status=execution_status,
        summary=summary[:300],
        iter=iteration_tag,
    )


def run_post_acceptance_tests(workspace: Path) -> Dict:
    """Run test suite after acceptance. Returns {passed, output, error}.

    Controlled by env vars:
    - ACCEPTANCE_TEST_ENABLED: "1" (default) to enable, "0" to skip
    - ACCEPTANCE_TEST_CMD: custom test command (default: python -m pytest agent/tests/ -x -q)
    - ACCEPTANCE_TEST_TIMEOUT: timeout in seconds (default: 120)
    """
    enabled = os.getenv("ACCEPTANCE_TEST_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
    if not enabled:
        return {"passed": True, "output": "(tests skipped)", "error": "", "skipped": True}

    test_cmd = os.getenv("ACCEPTANCE_TEST_CMD", "").strip()
    if not test_cmd:
        python_bin = sys.executable or "python"
        test_cmd = "{} -m unittest discover -s agent/tests -p test_*.py -f".format(python_bin)

    timeout = int(os.getenv("ACCEPTANCE_TEST_TIMEOUT", "120"))
    try:
        proc = subprocess.run(
            test_cmd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(workspace),
            check=False,
        )
        passed = proc.returncode == 0
        output = (proc.stdout or "")[-3000:] + "\n" + (proc.stderr or "")[-1000:]
        return {"passed": passed, "output": output.strip(), "error": ""}
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "", "error": "测试超时 ({}s)".format(timeout)}
    except Exception as exc:
        return {"passed": False, "output": "", "error": str(exc)[:500]}
