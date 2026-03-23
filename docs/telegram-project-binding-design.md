# Telegram 对话绑定项目运行时方案

## 问题

当前 Telegram 聊天只有一个 chat_id，但可能绑定到不同项目。切换项目时：
- 上下文要隔离（amingClaw 的对话不该出现在 toolboxClient 里）
- 记忆要隔离（查 dbservice 用不同 scope）
- Scheduled Task 要感知切换（只消费当前绑定项目的消息）
- 历史消息不丢（切换项目后旧项目的未处理消息怎么办）

## 架构

```
Telegram chat_id: 7848961760
    │
    ▼
Gateway 路由表 (Redis):
  chat:route:7848961760 → {
    project_id: "amingClaw",         ← 当前活跃项目
    token_hash: "9cb15f91",
    token: "gov-3506be...",
    bound_at: "2026-03-22T..."
  }
    │
    ├── 消息进入: chat:inbox:9cb15f91  (amingClaw 的 stream)
    │
    │   用户 /bind toolboxClient token → 路由表更新
    │
    └── 消息进入: chat:inbox:6643e5d7  (toolboxClient 的 stream)
```

## 运行时状态模型

```
每个项目独立的运行时状态:

Redis:
  chat:route:{chat_id}              → 当前活跃绑定
  chat:inbox:{token_hash}           → 该项目的消息 stream
  context:snapshot:{project_id}     → 该项目的 session context
  context:log:{project_id}          → 该项目的 session log
  lease:{lease_id}                  → 该项目的 agent 租约

SQLite (per project):
  governance.db                     → 节点状态、sessions、outbox
  gatekeeper_checks                 → coverage-check 结果

dbservice:
  scope={project_id}                → 该项目的记忆
```

## 项目切换流程

```
用户发送: /bind gov-48ed6f69... (toolboxClient token)
    │
    ▼
Gateway:
  1. 验证 token → 确认是 toolboxClient coordinator
  2. 保存旧绑定的 context:
     POST /api/context/amingClaw/save (自动)
  3. 更新路由表:
     chat:route:7848961760 → {project_id: "toolboxClient", ...}
  4. 加载新项目 context:
     GET /api/context/toolboxClient/load
  5. 回复用户:
     "已切换到 toolboxClient (89 节点, 89 qa_pass)"
    │
    ▼
Scheduled Task 感知:
  telegram-handler-amingclaw:
    → 查路由 → project_id = toolboxClient ≠ amingClaw
    → 静默退出

  telegram-handler-toolboxclient:
    → 查路由 → project_id = toolboxClient ✓
    → 消费消息 → 处理
```

## /menu 交互式切换

```
用户发送: /menu
    │
    ▼
Gateway 构建菜单:
  ┌─────────────────────────────────┐
  │ Aming Claw Gateway              │
  │                                 │
  │ 当前: amingClaw (9cb15f91...)    │
  │ 已注册 Coordinator: 2           │
  │                                 │
  │ [>> amingClaw (9cb1)]           │  ← 当前活跃
  │ [   toolboxClient (6643)]       │  ← 可切换
  │                                 │
  │ [项目状态] [项目列表]             │
  │ [服务健康] [解绑]                │
  └─────────────────────────────────┘
    │
    ▼
用户点击 "toolboxClient (6643)":
    │
    ▼
Gateway callback_query:
  1. 自动保存 amingClaw context
  2. 切换路由 → toolboxClient
  3. 加载 toolboxClient context
  4. 刷新菜单:
     "当前: toolboxClient (6643e5d7...)"
```

## 消息路由细节

### 同一时刻只有一个活跃项目

```
chat:route:{chat_id} 只存一条记录 → 消息只进一个 stream

优点: 简单，不混淆
缺点: 切换后旧项目消息不再消费
```

### 旧项目未处理消息

```
切换前:
  amingClaw stream 有 3 条未消费消息

切换后:
  这 3 条消息留在 stream 里
  telegram-handler-amingclaw 检测到路由不匹配 → 不消费
  消息不丢（stream 保留），但不处理

切回 amingClaw 时:
  telegram-handler-amingclaw 检测到路由匹配 → 恢复消费 → 处理积压消息
```

## Context 隔离

```
切换时自动保存/加载:

Gateway.handle_bind():
  1. old_route = get_route(chat_id)
  2. if old_route:
       # 保存旧项目 context
       POST /api/context/{old_project}/save
  3. bind_route(chat_id, new_token, new_project)
  4. # 加载新项目 context
     context = GET /api/context/{new_project}/load
  5. 回复: 包含新项目状态 + context 摘要
```

## Scheduled Task 项目感知

### 每个项目一个 Task

```
telegram-handler-amingclaw:     绑定 amingClaw
telegram-handler-toolboxclient: 绑定 toolboxClient

每次触发:
  1. 查路由表 → 当前 chat 绑的是哪个项目
  2. 不是我的项目 → 直接退出 (< 1秒)
  3. 是我的项目 → 消费消息 → 处理
```

### Task 处理时的上下文

```
Task 启动:
  1. GET /api/context/{my_project}/load → 获取上次工作状态
  2. POST /api/context/{my_project}/assemble → 获取项目记忆
  3. 处理消息时结合上下文理解用户意图
  4. 回复后保存更新的 context
```

## 跨项目查询

用户可以在当前项目的对话里查其他项目：

```
用户 (当前绑定 amingClaw): "toolboxClient 有多少节点？"
    │
    ▼
Task 识别跨项目查询:
  → GET /api/wf/toolboxClient/summary (无需 token，summary 是公开的)
  → 回复: "toolboxClient: 89 节点, 89 qa_pass"
  → 不切换项目绑定
```

## Gateway 改造点

### bind 时自动保存/加载 context

```python
# gateway.py handle_bind 改造
def handle_bind_with_context(chat_id, token, project_id):
    # 1. 保存旧 context
    old_route = get_route(chat_id)
    if old_route and old_route.get("project_id"):
        old_pid = old_route["project_id"]
        try:
            requests.post(f"{GOVERNANCE_URL}/api/context/{old_pid}/save",
                headers={"X-Gov-Token": old_route.get("token", "")},
                json={"context": {"saved_reason": "project_switch"}},
                timeout=3)
        except: pass

    # 2. 绑定新项目
    bind_route(chat_id, token, project_id)

    # 3. 加载新 context
    context = None
    try:
        resp = requests.get(f"{GOVERNANCE_URL}/api/context/{project_id}/load",
            headers={"X-Gov-Token": token}, timeout=3)
        context = resp.json().get("context")
    except: pass

    # 4. 获取项目状态
    summary = gov_api("GET", f"/api/wf/{project_id}/summary")

    return context, summary
```

### menu 切换时显示项目状态

```python
# 每个项目按钮显示:
#   项目名 (token_hash前4位) — N节点 M%通过
def build_project_button(route):
    pid = route.get("project_id", "?")
    summary = gov_api("GET", f"/api/wf/{pid}/summary")
    total = summary.get("total_nodes", 0)
    passed = summary.get("by_status", {}).get("qa_pass", 0)
    pct = int(passed / total * 100) if total else 0
    return f"{pid} — {total}节点 {pct}%通过"
```

## 实现优先级

| 步骤 | 内容 | 复杂度 |
|------|------|--------|
| 1 | Gateway bind 时自动保存/加载 context | 低 |
| 2 | /menu 显示项目状态 + 切换自动保存 | 低 |
| 3 | Task 启动时查路由 + 加载 context | 已有 |
| 4 | 跨项目查询 | 中 |
