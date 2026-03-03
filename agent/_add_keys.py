"""Add missing locale keys to zh.json and en.json."""
import json

NEW_KEYS_ZH = {
    "msg.add_workspace_first": "请先添加工作区（/menu -> 工作区管理 -> 添加）",
    "msg.analyzing_project": "⏳ 正在使用 AI 分析项目，请稍候...",
    "msg.archive_deleted": "✅ 归档记录 [{ref}] 已删除。",
    "msg.archive_detail_label": "归档详情",
    "msg.archive_file_generated": "归档文件已生成: {path}",
    "msg.cannot_cancel": "无法取消",
    "msg.cannot_read_log": "无法读取日志文件",
    "msg.cannot_read_stage": "无法读取阶段执行记录",
    "msg.content_truncated": "... (内容已截断)",
    "msg.enter_path": "请输入路径",
    "msg.error_info": "错误信息: {err}",
    "msg.invalid_operation": "无效操作",
    "msg.invalid_page": "无效分页",
    "msg.invalid_page_num": "无效页码",
    "msg.model_list_label": "模型清单",
    "msg.no_description": "(无描述)",
    "msg.no_doc_content": "(无文档内容)",
    "msg.no_log": "无日志",
    "msg.no_log_file": "无执行日志文件",
    "msg.no_queued_tasks": "无排队任务",
    "msg.no_records": "无记录",
    "msg.no_stage_records": "无阶段执行记录（日志文件不存在）",
    "msg.no_stage_records_short": "无阶段执行记录",
    "msg.no_summary_info": "(无概要信息)",
    "msg.no_tasks_label": "无任务",
    "msg.no_workspaces_label": "无工作目录",
    "msg.no_workspaces_short": "尚未注册任何工作目录。",
    "msg.none_short": "(无)",
    "msg.not_available": "不可用",
    "msg.only_cancel_active": "仅可取消待处理/执行中/排队中的任务，当前状态: {status}",
    "msg.only_cancel_pending": "仅可取消待处理任务，当前状态: {status}",
    "msg.page_num": "第 {num} 页",
    "msg.queue_status_label": "队列状态",
    "msg.read_failed": "读取失败",
    "msg.refreshed": "已刷新",
    "msg.rejection_reason": "拒绝原因: {reason}",
    "msg.search_roots_label": "搜索根目录",
    "msg.select_ws_default": "选择默认工作目录",
    "msg.select_ws_remove": "选择要删除的工作目录",
    "msg.stage_detail_label": "阶段详情",
    "msg.stage_overview_label": "阶段概览",
    "msg.task_cancelled": "✅ 任务 [{ref}] 已取消。",
    "msg.task_deleted": "✅ 任务 [{ref}] 已删除。",
    "msg.task_detail_label": "任务详情",
    "msg.task_not_found_short": "任务不存在",
    "msg.task_overview_label": "任务概览",
    "msg.truncated": "... (已截断)",
    "msg.unknown": "未知",
    "msg.user_cancelled": "用户取消",
    "msg.view_doc_label": "查看文档",
    "msg.view_log_label": "查看日志",
    "msg.view_summary_label": "查看概要",
    "msg.no_output": "(无输出)",
}

NEW_KEYS_EN = {
    "msg.add_workspace_first": "Please add a workspace first (/menu -> Workspace -> Add)",
    "msg.analyzing_project": "Analyzing project with AI, please wait...",
    "msg.archive_deleted": "Archive record [{ref}] deleted.",
    "msg.archive_detail_label": "Archive Detail",
    "msg.archive_file_generated": "Archive file generated: {path}",
    "msg.cannot_cancel": "Cannot cancel",
    "msg.cannot_read_log": "Cannot read log file",
    "msg.cannot_read_stage": "Cannot read stage execution record",
    "msg.content_truncated": "... (content truncated)",
    "msg.enter_path": "Enter path",
    "msg.error_info": "Error info: {err}",
    "msg.invalid_operation": "Invalid operation",
    "msg.invalid_page": "Invalid page",
    "msg.invalid_page_num": "Invalid page number",
    "msg.model_list_label": "Model List",
    "msg.no_description": "(no description)",
    "msg.no_doc_content": "(no document content)",
    "msg.no_log": "No log",
    "msg.no_log_file": "No execution log file",
    "msg.no_queued_tasks": "No queued tasks",
    "msg.no_records": "No records",
    "msg.no_stage_records": "No stage execution records (log file missing)",
    "msg.no_stage_records_short": "No stage execution records",
    "msg.no_summary_info": "(no summary info)",
    "msg.no_tasks_label": "No tasks",
    "msg.no_workspaces_label": "No workspaces",
    "msg.no_workspaces_short": "No workspaces registered.",
    "msg.none_short": "(none)",
    "msg.not_available": "Unavailable",
    "msg.only_cancel_active": "Can only cancel pending/processing/queued tasks, current status: {status}",
    "msg.only_cancel_pending": "Can only cancel pending tasks, current status: {status}",
    "msg.page_num": "Page {num}",
    "msg.queue_status_label": "Queue Status",
    "msg.read_failed": "Read failed",
    "msg.refreshed": "Refreshed",
    "msg.rejection_reason": "Rejection reason: {reason}",
    "msg.search_roots_label": "Search Roots",
    "msg.select_ws_default": "Select default workspace",
    "msg.select_ws_remove": "Select workspace to remove",
    "msg.stage_detail_label": "Stage Detail",
    "msg.stage_overview_label": "Stage Overview",
    "msg.task_cancelled": "Task [{ref}] cancelled.",
    "msg.task_deleted": "Task [{ref}] deleted.",
    "msg.task_detail_label": "Task Detail",
    "msg.task_not_found_short": "Task not found",
    "msg.task_overview_label": "Task Overview",
    "msg.truncated": "... (truncated)",
    "msg.unknown": "Unknown",
    "msg.user_cancelled": "User cancelled",
    "msg.view_doc_label": "View Document",
    "msg.view_log_label": "View Log",
    "msg.view_summary_label": "View Summary",
    "msg.no_output": "(no output)",
}

def add_keys(filepath, new_keys):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    added = 0
    for key, value in new_keys.items():
        parts = key.split(".")
        section = parts[0]
        name = parts[1]
        if section not in data:
            data[section] = {}
        if name not in data[section]:
            data[section][name] = value
            added += 1

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return added

zh_added = add_keys("locales/zh.json", NEW_KEYS_ZH)
en_added = add_keys("locales/en.json", NEW_KEYS_EN)
print(f"Added {zh_added} keys to zh.json, {en_added} keys to en.json")
