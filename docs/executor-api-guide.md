# Executor API 接入指南

> Executor HTTP API (端口 40100) 运行在宿主机，与 task 处理循环并行。
> 用于 Claude Code session 直接监控、介入、调试 Executor 和 AI 执行链路。

## 快速开始

```bash
# 检查 Executor 是否运行
curl http://localhost:40100/health

# 查看整体状态
curl http://localhost:40100/status

# 直接和 Coordinator 对话（绕过 Telegram）
curl -X POST http://localhost:40100/coordinator/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "当前项目状态", "project_id": "amingClaw"}'
```

## 端点列表

### 监控 (GET)

| 端点 | 说明 | 返回 |
|------|------|------|
| `/health` | API 健康检查 | `{status, port, ai_manager, orchestrator}` |
| `/status` | 整体状态 | `{pending_tasks, processing_tasks, active_ai_sessions}` |
| `/sessions` | 活跃 AI 进程 | `{sessions: [{session_id, role, pid, elapsed_sec}]}` |
| `/tasks?project_id=X&status=Y` | 任务列表 | `{tasks: [...], count}` |
| `/task/{task_id}` | 单任务详情 | 任务 JSON + `_stage` + `_file` |
| `/trace/{trace_id}` | 链路追踪 | `{trace_id, entries: [...]}` |

### 介入 (POST)

| 端点 | 说明 | Body |
|------|------|------|
| `/task/{id}/pause` | 暂停运行中的任务 | 无 |
| `/task/{id}/cancel` | 取消任务 | 无 |
| `/task/{id}/retry` | 重试失败的任务 | 无 |
| `/cleanup-orphans` | 清理僵尸进程和卡住的任务 | 无 |

### 直接对话 (POST)

| 端点 | 说明 | Body |
|------|------|------|
| `/coordinator/chat` | 直接启动 Coordinator session | `{message, project_id, chat_id?}` |

### 调试 (GET)

| 端点 | 说明 | 返回 |
|------|------|------|
| `/validator/last-result` | 最近一次校验结果 | `{approved, rejected, layers[], needs_retry}` |
| `/context/{project_id}` | 当前上下文组装结果 | `{project_id, context: {...}}` |
| `/ai-session/{id}/output` | AI 原始输出 | `{stdout, stderr, exit_code, elapsed_sec}` |

## 使用场景

### 1. 检查为什么任务没执行

```bash
# 查看队列
curl http://localhost:40100/status
# → pending_tasks: 3, processing_tasks: 1

# 看具体任务
curl http://localhost:40100/tasks?status=queued

# 看正在处理的任务
curl http://localhost:40100/task/task-xxx
```

### 2. 任务卡住了

```bash
# 查看活跃 AI session
curl http://localhost:40100/sessions

# 取消卡住的任务
curl -X POST http://localhost:40100/task/task-xxx/cancel

# 清理所有僵尸
curl -X POST http://localhost:40100/cleanup-orphans
```

### 3. 调试 Coordinator 回复不对

```bash
# 看最近的 validator 决策
curl http://localhost:40100/validator/last-result

# 看注入的上下文
curl http://localhost:40100/context/amingClaw

# 看 AI 原始输出
curl http://localhost:40100/ai-session/ai-coordinator-xxx/output
```

### 4. 直接和 Coordinator 对话（不经过 Telegram）

```bash
curl -X POST http://localhost:40100/coordinator/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "帮我分析一下 L15 的状态",
    "project_id": "amingClaw"
  }'

# 返回:
# {
#   "reply": "L15 共 9 个节点，全部 qa_pass...",
#   "actions_executed": 0,
#   "actions_rejected": 0
# }
```

### 5. 查看完整链路追踪

```bash
# 列出最近的 trace
ls shared-volume/codex-tasks/traces/

# 查看某个 trace 详情
curl http://localhost:40100/trace/trace-1774230000-abcdef12
```

## 与其他服务的关系

```
Claude Code Session (开发者)
    │ curl localhost:40100/...
    ▼
Executor API (:40100)  ← 本文档描述的接口
    │
    ├── AILifecycleManager  → 管理 AI 进程
    ├── TaskOrchestrator    → 任务编排
    ├── DecisionValidator   → 校验决策
    ├── ContextAssembler    → 组装上下文
    └── EvidenceCollector   → 采集证据

Telegram 用户
    │ 消息
    ▼
Gateway (:40010) → task 文件 → Executor task loop
    │
    ▼
Governance (:40006)  → 规则引擎
dbservice (:40002)   → 记忆层
Redis (:40079)       → 缓存
```

## 注意事项

- Executor API 只在宿主机上可访问（localhost:40100）
- 不经过 nginx，不需要 governance token
- `/coordinator/chat` 是同步的，会等 AI 完成后返回（最多 120s）
- `/task/{id}/cancel` 会终止 AI 进程，谨慎使用
- `/cleanup-orphans` 会 kill 所有超时进程
