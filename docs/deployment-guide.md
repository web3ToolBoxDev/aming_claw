# Aming Claw 部署指南 — 开发→生产切换

## 一、服务架构

```
Docker 容器 (docker-compose.governance.yml)
├── nginx          :40000  反代
├── governance     :40006  规则引擎
├── telegram-gw    :40010  消息网关
├── dbservice      :40002  记忆服务
└── redis          :40079  缓存/队列

宿主机
└── executor       :39101  任务执行 (Claude/Codex CLI)
```

## 二、完整部署流程

### 2.1 首次部署

```bash
cd C:\Users\z5866\Documents\amingclaw\aming_claw

# 1. 启动所有 Docker 服务
docker compose -f docker-compose.governance.yml up -d

# 2. 等待所有服务 healthy
docker compose -f docker-compose.governance.yml ps

# 3. 注册 dbservice domain pack (不持久化，每次重启需要)
curl -s -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain":"development","types":{"architecture":{"durability":"permanent","conflictPolicy":"replace","description":"Architecture decisions"},"pitfall":{"durability":"permanent","conflictPolicy":"append","description":"Known pitfalls"},"pattern":{"durability":"permanent","conflictPolicy":"replace","description":"Code patterns"},"workaround":{"durability":"durable","conflictPolicy":"replace","description":"Workarounds"},"session_summary":{"durability":"durable","conflictPolicy":"replace","description":"Session summaries"},"verify_decision":{"durability":"permanent","conflictPolicy":"append","description":"Verify decisions"}}}'

# 4. 初始化项目 (首次)
python init_project.py
# 输入项目名和密码 → 拿到 coordinator token

# 5. 导入验收图
curl -X POST http://localhost:40000/api/wf/{project_id}/import-graph \
  -H "X-Gov-Token: {token}" \
  -d '{"md_path":"/workspace/docs/aming-claw-acceptance-graph.md"}'

# 6. 启动宿主机 Executor
cd agent && python -m executor &

# 7. Telegram 绑定
# 在 Telegram 给 bot 发: /bind {coordinator_token}
```

### 2.2 代码更新部署 (日常)

```bash
# 方式 A: 快速部署 (5-10s 停机，Agent 自动重试)
docker compose -f docker-compose.governance.yml up -d --build
docker compose -f docker-compose.governance.yml restart nginx

# 方式 B: 零停机部署 (通过脚本)
GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh

# 方式 C: 只更新单个服务
docker compose -f docker-compose.governance.yml up -d --build governance
docker compose -f docker-compose.governance.yml up -d --build telegram-gateway
```

### 2.3 开发环境 → 生产环境切换

```
开发流程:
  1. 修改代码
  2. 建验收节点 (如果是新功能)
  3. 导入验收图
  4. 运行测试
  5. 跑 coverage-check
  6. verify-update (testing → t2_pass → qa_pass)
  7. verify_loop 自检
  8. 部署

部署检查清单:
  □ verify_loop 全绿 (7/7 pass)
  □ coverage-check pass
  □ 所有新节点 qa_pass
  □ 文档已更新 (/api/docs)
  □ 记忆已写入 (dbservice)
  □ git commit
```

## 三、重启恢复清单

电脑重启后需要恢复的步骤：

```bash
# 1. 启动 Docker 服务
cd C:\Users\z5866\Documents\amingclaw\aming_claw
docker compose -f docker-compose.governance.yml up -d

# 2. 等待 healthy
docker compose -f docker-compose.governance.yml ps
# 确认所有服务 healthy

# 3. 重启 nginx (解决 upstream 解析问题)
docker compose -f docker-compose.governance.yml restart nginx

# 4. 注册 dbservice domain pack
curl -s -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain":"development","types":{"architecture":{"durability":"permanent","conflictPolicy":"replace","description":"Architecture decisions"},"pitfall":{"durability":"permanent","conflictPolicy":"append","description":"Known pitfalls"},"pattern":{"durability":"permanent","conflictPolicy":"replace","description":"Code patterns"},"workaround":{"durability":"durable","conflictPolicy":"replace","description":"Workarounds"},"session_summary":{"durability":"durable","conflictPolicy":"replace","description":"Session summaries"},"verify_decision":{"durability":"permanent","conflictPolicy":"append","description":"Verify decisions"}}}'

# 5. 启动宿主机 Executor
cd agent && python -m executor &

# 6. 验证
curl -s http://localhost:40000/api/health     # governance
curl -s http://localhost:40002/health          # dbservice
curl -s http://localhost:40000/nginx-health    # nginx
```

## 四、数据持久化

| 数据 | 位置 | 重启后 |
|------|------|--------|
| 项目/节点状态 | Docker volume: governance-data (SQLite) | ✅ 保留 |
| DAG 图 | Docker volume: governance-data (graph.json) | ✅ 保留 |
| 审计日志 | Docker volume: governance-data (JSONL) | ✅ 保留 |
| 记忆数据 | Docker volume: memory-data (SQLite) | ✅ 保留 |
| Redis 缓存 | Docker volume: redis-data (AOF) | ✅ 保留 |
| Coordinator token | 不过期 | ✅ 有效 |
| **dbservice domain pack** | **内存** | **❌ 需要重新注册** |
| **Executor 进程** | **宿主机** | **❌ 需要手动启动** |
| **Telegram chat route** | **Redis** | **✅ 保留 (AOF)** |

## 五、回滚

```bash
# 回滚到上一个版本
docker tag aming_claw-governance:rollback aming_claw-governance:latest
docker compose -f docker-compose.governance.yml up -d governance
docker compose -f docker-compose.governance.yml restart nginx

# 查看回滚审计
curl -s http://localhost:40000/api/audit/amingClaw/log?limit=10
```

## 六、监控

```bash
# 服务健康
curl http://localhost:40000/api/health
curl http://localhost:40000/nginx-health
curl http://localhost:40002/health

# 节点状态
curl http://localhost:40000/api/wf/amingClaw/summary -H "X-Gov-Token: {token}"

# 运行时
curl http://localhost:40000/api/runtime/amingClaw -H "X-Gov-Token: {token}"

# 审计日志
curl http://localhost:40000/api/audit/amingClaw/log?limit=20 -H "X-Gov-Token: {token}"
```

## 七、Executor API (:40100)

Executor 在宿主机运行时暴露 HTTP API，支持监控和介入：

```
GET  /health              — 健康检查
GET  /status              — 运行状态 (pending/processing/sessions)
GET  /sessions            — 活跃 AI sessions
GET  /tasks               — 任务列表 (支持 ?project_id=&status= 过滤)
GET  /trace/{trace_id}    — 完整 trace 链路
GET  /traces              — 最近 trace 列表 (支持 ?project_id=&limit=)
POST /coordinator/chat    — 直接对话 Coordinator (绕过 Telegram)
POST /cleanup-orphans     — 清理僵尸 AI 进程
```

## 八、Git Worktree 工作流

Dev AI 在隔离 worktree 中工作，不影响主工作目录：

```bash
# Executor 自动执行:
git worktree add -b dev/task-xxx .worktrees/dev-task-xxx
# Dev AI 在 .worktrees/dev-task-xxx/ 内操作
# 完成后:
git worktree remove .worktrees/dev-task-xxx --force
# 分支保留: dev/task-xxx (供 review)

# 合并到 main:
git merge dev/task-xxx --no-ff
git branch -d dev/task-xxx
```

## 九、merge-and-deploy 流程

```bash
bash scripts/merge-and-deploy.sh dev/task-xxx

# 自动执行:
# 1. git merge dev/task-xxx → main
# 2. pre-deploy-check.sh (节点/coverage/docs/gatekeeper)
# 3. docker compose up -d --build
# 4. restart nginx
# 5. health check
# 6. sync governance data: prod → dev
# 7. restart executor
# 8. gateway 通知
```

## 十、已知问题

1. **dbservice domain pack 不持久化** — 容器重启后丢失，startup.sh 中自动注册。
2. **nginx healthcheck 偶尔 unhealthy** — 重启 nginx 解决。
3. **Executor 是宿主机进程** — 不在 Docker 里，需要手动管理生命周期。
4. **观察者和 Executor 并行操作** — 使用 git worktree 隔离，编辑前无需停 Executor。
