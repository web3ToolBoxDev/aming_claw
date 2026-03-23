# Session Runtime 状态服务设计

## 核心概念

```
用户 (Telegram)
    │ 只和 Coordinator 对话
    ▼
Coordinator Session (短生命周期)
    │ 每条消息/每个任务触发一个新 session
    │ session 持有项目级上下文
    │ session 管理所有角色的生命周期
    │
    ├── Dev Agent (长/短生命周期)
    │     独立上下文 + 独立任务
    ├── Tester Agent (短生命周期)
    │     独立上下文 + 独立任务
    └── QA Agent (短生命周期)
          独立上下文 + 独立任务
```

## Session 生命周期模型

### Coordinator Session

```
消息到达 (Telegram / Stream)
    │
    ▼
[OPEN] 新 Coordinator Session 启动
    │  1. 加载项目上下文 (focus, 进行中的任务, 角色状态)
    │  2. 检查: 有没有正在运行的旧 coord session?
    │     → 有 → 通知旧 session 关闭
    │     → 无 → 继续
    │  3. 接管所有角色管理权
    │
    ▼
[PROCESS] 处理消息
    │  理解用户意图:
    │  ├── 查询 → 直接回复 → 继续等新消息或关闭
    │  ├── 短任务 → 自己执行 → 回复结果
    │  └── 长任务 → 派发给角色 → 监控进度
    │
    ▼
[MANAGE] 管理角色
    │  检查各角色状态:
    │  ├── dev: running (task-xxx, 已运行 5 分钟)
    │  ├── tester: idle (无任务)
    │  └── qa: idle (无任务)
    │
    │  派发新任务:
    │  └── POST /api/task/create → assign to dev
    │
    ▼
[WAIT] 等待
    │  两种退出条件:
    │  ├── 新消息到达 → 关闭当前 session → 新 session 接管
    │  └── 所有任务完成 + 无新消息 (超时 5min) → 正常关闭
    │
    ▼
[CLOSE] 保存上下文 + 退出
    │  1. 保存项目上下文 (含角色状态、进行中任务)
    │  2. 不关闭角色 (角色独立运行)
    │  3. 释放 coord session 锁
```

### 角色 Session (Dev/Tester/QA)

```
Coordinator 派发任务
    │
    ▼
[SPAWN] 角色 Session 启动
    │  1. 加载角色上下文 (之前的工作记忆)
    │  2. Claim task: POST /api/task/claim
    │  3. 注册 lease: POST /api/agent/register
    │
    ▼
[EXECUTE] 执行任务
    │  运行 Claude Code CLI / 跑测试 / 代码审查
    │  定期 heartbeat 续租
    │  进度写入 task registry
    │
    ▼
[COMPLETE] 任务完成
    │  1. POST /api/task/complete {status, result}
    │  2. 保存角色上下文
    │  3. POST /api/agent/deregister
    │  4. 通知 Coordinator (Redis event)
    │
    ▼
Coordinator 收到通知 → 回复用户 → 决定下一步
```

## 状态服务 (Session Runtime)

### 数据模型

```json
// 存在 Redis + SQLite
// Key: runtime:{project_id}

{
  "project_id": "amingClaw",
  "coordinator": {
    "session_id": "coord-1774210000",
    "status": "active",           // active / closing / closed
    "started_at": "2026-03-22T...",
    "current_message": "帮我跑一下 L1.3 的测试",
    "lock": "coord-lock-9cb15f91"  // 同时只有一个 coord
  },
  "agents": {
    "dev": {
      "session_id": "dev-1774210050",
      "status": "running",        // idle / running / completed / failed
      "task_id": "task-xxx",
      "task_prompt": "为 L1.3 编写单元测试",
      "started_at": "2026-03-22T...",
      "lease_id": "lease-xxx",
      "progress": "正在分析代码结构...",
      "context": {
        "files_modified": ["agent/tests/test_xxx.py"],
        "decisions": ["用 unittest 而不是 pytest"]
      }
    },
    "tester": {
      "status": "idle",
      "last_task": "task-yyy",
      "context": {}
    },
    "qa": {
      "status": "idle",
      "last_task": null,
      "context": {}
    }
  },
  "pending_tasks": [
    {"task_id": "task-zzz", "prompt": "...", "assigned_to": null}
  ],
  "version": 42
}
```

### API

```
GET  /api/runtime/{project_id}           → 完整运行时状态
POST /api/runtime/{project_id}/acquire   → Coordinator 获取控制权
POST /api/runtime/{project_id}/release   → Coordinator 释放控制权
POST /api/runtime/{project_id}/spawn     → 派发角色任务
POST /api/runtime/{project_id}/update    → 更新角色状态
GET  /api/runtime/{project_id}/agents    → 各角色状态
```

## 消息驱动的 Session 切换

```
时间线:

T0: 用户发消息 "帮我改一下 auth 模块"
    → Coord Session A 启动
    → 理解任务 → 派发给 dev
    → Dev Agent 启动，开始改代码

T1: Dev 还在跑...Coord A 等待中

T2: 用户发新消息 "L3.2 状态怎么样"
    → 新消息进入 stream
    → Coord Session A 检测到新消息:
        1. 保存当前上下文 (含 dev 在跑的任务)
        2. 关闭自己
    → Coord Session B 启动:
        1. 加载上下文 → 知道 dev 正在跑 task-xxx
        2. 处理新消息 → 查 L3.2 状态 → 回复
        3. 检查 dev 状态 → 还在跑 → 不干预
        4. 无新消息 → 继续等待或超时退出

T3: Dev 完成任务
    → 发布 Redis event: task.completed
    → Coord Session C 启动 (或 B 还活着):
        1. 加载上下文
        2. 看到 dev task-xxx completed
        3. 回复用户: "auth 模块修改完成"
        4. 决定: 需要 tester 验证 → 派发 tester 任务
```

## Coordinator 控制权锁

```
同一项目同时只有一个 Coordinator Session:

acquire_coordinator(project_id):
    lock_key = f"coord:lock:{project_id}"

    # 检查旧 coord
    old = redis.get(lock_key)
    if old:
        # 通知旧 coord 关闭
        redis.publish(f"coord:signal:{project_id}", "close")
        # 等待旧 coord 释放 (最多 10 秒)
        for i in range(10):
            if not redis.get(lock_key):
                break
            sleep(1)

    # 获取锁
    redis.set(lock_key, session_id, ex=300)  # 5 分钟 TTL

release_coordinator(project_id):
    redis.delete(f"coord:lock:{project_id}")
```

## 角色上下文隔离

```
每个角色有独立的上下文存储:

context:snapshot:amingClaw:coordinator  → coord 的项目级上下文
context:snapshot:amingClaw:dev          → dev 的工作上下文
context:snapshot:amingClaw:tester       → tester 的测试上下文
context:snapshot:amingClaw:qa           → qa 的验收上下文

角色上下文内容:
  coordinator: {focus, pending_tasks, agent_status, recent_messages}
  dev: {current_files, code_changes, decisions, blocked_on}
  tester: {test_results, coverage, failed_tests}
  qa: {review_notes, verified_nodes, blocked_nodes}
```

## Scheduled Task 适配

```
当前:
  Task 启动 → 处理消息 → ACK → 退出

改为:
  Task 启动 → acquire_coordinator
    → 加载上下文
    → 处理消息
    → 检查角色状态 (有没有完成的任务?)
    → 派发新任务 (如需)
    → 等待 (XREADGROUP BLOCK 30s)
    → 新消息来了? → 处理
    → 超时? → 检查: 有运行中角色?
       → 有 → 继续等 (再 BLOCK 30s)
       → 无 → 保存上下文 → release → 退出

Session 超时规则:
  无任务运行 + 无新消息 → 5 分钟后退出
  有任务运行 + 无新消息 → 30 分钟后退出 (角色自己会完成)
  有新消息 → 立即处理
```

## 与现有系统的关系

```
Session Runtime (新)
    │
    ├── 调用 Task Registry (已有)
    │     create / claim / complete
    │
    ├── 调用 Agent Lifecycle (已有)
    │     register / heartbeat / deregister
    │
    ├── 调用 Session Context (已有)
    │     save / load / log
    │     扩展: 按角色隔离 context key
    │
    ├── 调用 Gateway (已有)
    │     reply / bind
    │
    └── 调用 Governance (已有)
         verify-update / summary / release-gate
```

## 实现分层

| 层 | 内容 | 优先级 |
|---|------|--------|
| 1 | Runtime 状态模型 + Redis 存储 | P0 |
| 2 | Coordinator 控制权锁 | P0 |
| 3 | 角色上下文隔离 (context key 加角色前缀) | P0 |
| 4 | 消息→任务分流 (查询直接回复 vs 长任务派发) | P1 |
| 5 | 角色 spawn/monitor (实际启动 dev/tester) | P1 |
| 6 | 任务完成通知 → Coordinator 回复 | P1 |
| 7 | Coord Session 切换 (新消息→关旧开新) | P2 |
