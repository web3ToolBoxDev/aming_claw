# Aming Claw 架构方案 v4

> v3 → v4 核心变更：补地基。消息可靠投递、事件不丢失、上下文不覆盖、token 可撤销、Agent 生命周期前置。

## 一、系统全景

```
┌─────────────────────────────────────────────────────────────────┐
│                        人类用户 (Telegram)                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │  反向代理 / 限流(预留)
                    └──┬────────┬────┬────┘
                       │        │    │
          ┌────────────▼──┐ ┌──▼────▼───────────┐
          │  Governance   │ │  Telegram Gateway  │
          │  (:40006)     │ │  (:40010)          │
          │  规则+事件源   │ │  消息+路由          │
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
          │  Coordinator Session (Claude)   │
          │    ├── ChatProxy (Stream 消费)   │
          │    ├── GovernanceClient (HTTP)   │
          │    └── Claude Code CLI          │
          │                                 │
          │  Message Worker (常驻/定时)      │
          │    └── 消息消费 + 上下文恢复      │
          └─────────────────────────────────┘
```

## 二、v3 → v4 变更清单

| 模块 | v3 | v4 | 原因 |
|------|----|----|------|
| 消息队列 | Redis LIST (LPUSH/RPOP) | **Redis Streams + Consumer Group + ACK** | RPOP 崩溃丢消息 |
| 治理事件 | Pub/Sub only | **Outbox + Pub/Sub 双轨** | Pub/Sub 丢事件 |
| Session Context | 单 refId replace | **Snapshot + Append Log + Version** | 多 writer 覆盖 |
| Token 模型 | 10 年 coordinator token | **Refresh + Access 双令牌** | 泄露不可撤销 |
| Agent Lifecycle | 第五轮扩展 | **第二轮基础设施** | 已是当前痛点 |
| dbservice 依赖 | 同步调用 | **异步补写 + 降级策略** | 记忆增强≠记忆依赖 |
| Scheduled Task | 每分钟 RPOP | **阻塞消费 + 租约 + Cron 兜底** | 分钟延迟太高 |
| 可观测性 | 无 | **trace_id 串联 + 结构化日志** | 排查无从下手 |

## 三、五层架构（v4 修订）

### 第 1 层：规则层 (Governance Service)

**职责**：强制执行 workflow 规则 + 事件源。

| 模块 | 功能 | v4 变更 |
|------|------|---------|
| DAG 图 (NetworkX) | 节点、依赖、gate 策略 | 不变 |
| 状态机 (SQLite) | verify status 流转、权限校验 | 不变 |
| 角色服务 | token 认证 | **双令牌模型** |
| Agent Lifecycle | register/heartbeat/deregister/orphans | **新增 (从第五轮提前)** |
| Event Outbox | 事件持久化 + 异步投递 | **新增** |
| 审计 | 谁在什么时候做了什么 | 不变 |
| 文档 API | /api/docs/* | 不变 |

**关键变更 1：双令牌模型**

```
人类调 /api/init
    → 返回 refresh_token (长期, 90 天, 只用于换 access_token)
    → 人类保存 refresh_token

Coordinator 启动时:
    POST /api/token/refresh {refresh_token}
    → 返回 access_token (短期, 4 小时)
    → 后续所有 API 调用用 access_token

access_token 过期:
    → 自动用 refresh_token 续期
    → 无需人工介入

安全操作:
    POST /api/token/revoke {refresh_token, password}  ← 人类撤销
    POST /api/token/rotate {refresh_token, password}  ← 换新 refresh_token
```

| Token | TTL | 持有者 | 能力 |
|-------|-----|--------|------|
| refresh_token | 90 天 | 人类 | 换 access_token、revoke |
| access_token | 4 小时 | Coordinator | 调所有 API |
| agent_token | 24 小时 | Agent (tester/qa/dev) | 调受限 API |

**关键变更 2：Event Outbox**

```
状态变更发生
    │
    ▼
1. 写 SQLite 状态表 (事务内)
2. 写 SQLite outbox 表 (同一事务)  ← 保证原子性
    │
    ▼
3. 后台 worker 读 outbox
    │
    ├──▶ Redis Pub/Sub (实时通知，best-effort)
    ├──▶ Redis Stream (持久化，可重试)
    └──▶ dbservice (记忆写入，异步)
    │
    ▼
4. 投递成功 → 标记 outbox 行 delivered
   投递失败 → 重试 (指数退避, 最多 5 次)
   5 次失败 → 进入死信, 告警
```

Outbox 表结构：
```sql
CREATE TABLE event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,          -- node.status_changed
    payload TEXT NOT NULL,             -- JSON
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,                 -- NULL = 待投递
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT,
    dead_letter INTEGER DEFAULT 0,    -- 1 = 死信
    trace_id TEXT                      -- 串联追踪
);
CREATE INDEX idx_outbox_pending ON event_outbox(delivered_at) WHERE delivered_at IS NULL;
```

**关键变更 3：Agent Lifecycle API**

```
POST /api/agent/register
  Body: {role, principal_id, expected_duration_sec}
  Returns: {agent_id, agent_token, lease_id}

POST /api/agent/heartbeat
  Body: {lease_id, status: "idle"|"busy"|"processing"}
  Returns: {ok, lease_renewed_until}

POST /api/agent/deregister
  Body: {lease_id}
  Returns: {ok}

GET  /api/agent/orphans
  Returns: {orphans: [{agent_id, last_heartbeat, lease_expired_at}]}

POST /api/agent/cleanup
  Coordinator 调用: 清理孤儿 agent, 释放资源, 失效路由
```

租约机制：
```
Agent 注册 → 拿到 lease_id (TTL 5 分钟)
Agent 每 2 分钟 heartbeat → 续租
超过 5 分钟无 heartbeat → lease 过期 → 标记 orphan
Coordinator 定期 /agent/orphans → 发现孤儿 → 清理
Gateway 检查路由时: lease 过期的 coordinator → 提示用户 "离线"
```

### 第 2 层：记忆层 (dbservice)

**职责**：知识存储与检索，辅助决策。**非关键路径依赖。**

不变的部分：
- Knowledge Store (SQLite + FTS4)
- Memory Schema + 冲突策略
- Memory Relations
- Embedder (本地向量)
- Context Assembly

**关键变更：降级策略**

```python
class MemoryClient:
    """Governance 调 dbservice 的客户端"""

    def write(self, entry):
        try:
            resp = requests.post(f"{DBSERVICE_URL}/knowledge/upsert",
                                 json=entry, timeout=3)
            return resp.json()
        except Exception:
            # 降级：写本地 pending 文件，后续补写
            self._write_to_local_pending(entry)
            log.warning("dbservice unavailable, queued locally")
            return {"ok": True, "degraded": True}

    def query(self, **kwargs):
        try:
            return requests.get(f"{DBSERVICE_URL}/knowledge/find",
                               params=kwargs, timeout=3).json()
        except Exception:
            # 降级：返回空，不阻塞主流程
            log.warning("dbservice unavailable, returning empty")
            return {"documents": [], "degraded": True}

    def assemble_context(self, task_type, scope, budget):
        try:
            return requests.post(f"{DBSERVICE_URL}/assemble-context",
                                json={...}, timeout=5).json()
        except Exception:
            # 降级：最小上下文（只有 project_id + token）
            return {"context": [], "degraded": True}
```

**原则**：governance 独立可运行，dbservice 挂了只影响"记忆增强"，不影响规则执行。

**开发 workflow domain pack**：
```javascript
registerPack("dev-workflow", {
  types: {
    "node_status":      { conflict: "temporal_replace" },
    "verify_decision":  { conflict: "append" },
    "pitfall":          { conflict: "append_set" },
    "session_snapshot": { conflict: "replace" },       // v4: 改名
    "session_log":      { conflict: "append" },        // v4: 新增
    "architecture":     { conflict: "replace" },
    "workaround":       { conflict: "append" },
    "release_note":     { conflict: "append" },
  }
})
```

### 第 3 层：消息层 (Telegram Gateway)

**职责**：消息收发、路由、交互菜单。

不变的部分：
- Telegram 长轮询
- InlineKeyboard 交互菜单
- HTTP API (/gateway/bind, /gateway/reply)

**关键变更 1：Redis Streams 替代 LIST**

```
# Gateway 写入消息
XADD chat:inbox:{token_hash} * chat_id 7848961760 text "你好" ts "2026-..."

# Consumer Group 创建 (首次)
XGROUP CREATE chat:inbox:{token_hash} coordinator-group 0 MKSTREAM

# Coordinator 消费 (阻塞等待)
XREADGROUP GROUP coordinator-group worker-1 COUNT 10 BLOCK 30000
  STREAMS chat:inbox:{token_hash} >

# 处理成功后 ACK
XACK chat:inbox:{token_hash} coordinator-group {message_id}

# 崩溃恢复: 读取未 ACK 的消息
XREADGROUP GROUP coordinator-group worker-1 COUNT 10
  STREAMS chat:inbox:{token_hash} 0
```

对比：

| | v3 (LIST) | v4 (Streams) |
|---|---|---|
| 消费崩溃 | 消息丢失 | 未 ACK 的自动重投 |
| 多消费者 | 抢消息 | Consumer Group 分配 |
| 历史回溯 | 不可能 | XRANGE 按时间查 |
| 监控 | LLEN | XINFO GROUPS / XPENDING |

**关键变更 2：路由表增加 lease 感知**

```python
def get_route(chat_id):
    route = redis.get(f"chat:route:{chat_id}")
    if not route:
        return None
    # 检查 coordinator 是否还活着
    lease = redis.get(f"lease:{route['token_hash']}")
    if not lease:
        route["status"] = "offline"
    else:
        route["status"] = "online"
    return route
```

用户发消息时：
- coordinator online → 正常转发
- coordinator offline → 提示 "Coordinator 离线，消息已排队，请在电脑上启动 session"

### 第 4 层：缓存/通信层 (Redis)

| 用途 | Key 模式 | 类型 | v4 变更 |
|------|---------|------|---------|
| Session 缓存 | session:{id} | STRING | 不变 |
| Token 映射 | token:{hash} | STRING | **增加 refresh/access 区分** |
| 分布式锁 | lock:{name} | STRING (NX) | 不变 |
| 幂等键 | idem:{key} | STRING | 不变 |
| **消息队列** | chat:inbox:{hash} | **STREAM** | **LIST → STREAM** |
| 路由表 | chat:route:{cid} | STRING | 不变 |
| 反向路由 | chat:reverse:{hash} | STRING | 不变 |
| 事件通知 | gov:events:{pid} | Pub/Sub | **降级为 best-effort** |
| **事件流** | gov:stream:{pid} | **STREAM** | **新增：持久化事件** |
| **Agent 租约** | lease:{token_hash} | **STRING (EX)** | **新增** |
| **Worker 锁** | worker:{hash}:owner | **STRING (NX EX)** | **新增：单消费者** |

### 第 5 层：执行层 (宿主机)

**关键变更：Message Worker 替代纯 Scheduled Task**

```
┌─ Message Worker (常驻进程) ─────────────────────┐
│                                                  │
│  主循环:                                          │
│    XREADGROUP BLOCK 30000 → 有消息 → 处理 → ACK  │
│    30 秒无消息 → 续租约 → 继续 BLOCK              │
│    lease 过期 → 检查是否有交互式 session 接管      │
│                                                  │
│  降级:                                            │
│    Worker 崩溃 → Cron 兜底 (每 5 分钟检查 XPENDING)│
│                                                  │
└──────────────────────────────────────────────────┘
```

两种消费模式并存：

| 模式 | 触发 | 延迟 | 用途 |
|------|------|------|------|
| **交互式 Session** | 人类启动 Claude Code | 实时 | ChatProxy 直接 XREADGROUP |
| **Message Worker** | 常驻/Scheduled Task | <1秒 (BLOCK) | 无人时自动处理 |
| **Cron 兜底** | 每 5 分钟 | 5 分钟 | Worker 也挂了时的最后防线 |

互斥保证（单消费者）：
```python
# Worker 启动时获取锁
acquired = redis.set(f"worker:{token_hash}:owner", worker_id, nx=True, ex=60)
if not acquired:
    # 其他 worker 或交互式 session 在消费
    log.info("Another consumer active, standing by")
    return

# 每 30 秒续锁
redis.expire(f"worker:{token_hash}:owner", 60)

# 退出时释放
redis.delete(f"worker:{token_hash}:owner")
```

## 四、Session Context（v4 修订）

### 4.1 从 replace 改为 Snapshot + Log

```
session_snapshot (replace, 最新快照)
    │
    │  每次保存覆盖
    │
    ├── coordinator_token
    ├── chat_id
    ├── project_id
    ├── current_focus
    ├── active_nodes
    ├── pending_tasks
    ├── version: 42          ← 乐观锁
    ├── updated_at
    └── recent_messages (最近 20 条, 从 log 压缩)

session_log (append, 追加事件)
    │
    │  每条消息/动作追加
    │
    ├── {type:"msg_in", text:"...", ts:"..."}
    ├── {type:"msg_out", text:"...", ts:"..."}
    ├── {type:"action", action:"verify_update", node:"L1.3", ts:"..."}
    ├── {type:"decision", content:"先修 A2 再修 A4", ts:"..."}
    └── ...
```

### 4.2 保存时乐观锁

```python
def save_context(project_id, context, expected_version):
    current = load_snapshot(project_id)
    if current and current.get("version", 0) != expected_version:
        raise ConflictError(
            f"Context version conflict: expected {expected_version}, "
            f"got {current['version']}. Another session modified it."
        )
    context["version"] = expected_version + 1
    upsert_snapshot(project_id, context)
```

### 4.3 过期归档

```
Context 24h 不活跃
    │
    ▼
归档 Scheduled Task:
    1. 读 session_log
    2. 提取有价值条目:
       - type:"decision" → 写入长期 verify_decision
       - type:"action" + 失败 → 写入 pitfall
       - type:"msg_in" 涉及架构 → 写入 architecture
    3. 压缩 log 为 session_summary → 写入长期记忆
    4. 清除过期 snapshot + log
```

## 五、Coordinator Session 生命周期（v4 修订）

### 5.1 交互式 Session

```
人类启动 Claude Code
    │
    ▼
[INIT]
    │  POST /api/token/refresh {refresh_token}  ← 换 access_token
    │  GET /api/docs/quickstart                 ← 接入指南
    │  GET context snapshot                     ← 恢复上次状态
    │  POST /api/agent/register                 ← 注册 + 拿 lease
    │  ChatProxy.bind(chat_id)                  ← 绑定 Telegram
    │  获取 worker 锁 (NX)                      ← 接管消息消费
    │  XREADGROUP 0 → 消费未 ACK 消息            ← 恢复崩溃残留
    │
    ▼
[ACTIVE]
    │  ┌─────────────────────────────────────────┐
    │  │  输入:                                   │
    │  │    终端 / ChatProxy(Stream) / Gov事件    │
    │  │                                         │
    │  │  处理:                                   │
    │  │    governance API / dbservice / CLI      │
    │  │                                         │
    │  │  输出:                                   │
    │  │    /gateway/reply / 代码变更 / 状态更新   │
    │  │                                         │
    │  │  持续:                                   │
    │  │    heartbeat 续租 (每 2 分钟)             │
    │  │    worker 锁续期 (每 30 秒)              │
    │  │    session_log 追加 (每次动作)            │
    │  │    snapshot 保存 (每 5 分钟或重要动作后)  │
    │  └─────────────────────────────────────────┘
    │
    ▼
[SUSPEND] (人类暂时离开)
    │  save snapshot (带 version)
    │  释放 worker 锁 → Message Worker 可接管
    │  heartbeat 继续 → lease 不过期
    │  ChatProxy 继续监听 → 消息入 Stream 排队
    │
    ▼
[RESUME]
    │  获取 worker 锁
    │  load snapshot
    │  XREADGROUP 0 → 消费积压
    │  继续工作
    │
    ▼
[EXIT]
    │  save snapshot (final)
    │  POST /api/agent/deregister → 释放 lease
    │  释放 worker 锁
    │  ChatProxy.stop()
    │  → Gateway 下次检查 lease → offline
    │  → 用户消息 → "Coordinator 离线，消息已排队"
```

### 5.2 Message Worker (常驻)

```
启动 (systemd / Scheduled Task / 手动)
    │
    ▼
[INIT]
    │  POST /api/token/refresh → access_token
    │  POST /api/agent/register → lease
    │
    ▼
[STANDBY] 等待 worker 锁
    │  尝试 SET worker:{hash}:owner NX EX 60
    │  ├── 获取到 → 进入 CONSUME
    │  └── 未获取 → 交互式 session 在消费
    │       sleep 30s → 重试
    │
    ▼
[CONSUME] 阻塞消费循环
    │  while True:
    │    XREADGROUP BLOCK 30000 COUNT 5
    │    ├── 有消息:
    │    │   load context snapshot
    │    │   POST dbservice /assemble-context (可降级)
    │    │   逐条处理 → ACK
    │    │   save context snapshot
    │    │   POST /gateway/reply
    │    │
    │    ├── 无消息 (30s 超时):
    │    │   续 lease heartbeat
    │    │   续 worker 锁
    │    │   continue
    │    │
    │    └── worker 锁被抢 (交互式 session 启动):
    │        释放 → 回到 STANDBY
    │
    ▼
[EXIT]
    │  释放 worker 锁 + lease
    │  → Cron 兜底 5 分钟后接管
```

### 5.3 Cron 兜底

```python
# 每 5 分钟执行
# 检查是否有未消费的消息且无活跃 worker

def cron_fallback():
    for token_hash in get_all_coordinator_hashes():
        # 检查是否有活跃 worker
        owner = redis.get(f"worker:{token_hash}:owner")
        if owner:
            continue  # 有人在消费

        # 检查 XPENDING
        pending = redis.xpending(f"chat:inbox:{token_hash}", "coordinator-group")
        if pending["count"] > 0:
            log.warning("Orphaned messages found for %s, processing", token_hash)
            # 认领并处理
            claim_and_process(token_hash)

        # 检查新消息 (未被任何 group 读取)
        info = redis.xinfo_stream(f"chat:inbox:{token_hash}")
        if info["length"] > 0:
            process_new_messages(token_hash)
```

## 六、可观测性

### 6.1 Trace ID 串联

```
用户 Telegram 消息
    │ trace_id = "tr-{uuid}"  ← Gateway 生成
    ▼
Gateway 日志: [tr-xxx] msg from 7848961760: "查 L1.3"
    │
    ▼
Redis Stream: message_id + trace_id
    │
    ▼
Worker 日志: [tr-xxx] processing message
    │
    ├─▶ Governance: [tr-xxx] GET /api/wf/amingClaw/node/L1.3
    ├─▶ dbservice:  [tr-xxx] /knowledge/find?tags=L1.3
    └─▶ Gateway:    [tr-xxx] POST /gateway/reply
         │
         ▼
Telegram 回复 [tr-xxx] 完成
```

### 6.2 结构化日志格式

```json
{
  "ts": "2026-03-22T13:35:00Z",
  "level": "info",
  "service": "gateway",
  "trace_id": "tr-a1b2c3",
  "message_id": "msg-123",
  "session_id": "ses-xxx",
  "event": "message_forwarded",
  "chat_id": 7848961760,
  "token_hash": "9cb15f91",
  "duration_ms": 12
}
```

### 6.3 关键指标

| 指标 | 来源 | 告警阈值 |
|------|------|---------|
| inbox 积压消息数 | XLEN chat:inbox:* | > 50 |
| 未 ACK 消息数 | XPENDING | > 10 持续 5 分钟 |
| outbox 未投递数 | SELECT COUNT WHERE delivered_at IS NULL | > 20 |
| 死信数 | SELECT COUNT WHERE dead_letter = 1 | > 0 |
| Agent 孤儿数 | /api/agent/orphans | > 0 持续 10 分钟 |
| dbservice 降级次数 | 日志计数 | > 5/分钟 |
| 消息端到端延迟 | trace_id 首尾时间差 | > 60 秒 |

## 七、Docker Compose（v4 完整）

```yaml
services:
  nginx:
    image: nginx:alpine
    ports: ["30000:80"]
    volumes: [./nginx/nginx.conf:/etc/nginx/nginx.conf:ro]
    depends_on:
      governance: { condition: service_healthy }
    restart: unless-stopped

  governance:
    build: { context: ., dockerfile: Dockerfile.governance }
    expose: ["30006"]
    volumes:
      - governance-data:/app/shared-volume/codex-tasks/state/governance
      - .:/workspace:ro
    environment:
      - GOVERNANCE_PORT=40006
      - REDIS_URL=redis://redis:6379/0
      - DBSERVICE_URL=http://dbservice:40002  # 新增
      - SHARED_VOLUME_PATH=/app/shared-volume
    depends_on:
      redis: { condition: service_healthy }
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:40006/api/health')"]
      interval: 10s
      timeout: 5s
      retries: 3

  telegram-gateway:
    build: { context: ., dockerfile: Dockerfile.telegram-gateway }
    expose: ["30010"]
    env_file: [.env]
    environment:
      - GOVERNANCE_URL=http://governance:40006
      - REDIS_URL=redis://redis:6379/0
      - GATEWAY_PORT=40010
    depends_on:
      governance: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped

  dbservice:                          # 新增
    build: { context: ./dbservice }
    expose: ["40002"]
    ports: ["40002:40002"]            # 宿主机也需要访问
    volumes:
      - memory-data:/app/db
    environment:
      - DBSERVICE_PORT=40002
      - DBSERVICE_SAVE_PATH=/app/db
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "node", "-e", "require('http').get('http://localhost:40002/health',r=>{process.exit(r.statusCode===200?0:1)})"]
      interval: 10s
      timeout: 5s
      retries: 3

  redis:
    image: redis:7-alpine
    expose: ["6379"]
    ports: ["40079:6379"]
    volumes: [redis-data:/data]
    command: redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
    restart: unless-stopped

volumes:
  governance-data: { driver: local }
  redis-data: { driver: local }
  memory-data: { driver: local }       # 新增
  task-data: { driver: local }
```

## 八、Nginx 路由（v4 完整）

```nginx
upstream governance       { server governance:40006; }
upstream telegram-gateway { server telegram-gateway:40010; }
upstream dbservice        { server dbservice:40002; }

server {
    listen 80;

    location /nginx-health { return 200 '{"ok":true}'; }

    location /api/     { proxy_pass http://governance/api/; ... }
    location /gateway/ { proxy_pass http://telegram-gateway/gateway/; ... }
    location /memory/  { proxy_pass http://dbservice/; ... }       # 新增

    # dev (按需)
    location /dev/api/ {
        set $dev governance-dev:40007;
        proxy_pass http://$dev/api/; ...
    }
}
```

## 九、实施路线（v4 重排）

### P0：地基（立即）

1. **Redis Streams 消息队列**
   - Gateway: XADD 替代 LPUSH
   - ChatProxy: XREADGROUP 替代 RPOP
   - Consumer Group + ACK

2. **Event Outbox**
   - outbox 表 + 后台 worker
   - Pub/Sub 降级为 best-effort 通知

3. **双令牌模型**
   - /api/token/refresh, /api/token/revoke
   - access_token 4h + refresh_token 90d

4. **Agent Lifecycle**
   - register/heartbeat/deregister/orphans
   - lease 租约 + 过期检测

### P1：一致性（紧接）

5. **Session Context snapshot + log + version**
   - 乐观锁防覆盖
   - append log 防丢失

6. **dbservice Docker 化**
   - 加入 compose
   - 注册 dev-workflow domain pack
   - 降级策略

7. **Message Worker**
   - 阻塞消费 + 租约 + Cron 兜底
   - 单消费者互斥

8. **可观测性**
   - trace_id 串联
   - 结构化日志
   - 关键指标监控

### P2：能力增强

9. **Context Assembly 集成**
10. **过期上下文自动归档**
11. **Governance memory → dbservice 代理**
12. **Task registry (文件 → 表)**

### P3：Workflow 功能

13. **import-graph 同步状态**
14. **Agent 友好错误信息**
15. **状态跳级 API**
16. **Release Profile**
17. **Gate 策略化**
18. **影响分析策略化**
19. **向量检索按需启用**
