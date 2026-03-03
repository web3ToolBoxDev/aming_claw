"""Third pass: convert remaining unicode-escaped Chinese strings to t() calls.
Uses line-by-line replacement with raw string matching.
"""
import re

FILEPATH = "bot_commands.py"

with open(FILEPATH, "r", encoding="utf-8") as f:
    lines = f.readlines()

count = 0

def replace_line(lineno, old_text, new_text):
    """Replace old_text with new_text on the given line (1-indexed)."""
    global count
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        print(f"SKIP: line {lineno} out of range")
        return
    if old_text not in lines[idx]:
        print(f"WARN line {lineno}: pattern not found: {old_text[:60]}")
        return
    lines[idx] = lines[idx].replace(old_text, new_text, 1)
    count += 1


# ---- Simple answer_callback_query replacements ----

# Line 955: "\u5df2\u53d6\u6d88" -> t("callback.cancelled")
replace_line(955, r'"\u5df2\u53d6\u6d88"', 't("callback.cancelled")')

# Line 979: permission insufficient
replace_line(979, r'"\u26a0\ufe0f \u6743\u9650\u4e0d\u8db3\uff0c\u4ec5\u6388\u6743\u7528\u6237\u53ef\u4fee\u6539\u6a21\u578b\u914d\u7f6e"', 't("callback.perm_insufficient")')

# Line 989: unavailable_reason default
replace_line(989, r'"\u4e0d\u53ef\u7528"', 't("msg.not_available")')

# Line 990: model unavailable
replace_line(990, r'"\u6a21\u578b\u4e0d\u53ef\u7528"', 't("callback.model_unavailable")')

# Line 991: set failed
replace_line(991, r'"\u274c \u8bbe\u7f6e\u5931\u8d25\uff1a\u6a21\u578b {} \u5f53\u524d\u4e0d\u53ef\u7528\uff08{}\uff09".format(model_id, reason)', 't("msg.set_failed", model=model_id, reason=reason)')

# Line 996: set as default
replace_line(996, r'"\u5df2\u8bbe\u4e3a\u9ed8\u8ba4"', 't("callback.set_as_default")')

# Line 999: default model set
replace_line(999, r'"\u5df2\u5c06\u9ed8\u8ba4\u6a21\u578b\u8bbe\u4e3a {} `{}`\uff0c\u7ba1\u7ebf\u4e2d\u672a\u5355\u72ec\u914d\u7f6e\u7684\u8282\u70b9\u5c06\u4f7f\u7528\u6b64\u6a21\u578b".format(tag, model_id)', 't("msg.default_model_set", tag=tag, model=model_id)')

# Lines 1034, 1191, 1722: role_pipeline display name
replace_line(1034, r'"role_pipeline": "\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf"', '"role_pipeline": t("msg.role_pipeline_config")')
replace_line(1191, r'"role_pipeline": "\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf"', '"role_pipeline": t("msg.role_pipeline_config")')
replace_line(1722, r'"role_pipeline": "\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf"', '"role_pipeline": t("msg.role_pipeline_config")')

# Line 1041: select preset
replace_line(1041, r'"\u9009\u62e9\u9884\u8bbe: {}".format(preset_display)', 't("callback.select_preset", name=preset_display)')

# Line 1088: save failed
replace_line(1088, r'"\u274c \u4fdd\u5b58\u5931\u8d25\uff1a{}".format(exc)', 't("msg.save_failed", err=str(exc))')

# Lines with "\u65e0\u6743\u9650" (no permission)
for ln in [1108, 1151, 1207, 1702]:
    replace_line(ln, r'"\u65e0\u6743\u9650"', 't("callback.no_permission")')

# Lines with config expired
for ln in [1116, 1163, 1213, 1709]:
    replace_line(ln, r'"\u2699\ufe0f \u914d\u7f6e\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u9009\u62e9\u9884\u8bbe\u3002"', 't("msg.config_expired")')

# Lines with please_reselect
for ln in [1119, 1166, 1216, 1712]:
    replace_line(ln, r'"\u8bf7\u91cd\u65b0\u9009\u62e9"', 't("callback.please_reselect")')

# Lines with invalid_data
for ln in [1125, 1156, 1173]:
    replace_line(ln, r'"\u65e0\u6548\u6570\u636e"', 't("callback.invalid_data")')

# Lines with invalid_stage
for ln in [1128, 1176]:
    replace_line(ln, r'"\u65e0\u6548\u9636\u6bb5"', 't("callback.invalid_stage")')

# Line 1138: emoji fallback
replace_line(1138, r'"\u2699\ufe0f"', 't("msg.gear_emoji")')  # Keep as-is, just an emoji

# Lines 1141-1143: configure stage model
replace_line(1141, r'"\U0001f527 \u914d\u7f6e\u300c{} {}\u300d\u9636\u6bb5\u6a21\u578b\n"', 't("msg.configure_stage_model", emoji=emoji, name=stage_name) + "\\n"')

# Line 1143: select model text
replace_line(1143, r'"\u9009\u62e9\u8981\u4f7f\u7528\u7684\u6a21\u578b\uff1a".format(emoji, stage_name)', '""')

# Line 1146: select model callback
replace_line(1146, r'"\u9009\u62e9\u6a21\u578b"', 't("callback.select_model")')

# Line 1183: saved callback
replace_line(1183, r'"\u5df2\u8bbe\u7f6e: {} {}".format(tag, model_id)', 't("callback.saved", tag=tag, model=model_id)')

# Lines 1195-1198: stage config overview
replace_line(1195, r'"\u2699\ufe0f \u9636\u6bb5\u914d\u7f6e\u6982\u89c8\n"', 't("msg.stage_config_overview", pipeline=preset_display)')
# Need to remove remaining lines of the multiline string - handle carefully

# Lines 1726-1729: duplicate stage config overview
replace_line(1726, r'"\u2699\ufe0f \u9636\u6bb5\u914d\u7f6e\u6982\u89c8\n"', 't("msg.stage_config_overview", pipeline=preset_display)')

# Line 1236: default label
replace_line(1236, r'"\uff08\u9ed8\u8ba4\uff09"', 't("msg.default_label")')

# Lines 1240-1243: pipeline applied
replace_line(1240, r'"\u2705 \u6d41\u6c34\u7ebf\u914d\u7f6e\u5df2\u751f\u6548\uff01\n"', 't("msg.pipeline_applied", summary=summary) + ""')

# Line 1246: config applied
replace_line(1246, r'"\u914d\u7f6e\u5df2\u5e94\u7528"', 't("callback.config_applied")')

# Line 1255: workspace not found
replace_line(1255, r'"\u5de5\u4f5c\u533a\u57df\u4e0d\u5b58\u5728"', 't("callback.workspace_not_found")')

# Line 1263: selected
replace_line(1263, r'"\u5df2\u9009: {}".format(ws.get("label", ws_id))', 't("callback.selected", label=ws.get("label", ws_id))')

# Line 1272: workspace not exist
replace_line(1272, r'"\u5de5\u4f5c\u533a\u4e0d\u5b58\u5728"', 't("callback.workspace_not_found")')

# Line 1274: generating summary
replace_line(1274, r'"\u751f\u6210\u603b\u7ed3..."', 't("callback.generating_summary")')

# Line 1282: workspace removed
replace_line(1282, r'"\u5de5\u4f5c\u76ee\u5f55\u5df2\u79fb\u9664: {}".format(ws_id)', 't("msg.workspace_dir_removed", id=ws_id)')

# Line 1283: deleted
replace_line(1283, r'"\u5df2\u5220\u9664"', 't("callback.deleted")')

# Line 1285: workspace not found
replace_line(1285, r'"\u5de5\u4f5c\u76ee\u5f55\u672a\u627e\u5230: {}".format(ws_id)', 't("msg.workspace_dir_not_found", id=ws_id)')

# Line 1286: not found
replace_line(1286, r'"\u672a\u627e\u5230"', 't("callback.ws_not_found")')

# Line 1296: default workspace set
replace_line(1296, r'"\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55\u5df2\u8bbe\u7f6e: {} ({})".format(ws_id, ws.get("label", "") if ws else "")', 't("msg.default_workspace_set", id=ws_id, label=ws.get("label", "") if ws else "")')

# Line 1299: default set
replace_line(1299, r'"\u5df2\u8bbe\u7f6e\u9ed8\u8ba4"', 't("callback.default_set")')

# Line 1301: workspace not found
replace_line(1301, r'"\u5de5\u4f5c\u76ee\u5f55\u672a\u627e\u5230: {}".format(ws_id)', 't("msg.workspace_dir_not_found", id=ws_id)')

# Line 1302: not found
replace_line(1302, r'"\u672a\u627e\u5230"', 't("callback.ws_not_found")')

# Line 1347: invalid index
replace_line(1347, r'"\u65e0\u6548\u5e8f\u53f7"', 't("callback.invalid_index")')

# Line 1354: search root deleted
replace_line(1354, r'"\u2705 \u5df2\u5220\u9664\u641c\u7d22\u6839\u76ee\u5f55: {}".format(msg)', 't("msg.search_root_deleted", path=msg)')

# Line 1357: deleted
replace_line(1357, r'"\u5df2\u5220\u9664"', 't("callback.deleted")')

# Line 1359: delete failed
replace_line(1359, r'"\u5220\u9664\u5931\u8d25: {}".format(msg)', 't("msg.search_root_delete_failed", msg=msg)')

# Line 1360: delete failed callback
replace_line(1360, r'"\u5220\u9664\u5931\u8d25"', 't("callback.delete_failed")')

# Line 1374: confirm cancel task
replace_line(1374, r'"\u786e\u8ba4\u53d6\u6d88\u4efb\u52a1 [{}]\uff1f\u53d6\u6d88\u540e\u4efb\u52a1\u5c06\u4e0d\u4f1a\u88ab\u6267\u884c\u3002".format(ref)', 't("msg.confirm_cancel_task", ref=ref)')

# Line 1377: confirm cancel
replace_line(1377, r'"\u8bf7\u786e\u8ba4\u53d6\u6d88"', 't("callback.confirm_cancel")')

# Line 1383: confirm delete task
replace_line(1383, r'"\u786e\u8ba4\u5220\u9664\u4efb\u52a1 [{}]\uff1f\u5220\u9664\u540e\u5c06\u4ece\u6d3b\u8dc3\u5217\u8868\u79fb\u9664\u3002".format(ref)', 't("msg.confirm_delete_task", ref=ref)')

# Line 1386: confirm delete
replace_line(1386, r'"\u8bf7\u786e\u8ba4\u5220\u9664"', 't("callback.confirm_delete")')

# Line 1404: confirm delete archive
replace_line(1404, r'"\u786e\u8ba4\u5220\u9664\u5f52\u6863\u8bb0\u5f55 [{}]\uff1f".format(ref)', 't("msg.confirm_delete_archive", ref=ref)')

# Line 1407: confirm delete
replace_line(1407, r'"\u8bf7\u786e\u8ba4\u5220\u9664"', 't("callback.confirm_delete")')

# Line 1418: unknown button
replace_line(1418, r'"\u672a\u77e5\u6309\u94ae"', 't("callback.unknown_button")')

# Line 1453: cancelled operation
replace_line(1453, r'"\u5df2\u53d6\u6d88\u64cd\u4f5c\u3002"', 't("msg.cancelled_op")')

# Lines 1462-1463: not set
replace_line(1462, r'"(\u672a\u8bbe\u7f6e)"', 't("msg.not_set")')
replace_line(1463, r'"(\u672a\u8bbe\u7f6e)"', 't("msg.not_set")')

# Lines 1550-1552: new task with workspace select
replace_line(1550, r'"\U0001f4dd \u65b0\u5efa\u4efb\u52a1\n"', 't("prompt.new_task_ws_select") + ""')

# Line 1652: model list footer
replace_line(1652, r'"(\u672a\u8bbe\u7f6e)"', 't("msg.not_set")')

# Line 1661: refreshed/model list
replace_line(1661, r'"\u5df2\u5237\u65b0"', 't("msg.refreshed")')
replace_line(1661, r'"\u6a21\u578b\u6e05\u5355"', 't("msg.model_list_label")')

# Lines 1667, 1701, 1757, 1847, 1886: no permission
for ln in [1667, 1701, 1757, 1847, 1886]:
    replace_line(ln, r'"\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002"', 't("callback.no_permission")')

# Lines 1950, 1966: no workspaces
replace_line(1950, r'"\u5c1a\u672a\u6ce8\u518c\u4efb\u4f55\u5de5\u4f5c\u76ee\u5f55\u3002"', 't("msg.no_workspaces_short")')
replace_line(1966, r'"\u5c1a\u672a\u6ce8\u518c\u4efb\u4f55\u5de5\u4f5c\u76ee\u5f55\u3002"', 't("msg.no_workspaces_short")')

# Lines 1951, 1967: no workspaces callback
replace_line(1951, r'"\u65e0\u5de5\u4f5c\u76ee\u5f55"', 't("msg.no_workspaces_label")')
replace_line(1967, r'"\u65e0\u5de5\u4f5c\u76ee\u5f55"', 't("msg.no_workspaces_label")')

# Line 1958: select ws to remove
replace_line(1958, r'"\u9009\u62e9\u8981\u5220\u9664\u7684\u5de5\u4f5c\u76ee\u5f55"', 't("msg.select_ws_remove")')

# Line 1974: select default ws
replace_line(1974, r'"\u9009\u62e9\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55"', 't("msg.select_ws_default")')

# Line 1997: search roots
replace_line(1997, r'"\u641c\u7d22\u6839\u76ee\u5f55"', 't("msg.search_roots_label")')

# Line 2008: enter path
replace_line(2008, r'"\u8bf7\u8f93\u5165\u8def\u5f84"', 't("msg.enter_path")')

# Line 2020: no queued tasks
replace_line(2020, r'"\u65e0\u6392\u961f\u4efb\u52a1"', 't("msg.no_queued_tasks")')

# Line 2037: queue status
replace_line(2037, r'"\u961f\u5217\u72b6\u6001"', 't("msg.queue_status_label")')

# Lines 2068, 2102, 2129: invalid operation
for ln in [2068, 2102, 2129]:
    replace_line(ln, r'"\u65e0\u6548\u64cd\u4f5c"', 't("msg.invalid_operation")')

# Lines 2072, 2357: task not found
replace_line(2072, r'"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(ctx)', 't("msg.task_not_found", ref=ctx)')
replace_line(2357, r'"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(task_code)', 't("msg.task_not_found", ref=task_code)')

# Lines 2073, 2358: task not found callback
replace_line(2073, r'"\u4efb\u52a1\u4e0d\u5b58\u5728"', 't("msg.task_not_found_short")')
replace_line(2358, r'"\u4efb\u52a1\u4e0d\u5b58\u5728"', 't("msg.task_not_found_short")')

# Line 2080: can only cancel pending
replace_line(2080, r'"\u4ec5\u53ef\u53d6\u6d88\u5f85\u5904\u7406\u4efb\u52a1\uff0c\u5f53\u524d\u72b6\u6001: {}".format(current_status)', 't("msg.only_cancel_pending", status=current_status)')

# Line 2081: cannot cancel
replace_line(2081, r'"\u65e0\u6cd5\u53d6\u6d88"', 't("msg.cannot_cancel")')

# Lines 2090, 4190: user cancelled
replace_line(2090, r'"\u7528\u6237\u53d6\u6d88"', 't("msg.user_cancelled")')
replace_line(4190, r'"\u7528\u6237\u53d6\u6d88"', 't("msg.user_cancelled")')

# Lines 2093, 4193: task cancelled
replace_line(2093, r'"\u2705 \u4efb\u52a1 [{}] \u5df2\u53d6\u6d88\u3002".format(ctx)', 't("msg.task_cancelled", ref=ctx)')
replace_line(4193, r'"\u2705 \u4efb\u52a1 [{}] \u5df2\u53d6\u6d88\u3002".format(cancel_arg)', 't("msg.task_cancelled", ref=cancel_arg)')

# Lines 2096: callback cancelled
replace_line(2096, r'"\u5df2\u53d6\u6d88"', 't("callback.cancelled")')

# Lines 2120, 2135: deleted
replace_line(2120, r'"\u2705 \u4efb\u52a1 [{}] \u5df2\u5220\u9664\u3002".format(ctx)', 't("msg.task_deleted", ref=ctx)')
replace_line(2123, r'"\u5df2\u5220\u9664"', 't("callback.deleted")')
replace_line(2135, r'"\u2705 \u5f52\u6863\u8bb0\u5f55 [{}] \u5df2\u5220\u9664\u3002".format(ctx)', 't("msg.archive_deleted", ref=ctx)')
replace_line(2138, r'"\u5df2\u5220\u9664"', 't("callback.deleted")')

# Line 2140: archive not found
replace_line(2140, r'"\u5f52\u6863\u8bb0\u5f55\u672a\u627e\u5230: {}".format(ctx)', 't("msg.archive_not_found", ref=ctx)')
replace_line(2141, r'"\u672a\u627e\u5230"', 't("callback.ws_not_found")')

# Line 2234: unknown operation
replace_line(2234, r'"\u672a\u77e5\u64cd\u4f5c"', 't("callback.unknown_button")')

# Lines 2249, 2262, 2278, 2284, 2296, 2304: task status menu callbacks
replace_line(2249, r'"\u4efb\u52a1\u6982\u89c8"', 't("msg.task_overview_label")')
replace_line(2262, r'"\u65e0\u4efb\u52a1"', 't("msg.no_tasks_label")')
replace_line(2278, r'"\u65e0\u6548\u5206\u9875"', 't("msg.invalid_page")')
replace_line(2284, r'"\u65e0\u6548\u9875\u7801"', 't("msg.invalid_page_num")')
replace_line(2296, r'"\u65e0\u4efb\u52a1"', 't("msg.no_tasks_label")')
replace_line(2304, r'"\u7b2c {} \u9875".format(page + 1)', 't("msg.page_num", num=page + 1)')

# Lines 2333, 2365: no description fallback
replace_line(2333, r'"(\u65e0\u63cf\u8ff0)"', 't("msg.no_description")')
replace_line(2365, r'"(\u65e0\u63cf\u8ff0)"', 't("msg.no_description")')

# Lines 2352, 2398: no summary
replace_line(2352, r'"(\u65e0)"', 't("msg.none_short")')
replace_line(2398, r'"(\u65e0)"', 't("msg.none_short")')

# Lines 2355, 2416: task detail callback
replace_line(2355, r'"\u4efb\u52a1\u8be6\u60c5"', 't("msg.task_detail_label")')
replace_line(2416, r'"\u4efb\u52a1\u8be6\u60c5"', 't("msg.task_detail_label")')

# Lines 2427, 2434, 2440: no stage records
replace_line(2427, r'"\u65e0\u9636\u6bb5\u6267\u884c\u8bb0\u5f55\uff08\u65e5\u5fd7\u6587\u4ef6\u4e0d\u5b58\u5728\uff09"', 't("msg.no_stage_records")')
replace_line(2428, r'"\u65e0\u8bb0\u5f55"', 't("msg.no_records")')
replace_line(2434, r'"\u65e0\u6cd5\u8bfb\u53d6\u9636\u6bb5\u6267\u884c\u8bb0\u5f55"', 't("msg.cannot_read_stage")')
replace_line(2435, r'"\u8bfb\u53d6\u5931\u8d25"', 't("msg.read_failed")')
replace_line(2440, r'"\u65e0\u9636\u6bb5\u6267\u884c\u8bb0\u5f55"', 't("msg.no_stage_records_short")')
replace_line(2441, r'"\u65e0\u8bb0\u5f55"', 't("msg.no_records")')

# Line 2464: unknown backend
replace_line(2464, r'"\u672a\u77e5"', 't("msg.unknown")')

# Line 2475: truncated
replace_line(2475, r'"... (\u5df2\u622a\u65ad)"', 't("msg.truncated")')

# Line 2478: no output
replace_line(2478, r'"(\u65e0\u8f93\u51fa)"', 't("msg.no_output")')

# Line 2483: truncated
replace_line(2483, r'"... (\u5df2\u622a\u65ad)"', 't("msg.truncated")')

# Line 2485: stage detail
replace_line(2485, r'"\u9636\u6bb5\u8be6\u60c5"', 't("msg.stage_detail_label")')

# Lines 2506, 2523, 2571: no doc content
replace_line(2506, r'"(\u65e0\u6587\u6863\u5185\u5bb9)"', 't("msg.no_doc_content")')
replace_line(2523, r'"(\u65e0\u6587\u6863\u5185\u5bb9)"', 't("msg.no_doc_content")')
replace_line(2571, r'"(\u65e0\u6587\u6863\u5185\u5bb9)"', 't("msg.no_doc_content")')

# Lines 2512, 2529, 2580: view doc callback
replace_line(2512, r'"\u67e5\u770b\u6587\u6863"', 't("msg.view_doc_label")')
replace_line(2529, r'"\u67e5\u770b\u6587\u6863"', 't("msg.view_doc_label")')
replace_line(2580, r'"\u67e5\u770b\u6587\u6863"', 't("msg.view_doc_label")')

# Line 2531, 2357: task not found
replace_line(2531, r'"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(ref)', 't("msg.task_not_found", ref=ref)')
replace_line(2532, r'"\u4efb\u52a1\u4e0d\u5b58\u5728"', 't("msg.task_not_found_short")')

# Line 2565: rejection reason
replace_line(2565, r'"\u62d2\u7edd\u539f\u56e0: {}".format(str(acceptance["reason"])[:500])', 't("msg.rejection_reason", reason=str(acceptance["reason"])[:500])')

# Line 2568: error info
replace_line(2568, r'"\u9519\u8bef\u4fe1\u606f: {}".format(error[:500])', 't("msg.error_info", err=error[:500])')

# Line 2608: no summary info
replace_line(2608, r'"(\u65e0\u6982\u8981\u4fe1\u606f)"', 't("msg.no_summary_info")')

# Line 2615: view summary
replace_line(2615, r'"\u67e5\u770b\u6982\u8981"', 't("msg.view_summary_label")')

# Lines 2625-2633: log viewing
replace_line(2625, r'"\u65e0\u6267\u884c\u65e5\u5fd7\u6587\u4ef6"', 't("msg.no_log_file")')
replace_line(2626, r'"\u65e0\u65e5\u5fd7"', 't("msg.no_log")')
replace_line(2632, r'"\u65e0\u6cd5\u8bfb\u53d6\u65e5\u5fd7\u6587\u4ef6"', 't("msg.cannot_read_log")')
replace_line(2633, r'"\u8bfb\u53d6\u5931\u8d25"', 't("msg.read_failed")')

# Line 2649: view log
replace_line(2649, r'"\u67e5\u770b\u65e5\u5fd7"', 't("msg.view_log_label")')

# Line 2676: archive not found
replace_line(2676, r'"\u5f52\u6863\u8bb0\u5f55\u672a\u627e\u5230: {}".format(archive_ref)', 't("msg.archive_not_found", ref=archive_ref)')
replace_line(2677, r'"\u672a\u627e\u5230"', 't("callback.ws_not_found")')

# Line 2712-2713: no description/none
replace_line(2712, r'"(\u65e0\u63cf\u8ff0)"', 't("msg.no_description")')
replace_line(2713, r'"(\u65e0)"', 't("msg.none_short")')

# Line 2745: archive detail
replace_line(2745, r'"\u5f52\u6863\u8be6\u60c5"', 't("msg.archive_detail_label")')

# Line 1734: stage overview
replace_line(1734, r'"\u9636\u6bb5\u6982\u89c8"', 't("msg.stage_overview_label")')

# Lines 3890: archive file generated
replace_line(3890, r'"\u5f52\u6863\u6587\u4ef6\u5df2\u751f\u6210: {}".format(str(archive_path))', 't("msg.archive_file_generated", path=str(archive_path))')

# Line 4165: task not found
replace_line(4165, r'"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(cancel_arg)', 't("msg.task_not_found", ref=cancel_arg)')

# Line 4173: only cancel pending
replace_line(4173, r'"\u4ec5\u53ef\u53d6\u6d88\u5f85\u5904\u7406/\u6267\u884c\u4e2d/\u6392\u961f\u4e2d\u7684\u4efb\u52a1\uff0c\u5f53\u524d\u72b6\u6001: {}".format(current_status)', 't("msg.only_cancel_active", status=current_status)')

# Line 3157: add workspace first
replace_line(3157, r'"\u8bf7\u5148\u6dfb\u52a0\u5de5\u4f5c\u533a\uff08/menu \u2192 \u5de5\u4f5c\u533a\u7ba1\u7406 \u2192 \u6dfb\u52a0\uff09"', 't("msg.add_workspace_first")')

# Line 3181: workspace path not exist
replace_line(3181, r'"\u5de5\u4f5c\u533a\u8def\u5f84\u4e0d\u5b58\u5728: {}".format(workspace_path)', 't("msg.path_not_exist", path=workspace_path)')

# Line 3186: analyzing with AI
replace_line(3186, r'"\u23f3 \u6b63\u5728\u4f7f\u7528 AI \u5206\u6790\u9879\u76ee\uff0c\u8bf7\u7a0d\u5019..."', 't("msg.analyzing_project")')

# Line 3192: content truncated
replace_line(3192, r'"... (\u5185\u5bb9\u5df2\u622a\u65ad)"', 't("msg.content_truncated")')

# Write back
with open(FILEPATH, "w", encoding="utf-8") as f:
    f.writelines(lines)

print(f"Transform 3 complete. {count} replacements made.")
