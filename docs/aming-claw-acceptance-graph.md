---
name: acceptance-graph
description: aming_claw 项目验收图（Verification Topology）— 治理服务 + 核心 Agent 系统
type: reference
version: v1.0
---

# aming_claw 验收图

## 状态说明

### verify_status
| 值 | 含义 |
|----|------|
| verify:pass | E2E 验收通过 |
| verify:T2-pass | 单元+API 测试通过 |
| verify:fail | 验收失败 |
| verify:pending | 待验证 |

## L0 — 基础设施层（无依赖）

```
L0.1  Python 运行环境  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[agent/requirements.txt]
      secondary:[runtime/python/]
      test:[]

L0.2  共享存储目录结构  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[shared-volume/]
      test:[agent/tests/test_task_state.py]

L0.3  JSON/JSONL 持久化工具  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[]
      test:[agent/tests/test_task_state.py]

L0.4  Telegram API 封装  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/utils.py]
      secondary:[]
      test:[agent/tests/test_bot_commands.py]

L0.5  国际化引擎  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/i18n.py, agent/locales/zh.json, agent/locales/en.json]
      secondary:[]
      test:[agent/tests/test_i18n.py]
```

## L1 — 服务层（依赖 L0）

```
L1.1  配置管理  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/config.py]
      secondary:[]
      test:[agent/tests/test_config.py]

L1.2  任务状态机  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/task_state.py]
      secondary:[]
      test:[agent/tests/test_task_state.py]

L1.3  Git 检查点与回滚  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/git_rollback.py]
      secondary:[]
      test:[agent/tests/test_git_rollback.py]

L1.4  工作区注册  [impl:done] [verify:pending] v1.0
      deps:[L0.2, L0.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/workspace_registry.py, agent/workspace.py]
      secondary:[]
      test:[agent/tests/test_workspace_queue.py]

L1.5  工作区任务队列  [impl:done] [verify:pending] v1.0
      deps:[L1.4, L1.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/workspace_queue.py]
      secondary:[]
      test:[agent/tests/test_workspace_queue.py]

L1.6  TOTP 双因素认证  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/auth.py]
      secondary:[]
      test:[]

L1.7  模型注册表  [impl:done] [verify:pending] v1.0
      deps:[L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/model_registry.py]
      secondary:[]
      test:[agent/tests/test_model_registry.py]
```

## L2 — 能力层（依赖 L0+L1）

```
L2.1  AI 后端集成（Claude/Codex/OpenAI）  [impl:done] [verify:pending] v1.0
      deps:[L1.1, L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py]
      secondary:[]
      test:[agent/tests/test_backends.py]

L2.2  多阶段流水线  [impl:done] [verify:pending] v1.0
      deps:[L2.1, L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py, agent/pipeline_config.py]
      secondary:[]
      test:[agent/tests/test_role_pipeline.py, agent/tests/test_pipeline_config.py]

L2.3  角色流水线（PM/Dev/Test/QA）  [impl:done] [verify:pending] v1.0
      deps:[L2.2, L1.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py, agent/config.py]
      secondary:[]
      test:[agent/tests/test_role_pipeline.py]

L2.4  Noop 检测与重试  [impl:done] [verify:pending] v1.0
      deps:[L2.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/backends.py]
      secondary:[]
      test:[agent/tests/test_backends.py]

L2.5  任务验收文档生成  [impl:done] [verify:pending] v1.0
      deps:[L1.2, L1.3]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/task_accept.py]
      secondary:[]
      test:[agent/tests/test_task_accept.py, agent/tests/test_acceptance_flow.py]

L2.6  并行调度器  [impl:done] [verify:pending] v1.0
      deps:[L1.4, L1.5]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/parallel_dispatcher.py]
      secondary:[]
      test:[agent/tests/test_parallel_dispatcher.py]

L2.7  服务管理器  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/service_manager.py]
      secondary:[]
      test:[]
```

## L3 — 场景层（依赖 L0+L1+L2）

```
L3.1  Telegram 命令路由  [impl:done] [verify:pending] v1.0
      deps:[L0.4, L1.1, L1.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/bot_commands.py]
      secondary:[]
      test:[agent/tests/test_bot_commands.py, agent/tests/test_interactive_commands.py]

L3.2  交互式菜单系统  [impl:done] [verify:pending] v1.0
      deps:[L3.1, L0.5]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[agent/interactive_menu.py]
      secondary:[]
      test:[agent/tests/test_interactive_menu.py]

L3.3  任务创建→执行→验收全链路  [impl:done] [verify:pending] v1.0
      deps:[L2.1, L2.5, L1.2, L1.3]
      gate_mode: explicit
      gates:[L2.1, L2.5]
      verify: L4
      test_coverage: partial
      primary:[agent/executor.py, agent/coordinator.py]
      secondary:[]
      test:[agent/tests/test_acceptance_flow.py]

L3.4  截图能力  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/executor.py]
      secondary:[executor-gateway/app/main.py]
      test:[agent/tests/test_screenshot_command_routing.py]

L3.5  自我更新（mgr_reinit）  [impl:done] [verify:pending] v1.0
      deps:[L2.7, L1.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/service_manager.py, agent/bot_commands.py]
      secondary:[]
      test:[]
```

## L4 — 治理服务层（依赖 L0）

```
L4.1  SQLite 数据库层  [impl:done] [verify:pending] v1.0
      deps:[L0.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/db.py]
      secondary:[]
      test:[agent/tests/test_governance_db.py]

L4.2  显式枚举与错误体系  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[agent/governance/enums.py, agent/governance/errors.py]
      secondary:[]
      test:[agent/tests/test_governance_enums.py]

L4.3  权限矩阵与状态机  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/permissions.py]
      secondary:[]
      test:[agent/tests/test_governance_permissions.py]

L4.4  结构化证据校验  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/evidence.py, agent/governance/models.py]
      secondary:[]
      test:[agent/tests/test_governance_evidence.py]

L4.5  Gate 策略引擎  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/gate_policy.py]
      secondary:[]
      test:[agent/tests/test_governance_gate_policy.py]

L4.6  NetworkX DAG 图管理  [impl:done] [verify:pending] v1.0
      deps:[L4.2]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/graph.py]
      secondary:[]
      test:[agent/tests/test_governance_graph.py]

L4.7  角色服务（Principal+Session+Auth）  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.2]
      gate_mode: explicit
      gates:[L4.1]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/role_service.py]
      secondary:[agent/governance/redis_client.py]
      test:[agent/tests/test_governance_role.py]

L4.8  状态服务（verify-update+release-gate+rollback）  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.3, L4.4, L4.5, L4.6]
      gate_mode: explicit
      gates:[L4.3, L4.4, L4.5, L4.6]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[agent/tests/test_governance_state.py]

L4.9  影响分析引擎  [impl:done] [verify:pending] v1.0
      deps:[L4.6, L4.8]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/impact_analyzer.py]
      secondary:[]
      test:[agent/tests/test_governance_impact.py]

L4.10  审计服务  [impl:done] [verify:pending] v1.0
      deps:[L4.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/audit_service.py]
      secondary:[]
      test:[agent/tests/test_governance_audit.py]

L4.11  项目服务（init+隔离+bootstrap）  [impl:done] [verify:pending] v1.0
      deps:[L4.7, L4.8, L4.6]
      gate_mode: explicit
      gates:[L4.7]
      verify: L2
      test_coverage: partial
      primary:[agent/governance/project_service.py]
      secondary:[]
      test:[agent/tests/test_governance_session_persistence.py]

L4.12  记忆服务  [impl:done] [verify:pending] v1.0
      deps:[L4.1, L4.10]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/memory_service.py]
      secondary:[]
      test:[agent/tests/test_governance_memory.py]

L4.13  事件总线  [impl:done] [verify:pending] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/event_bus.py]
      secondary:[]
      test:[agent/tests/test_governance_event_bus.py]

L4.14  幂等键管理  [impl:done] [verify:pending] v1.0
      deps:[L4.1]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[agent/governance/idempotency.py]
      secondary:[]
      test:[agent/tests/test_governance_idempotency.py]

L4.15  HTTP 服务（路由+中间件）  [impl:done] [verify:pending] v1.0
      deps:[L4.7, L4.8, L4.11, L4.12, L4.10]
      gate_mode: explicit
      gates:[L4.7, L4.8, L4.11]
      verify: L4
      test_coverage: partial
      primary:[agent/governance/server.py]
      secondary:[]
      test:[agent/tests/test_governance_server.py]

L4.16  GovernanceClient SDK  [impl:done] [verify:pending] v1.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/client.py]
      secondary:[]
      test:[]

L4.17  Docker 部署  [impl:done] [verify:pending] v1.0
      deps:[L4.15]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[Dockerfile.governance, docker-compose.governance.yml]
      secondary:[start_governance.py, init_project.py]
      test:[]
```

## L5 — v4 地基层（P0，依赖 L4）

```
L5.1  Redis Streams 消息队列  [impl:pending] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: explicit
      gates:[L4.17]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/telegram_gateway/chat_proxy.py]
      secondary:[docker-compose.governance.yml]
      test:[]
      description: Gateway LPUSH/RPOP → XADD/XREADGROUP+ACK，消息不丢失

L5.2  Event Outbox 双轨投递  [impl:pending] [verify:pending] v4.0
      deps:[L4.13, L4.1]
      gate_mode: explicit
      gates:[L4.13]
      verify: L4
      test_coverage: none
      primary:[agent/governance/event_bus.py, agent/governance/outbox.py]
      secondary:[agent/governance/db.py]
      test:[]
      description: 事件先写 outbox 表(同事务)，后台 worker 异步投递到 Redis/dbservice

L5.3  双令牌模型 (refresh+access)  [impl:pending] [verify:pending] v4.0
      deps:[L4.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[agent/governance/role_service.py, agent/governance/server.py]
      secondary:[agent/governance/project_service.py]
      test:[]
      description: refresh_token(90d)+access_token(4h)，支持 revoke/rotate

L5.4  Agent Lifecycle API  [impl:pending] [verify:pending] v4.0
      deps:[L4.7, L4.1]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[agent/governance/agent_lifecycle.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: register/heartbeat/deregister/orphans + lease 租约机制
```

## L6 — v4 一致性层（P1，依赖 L5）

```
L6.1  Session Context (snapshot+log+version)  [impl:pending] [verify:pending] v4.0
      deps:[L5.1, L5.4]
      gate_mode: explicit
      gates:[L5.1, L5.4]
      verify: L4
      test_coverage: none
      primary:[agent/governance/session_context.py]
      secondary:[]
      test:[]
      description: 乐观锁防覆盖，append log 防丢失

L6.2  dbservice Docker 集成  [impl:pending] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[dbservice/, docker-compose.governance.yml, nginx/nginx.conf]
      secondary:[]
      test:[]
      description: dbservice 容器化 + dev-workflow domain pack + 降级策略

L6.3  Message Worker (阻塞消费+租约)  [impl:pending] [verify:pending] v4.0
      deps:[L5.1, L5.4, L6.1]
      gate_mode: explicit
      gates:[L5.1, L5.4, L6.1]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/message_worker.py]
      secondary:[]
      test:[]
      description: 阻塞消费+租约互斥+Cron兜底，三级容错

L6.4  可观测性 (trace_id+结构化日志)  [impl:pending] [verify:pending] v4.0
      deps:[L5.2]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/observability.py]
      secondary:[agent/telegram_gateway/gateway.py]
      test:[]
      description: trace_id 串联全链路，结构化日志，关键指标监控
```
