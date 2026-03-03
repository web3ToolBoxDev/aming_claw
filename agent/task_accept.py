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


def _is_qa_boilerplate(text: str) -> bool:
    """Detect QA boilerplate responses that don't contain actual audit results.

    QA models sometimes return template text like "请提供以下材料..." or
    "已切换为QA验收专家模式" instead of actual audit results.
    """
    if not text:
        return True
    boilerplate_markers = [
        "请提供以下材料",
        "已切换为",
        "把材料发来后",
        "收到后我会输出",
        "收到后我将",
        "我将按",
        "我直接开始审计",
    ]
    has_boilerplate = any(m in text for m in boilerplate_markers)
    if not has_boilerplate:
        return False
    # Check if there's an actual verdict WITH a concrete value
    # (not template options like "通过 / 有条件通过 / 不通过")
    import re
    real_verdict_patterns = [
        r"验收结论[：:]\s*(通过|不通过|有条件通过)(?!\s*/)",  # actual verdict, not "通过 / 不通过"
        r"总体结论[：:]\s*(通过|不通过|有条件通过)(?!\s*/)",
        r"✓通过\s+\w",             # actual checkmark with item reference
        r"✗未通过\s+\w",           # actual X mark with item reference
        r"⚠部分通过\s+\w",        # actual warning with item reference
        r"AC-\d+.*[✓✗⚠]",        # "AC-001 ✓通过" acceptance case items
    ]
    for pattern in real_verdict_patterns:
        if re.search(pattern, text, re.MULTILINE):
            return False
    return True


def _generate_auto_verdict(stages: list) -> str:
    """Generate an automatic verdict summary when QA fails to produce one.

    Uses dev and test stage results to create a basic pass/fail assessment.
    """
    parts = []
    all_passed = True

    for s in stages:
        name = s.get("stage", "?")
        rc = s.get("returncode", -1)
        noop = s.get("noop_reason")
        preview = str(s.get("last_message_preview") or "").strip()

        if name == "test":
            if rc == 0 and not noop:
                # Extract test count from preview
                import re
                m = re.search(r"(\d+)\s*(passed|tests?.*OK|通过)", preview)
                if m:
                    parts.append("测试: {} 通过".format(m.group(0)))
                else:
                    parts.append("测试: 通过 (returncode=0)")
            else:
                parts.append("测试: 失败")
                all_passed = False
        elif name == "dev":
            if rc == 0 and not noop:
                parts.append("开发: 完成")
            else:
                parts.append("开发: 失败")
                all_passed = False
        elif name == "pm":
            if rc == 0 and not noop:
                parts.append("需求: 已产出")
        elif name == "qa":
            if rc != 0 or noop:
                all_passed = False

    verdict = "自动判定: {}".format("通过" if all_passed else "待人工验收")
    return "{}\n{}".format(verdict, "\n".join(parts))


def _build_pipeline_summary(executor: Dict) -> str:
    """Build a meaningful summary for pipeline tasks from all stage outputs.

    Instead of showing only the last stage (often QA boilerplate), extracts
    key info from each stage: dev changes, test results, etc.
    """
    stages = executor.get("stages") or []
    if not stages:
        return str(executor.get("last_message") or "")

    parts = []
    qa_was_boilerplate = False
    for s in stages:
        name = s.get("stage", "?")
        rc = s.get("returncode", -1)
        elapsed = s.get("elapsed_ms", 0)
        noop = s.get("noop_reason")
        preview = str(s.get("last_message_preview") or "").strip()
        model = s.get("model", "")
        status_icon = "\u2705" if rc == 0 and not noop else "\u274c"

        # Stage header
        time_str = "{:.0f}s".format(elapsed / 1000) if elapsed else "0s"
        model_str = " ({})".format(model) if model else ""
        noop_tag = " - NOOP: {}".format(noop) if noop else ""
        header = "{} {} {}{}{}".format(status_icon, name.upper(), time_str, model_str, noop_tag)
        parts.append(header)

        # Extract the most useful snippet from the stage output
        if preview:
            if name == "qa" and _is_qa_boilerplate(preview):
                qa_was_boilerplate = True
                parts.append("(QA 未产出实质审计，已自动生成判定)")
            else:
                snippet = _extract_stage_snippet(name, preview)
                if snippet:
                    parts.append(snippet)
        parts.append("")

    # If QA returned boilerplate, add auto-verdict based on other stages
    if qa_was_boilerplate:
        auto_verdict = _generate_auto_verdict(stages)
        parts.append("━━ 自动验收判定 ━━")
        parts.append(auto_verdict)

    return "\n".join(parts).strip()


def _extract_stage_snippet(stage_name: str, preview: str) -> str:
    """Extract the most relevant snippet from a stage's output."""
    lines = preview.split("\n")
    max_lines = 8

    if stage_name == "dev":
        # Look for file change summary or step summary
        for marker in ["修改文件列表", "已执行步骤", "变更说明",
                        "子任务实现状态", "修改文件", "| 文件"]:
            for i, line in enumerate(lines):
                if marker in line:
                    return "\n".join(lines[i:i + max_lines]).strip()
        # Fallback: last N lines (usually summary)
        return "\n".join(lines[-max_lines:]).strip()[:500]

    if stage_name == "test":
        # Look for test result numbers
        for marker in ["passed", "failed", "通过", "失败",
                        "结论", "结果", "Ran ", "OK"]:
            for i, line in enumerate(lines):
                if marker in line.lower() or marker in line:
                    start = max(0, i - 1)
                    return "\n".join(lines[start:start + max_lines]).strip()
        return "\n".join(lines[:max_lines]).strip()[:500]

    if stage_name == "qa":
        # QA: look for verdict
        for marker in ["验收结论", "总体结论", "✓通过", "✗未通过",
                        "⚠部分通过", "有条件通过"]:
            for i, line in enumerate(lines):
                if marker in line:
                    start = max(0, i - 1)
                    return "\n".join(lines[start:start + max_lines]).strip()
        # If QA returned boilerplate, the caller handles it
        if _is_qa_boilerplate(preview):
            return ""
        return "\n".join(lines[:max_lines]).strip()[:500]

    # pm or others: first few lines
    return "\n".join(lines[:max_lines]).strip()[:500]


def build_acceptance_cases(task: Dict, result: Dict) -> List[Dict]:
    exec_status = str(result.get("execution_status") or result.get("status") or "unknown")
    executor = result.get("executor") or {}
    changed = executor.get("git_changed_files")
    changed_count = len(changed) if isinstance(changed, list) else 0
    task_text = str(task.get("text") or "").strip()
    # For pipeline tasks, build a multi-stage summary instead of just last_message
    is_pipeline = task.get("action") == "pipeline" and executor.get("stages")
    if is_pipeline:
        summary = _build_pipeline_summary(executor)
    else:
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
    is_pipeline = task.get("action") == "pipeline" and executor.get("stages")
    if is_pipeline:
        summary_text = _build_pipeline_summary(executor)[:2000]
    else:
        summary_text = (executor.get("last_message") or result.get("error") or "(见结果文件)")[:2000]

    # Build pipeline stages section for document
    pipeline_section = ""
    if is_pipeline:
        stages = executor.get("stages") or []
        stage_lines = ["## 流水线执行详情\n"]
        for s in stages:
            name = s.get("stage", "?")
            rc = s.get("returncode", -1)
            noop = s.get("noop_reason")
            elapsed_s = s.get("elapsed_ms", 0)
            model = s.get("model", "")
            icon = "\u2705" if rc == 0 and not noop else "\u274c"
            time_s = format_elapsed(elapsed_s)
            model_s = " ({})".format(model) if model else ""
            stage_lines.append("### {} {}{} - {} {}".format(
                icon, name.upper(), model_s, time_s,
                "NOOP: {}".format(noop) if noop else ""))
            preview = str(s.get("last_message_preview") or "").strip()
            if preview:
                if name == "qa" and _is_qa_boilerplate(preview):
                    stage_lines.append("QA 未产出实质审计结论（模型响应被截断）\n")
                else:
                    snippet = _extract_stage_snippet(name, preview)
                    if snippet:
                        stage_lines.append("```\n{}\n```\n".format(snippet[:800]))
        # Auto-verdict if QA was boilerplate
        qa_stages = [s for s in stages if s.get("stage") == "qa"]
        if qa_stages:
            qa_preview = str(qa_stages[0].get("last_message_preview") or "")
            if _is_qa_boilerplate(qa_preview):
                verdict = _generate_auto_verdict(stages)
                stage_lines.append("### 自动验收判定")
                stage_lines.append("```\n{}\n```\n".format(verdict))
        pipeline_section = "\n".join(stage_lines) + "\n\n"

    doc = (
        "# 任务测试与验收文档\n\n"
        "## 基本信息\n"
        "- task_code: {task_code}\n"
        "- action: {action}\n"
        "- execution_status: {execution_status}\n"
        "- elapsed: {elapsed}\n"
        "- generated_at: {generated_at}\n\n"
        "## 任务内容\n"
        "{task_text}\n\n"
        "## 执行摘要\n"
        "{summary}\n\n"
        "{pipeline_section}"
        "## 功能测试/验收用例\n"
        "| 用例ID | 标题 | 步骤 | 预期结果 | 实际结果 | 结论 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "{case_table}\n\n"
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
        elapsed=format_elapsed(executor.get("elapsed_ms", 0)),
        generated_at=utc_iso(),
        task_text=task.get("text", ""),
        summary=summary_text,
        pipeline_section=pipeline_section,
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
    ref = task_code or task_id
    return {
        "inline_keyboard": [
            [
                {"text": "验收通过", "callback_data": "accept:{}".format(ref)},
                {"text": "验收拒绝", "callback_data": "reject:{}".format(ref)},
            ],
            [
                {"text": "查看文档", "callback_data": "task_doc:{}".format(ref)},
                {"text": "查看详情", "callback_data": "task_detail:{}".format(ref)},
            ],
        ]
    }


def generate_stage_summary(stage_result: Dict) -> str:
    """Generate a concise Chinese summary (<=200 chars) from a stage execution result.

    Works for both pipeline stage_detail dicts and single-task run dicts.
    Uses pure text extraction, no external AI calls.
    """
    parts: List[str] = []

    last_message = str(stage_result.get("last_message") or "").strip()
    stdout = str(stage_result.get("stdout") or "").strip()
    returncode = stage_result.get("returncode")
    noop_reason = str(stage_result.get("noop_reason") or "").strip()
    changed_files = stage_result.get("git_changed_files") or []
    error = str(stage_result.get("error") or "").strip()

    # Primary: extract from last_message or stdout
    source_text = last_message or stdout
    if source_text:
        # Take first meaningful lines
        lines = [l.strip() for l in source_text.splitlines() if l.strip()]
        preview = "\n".join(lines[:5])
        if len(preview) > 150:
            preview = preview[:150] + "..."
        parts.append(preview)
    elif noop_reason:
        parts.append("未执行: {}".format(noop_reason[:80]))
    elif error:
        parts.append("错误: {}".format(error[:80]))

    # Changed files info
    if isinstance(changed_files, list) and changed_files:
        file_list = ", ".join(str(f).split("/")[-1] for f in changed_files[:5])
        if len(changed_files) > 5:
            file_list += " 等{}个文件".format(len(changed_files))
        parts.append("变更文件: {}".format(file_list))

    # Return code info (only if error)
    if returncode and returncode != 0 and not error:
        parts.append("返回码: {}".format(returncode))

    summary = "; ".join(parts) if parts else "(无执行输出)"
    return summary[:200]


def _write_summary_file(task_id: str, summary_text: str) -> Path:
    """Write standalone summary file: logs/{task_id}.summary.txt"""
    summary_path = tasks_root() / "logs" / (task_id + ".summary.txt")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary_text, encoding="utf-8")
    return summary_path


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

    # Generate summary for single-task run
    summary_input = dict(run)
    if error:
        summary_input["error"] = error
    run_summary = generate_stage_summary(summary_input)

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
        "summary": run_summary,
    }
    run_log_path = write_run_log(task["task_id"], run_data)
    runlog_hash = json_sha256(run_data)

    result["executor"]["runlog_file"] = str(run_log_path)
    result["executor"]["runlog_sha256"] = runlog_hash
    result["executor"]["result_sha256"] = result_hash
    save_json(processing, result)

    # Write standalone summary file
    _write_summary_file(task["task_id"], run_summary)

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
        "stage_details": [],
    }

    # Build stage_details with per-stage summary
    all_stage_summaries: List[str] = []
    for sr in stage_results:
        sd = {
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
        stage_sum = generate_stage_summary(sr["run"])
        sd["summary"] = stage_sum
        all_stage_summaries.append("[{}] {}".format(sr["stage"], stage_sum))
        run_data["stage_details"].append(sd)

    # Overall pipeline summary
    pipeline_summary = "\n".join(all_stage_summaries)
    if len(pipeline_summary) > 200:
        pipeline_summary = pipeline_summary[:197] + "..."
    run_data["summary"] = pipeline_summary

    run_log_path = write_run_log(task["task_id"], run_data)
    runlog_hash = json_sha256(run_data)

    result["executor"]["runlog_file"] = str(run_log_path)
    result["executor"]["runlog_sha256"] = runlog_hash
    result["executor"]["result_sha256"] = result_hash
    save_json(processing, result)

    # Write standalone summary file
    _write_summary_file(task["task_id"], pipeline_summary)

    return result


def format_elapsed(ms) -> str:
    """Convert milliseconds to human-readable duration (秒/分钟)."""
    try:
        total_sec = int(ms or 0) / 1000.0
    except (TypeError, ValueError):
        return "未知"
    if total_sec < 60:
        return "{:.1f} 秒".format(total_sec) if total_sec >= 1 else "{:.2f} 秒".format(total_sec)
    minutes = int(total_sec // 60)
    secs = int(total_sec % 60)
    if secs == 0:
        return "{} 分钟".format(minutes)
    return "{} 分 {} 秒".format(minutes, secs)


def _truncate_lines(text: str, max_lines: int = 3) -> str:
    """Truncate text to max_lines, appending '...' if truncated."""
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return text.strip()
    return "\n".join(lines[:max_lines]) + "\n..."


def build_task_summary(result: Dict) -> str:
    executor = result.get("executor") or {}
    # For pipeline tasks, generate multi-stage summary
    if result.get("action") == "pipeline" and executor.get("stages"):
        pipeline_summary = _build_pipeline_summary(executor)
        if pipeline_summary:
            return pipeline_summary
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


def _format_pipeline_stage_line(s: Dict) -> str:
    """Format a single pipeline stage as a concise one-line summary."""
    name = s.get("stage", "?")
    rc = s.get("returncode", -1)
    noop = s.get("noop_reason")
    elapsed = s.get("elapsed_ms", 0)
    model = s.get("model", "")
    icon = "\u2705" if rc == 0 and not noop else "\u274c"
    time_str = format_elapsed(elapsed) if elapsed else "0s"
    model_str = " ({})".format(model) if model else ""
    noop_tag = " NOOP" if noop else ""
    return "  {} {}{} {}{}".format(icon, name.upper(), model_str, time_str, noop_tag)


def acceptance_notice_text(result: Dict, task_id: str, task_code: str, *, detailed: bool) -> str:
    execution_status = str(result.get("execution_status") or result.get("status") or "unknown")
    executor = result.get("executor") or {}
    elapsed = executor.get("elapsed_ms", 0)
    elapsed_str = format_elapsed(elapsed)
    acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else {}
    iteration = int(acceptance.get("iteration_count") or 1)
    iteration_tag = "（第{}轮）".format(iteration) if iteration > 1 else ""
    separator = "━━━━━━━━━━━━━━━━━━━━━━━━"

    is_pipeline = result.get("action") == "pipeline"
    stages = executor.get("stages") or []

    if execution_status == "failed":
        error_msg = str(result.get("error") or executor.get("noop_reason") or "").strip()
        lines = [
            "\u274c 任务 [{code}] 执行失败{iter}".format(code=task_code, iter=iteration_tag),
            "耗时: {}".format(elapsed_str),
            separator,
        ]
        if is_pipeline and stages:
            lines.append("流水线阶段:")
            for s in stages:
                lines.append(_format_pipeline_stage_line(s))
            lines.append(separator)
        if error_msg:
            lines.append("失败原因: {}".format(_truncate_lines(error_msg[:500], 3)))
        return "\n".join(lines)

    # ── Success ──
    lines = [
        "\u2705 任务 [{code}] 执行完成{iter}".format(code=task_code, iter=iteration_tag),
        "耗时: {}".format(elapsed_str),
        separator,
    ]

    if is_pipeline and stages:
        lines.append("流水线阶段:")
        for s in stages:
            lines.append(_format_pipeline_stage_line(s))
        lines.append(separator)

        # Build concise verdict
        test_stage = next((s for s in stages if s.get("stage") == "test"), None)
        qa_stage = next((s for s in stages if s.get("stage") == "qa"), None)

        # Test result summary
        if test_stage:
            test_preview = str(test_stage.get("last_message_preview") or "").strip()
            test_rc = test_stage.get("returncode", -1)
            if test_rc == 0 and not test_stage.get("noop_reason"):
                import re
                m = re.search(r"(\d+)\s*(passed|tests?.*OK|通过)", test_preview)
                if m:
                    lines.append("测试结果: \u2705 {}".format(m.group(0)))
                else:
                    lines.append("测试结果: \u2705 通过")
            else:
                lines.append("测试结果: \u274c 失败")

        # QA verdict or auto-verdict
        if qa_stage:
            qa_preview = str(qa_stage.get("last_message_preview") or "").strip()
            if _is_qa_boilerplate(qa_preview):
                lines.append("QA审计: \u26a0 未产出（已自动判定）")
            else:
                # Try extracting actual verdict
                snippet = _extract_stage_snippet("qa", qa_preview)
                if snippet:
                    lines.append("QA审计: {}".format(_truncate_lines(snippet, 3)))

        if detailed:
            # Dev changes summary
            dev_stage = next((s for s in stages if s.get("stage") == "dev"), None)
            if dev_stage:
                dev_preview = str(dev_stage.get("last_message_preview") or "").strip()
                if dev_preview:
                    snippet = _extract_stage_snippet("dev", dev_preview)
                    if snippet:
                        lines.append(separator)
                        lines.append("开发摘要:")
                        lines.append(_truncate_lines(snippet, 5))
    else:
        # Non-pipeline task
        summary = build_task_summary(result)
        lines.append("概要: {}".format(_truncate_lines(summary[:500], 3)))

    return "\n".join(lines)


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
