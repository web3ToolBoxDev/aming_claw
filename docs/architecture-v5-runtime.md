# Aming Claw 架构方案 v5 — Session Runtime

> v4 → v5 核心变更：去掉 Scheduled Task 轮询，Gateway 直接驱动 CLI 执行。引入 Session Runtime 状态服务管理 Coordinator + 角色生命周期。

## 一、系统全景

```
┌─────────────────────────────────────────────────────────────────┐
│                        人类用户 (Telegram)                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │
                    └──┬────────┬────┬────┘
                       │        │    │
          ┌────────────▼──┐ ┌──▼────▼───────────┐
          │  Governance   │ │  Telegram Gateway  │
          │  (:40006)     │ │  (:40010)          │
          │  规则+事件源   │ │  消息+路由+派发     │
          └──────┬────────┘ └──────┬─────────────┘
                 │                 │
          ┌──────▼─────────────────▼───────┐
          │          Redis (:6379)          │
          │  Streams / Pub-Sub / 缓存 / 锁  │
          └──────┬─────────────────────────┘
                 │
          ┌──────▼──────────┐
          │   dbservice     │
          │   (:40002)      │
          │   记忆层         │
          └─────────────────┘

─ ─ ─ ─ ─ ─ Docker 内网 ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

          ┌─────────────────────────────────┐
          │         宿主机                   │
          │                                 │
          │  Executor (常驻)                 │
          │    ├── 监听 task 文件            │
          │    ├── run_claude / run_codex   │
          │    └── 结果写回 + 通知           │
          │                                 │
          │  Claude Code CLI               │
          │  Codex CLI                      │
          └─────────────────────────────────┘
```

## 二、v4 → v5 变更

| 模块 | v4 | v5 | 原因 |
|------|----|----|------|
| 消息消费 | Scheduled Task 每分钟轮询 Redis Stream | **Gateway 收到消息直接派发** | 去掉延迟和复杂度 |
| 任务执行 | 无（消息处理=回复） | **Gateway 写 task 文件 → Executor 调 CLI** | 任务类消息需要实际执行 |
| Session 管理 | 无 | **Session Runtime 状态服务** | 管理 Coordinator + 角色生命周期 |
| 角色系统 | token 分配但无实际管理 | **角色有独立上下文 + 任务 + 生命周期** | 多角色协作 |
| Scheduled Task | 3 个定时任务 | **全部关闭** | 不再需要 |

## 三、消息处理流程（v5.1 修正）

> **v5.1 关键修正**：Gateway 不再直接派发任务。所有非命令消息转给 Coordinator。
> Coordinator 负责对话、决策、任务编排。Gateway 只做消息收发。

```
用户 Telegram 消息: "帮我给 L1.3 写测试"
    │
    ▼
Gateway (Docker, 实时 polling)
    │  1. getUpdates 收到消息
    │  2. 查路由表: chat_id → project_id + token
    │  3. 判断消息类型:
    │
    ├── 命令 (/menu /status /bind /help)
    │     → Gateway 直接处理 → 回复
    │
    └── 非命令 (任何文本)
          │
          ▼
    Gateway 启动 Coordinator Session:
      run claude CLI + 注入 context (项目状态/记忆/活跃任务)
          │
          ▼
    Coordinator (Claude CLI session):
      1. 理解用户意图
      2. 查询 governance API (节点状态/记忆)
      3. 决策:
         ├── 直接回答 → 通过 Gateway 回复用户
         ├── 需要执行代码 → 创建 task {role:"dev"} → 通知用户
         └── 需要确认 → 追问用户
      4. 如创建了 task:
          │
          ▼
    Executor (宿主机, 常驻进程):
      监听 pending/ → claim → run_claude/run_codex
      → 结果写回 + Redis 通知
          │
          ▼
    Gateway 收到通知 → 启动新 Coordinator session 评估结果 → 回复用户

角色职责边界:
  Gateway:     消息收发 + 命令处理 (不做决策、不创建task)
  Coordinator: 对话 + 决策 + 任务编排 (不自己写代码)
  Dev/Executor: 代码执行 (不和用户对话)
```

## 四、Session Runtime 状态服务

### 4.1 为什么需要

Coordinator 不再是一个持续运行的 session，而是：
- 每条消息可以触发一个新的处理流程
- 多个角色（dev/tester/qa）可能同时在执行任务
- 需要知道"谁在做什么"来决定下一步

### 4.2 数据模型

```json
// Redis key: runtime:{project_id}
{
  "project_id": "amingClaw",
  "active_tasks": [
    {
      "task_id": "task-xxx",
      "role": "dev",
      "prompt": "为 L1.3 写测试",
      "status": "running",
      "started_at": "2026-03-22T...",
      "backend": "claude"
    }
  ],
  "completed_tasks_pending_notify": [
    {
      "task_id": "task-yyy",
      "status": "succeeded",
      "result_summary": "15 tests passed",
      "completed_at": "2026-03-22T..."
    }
  ],
  "context": {
    "current_focus": "L1.3 测试补全",
    "recent_messages": ["...last 20..."],
    "decisions": []
  },
  "updated_at": "2026-03-22T..."
}
```

### 4.3 API

```
GET  /api/runtime/{project_id}
  → 完整运行时状态（活跃任务、待通知结果、上下文）

POST /api/runtime/{project_id}/dispatch
  → 派发任务（写 task 文件 + 更新 runtime 状态）
  Body: {prompt, role, backend}

POST /api/runtime/{project_id}/complete
  → 标记任务完成（executor 回调）
  Body: {task_id, status, result}

POST /api/runtime/{project_id}/notify
  → 标记已通知用户（Gateway 回调）
  Body: {task_id}
```

### 4.4 与现有组件的关系

```
Session Runtime (新)
    │
    ├── 读写 Redis (runtime:{pid})     ← 实时状态
    │
    ├── 调用 Task Registry (已有)       ← 持久化任务记录
    │     create / claim / complete
    │
    ├── 调用 Session Context (已有)     ← 跨消息上下文
    │     save / load
    │
    ├── 调用 Agent Lifecycle (已有)     ← 角色租约管理
    │     register / heartbeat
    │
    └── 被 Gateway + Executor 调用      ← 消息入口 + 执行出口
```

## 五、Gateway 改造（v5.1 修正）

> **v5.1 关键修正**：Gateway 不再分类 query/task/chat。
> 所有非命令消息统一转给 Coordinator 处理。

### 5.1 消息路由（简化）

```python
def handle_message(chat_id, text, route):
    """Gateway 消息路由 — 只区分命令和非命令"""
    if text.startswith("/"):
        handle_command(chat_id, text)  # /menu /status /bind 等
        return

    # 非命令 → 全部转给 Coordinator
    forward_to_coordinator(chat_id, text, route)
```

### 5.2 Coordinator 触发

```python
def forward_to_coordinator(chat_id, text, route):
    """启动 Coordinator CLI session 处理用户消息"""
    project_id = route["project_id"]
    token = route["token"]

    # 1. 组装 context
    context = assemble_context(project_id, token)  # 项目状态+记忆+活跃任务

    # 2. 启动 Claude CLI session
    result = run_coordinator_session(
        message=text,
        context=context,
        project_id=project_id,
        token=token,
    )

    # 3. 回复用户
    send_text(chat_id, result["reply"])

    # 注意: Coordinator 内部可能创建了 task
    # task 完成后由 Executor 通知 → Gateway → 新 Coordinator session 评估
```

### 5.3 职责边界（硬约束）

```
Gateway 可以做:
  ✅ 处理 /command
  ✅ 转发消息给 Coordinator
  ✅ 发送 Coordinator 的回复
  ✅ 发送 task 完成通知

Gateway 不可以做:
  ❌ 分类消息为 query/task/chat
  ❌ 直接创建 task 文件
  ❌ 直接调 governance API 回答查询
  ❌ 做任何决策
```

### 5.3 结果通知

```python
# Gateway 订阅 Redis Pub/Sub: task.completed
def on_task_completed(payload):
    task_id = payload["task_id"]
    project_id = payload["project_id"]
    result = payload["result_summary"]

    route = get_route_by_project(project_id)
    if route:
        chat_id = route["chat_id"]
        send_text(chat_id, f"任务完成: {result}")

    # 更新 runtime
    update_runtime(project_id, complete_task=task_id)
```

## 六、Executor 改造

### 6.1 现有能力（直接复用）

```
agent/executor.py        → 监听 pending/ 目录
agent/backends.py         → run_claude / run_codex / run_pipeline
agent/task_state.py       → 任务状态追踪
agent/task_accept.py      → 结果处理

已解决的坑:
  - Windows stdin 传 prompt (不用命令行参数)
  - 剥离 CLAUDECODE 环境变量防嵌套拒绝
  - 剥离 ANTHROPIC_API_KEY 防 OAuth 失效
  - git diff 执行前快照
  - noop 检测 + 重试
  - 超时处理 + 重试
```

### 6.2 新增：执行完成通知

```python
# executor.py 完成任务后，发 Redis 通知
def on_task_done(task_id, result):
    # 已有: 写结果到 results/
    # 新增: 发 Redis 通知
    redis.publish("task:completed", {
        "task_id": task_id,
        "project_id": result.get("project_id"),
        "status": "succeeded" if result["exit_code"] == 0 else "failed",
        "result_summary": result.get("stdout", "")[:200],
    })
```

## 七、项目绑定与切换

```
用户 /menu:
┌──────────────────────────┐
│ 当前: amingClaw           │
│ 运行中: 1 个任务          │  ← 从 runtime 读取
│                          │
│ [>> amingClaw (1任务)]    │
│ [   toolboxClient (空闲)] │
│                          │
│ [项目状态] [切换]         │
└──────────────────────────┘

切换流程:
  1. 保存 amingClaw 上下文
  2. 更新路由 → toolboxClient
  3. 加载 toolboxClient 上下文 + runtime
  4. amingClaw 的运行中任务不中断
     → 完成后通知 amingClaw 的 runtime
     → 但用户在 toolboxClient，暂不推送
     → 切回 amingClaw 时看到 "有 1 个任务已完成"
```

## 八、角色上下文隔离

```
每个角色独立上下文:

context:snapshot:amingClaw              → Coordinator 上下文
context:snapshot:amingClaw:dev          → Dev 工作上下文
context:snapshot:amingClaw:tester       → Tester 上下文
context:snapshot:amingClaw:qa           → QA 上下文

Coordinator 上下文:
  {focus, active_tasks, recent_messages, decisions}

Dev 上下文:
  {current_task, files_modified, code_decisions, blocked_on}

Tester 上下文:
  {test_results, coverage_data, failed_tests}

QA 上下文:
  {verified_nodes, review_notes, blocked_nodes}
```

## 九、Docker Compose (v5)

```yaml
services:
  nginx:             # 反向代理 (:40000)
  governance:        # 规则+事件+runtime (:40006)
  telegram-gateway:  # 消息+路由+分类+派发 (:40010)
  dbservice:         # 记忆层 (:40002)
  redis:             # 缓存/通信 (:6379→40079)

# 不在 Docker 里:
#   executor         → 宿主机常驻，监听 task 文件
#   claude/codex CLI → 宿主机，executor 调用
```

**去掉的组件：**
- ~~Scheduled Task (telegram-handler-*)~~ → Gateway 直接处理
- ~~Message Worker~~ → 不需要
- ~~ChatProxy~~ → 不需要（Gateway 直接 polling）

## 十、端到端流程示例

### 示例 1：查询

```
用户: "amingClaw 多少个节点？"
  → Gateway 收到 → classify: query
  → GET /api/wf/amingClaw/summary
  → 回复: "68 节点, 68 qa_pass"
  → 耗时: <1秒
```

### 示例 2：短任务

```
用户: "跑一下测试"
  → Gateway 收到 → classify: task
  → 写 task 文件 → 回复 "执行中..."
  → Executor: run_claude("python -m unittest discover...")
  → 30秒后完成 → Redis notify
  → Gateway: "测试完成: 1038 ran, 1031 passed"
```

### 示例 3：长任务

```
用户: "帮我实现 L9.7 deploy 前置检查"
  → Gateway: task 文件 → "执行中..."
  → Executor: run_claude(prompt) → 可能跑 5-10 分钟
  → 期间用户发新消息: "当前任务进度？"
    → Gateway: 查 runtime → "task-xxx 运行中 (5分钟)"
  → 任务完成 → "实现完成: deploy-governance.sh 已更新"
```

### 示例 4：人工介入

```
用户: "帮我发布 amingClaw"
  → Gateway: classify: task + 危险关键词 "发布"
  → 回复: "[需要人工确认] 发布操作需要确认，请回复'确认发布'"
  → 用户: "确认发布"
  → Gateway: POST /api/wf/amingClaw/release-gate
  → 通过 → "发布门禁通过 ✅"
```

## 十一、实施路线

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | Gateway 消息分类器 | 无 |
| 2 | Gateway 任务派发 (写 task 文件) | 1 |
| 3 | Runtime 状态 API | governance |
| 4 | Executor 完成通知 (Redis pub) | executor.py |
| 5 | Gateway 结果通知监听 | 4 |
| 6 | /menu 显示运行时状态 | 3 |
| 7 | 项目切换 context 自动保存/加载 | session_context |
| 8 | 角色上下文隔离 | 7 |
