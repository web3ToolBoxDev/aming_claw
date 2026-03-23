# Scheduled Task 消息处理设计

## 核心原则

- 每个 Scheduled Task 绑定一个项目
- 多项目 = 多个 Task 实例
- 通过 Gateway 路由表感知项目切换
- Context 和记忆按项目隔离

## 架构

```
Gateway 路由表 (Redis)
  chat:route:7848961760 → {token_hash, project_id: "amingClaw"}
      │
      │ 用户 /bind 切换项目时自动更新
      │
      ▼
Scheduled Task: telegram-handler-amingClaw
  │
  ├── 1. 查路由表确认 chat 还绑在本项目
  │     → 是 → 继续处理
  │     → 否 → 静默退出 (用户切换到了别的项目)
  │
  ├── 2. 加载项目 context
  │     GET /api/context/amingClaw/load
  │
  ├── 3. 加载项目记忆
  │     POST /api/context/amingClaw/assemble
  │
  ├── 4. 消费消息 + 处理 + 回复
  │
  └── 5. 保存 context
        POST /api/context/amingClaw/save

Scheduled Task: telegram-handler-toolboxClient
  └── 同上，绑定 toolboxClient
```

## 单项目 Task 模板

```
Task ID: telegram-handler-{project_id}
Schedule: * * * * * (每分钟)

启动流程:
  1. CHECK: 本项目是否是当前活跃绑定？
     → GET /gateway/status → 找 chat_id 对应的 project_id
     → project_id != 本 task 的项目 → 退出
     → project_id == 本 task 的项目 → 继续

  2. CHECK: 消息队列有内容？
     → XLEN chat:inbox:{token_hash}
     → 0 → 退出

  3. LOAD: 上下文 + 记忆
     → GET /api/context/{pid}/load → 上次工作状态
     → POST /api/context/{pid}/assemble → 项目相关记忆

  4. PROCESS: 逐条消费消息
     → XREADGROUP + 处理 + XACK
     → 回复: POST /gateway/reply

  5. SAVE: 更新 context
     → POST /api/context/{pid}/save
     → POST /api/context/{pid}/log (追加处理记录)
```

## 多项目切换场景

```
用户在 Telegram /menu 切换到 toolboxClient:
    │
    ▼
Gateway 更新路由表:
  chat:route:7848961760 → {project_id: "toolboxClient", token_hash: "xxx"}
    │
    ▼
下一次 Scheduled Task 触发:
  telegram-handler-amingClaw:
    → 查路由 → project_id = toolboxClient ≠ amingClaw
    → 静默退出 (不处理)

  telegram-handler-toolboxClient:
    → 查路由 → project_id = toolboxClient ✓
    → 消费消息 → 处理 → 回复
```

## 创建方式

人类或 Coordinator 为每个项目创建一个 Task:

```bash
# amingClaw
mcp__scheduled-tasks__create_scheduled_task(
    taskId="telegram-handler-amingClaw",
    cronExpression="* * * * *",
    prompt="... 绑定 amingClaw ..."
)

# toolboxClient
mcp__scheduled-tasks__create_scheduled_task(
    taskId="telegram-handler-toolboxClient",
    cronExpression="* * * * *",
    prompt="... 绑定 toolboxClient ..."
)
```

## 保留的多项目交互能力

1. **跨项目查询**: 消息里提到另一个项目 → task 可以调另一个项目的 API
   ```
   用户: "amingClaw 和 toolboxClient 各有多少节点？"
   → GET /api/wf/amingClaw/summary
   → GET /api/wf/toolboxClient/summary
   → 合并回复
   ```

2. **项目切换提示**: 用户 /bind 切换项目后，旧项目 task 检测到路由变化
   → 保存 context → 通知 "已切换到 xxx"

3. **全局记忆**: dbservice 的 scope=global 可存跨项目通用知识

4. **统一 Gateway**: 不管绑哪个项目，Gateway 路由表统一管理，task 只需查路由

## Task Prompt 模板

```
你是 {project_id} 项目的 Coordinator 助手。

TOKEN: {coordinator_token}
PROJECT: {project_id}
CHAT_ID: {chat_id}
STREAM: chat:inbox:{token_hash}
BASE_URL: http://localhost:40000

步骤:
1. 检查路由: curl -s http://localhost:40000/gateway/status
   → 找 chat_id={chat_id} 的绑定
   → 如果 project_id 不是 {project_id}，直接结束

2. 检查队列: docker exec aming_claw-redis-1 redis-cli XLEN {stream}
   → 0 则结束

3. 加载上下文:
   curl -s http://localhost:40000/api/context/{project_id}/load \
     -H "X-Gov-Token: {token}"

4. 读取消息:
   docker exec aming_claw-redis-1 redis-cli XRANGE {stream} - + COUNT 5

5. 处理每条消息并回复:
   curl -s -X POST http://localhost:40000/gateway/reply \
     -H "Content-Type: application/json" \
     -H "X-Gov-Token: {token}" \
     -d '{{"token":"{token}","chat_id":{chat_id},"text":"回复内容"}}'

6. ACK 消息:
   docker exec aming_claw-redis-1 redis-cli XACK {stream} coordinator-group {msg_id}

7. 保存上下文:
   curl -s -X POST http://localhost:40000/api/context/{project_id}/save \
     -H "Content-Type: application/json" \
     -H "X-Gov-Token: {token}" \
     -d '{{"context":{{"current_focus":"...","recent_messages":[...]}}}}'
```
