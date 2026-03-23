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
      primary:[agent/governance/project_service.py, agent/governance/session_persistence.py]
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
      secondary:[start_governance.py, init_project.py, Dockerfile.telegram-gateway, docker-compose.governance-dev.yml]
      test:[]
```

## L5 — v4 地基层（P0，依赖 L4）

```
L5.1  Redis Streams 消息队列  [impl:done] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: explicit
      gates:[L4.17]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/telegram_gateway/chat_proxy.py]
      secondary:[docker-compose.governance.yml, agent/telegram_gateway/__init__.py]
      test:[]
      description: Gateway LPUSH/RPOP → XADD/XREADGROUP+ACK，消息不丢失

L5.2  Event Outbox 双轨投递  [impl:done] [verify:pending] v4.0
      deps:[L4.13, L4.1]
      gate_mode: explicit
      gates:[L4.13]
      verify: L4
      test_coverage: none
      primary:[agent/governance/event_bus.py, agent/governance/outbox.py]
      secondary:[agent/governance/db.py, agent/telegram_gateway/gov_event_listener.py]
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

L6.2  dbservice Docker 集成  [impl:done] [verify:pending] v4.0
      deps:[L4.17]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[dbservice/index.js, dbservice/Dockerfile, dbservice/package.json, docker-compose.governance.yml, nginx/nginx.conf]
      secondary:[dbservice/package-lock.json, dbservice/lib/knowledgeStore.js, dbservice/lib/memorySchema.js, dbservice/lib/memoryRelations.js, dbservice/lib/contextAssembly.js, dbservice/lib/bridgeLLM.js, dbservice/lib/transformersEmbedder.js]
      test:[dbservice/lib/knowledgeStore.test.js, dbservice/lib/memorySchema.test.js, dbservice/lib/memoryRelations.test.js, dbservice/lib/contextAssembly.test.js, dbservice/lib/phase8.test.js]
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

## L7 — 能力增强层（P2，依赖 L5+L6）

```
L7.1  Context Assembly 集成  [impl:done] [verify:pending] v4.0
      deps:[L6.1, L6.2]
      gate_mode: explicit
      gates:[L6.1, L6.2]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py, dbservice/lib/contextAssembly.js]
      secondary:[]
      test:[]
      description: dbservice context assembly + dev-workflow task policies (telegram_handler/verify_node/code_review/release_check/dev_general)

L7.2  过期上下文自动归档  [impl:done] [verify:pending] v4.0
      deps:[L6.1, L6.2]
      gate_mode: explicit
      gates:[L6.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/outbox.py, agent/governance/session_context.py]
      secondary:[]
      test:[]
      description: OutboxWorker 每 60s 检测 stale context (>24h)，自动提取决策/pitfall 归档到长期记忆

L7.3  Memory 双写代理  [impl:done] [verify:pending] v4.0
      deps:[L4.12, L6.2]
      gate_mode: explicit
      gates:[L6.2]
      verify: L4
      test_coverage: none
      primary:[agent/governance/memory_service.py]
      secondary:[]
      test:[]
      description: memory_service.write_memory 双写 JSON + dbservice /knowledge/upsert (best-effort)

L7.4  Task Registry  [impl:done] [verify:pending] v4.0
      deps:[L4.1, L5.4]
      gate_mode: explicit
      gates:[L4.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/governance/server.py, agent/governance/db.py]
      secondary:[]
      test:[]
      description: SQLite task 表 + create/claim/complete/list + retry + DB migration v1→v2

L7.5  记忆迁移 + Domain Pack  [impl:done] [verify:pending] v4.0
      deps:[L6.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[dbservice/lib/contextAssembly.js, dbservice/lib/memorySchema.js]
      secondary:[]
      test:[]
      description: dev-workflow domain pack 注册 (architecture/pitfall/verify_decision/session_context/workaround/release_note/node_status/pattern) + Claude 自动记忆迁移到 dbservice
```

## L8 — Workflow 功能层（P3，依赖 L4+L5）

```
L8.1  import-graph 状态同步  [impl:done] [verify:pending] v4.0
      deps:[L4.8, L4.15]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[]
      description: 导入时解析 [verify:pass/T2-pass] 标记，同步到 DB（非 pending 的覆盖已有 pending 状态）

L8.2  Agent 友好错误信息  [impl:done] [verify:pending] v4.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/server.py, agent/governance/models.py]
      secondary:[]
      test:[]
      description: verify-update 缺字段/类型错误返回示例 JSON；evidence 字符串返回正确格式提示

L8.3  Release Profile  [impl:done] [verify:pending] v4.0
      deps:[L4.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/state_service.py]
      secondary:[]
      test:[]
      description: 命名 profile (full/hotfix/foundation/governance) + scope 过滤 + min_status 策略

L8.4  Token Service (双令牌 API)  [impl:done] [verify:pending] v4.0
      deps:[L5.3]
      gate_mode: explicit
      gates:[L5.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/token_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: POST /api/token/refresh|revoke|rotate 端点

L8.5  Quickstart 文档 API  [impl:done] [verify:pending] v4.0
      deps:[L4.15]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      description: GET /api/docs/* 返回 overview/quickstart/endpoints/workflow_rules/memory_guide/telegram_integration
```

## L9 — 流程保障层（Gate）

```
L9.1  Feature Coverage Check  [impl:pending] [verify:pending] v4.0
      deps:[L4.9, L4.15]
      gate_mode: explicit
      gates:[L4.9]
      verify: L4
      test_coverage: none
      primary:[agent/governance/coverage_check.py, agent/governance/server.py]
      secondary:[]
      test:[]
      description: release-gate 时检查 git diff 变更文件是否都有对应验收节点。无节点覆盖的文件 → 告警/阻断发布

L9.2  Node-Before-Code Gate  [impl:pending] [verify:pending] v4.0
      deps:[L9.1]
      gate_mode: explicit
      gates:[L9.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      description: verify-update 时检查提交的 evidence 中 changed_files 是否都被某个 node 的 primary/secondary 覆盖

L9.3  Artifacts 约束检查  [impl:pending] [verify:pending] v4.0
      deps:[L4.8, L4.15, L8.5]
      gate_mode: explicit
      gates:[L4.8, L8.5]
      verify: L4
      test_coverage: none
      primary:[agent/governance/artifacts.py, agent/governance/state_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: 节点 qa_pass 时自动检查配套工件(api_docs/changelog/test)是否完成。工件缺失 → 拒绝验收

L9.4  节点创建自动文档骨架  [impl:pending] [verify:pending] v4.0
      deps:[L9.3, L4.13]
      gate_mode: explicit
      gates:[L9.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/doc_generator.py, agent/governance/event_bus.py]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: 监听 node.created 事件，扫描 primary files 中的 @route 端点，自动生成 api_docs 骨架。qa_pass 时要求骨架已补充为完整文档

L9.5  Gatekeeper Coverage 校验  [impl:done] [verify:pending] v4.0
      deps:[L9.1, L4.8]
      gate_mode: explicit
      gates:[L9.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/gatekeeper.py, agent/governance/state_service.py, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: gatekeeper
      description: release-gate 自动检查最近一次 coverage-check 是否通过。未跑或 pass=false → 阻断发布。结果存 SQLite gatekeeper_checks 表

L9.6  Artifacts 自动推断  [impl:done] [verify:pending] v4.0
      deps:[L9.3]
      gate_mode: explicit
      gates:[L9.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/artifacts.py]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: coverage_check
      description: 节点无 artifacts 声明时自动推断：primary 有 @route → 要求 api_docs，有 test 声明 → 要求 test_file

L9.7  Deploy 前置 Coverage-Check  [impl:pending] [verify:pending] v4.0
      deps:[L9.5, L4.17]
      gate_mode: explicit
      gates:[L9.5]
      verify: L4
      test_coverage: none
      primary:[deploy-governance.sh]
      secondary:[]
      test:[]
      description: deploy 脚本自动跑 coverage-check，不通过不允许部署。堵住"改代码直接 docker build 绕过 workflow"的漏洞

L9.8  记忆写入检查  [impl:pending] [verify:pending] v4.0
      deps:[L7.3, L9.5]
      gate_mode: explicit
      gates:[L7.3]
      verify: L4
      test_coverage: none
      primary:[scripts/verify_loop.sh, agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: gatekeeper
      description: verify_loop 检查本次变更是否有新记忆写入 dbservice。git diff 有代码改动但 dbservice 最近无新记忆 → 告警提醒写入

L9.9  Scheduled Task 管理  [impl:done] [verify:pending] v4.0
      deps:[L6.3, L9.7]
      gate_mode: explicit
      gates:[L6.3]
      verify: L4
      verify_mode: manual
      test_coverage: none
      primary:[scripts/task-templates/telegram-handler.md]
      secondary:[docs/human-intervention-guide.md, docs/scheduled-task-design.md]
      test:[]
      description: Task prompt 模板存项目 git 跟踪。含人工介入流程：危险操作通知人类确认，验收需人工发消息测试

L9.10  Token 模型简化  [impl:pending] [verify:pending] v5.0
      deps:[L5.3, L4.7]
      gate_mode: explicit
      gates:[L5.3]
      verify: L4
      test_coverage: none
      primary:[agent/governance/role_service.py, agent/governance/server.py]
      secondary:[agent/governance/token_service.py]
      test:[]
      artifacts:
        - type: api_docs
          section: token_model
      description: project_token 不过期取代 refresh+access 双令牌。废弃 /api/token/refresh 和 /api/token/rotate。保留 revoke + agent_token 24h TTL

L9.11  Gateway Token 代理  [impl:pending] [verify:pending] v5.0
      deps:[L9.10, L5.1]
      gate_mode: explicit
      gates:[L9.10]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: Gateway 持有 project_token 代理所有 API 调用。CLI session 只需 project_id 不需要自己管 token
```

## L10 — Runtime 层（v5 P0，依赖 L7+L9）

```
L10.1  Task Registry 双字段状态机  [impl:pending] [verify:pending] v5.0
      deps:[L7.4]
      gate_mode: explicit
      gates:[L7.4]
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/governance/db.py]
      secondary:[]
      test:[]
      description: execution_status (queued/claimed/running/succeeded/failed/...) + notification_status (none/pending/sent) 双字段分离。DB migration v2→v3

L10.2  文件投递原子化  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L4.17]
      gate_mode: explicit
      gates:[L10.1]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: DB先→文件后(tmp+fsync+rename)。Claim 带 fencing token。启动恢复扫盘

L10.3  Executor 通知持久化  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: explicit
      gates:[L10.2]
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/backends.py]
      secondary:[]
      test:[]
      description: 执行完写 execution_status=succeeded + notification_status=pending。Pub/Sub 加速但不依赖

L10.4  Gateway 通知可补发  [impl:pending] [verify:pending] v5.0
      deps:[L10.3]
      gate_mode: explicit
      gates:[L10.3]
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: Gateway 每次 poll Telegram 时查 notification_status=pending 的任务并发送通知。Pub/Sub 为加速通道

L10.5  取消/重试/超时  [impl:pending] [verify:pending] v5.0
      deps:[L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/governance/task_registry.py, agent/executor.py]
      secondary:[agent/governance/server.py]
      test:[]
      description: cancel API + failed 自动重排队(attempt<max) + timeout 检测(lease 过期)

L10.6  进度 Heartbeat  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/governance/server.py]
      test:[]
      description: Executor 定期上报 phase(planning/coding/testing/reviewing/finalizing) + percent + message

L10.7  PID 追踪 + Orphan 扫盘  [impl:pending] [verify:pending] v5.0
      deps:[L10.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: 记录 worker_pid 到 Task Registry。启动时扫 processing/ + DB stale tasks，kill 孤儿进程，重排队

L10.8  Runtime 投影 API  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L10.3]
      gate_mode: explicit
      gates:[L10.1]
      verify: L4
      test_coverage: none
      primary:[agent/governance/server.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: task_registry
      description: GET /api/runtime/{pid} 只读 Task Registry 投影视图(active/queued/pending_notify)。不存自己的状态
```

## L11 — 交互体验层（v5 P1，依赖 L10）

```
L11.1  消息分类器 (两段式)  [impl:pending] [verify:pending] v5.0
      deps:[L10.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: 第一层规则快速拦截(命令/危险/查询)，第二层关键词兜底(后续接LLM)

L11.2  /menu 运行时状态  [impl:pending] [verify:pending] v5.0
      deps:[L10.8, L11.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: menu 显示当前项目运行中任务数、排队数、未读通知。各项目按钮显示节点通过率

L11.3  项目切换 context 保存/加载  [impl:pending] [verify:pending] v5.0
      deps:[L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: /bind 切换项目时自动保存旧项目context、加载新项目context

L11.4  通知归属 chat_id  [impl:pending] [verify:pending] v5.0
      deps:[L10.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      description: 任务完成通知发回创建时的 chat_id 而不是当前绑定项目。Gateway poll 时查 notification_status=pending

L11.5  Gateway Token 代理集成  [impl:done] [verify:pending] v5.0
      deps:[L9.11]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: handle_message 中查询类消息用 gov_api_for_chat 自动使用绑定的 project_token
```

## L12 — Executor 集成层（v5，依赖 L10, L3）

```
L12.1  Executor Task Registry 集成  [impl:pending] [verify:pending] v5.0
      deps:[L10.1, L10.2, L3.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      description: pick_pending_task 时调 Task Registry claim (DB insert queued→claimed→running)。完成时调 complete。双字段状态 execution_status + notification_status

L12.2  Executor 原子投递  [impl:pending] [verify:pending] v5.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      description: task 文件写入先 .tmp 后 rename。Executor 只扫正式 .json 文件。启动时扫 processing/ 恢复 stale 任务

L12.3  Executor Redis 通知  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L5.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: 任务完成后 redis.publish task:completed。同时写 Task Registry succeeded + notification_status=pending

L12.4  Executor heartbeat + 进度上报  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L10.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: heartbeat 线程定期上报 phase(planning/coding/testing) + percent。写入 Task Registry metadata

L12.5  Executor 启动恢复  [impl:pending] [verify:pending] v5.0
      deps:[L12.1, L10.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: 启动时扫 processing/ 和 DB 中 claimed/running 且 lease 过期的任务。kill 孤儿进程，重排队或标记 failed

L12.6  Tool Policy 策略  [impl:pending] [verify:pending] v5.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: auto_allow/needs_approval/always_deny 命令策略。workspace allowlist 限制。危险操作需人工确认
```

## L13 — 部署检测层（v5，依赖 L9, L12）

```
L13.1  Pre-Deploy 检测脚本  [impl:pending] [verify:pending] v5.0
      deps:[L9.5, L9.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[deploy-governance.sh]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: 部署前自动检测：verify_loop全绿、coverage-check pass、所有新节点qa_pass、文档已更新、记忆已写入

L13.2  Staging 环境自动验证  [impl:pending] [verify:pending] v5.0
      deps:[L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[docker-compose.governance.yml]
      test:[]
      description: 启动 staging 容器(40007)，跑 health check + smoke test + API 端点验证，通过后才允许切换

L13.3  Dev/Prod 配置一致性检查  [impl:pending] [verify:pending] v5.0
      deps:[L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[]
      test:[]
      description: 对比 dev/prod 环境变量、端口映射、volume 挂载是否一致。检测漏配/错配

L13.4  Gateway 消息通道验证  [impl:pending] [verify:pending] v5.0
      deps:[L13.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[]
      test:[]
      description: 部署后自动发测试消息到 Telegram 验证 Gateway 通道正常

L13.5  Deploy 集成到 workflow  [impl:pending] [verify:pending] v5.0
      deps:[L13.1, L13.2, L13.3, L13.4]
      gate_mode: explicit
      gates:[L13.1, L13.2, L13.3, L13.4]
      verify: L4
      test_coverage: none
      primary:[deploy-governance.sh]
      secondary:[]
      test:[]
      description: deploy-governance.sh 调 pre-deploy-check.sh 作为前置步骤。检测不通过则阻止部署

L13.6  端到端任务执行测试  [impl:pending] [verify:pending] v5.0
      deps:[L13.1, L12.1, L12.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/e2e-task-test.sh]
      secondary:[scripts/pre-deploy-check.sh]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: E2E测试：Gateway写task文件→Executor消费→结果写回→通知。验证Docker volume绑定、文件跨容器可见、executor claim+complete链路

L13.7  Volume 挂载一致性检查  [impl:pending] [verify:pending] v5.0
      deps:[L13.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/pre-deploy-check.sh]
      secondary:[docker-compose.governance.yml]
      test:[]
      description: 检查Gateway的task-data volume是bind mount到宿主机shared-volume而不是Docker volume。防止任务文件跨容器不可见
```

## L14 — Coordinator 对话层 + Orphan 治理（v5.1，依赖 L11, L12）

```
L14.1  Gateway 消息转发重构  [impl:pending] [verify:pending] v5.1
      deps:[L11.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: telegram_integration
      description: 去掉消息分类器的task直接派发。非命令消息全部转发给Coordinator处理。Gateway只做收发不做决策

L14.2  Coordinator CLI 触发器  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, scripts/coordinator_session.py]
      secondary:[]
      test:[]
      description: Gateway收到非命令消息→启动claude CLI session(带项目context+记忆)→处理消息→回复→退出。Coordinator决定是否派task

L14.3  Coordinator Context 注入  [impl:pending] [verify:pending] v5.1
      deps:[L14.2, L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/coordinator_session.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: Coordinator session启动时自动加载：项目context、governance状态、dbservice记忆、当前活跃任务。组装成system prompt

L14.4  Executor Orphan 巡检  [impl:pending] [verify:pending] v5.1
      deps:[L12.5, L5.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor定期(60s)查/api/agent/orphans，找到orphan→检查PID→kill僵尸进程→重排队task→POST /api/agent/cleanup

L14.5  Executor Lease 集成  [impl:pending] [verify:pending] v5.1
      deps:[L14.4, L5.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor启动时register lease。执行task时heartbeat续期(带PID)。完成/崩溃时deregister。Lease过期→标记orphan

L14.6  Task 权限隔离  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L14.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: 只有Coordinator能创建task。Gateway不再直接创建task文件。Executor验证task来源是coordinator角色

L14.7  v5架构文档修正  [impl:pending] [verify:pending] v5.1
      deps:[L14.1, L14.2, L14.3]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[docs/architecture-v5-runtime.md]
      secondary:[]
      test:[]
      description: 修正v5文档中Gateway直接派发task的错误设计。明确Coordinator在消息流中的对话+决策+编排角色

L14.8  Coordinator 宿主机代理  [impl:pending] [verify:pending] v5.1
      deps:[L14.2, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: telegram_integration
      description: Gateway通过task文件触发宿主机Executor执行coordinator_chat任务。Executor区分dev_task(写代码)和coordinator_chat(对话决策，stdout作为回复)。解决Docker容器无法直接调用宿主机claude CLI的问题
```

## L15 — Executor 驱动架构 v6 P0（依赖 L12, L14）

```
L15.1  AILifecycleManager  [impl:pending] [verify:pending] v6.0
      deps:[L12.1, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/ai_lifecycle.py]
      secondary:[]
      test:[]
      description: AI进程统一管理。create_session(role,context,prompt)→启动CLI→monitor PID→collect output→kill/cleanup。AI不能自启AI

L15.2  AI输出解析器  [impl:pending] [verify:pending] v6.0
      deps:[L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/ai_output_parser.py]
      secondary:[]
      test:[]
      description: 从Claude stdout提取结构化JSON。schema_version校验。支持AI输出混杂文本+JSON的情况

L15.3  角色权限矩阵  [impl:pending] [verify:pending] v6.0
      deps:[]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py]
      secondary:[]
      test:[]
      description: 硬编码角色权限。coordinator:create_task/reply/archive。dev:modify_code/run_tests。tester:verify(testing/t2_pass)。qa:verify(qa_pass)

L15.4  验收图约束校验器  [impl:pending] [verify:pending] v6.0
      deps:[L9.1, L9.5]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/graph_validator.py]
      secondary:[]
      test:[]
      description: Executor代码拉取验收图缓存(带version CAS)。强制执行:文件覆盖率/依赖满足/gate策略/角色验证级别/artifacts完整/新文件建节点

L15.5  证据独立采集器  [impl:pending] [verify:pending] v6.0
      deps:[L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/evidence_collector.py]
      secondary:[]
      test:[]
      description: Executor独立采集事实证据(git diff/pytest/file stat)。不信AI自报的changed_files和test_results。Evidence分decision(AI)和fact(代码)

L15.6  任务状态机  [impl:pending] [verify:pending] v6.0
      deps:[L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_state_machine.py]
      secondary:[]
      test:[]
      description: 显式TaskStatus枚举(created/queued/claimed/running/waiting_retry/waiting_human/blocked_by_dep/succeeded/failed_retryable/failed_terminal/eval_pending/eval_approved/eval_rejected/cancelled/archived)。VALID_TRANSITIONS转换规则

L15.7  4层分层校验器  [impl:pending] [verify:pending] v6.0
      deps:[L15.2, L15.3, L15.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/decision_validator.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: SchemaValidator→PolicyValidator→GraphValidator→ExecutionPreconditionValidator。每层独立返回{layer,passed,errors[]}。含错误分类重试策略(5类)

L15.8  预算化上下文组装器  [impl:pending] [verify:pending] v6.0
      deps:[L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/context_assembler.py]
      secondary:[]
      test:[]
      description: 按角色预算组装上下文(coordinator 8k/dev 4k/tester 3k/qa 3k)。分层:hard_context→conversation→memory→runtime。超预算截断

L15.9  任务编排器  [impl:pending] [verify:pending] v6.0
      deps:[L15.1, L15.7, L15.8, L15.5, L15.6]
      gate_mode: explicit
      gates:[L15.1, L15.7, L15.8]
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/executor.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: handle_user_message(组装context→启动Coordinator AI→校验决策→执行action→回复→更新context)。代码控制全流程，AI只输出决策JSON
```

## L16 — v6 P1 闭环链路（依赖 L15）

```
L16.1  Dev完成→证据校验集成  [impl:pending] [verify:pending] v6.0
      deps:[L15.5, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/evidence_collector.py]
      test:[]
      description: Executor dev_task完成后调evidence_collector独立采集(git diff/pytest)。对比AI自报→记录差异→传给eval

L16.2  Coordinator eval自动触发  [impl:pending] [verify:pending] v6.0
      deps:[L16.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: dev_task succeeded→Executor代码自动创建coordinator_eval task→Coordinator评估dev结果→决定下一步→回复用户

L16.3  错误分类重试集成  [impl:pending] [verify:pending] v6.0
      deps:[L15.7, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: 校验失败时分类错误(retryable_model/retryable_env/blocked_by_dep/non_retryable/needs_human)→按策略重试或终止或人工介入

L16.4  对话历史持久化  [impl:pending] [verify:pending] v6.0
      deps:[L15.8, L6.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/context_assembler.py]
      secondary:[agent/governance/session_context.py]
      test:[]
      description: 每条消息+回复写入session_context。新session启动时加载最近10条对话历史。跨消息上下文连续

L16.5  记忆写入治理  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/memory_write_guard.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: 写入前检查:去重(相似度>0.85)、可信度(>0.6)、来源(qa_pass才写长期decision)、TTL(workaround 30天)。防止污染长期记忆

L16.6  自动归档集成  [impl:pending] [verify:pending] v6.0
      deps:[L16.1, L16.5]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/memory_write_guard.py]
      test:[]
      description: 任务完成→自动归档:决策写长期记忆(经治理检查)、dev摘要写pattern、上下文过期归档

L16.7  propose_node 校验集成  [impl:pending] [verify:pending] v6.0
      deps:[L15.4, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/graph_validator.py]
      secondary:[]
      test:[]
      description: Coordinator输出propose_node action→graph_validator校验(ID/唯一性/依赖/无环/路径安全)→通过则调governance API创建

L16.8  任务DB化  [impl:pending] [verify:pending] v6.0
      deps:[L15.6, L10.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/governance/task_registry.py]
      test:[]
      artifacts:
        - type: api_docs
          section: task_registry
      description: task_orchestrator写task前先DB insert(source of truth)再写文件(secondary)。Executor claim时更新DB状态。全生命周期DB驱动
```

## L17 — v6 P2 增强（依赖 L15, L16）

```
L17.1  执行沙箱  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/execution_sandbox.py]
      secondary:[agent/executor.py]
      test:[]
      description: Dev/Test命令跑在隔离工作目录。命令白名单+参数约束。workspace overlay。高危命令人工确认

L17.2  多角色并行  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: TaskOrchestrator支持同时运行dev+tester AI session。AILifecycleManager并发session管理。lease互斥保护

L17.3  任务依赖链  [impl:pending] [verify:pending] v6.0
      deps:[L16.2, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: dev完成→自动创建tester task→tester完成→自动创建qa task。parent_task_id链接。blocked_by_dep状态管理

L17.4  人工审批对象  [impl:pending] [verify:pending] v6.0
      deps:[L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/approval_manager.py]
      secondary:[agent/task_orchestrator.py, agent/telegram_gateway/gateway.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: 敏感操作创建approval对象(approval_id/action/risk/expires)。Telegram按钮确认。approved_by/scope记录

L17.5  Plan层  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L16.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: 复杂请求先生成plan对象(plan下挂多个task)。plan审批后按序执行。支持恢复/可视化/审计

L17.6  观测性 trace+replay  [impl:pending] [verify:pending] v6.0
      deps:[L15.9, L15.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/observability.py]
      secondary:[agent/task_orchestrator.py, agent/ai_lifecycle.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: trace_id串联全链路(message→coordinator→dev→eval→reply)。记录原始prompt/context/AI输出/validator决策/执行日志。支持replay调试

L17.7  PM角色集成  [impl:done] [verify:pending] v6.1
      deps:[L15.3, L15.8, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py, agent/task_orchestrator.py, agent/context_assembler.py]
      secondary:[docs/architecture-v6-executor-driven.md]
      test:[]
      artifacts:
        - type: api_docs
          section: executor
      description: PM角色:需求分析→PRD→节点设计。权限(generate_prd/design_nodes/propose_node)。TaskOrchestrator自动检测新功能请求→启动PM session→PRD传给Coordinator编排
```

## L18 — Session 介入层（v6.1，依赖 L15, L17）

```
L18.1  Executor HTTP API Server  [impl:pending] [verify:pending] v6.1
      deps:[L15.9, L15.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/executor.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: Executor内嵌HTTP server(:40100)与task loop并行。提供监控/介入/调试接口。Claude Code session通过curl直接操作

L18.2  监控接口  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.1, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      description: GET /status(整体状态) /sessions(AI进程列表) /tasks(任务队列) /trace/{id}(链路详情) /task/{id}(单任务详情+evidence+validator日志)

L18.3  介入接口  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.1, L15.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: POST /task/{id}/pause /task/{id}/cancel /task/{id}/retry /cleanup-orphans。支持暂停/取消/重试任务和清理僵尸进程

L18.4  直接对话接口  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: POST /coordinator/chat(绕过Telegram直接启动Coordinator session)。支持同步等待回复。开发者终端调试入口

L18.5  调试接口  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L15.7, L15.8, L17.6]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      description: GET /validator/last-result /context/{pid} /ai-session/{id}/output。查看validator决策详情、context组装结果、AI原始输出

L18.6  接入文档  [impl:pending] [verify:pending] v6.1
      deps:[L18.1, L18.2, L18.3, L18.4, L18.5]
      gate_mode: explicit
      gates:[L18.2, L18.3, L18.4, L18.5]
      verify: L4
      test_coverage: none
      primary:[docs/executor-api-guide.md]
      secondary:[agent/governance/server.py]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: 完整接入文档：所有端点说明、请求/响应示例、Claude Code session使用指南、常用调试命令
```

## L19 — 生产链路补全（v6.2，依赖 L15, L18）

```
L19.1  上下文持久化修复  [impl:pending] [verify:pending] v6.2
      deps:[L15.8, L15.9, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[agent/context_assembler.py]
      test:[]
      description: process_coordinator_chat改为调TaskOrchestrator.handle_user_message。对话历史正确保存(user msg+coordinator reply)到session_context。ContextAssembler加载最近10条注入prompt

L19.2  Dev分支工作流  [impl:pending] [verify:pending] v6.2
      deps:[L15.9, L12.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Dev task创建时自动git checkout -b dev/task-{id}。完成后不合并，等人工审核。Coordinator eval报告分支名和diff。merge由人工触发

L19.3  Telegram markdown转义  [impl:pending] [verify:pending] v6.2
      deps:[L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/telegram_gateway/gateway.py, agent/executor.py]
      secondary:[]
      test:[]
      description: 发送Telegram消息前转义MarkdownV2特殊字符(_*[]()~`>#+-=|{}.!)。或改用纯文本模式避免格式问题

L19.4  转译格式标准化  [impl:pending] [verify:pending] v6.2
      deps:[L18.1, L18.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor_api.py]
      secondary:[]
      test:[]
      artifacts:
        - type: api_docs
          section: executor_api
      description: /coordinator/chat响应增加structured字段:reply(给用户看)+actions_summary(操作摘要)+status(成功/失败/需确认)+next_step(下一步建议)。便于终端角色转译

L19.5  审核→合并→部署链路  [impl:pending] [verify:pending] v6.2
      deps:[L19.2, L13.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/merge-and-deploy.sh]
      secondary:[agent/executor_api.py]
      test:[]
      artifacts:
        - type: api_docs
          section: deployment
      description: POST /merge(审核通过后合并dev分支到main)→pre-deploy-check→deploy。完整的审核→合并→验收→部署链路
```

## L20 — Dev Task v6 链路集成（v6.2，依赖 L15, L16, L19）

```
L20.1  Dev task 走 v6 执行链路  [impl:pending] [verify:pending] v6.2
      deps:[L15.1, L15.5, L15.7, L19.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/ai_lifecycle.py, agent/evidence_collector.py, agent/decision_validator.py]
      test:[]
      description: dev_task不再走旧process_claude，改为AILifecycleManager启动dev session→结构化输出→DecisionValidator校验→EvidenceCollector独立采集→git分支工作

L20.2  Dev 完成自动触发 Coordinator eval  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L16.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: dev_task succeeded后Executor代码自动创建coordinator_eval task(含独立采集的evidence)。Coordinator评估结果→决定下一步→回复用户

L20.3  E2E Dev链路验证  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L20.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[scripts/e2e-dev-chain-test.sh]
      secondary:[]
      test:[]
      description: 端到端测试：Coordinator派dev task→Dev在分支改代码→证据采集→Coordinator eval→回复。验证完整v6 dev链路

L20.4  Dev task chat_id 注入  [impl:pending] [verify:pending] v6.2
      deps:[L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[]
      test:[]
      description: Coordinator创建dev task时自动注入chat_id到task文件。Executor process_dev_task_v6不再因KeyError崩溃。完成通知发回原chat

L20.5  任务重试限制 (max_retry+dead_letter)  [impl:pending] [verify:pending] v6.2
      deps:[L15.6, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/task_state_machine.py]
      test:[]
      description: task失败时检查attempt_count。超过max_retry(默认3)→移到dead_letter目录→标记failed_terminal→不再重试。防止无限循环

L20.6  Orphan 进程实际清理  [impl:pending] [verify:pending] v6.2
      deps:[L14.4, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor巡检时检查processing/里的task→读worker_pid→检查进程是否存活→死进程的task重排队或标记failed→清理stale文件

L20.7  通知改 Gateway API  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L14.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[]
      test:[]
      description: Executor所有通知改为调Gateway API(POST /gateway/reply)而非直接send_text。统一通知通道。Gateway处理markdown转义

L20.8  Dev→Tester→QA→Gatekeeper 自动触发链  [impl:pending] [verify:pending] v6.2
      deps:[L16.2, L17.3, L20.2]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py, agent/executor.py]
      secondary:[agent/governance/gatekeeper.py]
      test:[]
      description: Dev完成→eval通过→Executor代码创建test_task→Tester完成→创建qa_task→QA完成→触发Gatekeeper检查→通知用户审批。全链路代码驱动不靠AI

L20.9  AI 任务日志系统  [impl:pending] [verify:pending] v6.2
      deps:[L15.1, L20.1]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/ai_lifecycle.py]
      secondary:[]
      test:[]
      description: 每个task创建独立日志目录shared-volume/codex-tasks/logs/task-xxx/。记录:prompt.txt(输入)、stdout.txt(AI输出)、evidence.json(证据)、validator.json(校验结果)、timeline.jsonl(时间线)。观察者可实时tail

L20.10  Dev task 统一走 v6 链路  [impl:pending] [verify:pending] v6.2
      deps:[L20.1, L15.9]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py]
      secondary:[agent/task_orchestrator.py]
      test:[]
      description: 所有dev_task统一走process_dev_task_v6而非旧process_claude。修复parallel dispatcher走旧路径的问题。完成后调handle_dev_complete触发自动链

L20.11  PM 日志可观测  [impl:pending] [verify:pending] v6.2
      deps:[L20.9, L17.7]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/task_orchestrator.py]
      secondary:[agent/ai_lifecycle.py]
      test:[]
      description: PM session执行时写日志到logs/目录。失败时记录原因。handle_user_message增加PM执行日志方便观察者排查

L20.12  Chain Depth 限制  [impl:pending] [verify:pending] v6.2
      deps:[L20.2, L20.8]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/executor.py, agent/task_orchestrator.py]
      secondary:[]
      test:[]
      description: 防止eval→dev无限循环。task文件携带_chain_depth字段。_trigger_coordinator_eval读取depth,>=3则停止不创建新task。_write_task_file传递parent depth+1。_trigger_tester/_trigger_qa同样继承depth

L20.13  记忆删除审核  [impl:pending] [verify:pending] v6.2
      deps:[L15.3, L17.4]
      gate_mode: auto
      verify: L4
      test_coverage: none
      primary:[agent/role_permissions.py, agent/task_orchestrator.py]
      secondary:[agent/memory_write_guard.py]
      test:[]
      description: Dev不能直接删除记忆,只能propose_memory_cleanup。Executor拦截delete操作→创建approval→QA审核后执行。amingclaw:arch和pitfall前缀需人工批准
```
