# Aming Claw 完整架构方案 v3

## 一、系统全景

```
┌─────────────────────────────────────────────────────────────────┐
│                        人类用户 (Telegram)                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:30000)    │  反向代理
                    └──┬─────────┬────────┘
                       │         │
          ┌────────────▼──┐  ┌──▼────────────────┐
          │  Governance   │  │  Telegram Gateway  │
          │  (:30006)     │  │  (:30010)          │
          │  规则层        │  │  消息层             │
          └──────┬────────┘  └──────┬─────────────┘
                 │                  │
          ┌──────▼──────────────────▼──────┐
          │           Redis (:6379)         │
          │   缓存 / Pub-Sub / 消息队列      │
          └──────┬─────────────────────────┘
                 │
          ┌──────▼──────────┐
          │   dbservice     │
          │   (:30002)      │
          │   记忆层         │
          └─────────────────┘

─ ─ ─ ─ ─ Docker 内网 ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

          ┌─────────────────────────────────┐
          │         宿主机                   │
          │                                 │
          │  Coordinator Session (Claude)   │
          │    ├── ChatProxy (Redis 订阅)    │
          │    ├── GovernanceClient (HTTP)   │
          │    └── Claude Code CLI          │
          │                                 │
          │  Scheduled Task (定时)           │
          │    └── 消息处理 + 上下文恢复      │
          └─────────────────────────────────┘
```

## 二、五层架构

### 第 1 层：规则层 (Governance Service)

**职责**：强制执行 workflow 规则，不可绕过。

| 模块 | 功能 |
|------|------|
| DAG 图 (NetworkX) | 节点定义、依赖关系、gate 策略 |
| 状态机 (SQLite) | verify status 流转、权限校验 |
| 角色服务 | principal + session 模型、token 认证 |
| 审计 | 谁在什么时候做了什么变更 |
| 发布门禁 | release-gate 检查 |
| 文档 API | /api/docs/* 接入指南 |

**数据存储**：
- 图定义：JSON + NetworkX（只读为主）
- 运行态：SQLite per project（WAL 模式）
- 审计：JSONL append-only + SQLite 索引

**原则**：governance 管"能不能做"。

### 第 2 层：记忆层 (dbservice)

**职责**：存储和检索开发知识，辅助 Agent 决策。

| 模块 | 功能 |
|------|------|
| Knowledge Store | 结构化知识 CRUD + FTS4 全文搜索 |
| Memory Schema | 类型分类 + 冲突策略（replace/append/temporal_replace） |
| Memory Relations | 文档间关系图 |
| Embedder | 本地向量嵌入（Xenova/all-MiniLM-L6-v2，无需 API） |
| Context Assembly | 按任务类型 + token 预算自动组装上下文 |
| Semantic Search | mem0 向量相似度搜索 + 去重 |

**数据存储**：
- 结构化：SQLite + FTS4
- 向量：mem0 (SQLite-backed)
- 全部本地运行，零外部 API 依赖

**开发 workflow 专用 domain pack**：
```
node_status      — 节点状态变更记录     (temporal_replace)
verify_decision  — 验证决策及原因       (append)
pitfall          — 踩坑记录            (append_set)
session_context  — 短期会话上下文       (replace, TTL 24h)
architecture     — 架构决策            (replace)
workaround       — 临时方案            (append)
release_note     — 发布记录            (append)
```

**原则**：dbservice 管"该怎么做"。

### 第 3 层：消息层 (Telegram Gateway)

**职责**：消息收发、路由、交互菜单。

| 功能 | 实现 |
|------|------|
| Telegram 长轮询 | getUpdates polling |
| 消息路由 | Redis 路由表：chat_id → coordinator token |
| 交互菜单 | InlineKeyboard：coordinator 列表、切换、状态查看 |
| HTTP API | /gateway/bind, /gateway/reply, /gateway/unbind |
| 事件通知 | Redis Pub/Sub 订阅 gov:events:* → Telegram 推送 |
| 多 coordinator | 路由表支持多个 coordinator 绑定不同 chat |

**消息队列（可靠投递）**：
```
用户消息入队：  LPUSH chat:inbox:{token_hash} {message_json}
Coordinator 消费：RPOP chat:inbox:{token_hash}
消息不丢失，按序消费。
```

### 第 4 层：缓存/通信层 (Redis)

| 用途 | Key 模式 |
|------|---------|
| Session 缓存 | session:{id}, token:{hash} |
| 分布式锁 | lock:{name} |
| 幂等键 | idem:{key} |
| 消息队列 | chat:inbox:{token_hash} (LIST) |
| 路由表 | chat:route:{chat_id}, chat:reverse:{token_hash} |
| 事件通知 | gov:events:{project_id} (Pub/Sub) |
| Session 上下文缓存 | context:{project_id}:{token_hash} |

### 第 5 层：执行层 (宿主机)

**职责**：运行 Claude Code / Codex 等 CLI 工具。

| 组件 | 功能 |
|------|------|
| Coordinator Session | Claude Code 交互式 session |
| ChatProxy | Redis 订阅消息 → 处理 → 回复 |
| GovernanceClient | HTTP 调用 governance API |
| Executor Worker | 监听 task 文件 → 启动 CLI → 写结果 |

## 三、Coordinator Session 生命周期

### 3.1 Session 类型

| 类型 | 触发方式 | 生命周期 | 用途 |
|------|---------|---------|------|
| **交互式 Session** | 人类在终端启动 Claude Code | 人类控制，手动退出 | 日常开发、复杂任务 |
| **Scheduled Session** | 定时任务自动启动 | 执行完毕自动退出 | 消息处理、巡检、自动化 |

### 3.2 交互式 Session 生命周期

```
人类启动 Claude Code
    │
    ▼
[INIT] 加载记忆
    │  GET /api/docs/quickstart         ← 获取接入指南
    │  GET /api/context/{pid}/load      ← 恢复上次工作状态
    │  ChatProxy.bind(chat_id, pid)     ← 绑定 Telegram
    │
    ▼
[ACTIVE] 工作循环
    │  ┌─────────────────────────────────────────┐
    │  │  接收输入来源:                            │
    │  │    1. 人类终端输入 (直接)                  │
    │  │    2. Telegram 消息 (ChatProxy → Redis)   │
    │  │    3. Governance 事件 (Redis Pub/Sub)     │
    │  │                                          │
    │  │  执行动作:                                │
    │  │    - 调 governance API (verify/baseline)  │
    │  │    - 调 dbservice (写/查记忆)              │
    │  │    - 运行 Claude Code CLI (代码变更)       │
    │  │    - 回复 Telegram (ChatProxy.reply)      │
    │  │                                          │
    │  │  持续保存:                                │
    │  │    - Session 上下文 → dbservice            │
    │  │    - 重要决策 → dbservice (verify_decision)│
    │  │    - Governance 事件 → 自动记录            │
    │  └─────────────────────────────────────────┘
    │
    ▼
[SUSPEND] 人类暂时离开
    │  POST /api/context/{pid}/save     ← 保存当前状态
    │  {
    │    current_focus: "修复 A1-A4",
    │    pending_tasks: [...],
    │    recent_messages: [...last 20...],
    │    active_nodes: ["L1.3", "L2.1"]
    │  }
    │  ChatProxy 继续监听 (后台线程)
    │  → 新消息入 Redis LIST，不丢失
    │
    ▼
[RESUME] 人类回来 / Scheduled Task 恢复
    │  GET /api/context/{pid}/load      ← 加载状态
    │  RPOP chat:inbox:{hash}           ← 消费积压消息
    │  继续工作
    │
    ▼
[EXIT] Session 结束
    │  POST /api/context/{pid}/save     ← 最终保存
    │  POST /api/context/{pid}/archive  ← 归档有价值的内容到长期记忆
    │  ChatProxy.stop()
    │  Gateway 自动检测 coordinator 离线
    │  → Telegram 用户发消息时提示 "Coordinator 离线"
```

### 3.3 Scheduled Session 生命周期

```
定时触发 (每 1 分钟 / 按需)
    │
    ▼
[INIT] 最小化启动
    │  1. 检查 Redis LIST chat:inbox:{hash} 是否有消息
    │     → 没有消息 → 立即退出（不浪费资源）
    │  2. 有消息 → 继续
    │
    ▼
[LOAD CONTEXT] 恢复上下文
    │  GET /api/context/{pid}/load
    │  POST dbservice /assemble-context {
    │    task_type: "telegram_handler",
    │    scope: project_id,
    │    token_budget: 4000
    │  }
    │  → 拿到: session_context + 相关决策 + pitfall
    │
    ▼
[PROCESS] 处理消息
    │  while msg = RPOP chat:inbox:{hash}:
    │    1. 理解消息 (结合上下文)
    │    2. 判断类型:
    │       - 查询类 → 直接回答 (查 governance/dbservice)
    │       - 操作类 → 执行 (调 governance API)
    │       - 任务类 → 创建 task 文件 (等 executor 执行)
    │       - 闲聊类 → 简单回复
    │    3. POST /gateway/reply → 回复 Telegram
    │    4. 追加到 session context
    │
    ▼
[SAVE & EXIT]
    │  POST /api/context/{pid}/save     ← 保存更新后的上下文
    │  如果有重要决策 → 写入长期记忆
    │  Session 自动结束
```

### 3.4 Session Context Store

**存储位置**：dbservice（type: session_context, TTL: 24h）

```json
{
  "type": "session_context",
  "scope": "amingClaw",
  "content": {
    "coordinator_token": "gov-3506be...",
    "chat_id": 7848961760,
    "project_id": "amingClaw",
    "current_focus": "修复 A1-A4 基础项",
    "active_nodes": ["L1.3", "L2.1", "L0.1"],
    "pending_tasks": [
      "import-graph 同步状态",
      "Agent 友好错误信息"
    ],
    "recent_messages": [
      {"role": "user", "text": "L1.3 状态怎么样？", "ts": "2026-03-22T13:00:00Z"},
      {"role": "coordinator", "text": "L1.3 当前 testing...", "ts": "2026-03-22T13:00:05Z"}
    ],
    "decisions_this_session": [
      "决定先修 import-graph 再修 error handling"
    ]
  },
  "updated_at": "2026-03-22T13:35:00Z",
  "ttl_hours": 24
}
```

**过期归档流程**：

```
Context TTL 到期
    │
    ▼
归档检查 (Scheduled Task)
    │
    ├── recent_messages 中的决策 → 写入 verify_decision
    ├── 发现的 pitfall → 写入 pitfall
    ├── 架构变更 → 写入 architecture
    └── 日常对话 → 丢弃
    │
    ▼
清除过期 context
```

## 四、数据流全图

### 4.1 用户发送消息 → Coordinator 处理 → 回复

```
用户 Telegram 消息: "帮我检查 L1.3 的状态"
    │
    ▼
Gateway (Docker)
    │  1. poll_updates 收到消息
    │  2. 查路由表: chat:route:7848961760 → coordinator token_hash
    │  3. LPUSH chat:inbox:9cb15f91 {text, chat_id, ts}
    │
    ▼
Redis LIST: chat:inbox:9cb15f91
    │
    ▼
Coordinator Session (宿主机)
    │  1. ChatProxy.RPOP → 收到消息
    │  2. 查 dbservice: 相关记忆 (L1.3 的 pitfall/decision)
    │  3. 查 governance: GET /api/wf/amingClaw/node/L1.3
    │  4. 组装回复
    │  5. POST /gateway/reply → "L1.3 当前 testing，上次 tester-001..."
    │  6. 写 dbservice: 追加 session_context
    │
    ▼
Gateway → Telegram API → 用户收到回复
```

### 4.2 Governance 事件 → 自动通知 + 自动记忆

```
某 Agent 调用 verify-update: L2.1 → qa_pass
    │
    ▼
Governance
    │  1. 状态机校验 → 允许
    │  2. SQLite 写入
    │  3. EventBus.publish("node.status_changed", payload)
    │
    ▼
Redis Pub/Sub: gov:events:amingClaw
    │
    ├──▶ Gateway: 格式化 → Telegram 通知: "✅ L2.1 → qa_pass"
    │
    └──▶ Governance: 自动写 dbservice
         POST dbservice /knowledge/upsert {
           type: "node_status",
           refId: "L2.1:qa_pass:2026-03-22",
           content: "L2.1 通过 QA 验证",
           tags: ["L2.1", "qa_pass"],
           scope: "amingClaw"
         }
```

### 4.3 Scheduled Task 消息处理

```
Cron 触发 (每 1 分钟)
    │
    ▼
新 Session 启动
    │
    ▼
检查 Redis: LLEN chat:inbox:9cb15f91
    │
    ├── 0 条 → 退出（<1秒）
    │
    └── 3 条 → 继续处理
         │
         ▼
    加载上下文:
         GET dbservice /knowledge/find?type=session_context&scope=amingClaw
         POST dbservice /assemble-context {task_type: "telegram_handler"}
         │
         ▼
    逐条处理:
         msg1: "L1.3 什么状态" → 查 governance → 回复
         msg2: "帮我跑一下测试" → 创建 task 文件 → 回复 "已创建任务"
         msg3: "之前那个 bug 修了吗" → 查 dbservice 记忆 → 回复
         │
         ▼
    保存上下文:
         POST dbservice /knowledge/upsert {type: "session_context", ...}
         │
         ▼
    Session 结束
```

## 五、Docker Compose 完整拓扑

```yaml
services:
  nginx:          # 反向代理 (:30000)
  governance:     # 规则层 (:30006)
  governance-dev: # 开发环境 (:30007, profile: dev)
  telegram-gateway: # 消息层 (:30010)
  dbservice:      # 记忆层 (:30002)
  redis:          # 缓存/通信 (:6379, host:6380)

volumes:
  governance-data:     # governance SQLite + graph
  governance-dev-data: # dev 环境数据
  redis-data:          # Redis AOF
  memory-data:         # dbservice SQLite + 向量
  task-data:           # 任务文件 (shared-volume)
```

**端口映射**：

| 服务 | 容器端口 | 宿主机端口 | 用途 |
|------|---------|-----------|------|
| Nginx | 80 | 30000 | 统一入口 |
| Governance | 30006 | (nginx) | 规则 API |
| Gateway | 30010 | (nginx) | 消息 API |
| dbservice | 30002 | 30002 | 记忆 API |
| Redis | 6379 | 6380 | 宿主机访问 |

**Nginx 路由**：

| 路径 | 上游 |
|------|------|
| /api/* | governance:30006 |
| /gateway/* | telegram-gateway:30010 |
| /dev/api/* | governance-dev:30007 (按需) |
| /memory/* | dbservice:30002 (新增) |

## 六、Governance ↔ dbservice 交互契约

### 6.1 Governance 写记忆 (事件驱动)

Governance EventBus 订阅者自动将事件写入 dbservice：

```python
# governance/event_bus.py 中新增订阅者
def _write_to_memory(payload):
    requests.post("http://dbservice:30002/knowledge/upsert", json={
        "refId": f"{payload['node_id']}:{payload['event']}:{payload['timestamp']}",
        "type": event_to_memory_type(payload["event"]),
        "title": format_title(payload),
        "body": json.dumps(payload),
        "tags": extract_tags(payload),
        "scope": payload.get("project_id", "global"),
        "status": "active",
    })
```

### 6.2 Coordinator 查记忆

```python
# 查询某节点的所有相关记忆
GET dbservice:30002/knowledge/find?scope=amingClaw&tags=L1.3

# 语义搜索
POST dbservice:30002/search
{"query": "文件锁并发问题", "namespace": "amingClaw"}

# 组装上下文 (Scheduled Task 启动时)
POST dbservice:30002/assemble-context
{"task_type": "telegram_handler", "scope": "amingClaw", "token_budget": 4000}
```

### 6.3 Session Context (短期)

```python
# 保存
POST dbservice:30002/knowledge/upsert
{
    "refId": "session-context:amingClaw",
    "type": "session_context",
    "title": "Coordinator Session Context",
    "body": json.dumps(context_data),
    "scope": "amingClaw",
    "status": "active",
    "meta": {"ttl_hours": 24}
}

# 加载
GET dbservice:30002/knowledge/find?type=session_context&scope=amingClaw&refId=session-context:amingClaw
```

## 七、Scheduled Task 配置

```python
# 创建消息处理定时任务
create_scheduled_task(
    taskId="telegram-message-handler",
    cronExpression="* * * * *",  # 每分钟
    description="检查 Telegram 消息队列并处理",
    prompt="""
    你是 amingClaw 项目的 Coordinator。

    1. 连接 Redis (redis://localhost:6380/0)
    2. 检查消息队列: LLEN chat:inbox:9cb15f91dcad09a5
       - 如果为 0，直接结束
    3. 加载上下文: GET http://localhost:30002/knowledge/find?type=session_context&scope=amingClaw
    4. 逐条处理消息: RPOP chat:inbox:9cb15f91dcad09a5
       - 查询类 → 调 governance API 查状态，回复
       - 操作类 → 调 governance API 执行，回复
       - 闲聊类 → 简单回复
    5. 回复: POST http://localhost:30000/gateway/reply
       {token: "gov-3506be...", chat_id: 7848961760, text: "..."}
    6. 保存上下文: POST http://localhost:30002/knowledge/upsert
    """,
)
```

## 八、安全边界

| 层级 | 安全措施 |
|------|---------|
| Init | 密码保护，一次性 |
| Coordinator Token | 10 年 TTL，人类持有并分发 |
| Agent Token | 24h TTL，由 coordinator 分配 |
| Gateway | Token 验证后才允许 bind/reply |
| Governance | 所有状态变更需 token + 角色权限 |
| dbservice | scope 隔离，按 project_id 分区 |
| Redis | 容器内网，宿主机通过 6380 访问 |
| Nginx | 统一入口，未来可加 rate limit |

## 九、实施路线

### 第一轮：当前已完成 ✅

- [x] Governance Service (Docker, port 30006)
- [x] Redis (Docker, port 6380)
- [x] Nginx 反代 (Docker, port 30000)
- [x] Telegram Gateway (Docker, port 30010)
- [x] EventBus → Redis Pub/Sub 桥接
- [x] Gateway 交互式菜单 (/menu, InlineKeyboard)
- [x] Gateway HTTP API (/gateway/bind, /gateway/reply)
- [x] Governance /api/docs/* 文档接口
- [x] Governance /api/role/verify 接口
- [x] ChatProxy 宿主机客户端

### 第二轮：记忆层集成

- [ ] dbservice Docker 化加入 compose
- [ ] 注册 dev-workflow domain pack
- [ ] Governance memory_service → dbservice 代理
- [ ] Session Context API (save/load/archive)
- [ ] EventBus 事件 → dbservice 自动写入
- [ ] Nginx 添加 /memory/* 路由

### 第三轮：Scheduled Task + 自动化

- [ ] Gateway inbox 改成 Redis LIST (可靠投递)
- [ ] 创建 telegram-message-handler Scheduled Task
- [ ] Context Assembly 集成
- [ ] 过期上下文自动归档到长期记忆

### 第四轮：基础项修复 (走 workflow)

- [ ] A2: import-graph 同步状态 (解析 [verify:pass])
- [ ] A4: Agent 友好错误信息 (evidence 校验)
- [ ] A5: 状态跳级 API (force_baseline)
- [ ] A3: 项目 ID 规范 (normalize)
- [ ] A1: API 文档自动生成

### 第五轮：能力扩展

- [ ] Agent Lifecycle API (register/deregister/orphans)
- [ ] Release Profile (按范围检查，不要求全项目全绿)
- [ ] Gate 策略化 (min_status, policy)
- [ ] 影响分析策略化 (file hit + propagation policy)
- [ ] 向量检索按需启用 (sqlite-vec 或 Chroma)
