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
| `/workspaces` | 工作区注册表 | `{workspaces: [...], count}` |
| `/workspaces/resolve?project_id=X` | 按项目ID解析工作区 | `{workspace, matched_by}` |

### 介入 (POST)

| 端点 | 说明 | Body |
|------|------|------|
| `/task/{id}/pause` | 暂停运行中的任务 | 无 |
| `/task/{id}/cancel` | 取消任务 | 无 |
| `/task/{id}/retry` | 重试失败的任务 | 无 |
| `/cleanup-orphans` | 清理僵尸进程和卡住的任务 | 无 |
| `/tasks/create` | Idempotent task file creation (used by Orchestrator) | JSON task object |

### POST /tasks/create — Idempotency Guarantees

Before creating a new task file, the endpoint checks all three active stages in order:

1. `pending/` — task file already waiting to be picked up
2. `processing/` — task is currently being executed
3. `results/` — task has already completed (pending acceptance)

If a match is found in **any** of these stages, the endpoint returns the existing task with `"status": "exists"` instead of creating a duplicate. Only if no match is found does it write a new task file to `pending/`.

**Request schema:**

```json
{
  "task_id": "task-abc123",          // required, must be unique per logical task
  "project_id": "aming-claw",        // required
  "role": "dev",                     // required: dev | tester | qa | merge
  "description": "Implement X",      // required
  "target_files": ["agent/foo.py"],  // optional
  "context": {}                      // optional, extra context passed to AI
}
```

**Response schema:**

```json
// New task created:
{"status": "created", "task_id": "task-abc123", "stage": "pending"}

// Task already exists:
{"status": "exists",  "task_id": "task-abc123", "stage": "processing"}
```

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

## 工作区路由

任务通过 `project_id` 自动路由到正确的工作区。路由优先级：

1. `target_workspace_id` — 精确 ID 匹配
2. `target_workspace` — 标签匹配
3. **`project_id`** — 归一化项目 ID 匹配（推荐）
4. `@workspace:<label>` 前缀
5. 默认工作区（fallback）

### project_id 归一化规则

所有变体自动统一为 kebab-case：

| 输入 | 归一化结果 |
|------|-----------|
| `amingClaw` | `aming-claw` |
| `aming_claw` | `aming-claw` |
| `toolBoxClient` | `tool-box-client` |

### 查询工作区

```bash
# 列出所有注册的工作区
curl http://localhost:40100/workspaces

# 查询某项目对应的工作区
curl "http://localhost:40100/workspaces/resolve?project_id=amingClaw"
# → {"workspace": {"id":"ws-xxx", "path":"C:/...", "project_id":"aming-claw"}, "matched_by":"project_id"}
```

### 注册工作区

工作区在 Executor 启动时自动注册当前目录。也可通过 `workspace_registry.add_workspace()` 手动注册：

```python
from workspace_registry import add_workspace
add_workspace(Path("/path/to/repo"), label="my-project", project_id="my-project")
```

### Redis Stream 审计

每次 AI session 的 prompt（输入）和 result（输出）会写入 Redis Stream `ai:prompt:{session_id}`，用于审计和调试：

```bash
# 查看某 session 的完整 prompt+result
redis-cli -p 40079 XRANGE ai:prompt:ai-dev-xxx - +
```

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

## QA Status Types

Tasks processed by the QA role can finish with one of three status values:

| Status | Meaning |
|---|---|
| `completed` | QA passed cleanly; all checks green. |
| `failed` | QA found blocking issues; task is marked failed and may trigger a retry budget check. |
| `passed_with_fallback` | QA passed, but one or more non-critical checks were skipped or substituted with a fallback strategy. This typically happens when an optional validation tool is unavailable (e.g., coverage reporter not installed) or a secondary lint rule is suppressed. The task proceeds to Merge, but the audit log records the fallback reason. |

Orchestrator downstream logic should treat `passed_with_fallback` the same as `completed` for routing purposes, but flag it for human review if the audit log shows repeated fallbacks on the same check.

## Auto-Chain Pipeline

After a Dev task completes successfully, the Executor automatically chains into the full validation pipeline without manual intervention. Each stage is logged to the audit trail.

### Pipeline Stages

```
Dev  ──→  Checkpoint Gate  ──→  Tester  ──→  QA  ──→  Merge
 │              │                  │          │          │
 └─ audit       └─ audit           └─ audit   └─ audit   └─ audit
```

| Stage | Role | Purpose |
|---|---|---|
| **Dev** | `dev` | Implements the task (code changes, file edits). |
| **Checkpoint Gate** | internal | Validates that Dev output meets minimum quality bar before proceeding (e.g., syntax check, required files present). Aborts chain if gate fails. |
| **Tester** | `tester` | Runs automated tests; writes results to the task result file. |
| **QA** | `qa` | Reviews test results and code diff; emits `completed`, `failed`, or `passed_with_fallback`. |
| **Merge** | `merge` | Merges the dev worktree branch into `main` and cleans up the worktree. |

Each stage transition is recorded as an entry in `pipeline_audit.jsonl` with a timestamp, stage name, outcome, and any notes.

### Pipeline State Files

The pipeline stores its state in the task's working directory under `shared-volume/codex-tasks/state/`:

| File | Purpose |
|---|---|
| `pipeline_idempotency.json` | Records which pipeline stages have already been submitted for a given `task_id`. Prevents the Orchestrator from re-submitting a stage that is already pending/processing/completed. |
| `pipeline_retry_budget.json` | Tracks how many retry attempts remain for each stage. Each stage starts with a configured budget (default 2). When a stage fails and is retried, the budget decrements. At 0, the pipeline halts and marks the task `failed`. |
| `pipeline_audit.jsonl` | Append-only log of every stage transition. Each line is a JSON object: `{ts, task_id, stage, outcome, notes}`. Used for post-mortem analysis and human review of `passed_with_fallback` cases. |

```bash
# Example: inspect pipeline audit for a task
cat shared-volume/codex-tasks/state/pipeline_audit.jsonl | grep "task-abc123"

# Example: check remaining retry budget
cat shared-volume/codex-tasks/state/pipeline_retry_budget.json
```

## Spin Loop Prevention: `_skipped_tasks`

The Executor's main processing loop maintains an in-memory set called `_skipped_tasks`. When a task is evaluated but cannot be processed in the current loop iteration (e.g., its workspace is busy, its dependencies are unmet, or it has been retried too many times within a short window), its `task_id` is added to `_skipped_tasks`.

On each subsequent loop pass, tasks present in `_skipped_tasks` are **not re-evaluated** until the set is cleared (which happens at the start of each full scan cycle). This prevents a single problematic task from consuming 100% of loop iterations and starving other tasks.

```
loop iteration N:
  for task in pending_tasks:
    if task.id in _skipped_tasks → skip
    else → try to process
      if cannot process now → _skipped_tasks.add(task.id)

end of full scan cycle → _skipped_tasks.clear()
```

## 注意事项

- Executor API 只在宿主机上可访问（localhost:40100）
- 不经过 nginx，不需要 governance token
- `/coordinator/chat` 是同步的，会等 AI 完成后返回（最多 120s）
- `/task/{id}/cancel` 会终止 AI 进程，谨慎使用
- `/cleanup-orphans` 会 kill 所有超时进程（仅限 `_EXECUTOR_SPAWNED_PIDS` 中追踪的进程，不会误杀用户 Claude session）
