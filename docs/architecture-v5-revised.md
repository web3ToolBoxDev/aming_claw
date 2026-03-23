# Aming Claw 架构方案 v5 修订版

> 基于 v5 初版 + 10 条评审反馈 + Toolbox 项目实战教训修订。
> 核心原则：先把单角色单任务跑稳，再上多角色。

## 修订记录

### 评审反馈采纳

| # | 建议 | 采纳 | 修订 |
|---|------|------|------|
| 1 | Runtime 改为状态投影，Task Registry 为唯一事实源 | ✅ | Runtime 只做读模型 |
| 2 | task 文件补原子性 + 恢复机制 | ✅ | tmp+rename、claim lease、启动恢复扫盘 |
| 3 | Pub/Sub 不做唯一通知通道 | ✅ | Pub/Sub 加速，持久化兜底 |
| 4 | 任务状态枚举定全 | ✅ | 11 个状态 |
| 5 | 消息分类器改两段式 | ✅ | 规则拦截 + LLM 后续 |
| 6 | 通知归属任务不归属项目视图 | ✅ | 任务完成发回原 chat |
| 7 | 角色冲突治理 | ✅ 延后 | P2 做 |
| 8 | Executor 权限边界 | ✅ | workspace allowlist + tool policy |
| 9 | 长任务进度 heartbeat | ✅ | phase + percent |
| 10 | 实施顺序调整 | ✅ | 先可靠性闭环再多角色 |

### Toolbox 实战教训采纳

| 教训 | 来源 | 对 v5 的影响 |
|------|------|-------------|
| Coordinator 只做调度不做业务 | toolbox v1.4.4 | **角色职责硬约束**：coord 不碰代码/分析/验证 |
| Gatekeeper 记忆隔离 | toolbox wf-gatekeeper | **Gatekeeper 检查时只看验收图+task-log**，不看 coord context |
| 子进程 PID + orphan 管理 | toolbox 14 个残留 worktree | **Runtime 记录 worker_pid**，启动时恢复扫盘 kill 孤儿 |
| 非阻塞调度 | toolbox coord 卡死 | **Agent 必须后台启动**，coord 不阻塞等待 |
| Release gate 硬检查 | toolbox 6 节点未绿就发布 | **代码修复 ≠ verify:pass**，改了代码的节点必须重新验证 |
| coverage-check 属于 governance 不属于 runtime | 架构分析 | **静态分析不进 runtime**，phase 转换时自动触发 |

## 一、核心原则

```
1. 唯一事实源：Task Registry (SQLite)
2. Runtime 是投影，不是双写
3. 文件队列保留，但补原子性
4. Pub/Sub 加速，持久化兜底
5. 先单角色跑稳，再上多角色
6. Coordinator 只调度不做业务 (toolbox 教训)
7. Gatekeeper 记忆隔离，天然免疫偏移 (toolbox 教训)
8. 代码修复 ≠ verify:pass，必须重新验证 (toolbox 教训)
9. 消息驱动：session 无状态，token 绑项目不绑 session
```

## 二、任务状态机（定稿，修订 #2）

### 双字段模型：执行状态 + 通知状态

```
执行状态和通知状态是两个独立维度，不混在一条状态链里。

execution_status:
  queued ──→ claimed ──→ running ──→ succeeded
    │           │          │
    │           │          ├──→ failed
    │           │          │
    │           │          └──→ timed_out
    │           │
    └──→ cancelled

  running ──→ waiting_human ──→ running (确认后)
  running ──→ blocked ──→ running (解除后)

notification_status (独立字段):
  none ──→ pending ──→ sent ──→ read
```

### 表结构

```sql
ALTER TABLE tasks ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE tasks ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE tasks ADD COLUMN notified_at TEXT;

-- execution_status 管"任务做到哪了"
-- notification_status 管"用户知道没"
-- 两者独立变更，不互相阻塞
```

### 状态枚举

```python
EXECUTION_STATUSES = {
    "queued",           # 已创建，等待 claim
    "claimed",          # Executor 已认领，尚未开始
    "running",          # 正在执行
    "waiting_human",    # 等待人工确认（发布/rollback）
    "blocked",          # 缺少上下文/权限，暂停
    "succeeded",        # 执行成功
    "failed",           # 执行失败（可重试）
    "cancelled",        # 被取消
    "timed_out",        # 超时
    "enqueue_failed",   # DB 写成功但文件投递失败
}

NOTIFICATION_STATUSES = {
    "none",             # 不需要通知（查询类）
    "pending",          # 需要通知但还没发
    "sent",             # 已发送 Telegram
    "read",             # 用户已确认查看
}
```

## 三、Token 模型（简化版）

### 旧模型 vs 新模型

```
旧（v4 双令牌）:
  人类 init → refresh_token(90d)
  Session 启动 → POST /api/token/refresh → access_token(4h)
  所有 API 用 access_token
  每 4h 刷新 → session 结束 deregister

新（v5 消息驱动）:
  人类 init → project_token（不过期）
  Gateway 持有 project_token → 代理所有 API 调用
  CLI session 只需 project_id → Gateway 转发
  没有 refresh/rotate/expire 开销
```

### Token 分类

| Token | 持有者 | TTL | 用途 |
|-------|--------|-----|------|
| **project_token** | Gateway / 人类 | 不过期 | 项目 API 全权限（coordinator 级别） |
| **agent_token** | dev/tester/qa 进程 | 24h | 受限 API（只能 verify-update 等角色操作） |

### 安全保障

```
不过期不等于不安全：

1. 密码保护：init 时设密码，重置 token 需要密码
2. 可撤销：POST /api/token/revoke (人工操作)
3. 网络隔离：token 只在 localhost / Docker 内网使用
4. Gateway 代理：CLI session 不直接持有 token
   → Gateway 收到消息 → 用自己存的 token 调 API
   → CLI session 只需要 project_id
5. agent_token 仍有 TTL：独立进程的权限有时间限制
```

### 去掉的组件

```
删除：
  - /api/token/refresh  → 不需要，project_token 不过期
  - /api/token/rotate   → 简化为 /api/token/revoke + 重新 init
  - access_token (gat-*) → 不需要，直接用 project_token (gov-*)
  - token_service.py    → 可以保留但标记废弃

保留：
  - /api/token/revoke   → 安全撤销能力
  - /api/init           → 创建项目 + 获取 project_token
  - /api/role/assign    → coordinator 分配 agent_token (24h TTL)
```

### Gateway 作为 Token 代理

```
用户 Telegram 消息
    ↓
Gateway 查路由表 → 找到 project_token
    ↓
Gateway 用 project_token 调 governance API
    ↓
不需要 CLI session 自己管 token

CLI session 启动时:
    不需要: token refresh / agent register / lease
    只需要: 知道 project_id + Gateway URL
    Gateway 替它做所有认证
```

## 四、Task Registry 为唯一事实源

```
所有状态变更只写 Task Registry (SQLite):

  Gateway 创建任务   → INSERT tasks SET status='queued'
  Executor claim    → UPDATE tasks SET status='claimed', worker_id, lease_expires_at
  Executor 开始执行  → UPDATE tasks SET status='running'
  Executor 完成     → UPDATE tasks SET status='succeeded', result_json
  Executor 失败     → UPDATE tasks SET status='failed', error, attempt+1
  超时              → UPDATE tasks SET status='timed_out'
  人工确认等待       → UPDATE tasks SET status='waiting_human'
  Gateway 已通知     → UPDATE tasks SET status='notified', notified_at

Runtime API 只读取 Task Registry 做投影，不维护自己的状态。
```

### Runtime 投影 API

```python
@route("GET", "/api/runtime/{project_id}")
def handle_runtime(ctx):
    """投影视图，不存状态。每次从 Task Registry 实时查询。"""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        active = task_registry.list_tasks(conn, project_id, status="running")
        queued = task_registry.list_tasks(conn, project_id, status="queued")
        pending_notify = task_registry.list_tasks(conn, project_id, status="notify_pending")
        context = session_context.load_snapshot(project_id)

    return {
        "project_id": project_id,
        "active_tasks": active,
        "queued_tasks": queued,
        "pending_notifications": pending_notify,
        "context": context,
    }
```

## 四、文件投递原子化

### 4.1 写入顺序：DB 先于文件（修订 #1）

```
关键原则：任务的"存在性"由 DB 定义，不由文件定义。

顺序：
  1. DB INSERT tasks (status=queued)    ← 任务诞生
  2. 写 task 文件 (tmp → fsync → rename) ← 投递给 Executor
  3. 若写文件失败 → DB UPDATE status='enqueue_failed'

好处：
  - Executor 扫到文件时，DB 一定有记录
  - DB 有记录但没文件 → 恢复时重投递或标记失败
  - 不存在"文件有但 DB 没有"的不一致
```

```python
def create_task_file(project_id, prompt, backend="claude", chat_id=0):
    task_id = new_task_id()

    # 1. 先写 DB（任务诞生点）
    with DBContext(project_id) as conn:
        task_registry.create_task(conn, project_id, prompt,
            task_type=backend, created_by="gateway",
            metadata={"chat_id": chat_id})

    # 2. 再写文件（投递给 Executor）
    task_data = {
        "task_id": task_id,
        "project_id": project_id,
        "chat_id": chat_id,
        "prompt": prompt,
        "backend": backend,
        "attempt": 0,
        "max_attempts": 3,
        "created_at": utc_iso(),
    }

    try:
        # 原子写入：先写 tmp，fsync，再 rename
        tmp_path = pending_dir / f"{task_id}.json.tmp"
        final_path = pending_dir / f"{task_id}.json"

        with open(tmp_path, "w") as f:
        json.dump(task_data, f)
        f.flush()
        os.fsync(f.fileno())

    os.rename(tmp_path, final_path)  # 原子操作

    # 同时写 Task Registry
    with DBContext(project_id) as conn:
        task_registry.create_task(conn, project_id, prompt,
            task_type=backend, created_by="gateway",
            metadata={"chat_id": chat_id})

    return task_id
```

### 4.2 Claim with Fencing Token（修订 #3）

```python
def claim_task(task_file):
    task = load_json(task_file)
    task_id = task["task_id"]
    project_id = task["project_id"]

    # 生成 fencing token（防止双执行）
    fence_token = f"fence-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    lease_expires = utc_iso_after(seconds=300)  # 5 分钟 lease

    # 1. 原子 claim：CAS 更新（只有 queued 状态才能 claim）
    with DBContext(project_id) as conn:
        result = conn.execute(
            """UPDATE tasks SET execution_status='claimed',
               assigned_to=?,
               started_at=?,
               metadata_json=json_set(metadata_json,
                 '$.lease_expires_at', ?,
                 '$.lease_owner', ?,
                 '$.fence_token', ?,
                 '$.lease_version', COALESCE(
                   json_extract(metadata_json, '$.lease_version'), 0) + 1
               )
               WHERE task_id=? AND execution_status IN ('queued','created')""",
            (worker_id, utc_iso(), lease_expires, worker_id, fence_token, task_id)
        )
        if result.rowcount == 0:
            return None  # 已被其他 worker claim

    # 2. 移动文件
    os.rename(pending_path, processing_path)
    return task, fence_token

# 执行任务时，每次写 DB 都校验 fence_token
def update_with_fence(conn, task_id, fence_token, **updates):
    """带 fencing token 的更新，防止旧 worker 覆盖新 worker 的状态"""
    current = conn.execute(
        "SELECT json_extract(metadata_json, '$.fence_token') FROM tasks WHERE task_id=?",
        (task_id,)
    ).fetchone()
    if current and current[0] != fence_token:
        raise RuntimeError(f"Fence token mismatch: task reclaimed by another worker")
    # 安全更新...

# Lease 续期（heartbeat 时）
def renew_lease(conn, task_id, fence_token):
    new_expires = utc_iso_after(seconds=300)
    conn.execute(
        """UPDATE tasks SET metadata_json=json_set(metadata_json,
             '$.lease_expires_at', ?,
             '$.lease_version', json_extract(metadata_json, '$.lease_version') + 1
           ) WHERE task_id=? AND json_extract(metadata_json, '$.fence_token')=?""",
        (new_expires, task_id, fence_token)
    )
```

### 4.3 启动恢复

```python
def recover_on_startup():
    """Executor 启动时恢复卡住的任务"""

    # 1. 扫 processing/ 目录
    for f in processing_dir.glob("*.json"):
        task = load_json(f)
        task_id = task["task_id"]

        # 检查 Task Registry 状态
        with DBContext(task["project_id"]) as conn:
            db_task = task_registry.get_task(conn, task_id)

        if not db_task:
            # 孤儿文件，移回 pending
            os.rename(f, pending_dir / f.name)
            continue

        if db_task["status"] in ("claimed", "running"):
            # lease 过期了 → 重排队
            if db_task.get("lease_expires_at", "") < utc_iso():
                os.rename(f, pending_dir / f.name)
                with DBContext(task["project_id"]) as conn:
                    conn.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (task_id,))
                    conn.commit()

    # 2. 扫 Task Registry 中 claimed/running 但 lease 过期的
    for project in list_projects():
        with DBContext(project["project_id"]) as conn:
            stale = conn.execute(
                """SELECT task_id FROM tasks
                   WHERE status IN ('claimed','running')
                   AND json_extract(metadata_json, '$.lease_expires_at') < ?""",
                (utc_iso(),)
            ).fetchall()
            for row in stale:
                conn.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (row["task_id"],))
            conn.commit()
```

## 五、通知可靠性

```
Executor 完成任务:
    │
    ├── 1. UPDATE Task Registry: running → succeeded (持久化)
    ├── 2. UPDATE Task Registry: status = 'notify_pending' (持久化)
    ├── 3. Redis PUBLISH task:completed (加速，非必须)
    │
    ▼
Gateway 通知用户 (两条路径，互为备份):
    │
    ├── 路径 A: Pub/Sub 订阅 → 收到 → 回复 Telegram → UPDATE notified
    │
    └── 路径 B: 定期扫描 Task Registry 中 notify_pending 的任务
         → 找到 → 回复 Telegram → UPDATE notified
         (Gateway 每次 poll Telegram 时顺便查一次，无需额外定时)

判断已通知: notified_at IS NOT NULL，不是靠 Pub/Sub 是否收到
```

## 六、消息分类器（两段式）

### 第一层：规则快速拦截

```python
def classify_fast(text: str) -> str | None:
    """规则拦截，确定性高的直接返回"""
    if text.startswith("/"):
        return "command"

    # 危险操作（必须人工确认）
    danger = ["rollback", "delete", "revoke", "release", "deploy",
              "回滚", "删除", "发布", "撤销"]
    if any(kw in text.lower() for kw in danger):
        return "dangerous"

    # 明确查询模板
    query_patterns = [
        r"(状态|status)\s*(怎么样|是什么|查|看)",
        r"(多少|几个)\s*(节点|node|任务|task)",
        r"(列表|list|列出)",
    ]
    for p in query_patterns:
        if re.search(p, text, re.I):
            return "query"

    return None  # 不确定，交给第二层
```

### 第二层：LLM 意图解析（后续接入）

```python
def classify_llm(text: str, context: dict) -> dict:
    """LLM 解析意图，当前先用简单规则代替"""
    # 阶段 1: 关键词兜底
    task_kw = ["帮我", "写", "改", "修", "创建", "实现", "优化",
               "测试", "fix", "add", "create", "implement"]
    if any(kw in text for kw in task_kw):
        return {"intent": "execute", "risk": "low", "needs_workspace": True}

    # 阶段 2: 后续替换为 LLM 调用
    # return llm_classify(text, context)

    return {"intent": "chat", "risk": "none", "needs_workspace": False}
```

## 七、通知归属任务（不归属项目视图）

```
任务创建时记录 chat_id:
  task.chat_id = 7848961760

任务完成时:
  不管用户当前绑在哪个项目
  直接发回 task.chat_id

/menu 显示各项目未读:
  ┌──────────────────────────┐
  │ [>> amingClaw]     2 未读 │  ← 有完成但未查看的任务
  │ [   toolboxClient] 0 未读 │
  └──────────────────────────┘
```

## 八、Executor 权限边界

### workspace allowlist

```python
# 每个项目只能访问自己的 repo 路径
PROJECT_WORKSPACES = {
    "amingClaw": "C:/Users/z5866/Documents/amingclaw/aming_claw",
    "toolboxClient": "C:/Users/z5866/Documents/Toolbox/toolBoxClient",
}

def validate_workspace(project_id, task):
    allowed = PROJECT_WORKSPACES.get(project_id)
    if not allowed:
        raise RuntimeError(f"No workspace configured for {project_id}")
    # backends.py 已有 is_sensitive_path 检查
```

### tool policy（修订 #4：结构化命令策略）

```python
# 阶段 1：字符串规则（当前）
# 阶段 2：结构化命令能力模型（后续升级）

TOOL_POLICY = {
    "auto_allow": [
        {"program": "git", "args": ["diff", "status", "log", "show", "blame"],
         "write": False, "network": False},
        {"program": "python", "args": ["-m", "unittest"],
         "write": False, "network": False},
        {"program": "pytest", "write": False, "network": False},
        {"program": "npm", "args": ["test"], "write": False, "network": False},
    ],
    "needs_approval": [
        {"program": "git", "args": ["push", "reset", "rebase"],
         "write": True, "network": True,
         "reason": "Modifies remote or history"},
        {"program": "docker", "args": ["compose"],
         "write": True, "network": True,
         "reason": "Controls infrastructure"},
        {"program": "bash", "args": ["deploy-governance.sh"],
         "write": True, "network": True,
         "reason": "Production deployment"},
    ],
    "always_deny": [
        {"pattern": "rm -rf /", "reason": "Destructive"},
        {"pattern": "DROP TABLE", "reason": "Database destruction"},
        {"pattern": "format C:", "reason": "Disk format"},
    ],
}

# 校验逻辑
def check_command_policy(cmd: list[str], project_id: str) -> str:
    """Returns: 'allow' | 'approve' | 'deny'"""
    program = cmd[0] if cmd else ""
    for rule in TOOL_POLICY["always_deny"]:
        if rule["pattern"] in " ".join(cmd):
            return "deny"
    for rule in TOOL_POLICY["needs_approval"]:
        if program == rule["program"]:
            if any(a in cmd for a in rule.get("args", [])):
                return "approve"
    for rule in TOOL_POLICY["auto_allow"]:
        if program == rule["program"]:
            return "allow"
    return "approve"  # 默认需要审批
```

## 九、长任务进度 Heartbeat

```python
# Executor 执行期间定期上报进度
def report_progress(task_id, project_id, phase, percent, message):
    with DBContext(project_id) as conn:
        conn.execute(
            """UPDATE tasks SET metadata_json = json_set(
                 metadata_json,
                 '$.progress_phase', ?,
                 '$.progress_percent', ?,
                 '$.progress_message', ?,
                 '$.progress_at', ?
               ) WHERE task_id = ?""",
            (phase, percent, message, utc_iso(), task_id)
        )
        conn.commit()

# phase 枚举
PHASES = [
    "planning",        # 分析任务
    "coding",          # 编写代码
    "testing",         # 运行测试
    "reviewing",       # 自检
    "waiting_human",   # 等人工确认
    "finalizing",      # 收尾
]

# 用户查询进度时
# GET /api/runtime/{pid} → active_tasks[0].progress
# → "coding (60%) — 已修改 3 个文件，正在跑单测"
```

## 十、角色职责硬约束（Toolbox 教训）

### 10.1 角色定义

| 角色 | 只做 | 不做 |
|------|------|------|
| **Coordinator** | 接收指令→派发→监控→汇报 | ❌ 不读代码、不写代码、不分析需求、不跑测试 |
| **PM** (未来) | 需求分析+方案设计+验收标准 | ❌ 不写代码 |
| **Dev** | 代码实现+单元测试 | ❌ 不做需求分析、不做 QA |
| **Tester** | 运行测试+生成测试报告 | ❌ 不改代码 |
| **QA** | 真实环境 E2E 验收 | ❌ 不改代码 |
| **Gatekeeper** | 审计+对齐+纠正+裁决 | ❌ 不改文件、不派 agent、不跑测试 |

### 10.2 Coordinator 允许的代码修改上限

```
Coordinator 可以做的"小修"（最多 2 次/任务）:
  - 修改配置文件（docker-compose, nginx.conf, .env）
  - 修改文档（docs/, README）
  - 修改 acceptance graph

超过 2 次代码修改 → 自动触发 Gatekeeper 角色坍塌检查
```

### 10.3 代码修复 ≠ verify:pass

```
节点被标记 qa_pass 后，如果代码被修改:
  → 节点自动降级为 testing（不是 pending）
  → 必须重新走 tester → qa 验证
  → 不可跳过

实现方式:
  verify-update 时检查: 该节点的 primary/secondary 文件
  自上次 qa_pass 以来是否有 git 变更
  如果有 → 阻断: "Node L1.3 files changed since qa_pass, re-verify required"
```

## 十一、Gatekeeper 设计（Toolbox 记忆隔离模型）

### 11.1 Gatekeeper 触发点（修订 #5：错误前移，不堆到 release）

```
原则：把检查尽量前移到出错的那一步，release-gate 只做最终不可绕过检查。
```

| 触发点 | 时机 | 检查内容 | 阻断级别 |
|--------|------|---------|---------|
| G-coverage | **verify-update (t2_pass/qa_pass)** | 节点 primary 文件是否都有图覆盖 | 拒绝推进 |
| G-artifacts | **verify-update (qa_pass)** | 文档/测试文件是否完整（含自动推断） | 拒绝推进 |
| G-role | **coord 改代码 ≥2 次时** | 角色坍塌检查 | 告警 |
| G-file-change | **verify-update 时** | 节点 qa_pass 后 primary 文件被改了？ | 自动降级到 testing |
| G-release | **release-gate 时** | 最终检查：最近 1h 内有 coverage-check pass + 全绿 | 阻断发布 |

```
错误前移链路：

  改代码 → verify-update
            ├── G-coverage: 文件有节点覆盖？ (前移)
            ├── G-file-change: qa_pass 后文件改了？ (前移)
            └── G-artifacts: 文档/测试完整？ (前移)
                      ↓
            全部通过 → 允许推进
                      ↓
  release-gate
            └── G-release: 只检查"是否有人跑过 coverage-check 且通过"
                          不重复检查已经前移的内容
```

### 11.2 Gatekeeper 记忆隔离

```
Gatekeeper 检查时只接收:
  ✅ acceptance-graph 当前状态（节点+状态）
  ✅ task-log（角色实例+状态）
  ✅ 用户原始指令（一句话）
  ✅ 当前 phase 转换方向

Gatekeeper 不接收:
  ❌ Coordinator 的 context（recent_messages, decisions）
  ❌ 调试上下文、错误日志、代码 diff
  ❌ 多轮迭代的历史
  ❌ 角色间的对话内容

原因: 长时间执行后 Coordinator 会积累沉没成本，
     导致"够好了"的妥协心理。
     Gatekeeper 不知道调了多少轮 bug，
     只看节点是否全绿，天然免疫偏移。
```

### 11.3 Governance vs Runtime 职责分界

```
Governance (静态规则):
  ├── 节点状态机（谁能做什么转换）
  ├── Gate 策略（节点间依赖）
  ├── Coverage-check（文件→节点映射）
  ├── Artifacts 检查（文档/测试完整性）
  ├── Gatekeeper checks（发布前全局校验）
  └── Release profile（发布范围）

Runtime (动态状态):
  ├── 谁在运行（worker_pid, lease）
  ├── 跑什么任务（task_id, prompt, phase, percent）
  ├── 进度多少（heartbeat + progress）
  ├── 哪些结果待通知（notify_pending）
  └── 孤儿进程检测（pid 存活检查）

coverage-check 属于 Governance，不进 Runtime。
phase 转换时 Governance 自动触发 coverage-check。
```

## 十二、进程生命周期管理（Toolbox 教训）

### 12.1 PID 追踪

```python
# Executor 启动 CLI 进程时记录 PID
def run_with_pid_tracking(task_id, project_id, cmd):
    proc = subprocess.Popen(cmd, ...)

    # 记录到 Task Registry
    with DBContext(project_id) as conn:
        conn.execute(
            """UPDATE tasks SET metadata_json = json_set(
                 metadata_json, '$.worker_pid', ?, '$.worker_started', ?
               ) WHERE task_id = ?""",
            (proc.pid, utc_iso(), task_id)
        )
        conn.commit()

    return proc
```

### 12.2 启动时 orphan 扫盘

```python
def cleanup_orphan_processes():
    """Executor 启动时清理孤儿进程"""
    for project in list_projects():
        with DBContext(project["project_id"]) as conn:
            stale = conn.execute(
                """SELECT task_id, json_extract(metadata_json, '$.worker_pid') as pid
                   FROM tasks WHERE status IN ('claimed','running')"""
            ).fetchall()

            for row in stale:
                pid = row["pid"]
                if pid and not is_process_alive(pid):
                    # 进程已死但状态还是 running → 重排队
                    conn.execute(
                        "UPDATE tasks SET status='queued' WHERE task_id=?",
                        (row["task_id"],)
                    )
            conn.commit()
```

### 12.3 任务结束时进程清理

```python
def cleanup_after_task(task_id, project_id):
    """任务完成后清理所有相关进程"""
    with DBContext(project_id) as conn:
        task = task_registry.get_task(conn, task_id)
        pid = task.get("metadata", {}).get("worker_pid")
        if pid and is_process_alive(pid):
            kill_process_tree(pid)
```

## 十三、实施路线（最终版）

### P0：单角色单任务跑稳

| 步骤 | 内容 | 交付物 |
|------|------|--------|
| 1 | Token 模型简化 (去掉 refresh/access，project_token 不过期) | token_service.py 废弃 |
| 2 | Task Registry 状态机 (双字段: execution + notification) | task_registry.py |
| 3 | 文件投递原子化 (DB先→文件后+fencing token) | Gateway + Executor |
| 4 | Executor 完成写持久状态 (notification_status=pending) | executor.py |
| 5 | 通知持久化 + 可补发 (Gateway poll 查 notification_status=pending) | gateway.py |
| 6 | 取消 / 重试 / 超时 | task_registry + executor |
| 7 | 进度 heartbeat (phase+percent) | executor |
| 8 | PID 追踪 + orphan 扫盘 (toolbox 教训) | executor |

### P1：交互体验

| 步骤 | 内容 |
|------|------|
| 9 | 消息分类器 (两段式: 规则+LLM) |
| 10 | Runtime 投影 API (只读 Task Registry) |
| 11 | /menu 运行时状态 + 未读通知 |
| 12 | 项目切换 context 自动保存/加载 |
| 13 | 通知归属 chat_id (跨项目通知) |
| 14 | Gateway 作为 token 代理 (CLI session 不需要自己管 token) |

### P2：多角色协作

| 步骤 | 内容 |
|------|------|
| 15 | 角色职责硬约束 (coord 改代码上限) |
| 16 | 角色上下文隔离 (per-role context key) |
| 17 | Gatekeeper 记忆隔离 (只看验收图+task-log) |
| 18 | 代码修复→节点自动降级 (qa_pass 后文件变更) |
| 19 | 角色冲突治理 (workspace 锁 + 资源范围) |
| 20 | Executor workspace allowlist + 结构化 tool policy |
| 21 | 角色交接协议 (dev→tester→qa 自动流转) |

### P3：智能化

| 步骤 | 内容 |
|------|------|
| 22 | LLM 意图分类器 (替换关键词) |
| 23 | 自动任务分解 (大任务→子任务 DAG) |
| 24 | Context Assembly 驱动的智能回复 |
| 25 | PM 角色 (需求分析+方案设计) |
