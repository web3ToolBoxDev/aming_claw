# Aming Claw 架构方案 v6 — Executor 驱动架构

> 核心原则：**代码管流程，AI 管决策。** 所有 AI 调用由 Executor 代码控制，AI 不能直接操作系统。
> AI 进程生命周期由 Executor 管理，Coordinator 指挥 Executor 分派上下文和记忆。
>
> v6.1 新增 PM 角色：需求分析→PRD→节点设计→Coordinator 编排→Dev 执行。完整的需求到交付链路。

## 零、角色全景（v6.1）

```
用户需求
    ↓
PM (需求分析 + PRD + 节点设计)
    ↓ PRD + proposed_nodes
Coordinator (编排 + 决策 + 回复用户)
    ↓ create_dev_task / create_test_task
Dev (编码) → Tester (测试) → QA (验收)
    ↓ eval_complete
Coordinator (评估结果 + 归档)
    ↓
用户收到回复
```

| 角色 | 输入 | 输出 | 不能做 |
|------|------|------|--------|
| **PM** | 用户需求 | PRD + 节点提议 + 工作量评估 | 写代码、执行命令、创建任务、验证节点 |
| **Coordinator** | PRD + 项目状态 | 任务编排 + 回复用户 | 写代码、需求分析 |
| **Dev** | 任务 prompt | 代码修改 + 测试 | 和用户对话、创建任务 |
| **Tester** | 代码变更 | 测试报告 + verify(testing/t2_pass) | 修改代码、创建任务 |
| **QA** | 测试结果 | 验收决策 + verify(qa_pass) | 修改代码、运行测试 |

## 一、v5.1 → v6 核心变更

| 维度 | v5.1 | v6 | 原因 |
|------|------|----|------|
| AI 调用 | Gateway/AI 自己调 CLI | **Executor 统一调 CLI** | 代码控制 = 可校验、可拦截、可重试 |
| AI 输出 | 自由文本 + 直接操作 API | **结构化决策 JSON** | 代码可解析、可验证 |
| 任务创建 | Coordinator AI 调 API 创建 | **Coordinator 输出决策 → Executor 代码创建** | AI 不直接操作系统 |
| AI 生命周期 | 无统一管理 | **Executor AI Lifecycle Manager** | 统一启动/监控/kill/回收 |
| 结果评估 | Gateway 直接发用户 | **Executor 自动创建 Coordinator eval** | 决策闭环 |
| 记忆归档 | 手动 | **任务完成自动触发** | 不遗漏 |
| 越权检查 | verify_loop 事后检查 | **Executor 代码实时拦截** | 前置防御 |

## 二、系统全景

```
┌─────────────────────────────────────────────────────────────────┐
│                        人类用户 (Telegram)                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 消息
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │
                    └──────┬──────────────┘
                           │
              ┌────────────▼──────────────────────┐
              │       Telegram Gateway             │
              │       (Docker, 消息收发)            │
              │                                    │
              │  职责: 只做消息收发                  │
              │  收到非命令消息 → 写 task 文件       │
              │  收到通知 → 发 Telegram              │
              └────────────┬──────────────────────┘
                           │ task 文件 (shared-volume)
─ ─ ─ ─ ─ ─ Docker ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
                           │
              ┌────────────▼──────────────────────────────────────┐
              │            Executor (宿主机, 常驻)                  │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         AI Lifecycle Manager                  │  │
              │  │                                              │  │
              │  │  管理所有 AI 进程:                             │  │
              │  │    create_session(role, context, prompt)      │  │
              │  │    monitor_session(pid, timeout)              │  │
              │  │    kill_session(pid)                          │  │
              │  │    collect_output(pid) → 结构化 JSON          │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         Decision Validator                    │  │
              │  │                                              │  │
              │  │  校验 AI 输出的每个 action:                    │  │
              │  │    check_permission(role, action_type)        │  │
              │  │    check_node_exists(node_id)                 │  │
              │  │    check_coverage(target_files)               │  │
              │  │    check_tool_policy(command)                 │  │
              │  │    verify_evidence(evidence)                  │  │
              │  │    validate_workflow_step(current → next)     │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │         Task Orchestrator                     │  │
              │  │                                              │  │
              │  │  任务状态机 + 自动触发链:                       │  │
              │  │    user_message → coordinator                 │  │
              │  │    coordinator_decision → validate → execute  │  │
              │  │    dev_complete → coordinator_eval             │  │
              │  │    eval_complete → archive + notify            │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  │
              │  │Coord AI│  │Dev AI  │  │Test AI │  │QA AI   │  │
              │  │(决策)  │  │(编码)  │  │(测试)  │  │(验收)  │  │
              │  └────────┘  └────────┘  └────────┘  └────────┘  │
              └───────────────────────────────────────────────────┘
                           │
              ┌────────────▼──────────────────────┐
              │  Governance Service (Docker)        │
              │  规则引擎 + 状态 + 审计              │
              └────────────────────────────────────┘
              ┌────────────────────────────────────┐
              │  dbservice (Docker) — 记忆层        │
              └────────────────────────────────────┘
              ┌────────────────────────────────────┐
              │  Redis (Docker) — 缓存/队列/通知    │
              └────────────────────────────────────┘
```

## 三、核心组件设计

### 3.1 AI Lifecycle Manager

管理所有 AI 进程的启动、监控、回收。**AI 不能自己启动 AI。**

```python
class AILifecycleManager:
    """管理所有 AI 进程。代码控制，AI 不能自启。"""

    def __init__(self):
        self.sessions: dict[str, AISession] = {}

    def create_session(
        self,
        role: str,           # coordinator / dev / tester / qa
        prompt: str,         # AI 的输入
        context: dict,       # 注入的上下文
        project_id: str,
        timeout_sec: int = 120,
        output_format: str = "structured",  # structured / freeform
    ) -> AISession:
        """启动一个 AI CLI 进程。

        - 组装 system prompt (角色 + 上下文 + 输出格式要求)
        - 启动 claude CLI subprocess
        - 记录 PID 到 sessions 表
        - 返回 AISession 对象
        """

    def wait_for_output(self, session_id: str) -> AIOutput:
        """等待 AI 完成，收集 stdout → 解析为结构化输出。"""

    def kill_session(self, session_id: str, reason: str) -> None:
        """强制终止 AI 进程。记录原因到审计。"""

    def cleanup_orphans(self) -> int:
        """扫描所有 session，kill 超时/无心跳的。"""

    def list_active(self) -> list[AISession]:
        """列出所有活跃 AI 进程。"""
```

```python
@dataclass
class AISession:
    session_id: str
    role: str           # coordinator / dev / tester / qa
    pid: int            # OS 进程 ID
    project_id: str
    prompt: str
    context: dict
    started_at: str
    timeout_sec: int
    status: str         # running / completed / failed / killed
    output: AIOutput | None
```

### 3.2 AI 结构化输出

**AI 不输出自由操作，只输出结构化决策 JSON。**

#### Coordinator 输出格式

```json
{
  "reply": "用户可见的回复文本",
  "actions": [
    {
      "type": "create_dev_task",
      "prompt": "修复 deploy-governance.sh 端口从 40006 改为 40000",
      "target_files": ["deploy-governance.sh"],
      "related_nodes": ["L13.5"],
      "priority": 1
    },
    {
      "type": "create_test_task",
      "prompt": "测试端口修复后 pre-deploy-check 是否通过",
      "depends_on": "上一个 dev_task 完成后"
    },
    {
      "type": "query_governance",
      "endpoint": "/api/wf/amingClaw/summary"
    },
    {
      "type": "update_context",
      "current_focus": "修复部署端口配置",
      "decisions": ["端口应统一通过 nginx:40000 访问"]
    }
  ],
  "needs_human_confirm": false,
  "confidence": "high"
}
```

#### Dev 输出格式

```json
{
  "summary": "修复了 deploy-governance.sh 中的端口配置",
  "changed_files": ["deploy-governance.sh"],
  "test_results": {
    "ran": true,
    "passed": 15,
    "failed": 0,
    "command": "python -m pytest tests/"
  },
  "git_diff_summary": "+3 -3 lines in 1 file",
  "needs_review": false,
  "related_nodes": ["L13.5"]
}
```

#### Tester 输出格式

```json
{
  "test_report": {
    "total": 1038,
    "passed": 1038,
    "failed": 0,
    "skipped": 0,
    "duration_sec": 37
  },
  "coverage_affected_nodes": ["L13.5"],
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {"passed": 1038, "failed": 0}
  },
  "recommendation": "pass"
}
```

### 3.3 Decision Validator

**代码实时校验 AI 的每个决策，不合法则拒绝并让 AI 重新分析。**

```python
class DecisionValidator:
    """校验 AI 输出的每个 action。代码强制执行，不靠 AI 自律。"""

    def validate(self, role: str, output: AIOutput, project_id: str) -> ValidationResult:
        """
        Returns:
            ValidationResult with:
                - approved_actions: list[Action]  — 通过校验
                - rejected_actions: list[{action, reason}]  — 被拒绝
                - needs_retry: bool  — 是否需要让 AI 重分析
        """
        results = ValidationResult()

        for action in output.actions:
            # 1. 角色权限检查
            if not self._check_role_permission(role, action.type):
                results.reject(action, f"{role} 无权执行 {action.type}")
                continue

            # 2. 节点存在性检查
            if action.related_nodes:
                for node in action.related_nodes:
                    if not self._node_exists(node, project_id):
                        results.reject(action, f"节点 {node} 不存在")
                        continue

            # 3. 文件覆盖率检查
            if action.target_files:
                uncovered = self._coverage_check(action.target_files, project_id)
                if uncovered:
                    results.reject(action, f"文件无节点覆盖: {uncovered}")
                    continue

            # 4. 工具策略检查
            if action.type in ("run_command", "execute_script"):
                policy = self._check_tool_policy(action.command)
                if policy == "deny":
                    results.reject(action, f"命令被策略禁止: {action.command}")
                    continue
                if policy == "approval":
                    results.reject(action, f"命令需要人工确认: {action.command}")
                    continue

            # 5. Workflow 步骤检查
            if action.type == "verify_update":
                if not self._validate_transition(action):
                    results.reject(action, "状态转换不合法")
                    continue

            results.approve(action)

        return results
```

#### 角色权限矩阵

```python
ROLE_PERMISSIONS = {
    "coordinator": {
        "allowed": [
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "query_governance",
            "update_context",
            "reply_user",
            "archive_memory",
        ],
        "denied": [
            "modify_code",      # Coordinator 不改代码
            "run_tests",        # Coordinator 不跑测试
            "verify_update",    # Coordinator 不直接验证节点
            "release_gate",     # 需要通过 Executor 代码执行
        ],
    },
    "dev": {
        "allowed": [
            "modify_code",
            "run_tests",
            "git_diff",
            "read_file",
        ],
        "denied": [
            "create_dev_task",  # Dev 不能派活给别人
            "reply_user",       # Dev 不和用户对话
            "release_gate",
            "delete_node",
        ],
    },
    "tester": {
        "allowed": [
            "run_tests",
            "read_file",
            "verify_update",    # testing / t2_pass
        ],
        "denied": [
            "modify_code",
            "reply_user",
            "create_dev_task",
        ],
    },
    "qa": {
        "allowed": [
            "verify_update",    # qa_pass
            "read_file",
            "query_governance",
        ],
        "denied": [
            "modify_code",
            "run_tests",
            "create_dev_task",
        ],
    },
}
```

### 3.4 Task Orchestrator

**代码控制任务流转。自动触发链路，不靠 AI 记得下一步该做什么。**

```python
class TaskOrchestrator:
    """任务编排。代码驱动，AI 不控制流程。"""

    def handle_user_message(self, chat_id: int, text: str, project_id: str):
        """用户消息 → 自动创建 coordinator session。"""
        # 1. 组装 context
        context = self.assemble_context(project_id, chat_id)

        # 2. 启动 Coordinator AI
        session = self.ai_manager.create_session(
            role="coordinator",
            prompt=text,
            context=context,
            project_id=project_id,
            output_format="structured",
        )

        # 3. 等待输出
        output = self.ai_manager.wait_for_output(session.session_id)

        # 4. 校验决策
        validation = self.validator.validate("coordinator", output, project_id)

        # 5. 处理结果
        if validation.rejected_actions:
            # 有被拒绝的 action → 让 AI 重新分析
            retry_prompt = self._build_retry_prompt(output, validation)
            retry_session = self.ai_manager.create_session(
                role="coordinator",
                prompt=retry_prompt,
                context=context,
                project_id=project_id,
            )
            output = self.ai_manager.wait_for_output(retry_session.session_id)
            validation = self.validator.validate("coordinator", output, project_id)

        # 6. 执行通过的 action
        for action in validation.approved_actions:
            self._execute_action(action, project_id)

        # 7. 发回复
        self.gateway_reply(chat_id, output.reply)

        # 8. 更新上下文
        self._update_context(project_id, chat_id, text, output)

    def handle_dev_complete(self, task_id: str, dev_output: AIOutput, project_id: str):
        """Dev 完成 → 自动创建 Coordinator eval。"""
        chat_id = self._get_task_chat_id(task_id)

        # 1. 代码校验 dev 输出
        dev_validation = self.validator.validate("dev", dev_output, project_id)

        if dev_validation.has_critical_issues:
            # Dev 输出有问题 → 自动重试
            self._retry_dev_task(task_id, dev_validation.issues)
            return

        # 2. 组装 eval context
        eval_context = self.assemble_context(project_id, chat_id)
        eval_context["dev_result"] = {
            "task_id": task_id,
            "summary": dev_output.summary,
            "changed_files": dev_output.changed_files,
            "test_results": dev_output.test_results,
            "validation": dev_validation.summary,
        }

        # 3. 启动 Coordinator eval session
        eval_session = self.ai_manager.create_session(
            role="coordinator",
            prompt="Dev 任务完成，请评估结果并决定下一步",
            context=eval_context,
            project_id=project_id,
        )

        eval_output = self.ai_manager.wait_for_output(eval_session.session_id)

        # 4. 校验 + 执行
        eval_validation = self.validator.validate("coordinator", eval_output, project_id)
        for action in eval_validation.approved_actions:
            self._execute_action(action, project_id)

        # 5. 回复用户
        self.gateway_reply(chat_id, eval_output.reply)

        # 6. 自动归档
        self._auto_archive(project_id, task_id, dev_output, eval_output)

    def _auto_archive(self, project_id, task_id, dev_output, eval_output):
        """任务完成后自动归档记忆和上下文。"""
        # 归档对话历史到 session_context
        self.context_service.archive_if_expired(project_id)

        # 提取有价值决策写入长期记忆
        if eval_output.context_update and eval_output.context_update.get("decisions"):
            for decision in eval_output.context_update["decisions"]:
                self.memory_service.write({
                    "type": "decision",
                    "content": decision,
                    "scope": project_id,
                    "source_task": task_id,
                })

        # 记录 dev 改动摘要
        if dev_output.changed_files:
            self.memory_service.write({
                "type": "workaround" if "fix" in dev_output.summary.lower() else "pattern",
                "content": dev_output.summary,
                "scope": project_id,
                "related_files": dev_output.changed_files,
            })
```

### 3.5 Context Assembly

**Executor 代码组装 AI 的上下文，AI 不自己拉数据。**

```python
class ContextAssembler:
    """为每个 AI session 组装上下文。代码控制，保证一致性。"""

    def assemble(self, project_id: str, chat_id: int, role: str) -> dict:
        """根据角色组装不同粒度的上下文。"""

        context = {}

        # 1. 对话历史（Coordinator 需要）
        if role == "coordinator":
            history = self.context_service.load(project_id)
            context["conversation_history"] = history.get("recent_messages", [])[-10:]
            context["current_focus"] = history.get("current_focus", "")

        # 2. 项目状态（所有角色需要）
        summary = self.gov_api(f"/api/wf/{project_id}/summary")
        context["project_status"] = summary

        # 3. 活跃任务（Coordinator 需要）
        if role == "coordinator":
            runtime = self.gov_api(f"/api/runtime/{project_id}")
            context["active_tasks"] = runtime.get("active_tasks", [])
            context["queued_tasks"] = runtime.get("queued_tasks", [])

        # 4. 相关记忆（所有角色按需）
        memories = self.dbservice.search(project_id, role=role, limit=5)
        context["memories"] = memories

        # 5. 角色特定上下文
        if role == "dev":
            # Dev 需要知道 workspace 路径、git 状态
            context["workspace"] = str(self.workspace_path)
            context["git_status"] = self._get_git_status()

        if role == "tester":
            # Tester 需要知道测试命令、覆盖范围
            context["test_command"] = self.config.get("test_command", "pytest")
            context["affected_nodes"] = self._get_affected_nodes(project_id)

        return context
```

## 四、完整消息流程

### 4.1 用户消息 → Coordinator 对话

```
用户: "帮我修复部署端口的 bug"
    │
    ▼
Gateway: 写 coordinator_chat task 文件 (atomic)
    │
    ▼
Executor TaskOrchestrator.handle_user_message():
    │
    ├── 1. ContextAssembler 组装上下文
    │     - 对话历史 (最近 10 条)
    │     - 项目状态 (104 nodes, all qa_pass)
    │     - 活跃任务 (0 running)
    │     - 相关记忆 (部署相关 pitfall)
    │
    ├── 2. AILifecycleManager.create_session("coordinator", ...)
    │     → 启动 claude CLI, PID=12345
    │     → 等待输出
    │
    ├── 3. AI 输出结构化决策:
    │     {
    │       "reply": "我来分析部署端口问题...",
    │       "actions": [
    │         {"type": "create_dev_task", "prompt": "修复端口", "target_files": ["deploy-governance.sh"]}
    │       ]
    │     }
    │
    ├── 4. DecisionValidator.validate("coordinator", output):
    │     ✅ create_dev_task — coordinator 有权
    │     ✅ target_files coverage — deploy-governance.sh 有 L13.5 覆盖
    │
    ├── 5. 执行 approved actions:
    │     → 创建 dev_task 文件
    │     → 更新 Task Registry
    │
    ├── 6. Gateway 回复用户: "我来分析部署端口问题..."
    │
    └── 7. 更新上下文:
          → 保存用户消息 + coordinator 回复
          → 更新 current_focus
```

### 4.2 Dev 完成 → Coordinator 评估 → 归档

```
Executor 执行 dev_task:
    │
    ├── AILifecycleManager.create_session("dev", ...)
    │     → claude CLI 修改代码
    │
    ├── AI 输出:
    │     {"summary": "修改了端口", "changed_files": ["deploy.sh"], "test_results": {...}}
    │
    ├── DecisionValidator.validate("dev", output):
    │     ✅ 修改文件在 allowed 范围
    │     ✅ 测试通过
    │
    ▼
TaskOrchestrator.handle_dev_complete():  ← 代码自动触发
    │
    ├── 1. 组装 eval context (含 dev 结果)
    │
    ├── 2. AILifecycleManager.create_session("coordinator", eval_prompt)
    │     → Coordinator 评估 dev 结果
    │
    ├── 3. Coordinator 输出:
    │     {
    │       "reply": "端口已修复，测试通过。建议部署。",
    │       "actions": [
    │         {"type": "update_context", "decisions": ["端口统一用 nginx:40000"]}
    │       ]
    │     }
    │
    ├── 4. 回复用户
    │
    └── 5. 自动归档:
          → session_context: 保存对话
          → dbservice: 写入决策 "端口统一用 nginx:40000"
          → dbservice: 写入修改摘要
```

### 4.3 校验失败 → 自动重试

```
Coordinator 输出:
  {
    "actions": [
      {"type": "modify_code", "file": "server.py"}  ← 越权！
    ]
  }
    │
    ▼
DecisionValidator:
  ❌ coordinator 无权 modify_code
    │
    ▼
TaskOrchestrator:
  构建重试 prompt:
    "你之前的决策被拒绝:
     - modify_code: coordinator 无权直接修改代码
     请重新分析，使用 create_dev_task 让 dev 角色执行"
    │
    ▼
AILifecycleManager.create_session("coordinator", retry_prompt)
    │
    ▼
Coordinator 重新输出:
  {
    "actions": [
      {"type": "create_dev_task", "prompt": "修改 server.py ..."}
    ]
  }
  → ✅ 通过
```

## 五、文件结构

```
agent/
├── executor.py                    # 现有，增加 orchestrator 入口
├── ai_lifecycle.py                # NEW: AI Lifecycle Manager
├── decision_validator.py          # NEW: 决策校验器
├── task_orchestrator.py           # NEW: 任务编排器
├── context_assembler.py           # NEW: 上下文组装器
├── ai_output_parser.py            # NEW: AI 输出解析 (JSON extraction)
├── role_permissions.py            # NEW: 角色权限矩阵
├── backends.py                    # 现有: run_claude / run_codex
├── telegram_gateway/
│   └── gateway.py                 # 简化: 只写 task 文件
└── governance/
    ├── server.py                  # 现有
    ├── task_registry.py           # 现有
    ├── session_context.py         # 现有
    └── ...
```

## 六、Coordinator System Prompt 模板

```
你是 {project_id} 项目的 Coordinator。

## 你的职责
1. 理解用户意图
2. 回答问题
3. 如需执行代码修改，输出 create_dev_task action
4. 如需确认，追问用户

## 当前上下文
{context_json}

## 输出格式 (严格 JSON)
你必须输出以下 JSON 格式，不要输出其他内容:

```json
{
  "reply": "给用户的回复文本",
  "actions": [
    {
      "type": "create_dev_task | create_test_task | query_governance | update_context | reply_only",
      "prompt": "任务描述 (如果是 task)",
      "target_files": ["文件路径"],
      "related_nodes": ["L节点ID"]
    }
  ],
  "context_update": {
    "current_focus": "当前工作焦点",
    "decisions": ["做出的决策"]
  }
}
```

## 约束
- 你不能直接修改代码，必须通过 create_dev_task 让 dev 角色执行
- 你不能直接调用 API，只能输出 query_governance action
- 你的所有 action 都会被代码校验，越权会被拒绝
- 如果你不确定，使用 reply_only 追问用户
```

## 七、实施路线

### P0 — 核心框架（必须先完成）

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | ai_lifecycle.py: AILifecycleManager | 无 |
| 2 | ai_output_parser.py: 从 claude stdout 提取 JSON | 无 |
| 3 | role_permissions.py: 角色权限矩阵 | 无 |
| 4 | decision_validator.py: 校验逻辑 | 2, 3 |
| 5 | context_assembler.py: 上下文组装 | 无 |
| 6 | task_orchestrator.py: handle_user_message | 1, 4, 5 |

### P1 — 闭环链路

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 7 | task_orchestrator: handle_dev_complete | 6 |
| 8 | Coordinator eval 自动触发 | 7 |
| 9 | 自动重试 (校验失败 → 重新分析) | 4, 6 |
| 10 | 对话历史持久化 (跨消息上下文) | 5 |
| 11 | 自动归档 (记忆 + 上下文) | 7 |

### P2 — 增强

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 12 | 多角色并行 (dev + tester 同时) | 6 |
| 13 | 任务依赖链 (dev 完成后自动跑 tester) | 7 |
| 14 | 人工确认流程 (危险操作 → Telegram 确认) | 6 |
| 15 | 记忆辅助决策 (context 注入 pitfall) | 5 |
| 16 | 观测性 (trace_id 串联全链路) | 6 |

## 八、与现有系统的兼容性

| 现有组件 | 保留/修改/废弃 |
|---------|---------------|
| Gateway 消息收发 | **保留** — 继续做 Telegram polling + task 文件写入 |
| Gateway 消息分类器 | **废弃** — 不再分类，全部转 Executor |
| Gateway handle_task_dispatch | **废弃** — 不再直接创建 task |
| Executor run loop | **修改** — 加入 TaskOrchestrator |
| Executor process_task | **修改** — 区分 coordinator_chat / dev_task |
| process_coordinator_chat | **重构** — 改用 AILifecycleManager |
| Governance API | **保留** — 被 Executor 代码调用 |
| Task Registry | **保留** — 被 TaskOrchestrator 调用 |
| Session Context | **保留** — 被 ContextAssembler 调用 |
| dbservice | **保留** — 被自动归档调用 |
| verify_loop | **保留** — 作为事后检查的补充 |

## 九、安全边界

```
硬约束 (代码强制执行，AI 无法绕过):

1. AI 不能启动 AI
   → AILifecycleManager 是唯一入口
   → AI 的 action 只是"请求"，代码决定是否执行

2. AI 不能直接操作系统
   → 所有 API 调用由代码执行
   → AI 输出 JSON，代码解析后操作

3. 角色权限不可逾越
   → role_permissions.py 硬编码
   → DecisionValidator 每个 action 都检查

4. 越权自动重试
   → 被拒绝的 action 带原因反馈给 AI
   → AI 必须在权限范围内重新决策
   → 最多重试 3 次，超过则人工介入

5. 任务流转由代码控制
   → dev 完成 → 代码自动创建 eval
   → 不靠 AI 记得调 API
   → 不靠 AI 记得归档记忆
```

## 十、验收图集成 — Executor 感知 DAG

### 10.1 为什么 Executor 必须感知验收图

验收图是整个 workflow 的规则核心。v5.1 之前，AI 通过 API 查询验收图，然后"自觉"遵守规则。
这违反了 v6 的核心原则——**规则由代码强制执行，不靠 AI 自律**。

```
v5.1:  AI 查询验收图 → AI "自觉"遵守 → 经常跳步骤
v6:    Executor 代码主动拉取验收图 → 编码进校验逻辑 → AI 无法绕过
```

### 10.2 Executor 拉取的验收图数据

Executor 启动时和定期（每 60s）从 Governance 拉取验收图快照缓存：

```python
class GraphAwareValidator:
    """验收图感知的校验器。Executor 代码强制执行图约束。"""

    def __init__(self):
        self._graph_cache = None
        self._cache_ts = 0
        self._cache_ttl = 60  # 60s 刷新

    def refresh_graph(self, project_id: str):
        """从 Governance 拉取验收图快照。"""
        self._graph_cache = gov_api("GET", f"/api/wf/{project_id}/export?format=json")
        self._cache_ts = time.time()
        # 缓存结构:
        # {
        #   "nodes": {
        #     "L13.5": {
        #       "verify_status": "qa_pass",
        #       "deps": ["L13.1", "L13.2"],
        #       "gate_mode": "explicit",
        #       "gates": [{"node": "L13.1", "min_status": "qa_pass"}],
        #       "primary": ["deploy-governance.sh"],
        #       "secondary": ["scripts/pre-deploy-check.sh"],
        #       "artifacts": [{"type": "api_docs", "section": "deployment"}],
        #       "verify": "L4",
        #     }
        #   },
        #   "edges": [...],
        # }
```

### 10.3 验收图约束在 Executor 中的强制执行

#### 约束 1：文件修改必须有节点覆盖

```python
def check_file_coverage(self, changed_files: list[str], project_id: str) -> list[str]:
    """检查 AI 要修改的文件是否都有节点覆盖。

    Returns: 未覆盖的文件列表（空 = 全部覆盖）
    """
    graph = self._get_graph(project_id)
    covered_files = set()
    for node_id, node in graph["nodes"].items():
        covered_files.update(node.get("primary", []))
        covered_files.update(node.get("secondary", []))

    uncovered = []
    for f in changed_files:
        if not any(f.endswith(cf) or cf.endswith(f) for cf in covered_files):
            uncovered.append(f)
    return uncovered
```

**触发时机**：Dev AI 输出 changed_files 时，Executor 代码检查。

```python
# TaskOrchestrator.handle_dev_complete()
uncovered = self.graph_validator.check_file_coverage(
    dev_output.changed_files, project_id
)
if uncovered:
    # 拒绝 dev 结果，让 Coordinator 决定是否创建节点
    self._reject_dev_output(task_id, f"文件无节点覆盖: {uncovered}")
    return
```

#### 约束 2：节点依赖必须满足

```python
def check_node_deps_satisfied(self, node_id: str, project_id: str) -> list[str]:
    """检查节点的上游依赖是否都已 pass。

    Returns: 未满足的依赖节点列表
    """
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return [f"node {node_id} not found"]

    unsatisfied = []
    for dep in node.get("deps", []):
        dep_node = graph["nodes"].get(dep)
        if not dep_node:
            unsatisfied.append(f"{dep}: not found")
        elif dep_node.get("verify_status") not in ("t2_pass", "qa_pass"):
            unsatisfied.append(f"{dep}: {dep_node.get('verify_status', 'unknown')}")
    return unsatisfied
```

**触发时机**：Coordinator AI 输出 verify_update action 时。

```python
# DecisionValidator.validate()
if action.type == "verify_update":
    unsatisfied = self.graph_validator.check_node_deps_satisfied(
        action.node_id, project_id
    )
    if unsatisfied:
        results.reject(action, f"上游依赖未满足: {unsatisfied}")
```

#### 约束 3：Gate 策略检查

```python
def check_gate_policy(self, node_id: str, target_status: str, project_id: str) -> bool:
    """检查节点的 gate 策略是否允许推进到目标状态。"""
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return False

    gates = node.get("gates", [])
    for gate in gates:
        gate_node_id = gate.get("node")
        min_status = gate.get("min_status", "qa_pass")
        gate_node = graph["nodes"].get(gate_node_id)
        if not gate_node:
            return False
        actual_status = gate_node.get("verify_status", "pending")
        if not self._status_gte(actual_status, min_status):
            return False
    return True
```

#### 约束 4：角色验证级别限制

```python
def check_role_verify_level(self, role: str, target_status: str) -> bool:
    """检查角色是否有权推进到目标状态。

    tester: pending → testing → t2_pass
    qa:     t2_pass → qa_pass
    coordinator: 不能直接 verify
    """
    ROLE_VERIFY_LIMITS = {
        "tester": {"testing", "t2_pass"},
        "qa": {"qa_pass"},
        "coordinator": set(),  # coordinator 不能直接 verify
        "dev": set(),          # dev 不能 verify
    }
    allowed = ROLE_VERIFY_LIMITS.get(role, set())
    return target_status in allowed
```

#### 约束 5：Artifacts 完整性检查

```python
def check_artifacts_complete(self, node_id: str, project_id: str) -> list[str]:
    """检查节点的 artifacts 约束是否满足。

    Returns: 缺失的 artifact 列表
    """
    graph = self._get_graph(project_id)
    node = graph["nodes"].get(node_id)
    if not node:
        return []

    missing = []
    for artifact in node.get("artifacts", []):
        if artifact["type"] == "api_docs":
            section = artifact.get("section", "")
            docs = gov_api("GET", f"/api/docs/{section}")
            if "error" in docs:
                missing.append(f"api_docs:{section}")
    return missing
```

**触发时机**：推进到 qa_pass 时自动检查。

#### 约束 6：新文件必须先建节点

```python
def check_new_files_have_nodes(self, dev_output, project_id: str) -> list[str]:
    """检查 dev 新创建的文件是否有对应节点。

    已有文件修改 → 检查 coverage
    新文件创建 → 必须先有节点（或 Coordinator 提议创建）
    """
    graph = self._get_graph(project_id)
    all_tracked_files = set()
    for node in graph["nodes"].values():
        all_tracked_files.update(node.get("primary", []))
        all_tracked_files.update(node.get("secondary", []))

    new_untracked = []
    for f in dev_output.get("new_files", []):
        if f not in all_tracked_files:
            new_untracked.append(f)
    return new_untracked
```

### 10.4 验收图修改权限 — Executor 控制

**AI 不能直接修改验收图。所有图变更通过 Executor 代码校验。**

```python
# Coordinator AI 输出（请求，不是直接操作）
{
    "actions": [
        {
            "type": "propose_node",
            "node": {
                "id": "L15.1",
                "title": "新功能 X",
                "deps": ["L14.1"],
                "primary": ["agent/new_module.py"],
                "verify": "L4",
                "description": "..."
            },
            "reason": "dev 需要创建新文件 agent/new_module.py，当前无节点覆盖"
        },
        {
            "type": "propose_node_update",
            "node_id": "L13.5",
            "changes": {
                "secondary": {"add": ["scripts/new_helper.sh"]}
            },
            "reason": "新增辅助脚本需要跟踪"
        }
    ]
}
```

```python
class GraphModificationValidator:
    """验收图修改校验。代码强制执行，AI 不能直接改图。"""

    def validate_propose_node(self, proposal: dict, project_id: str) -> ValidationResult:
        """校验新节点提议。"""
        node = proposal["node"]
        result = ValidationResult()

        # 1. ID 格式检查
        if not re.match(r"^L\d+\.\d+$", node["id"]):
            result.reject("ID 格式不合法，应为 L{layer}.{index}")
            return result

        # 2. ID 唯一性
        graph = self._get_graph(project_id)
        if node["id"] in graph["nodes"]:
            result.reject(f"节点 {node['id']} 已存在")
            return result

        # 3. 依赖存在性
        for dep in node.get("deps", []):
            if dep not in graph["nodes"]:
                result.reject(f"依赖 {dep} 不存在")
                return result

        # 4. 无环检测
        if self._would_create_cycle(graph, node["id"], node.get("deps", [])):
            result.reject("会造成循环依赖")
            return result

        # 5. primary 文件路径格式检查
        for f in node.get("primary", []):
            if ".." in f or f.startswith("/"):
                result.reject(f"文件路径不安全: {f}")
                return result

        result.approve()
        return result

    def validate_propose_update(self, update: dict, project_id: str) -> ValidationResult:
        """校验节点更新提议。"""
        node_id = update["node_id"]
        graph = self._get_graph(project_id)

        if node_id not in graph["nodes"]:
            return ValidationResult(rejected=True, reason=f"节点 {node_id} 不存在")

        # 只允许修改 secondary 和 description
        # primary / deps / gates 修改需要人工确认
        changes = update.get("changes", {})
        sensitive_fields = {"primary", "deps", "gates", "gate_mode", "verify"}
        for field in changes:
            if field in sensitive_fields:
                return ValidationResult(
                    rejected=True,
                    reason=f"修改 {field} 需要人工确认",
                    needs_human_confirm=True
                )

        return ValidationResult(approved=True)
```

#### 执行流程

```
Coordinator AI: {"type": "propose_node", "node": {...}}
    │
    ▼
Executor GraphModificationValidator:
    ├── ID 格式 ✅
    ├── 唯一性 ✅
    ├── 依赖存在 ✅
    ├── 无环 ✅
    └── 路径安全 ✅
    │
    ▼ 通过
Executor 代码调 Governance API:
    POST /api/wf/{project_id}/import-graph (or node-create)
    │
    ▼
验收图更新完成 → 审计记录:
    {
        "action": "node_created",
        "node_id": "L15.1",
        "proposed_by": "coordinator_session_xxx",
        "validated_by": "executor_code",
        "timestamp": "..."
    }
```

### 10.5 验收图约束总览 — Executor 代码执行矩阵

| 约束 | 触发时机 | 检查内容 | 失败处理 |
|------|---------|---------|---------|
| 文件覆盖率 | Dev AI 输出 changed_files | 每个文件有对应节点 | 拒绝 dev 结果 → Coordinator 决定建节点 |
| 依赖满足 | verify_update action | 上游节点全部 pass | 拒绝 → 告诉 AI 哪些依赖未满足 |
| Gate 策略 | verify_update 到 qa_pass | gate 条件全部满足 | 拒绝 → 列出未满足的 gate |
| 角色验证级别 | verify_update action | 角色权限匹配目标状态 | 拒绝 → 告诉 AI 谁有权 |
| Artifacts 完整 | 推进到 qa_pass | 文档/测试/证据齐全 | 拒绝 → 列出缺失 artifacts |
| 新文件建节点 | Dev 创建新文件 | 新文件有对应节点 | 暂停 → 让 Coordinator propose_node |
| 节点创建校验 | propose_node action | ID/依赖/无环/路径安全 | 拒绝 → 原因反馈给 AI |
| 节点修改校验 | propose_node_update | 敏感字段需人工确认 | 需确认 → Telegram 通知人类 |
| Coverage 新代码 | Dev 完成后 | 所有改动文件有覆盖 | 拒绝 → 让 Coordinator 分析 |

### 10.6 Dev 任务全生命周期中的验收图检查

```
Coordinator 创建 dev_task:
    │
    ├── Executor 检查:
    │   ✓ target_files 都有节点覆盖?
    │   ✓ 对应节点状态允许修改? (pending/testing, 不能改 qa_pass 的)
    │   ✓ 节点依赖满足?
    │
    ▼ 通过 → 启动 Dev AI
    │
Dev AI 执行:
    │ ... 修改代码 ...
    │
    ▼ 完成
    │
    ├── Executor 检查 dev 输出:
    │   ✓ changed_files 都有节点覆盖?
    │   ✓ 没有修改不相关的文件?
    │   ✓ 新创建的文件有节点?
    │   ✓ 测试通过?
    │
    ├── 如有问题:
    │   → 构建重试 prompt (含拒绝原因)
    │   → 重新启动 Dev AI (最多 3 次)
    │
    ▼ 通过 → 启动 Coordinator eval
    │
Coordinator eval:
    │
    ├── Executor 检查 eval 输出:
    │   ✓ 不能越权 (Coordinator 不能自己 verify)
    │   ✓ 如有 verify_update action → 检查角色+依赖+gate
    │   ✓ 如有 propose_node → 检查 ID/依赖/无环
    │
    ▼ 通过 → 执行 actions + 回复用户 + 自动归档
```

## 十一、证据采集 — Executor 独立采集，不信 AI 自报

### 11.1 核心原则

AI 输出分两类，Executor 区别对待：

| 类别 | 来源 | 信任度 | 例子 |
|------|------|--------|------|
| **Decision** (决策) | AI 生成 | 需校验后执行 | create_dev_task, reply, update_context |
| **Evidence** (证据) | **Executor 独立采集** | 可信 | changed_files, test_results, git_diff |

```python
class EvidenceCollector:
    """Executor 独立采集事实证据，不依赖 AI 自报。"""

    def collect_after_dev(self, workspace: Path, before_snapshot: dict) -> DevEvidence:
        """Dev AI 执行完后，代码独立采集真实结果。"""

        # 1. 真实 changed_files — 从 git diff 采集，不信 AI 报的
        changed = subprocess.run(
            ["git", "diff", "--name-only", before_snapshot["commit"]],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip().splitlines()

        # 2. 真实 new_files — 从 git status 采集
        new_files = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip().splitlines()

        # 3. 真实 test_results — 从 pytest/junit 报告采集
        test_result = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            capture_output=True, text=True, cwd=workspace,
            timeout=300
        )
        test_evidence = {
            "exit_code": test_result.returncode,
            "stdout": test_result.stdout[-2000:],
            "passed": test_result.returncode == 0,
        }

        # 4. 真实 diff 统计
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", before_snapshot["commit"]],
            capture_output=True, text=True, cwd=workspace
        ).stdout.strip()

        return DevEvidence(
            changed_files=changed,
            new_files=new_files,
            test_results=test_evidence,
            diff_stat=diff_stat,
            collected_by="executor_code",
            collected_at=utc_iso(),
        )
```

### 11.2 AI 自报 vs Executor 采集对比

```
Dev AI 完成后:

AI 自报:                          Executor 独立采集:
  changed_files: ["a.py"]          git diff: ["a.py", "b.py"]  ← 多改了 b.py!
  test_results: {passed: 10}       pytest exit_code: 1          ← 测试其实没通过!
  summary: "修复完成"               diff_stat: "+50 -3"          ← 真实统计

→ Executor 以独立采集为准
→ AI 自报只用于 Coordinator eval 的参考
→ 不一致时记录到审计
```

## 十二、任务状态机 — 显式定义

### 12.1 状态枚举

```python
class TaskStatus(str, Enum):
    # 创建态
    CREATED = "created"
    QUEUED = "queued"

    # 执行态
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    WAITING_HUMAN = "waiting_human"
    BLOCKED_BY_DEP = "blocked_by_dep"

    # 终结态
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"

    # 评估态
    EVAL_PENDING = "eval_pending"        # 等待 Coordinator eval
    EVAL_APPROVED = "eval_approved"      # Coordinator 确认通过
    EVAL_REJECTED = "eval_rejected"      # Coordinator 要求重做

    # 通知态
    NOTIFY_PENDING = "notify_pending"
    NOTIFIED = "notified"

    # 归档态
    ARCHIVED = "archived"
```

### 12.2 状态转换规则

```python
VALID_TRANSITIONS = {
    "created":           {"queued", "cancelled"},
    "queued":            {"claimed", "cancelled"},
    "claimed":           {"running", "queued"},           # claim 失败可退回
    "running":           {"succeeded", "failed_retryable", "failed_terminal", "cancelled"},
    "waiting_retry":     {"queued"},                      # 重新排队
    "waiting_human":     {"queued", "cancelled"},         # 人工决定
    "blocked_by_dep":    {"queued"},                      # 依赖满足后恢复
    "succeeded":         {"eval_pending"},                # 自动触发 eval
    "failed_retryable":  {"waiting_retry", "failed_terminal"},
    "failed_terminal":   {"notify_pending", "archived"},
    "eval_pending":      {"eval_approved", "eval_rejected"},
    "eval_approved":     {"notify_pending"},
    "eval_rejected":     {"queued"},                      # 重新执行
    "notify_pending":    {"notified"},
    "notified":          {"archived"},
    "cancelled":         {"archived"},
}
```

### 12.3 Task 扩展字段

```python
@dataclass
class Task:
    task_id: str
    task_type: str           # coordinator_chat / dev_task / test_task / qa_task
    status: TaskStatus
    project_id: str
    prompt: str

    # 调度
    attempt: int = 0
    max_attempts: int = 3
    priority: int = 0
    parent_task_id: str = ""  # 父任务（eval 的父是 dev_task）

    # Lease
    lease_owner: str = ""
    lease_expire_at: str = ""

    # 追踪
    trace_id: str = ""
    idempotency_key: str = ""
    schema_version: str = "v1"

    # 证据（Executor 独立采集）
    evidence: dict = field(default_factory=dict)

    # AI 决策（需校验）
    ai_decision: dict = field(default_factory=dict)

    # 时间线
    created_at: str = ""
    claimed_at: str = ""
    completed_at: str = ""
    archived_at: str = ""
```

## 十三、Validator 分层

```
┌───────────────────────────────────────┐
│ Layer 1: SchemaValidator              │
│   JSON 格式、schema_version、必填字段  │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 2: PolicyValidator              │
│   角色权限、tool policy、危险操作检测   │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 3: GraphValidator               │
│   节点存在、依赖满足、gate、coverage    │
│   图版本一致性 (version/etag CAS)      │
└───────────────┬───────────────────────┘
                ▼
┌───────────────────────────────────────┐
│ Layer 4: ExecutionPreconditionValidator│
│   workspace 可用、文件存在、lease 有效  │
│   并发冲突检测、资源限制               │
└───────────────────────────────────────┘
```

每层独立返回 `{layer, passed, errors[]}`，审计时可精确定位哪层拦截了。

## 十四、错误分类重试策略

```python
class ErrorCategory(str, Enum):
    RETRYABLE_MODEL = "retryable_model"      # JSON 解析失败、AI 输出格式错
    RETRYABLE_ENV = "retryable_env"          # 网络超时、文件系统临时错误
    BLOCKED_BY_DEP = "blocked_by_dep"        # 图依赖未满足
    NON_RETRYABLE_POLICY = "non_retryable"   # 权限拒绝、命令 deny
    NEEDS_HUMAN = "needs_human"              # 敏感操作需确认

RETRY_STRATEGY = {
    "retryable_model":     {"max_retries": 3, "backoff": "immediate", "action": "rebuild_prompt"},
    "retryable_env":       {"max_retries": 2, "backoff": "exponential", "action": "retry_same"},
    "blocked_by_dep":      {"max_retries": 0, "backoff": None, "action": "set_blocked_status"},
    "non_retryable":       {"max_retries": 0, "backoff": None, "action": "fail_terminal"},
    "needs_human":         {"max_retries": 0, "backoff": None, "action": "create_approval"},
}
```

## 十五、记忆写入治理

```python
class MemoryWriteGuard:
    """记忆写入前的治理检查。防止污染长期记忆。"""

    def should_write(self, entry: dict, project_id: str) -> tuple[bool, str]:
        # 1. 去重 — 检查是否已有高度相似记忆
        existing = self.dbservice.search(entry["content"][:100], scope=project_id, limit=3)
        for e in existing:
            if self._similarity(e["doc"]["content"], entry["content"]) > 0.85:
                return False, "duplicate"

        # 2. 来源检查 — 只有 qa_pass 的决策才能写长期记忆
        if entry.get("type") == "decision":
            source_node = entry.get("related_node")
            if source_node:
                node = self.gov_api(f"/api/wf/{project_id}/node/{source_node}")
                if node.get("verify_status") != "qa_pass":
                    return False, "node_not_qa_pass"

        # 3. 可信度 — 低于阈值的不写
        if entry.get("confidence", 1.0) < 0.6:
            return False, "low_confidence"

        # 4. TTL — workaround 类自动设 30 天过期
        if entry.get("type") == "workaround":
            entry.setdefault("ttl_days", 30)

        return True, "ok"
```

## 十六、Context 预算与确定性

```python
CONTEXT_BUDGET = {
    "coordinator": {
        "hard_context": 3000,      # task, node, files, current status
        "conversation": 3000,      # recent 10 messages
        "memory": 1500,            # top-3 related
        "runtime": 500,            # active/queued tasks
        "total_max": 8000,
    },
    "dev": {
        "hard_context": 2000,      # task prompt, target files, workspace
        "memory": 1500,            # related pitfalls
        "git_context": 500,        # git status, recent commits
        "total_max": 4000,
    },
    "tester": {
        "hard_context": 1500,      # test command, affected nodes
        "memory": 1000,            # test patterns
        "total_max": 3000,
    },
    "qa": {
        "hard_context": 1500,      # review scope
        "memory": 1000,            # qa criteria
        "total_max": 3000,
    },
}
```

上下文组装时严格按预算截断，保证确定性：

```python
def assemble(self, project_id, chat_id, role):
    budget = CONTEXT_BUDGET[role]
    context = {}

    # 按优先级填充，超预算则截断
    used = 0
    for layer in ["hard_context", "conversation", "memory", "runtime", "git_context"]:
        layer_budget = budget.get(layer, 0)
        if layer_budget == 0:
            continue
        data = self._fetch_layer(layer, project_id, chat_id, role)
        truncated = self._truncate_to_tokens(data, layer_budget)
        context[layer] = truncated
        used += self._count_tokens(truncated)
        if used >= budget["total_max"]:
            break

    return context
```

## 十七、图版本一致性 (CAS)

```python
class GraphAwareValidator:
    def _get_graph(self, project_id):
        """带版本号的图缓存。"""
        now = time.time()
        if self._graph_cache and now - self._cache_ts < self._cache_ttl:
            return self._graph_cache

        result = gov_api("GET", f"/api/wf/{project_id}/export?format=json")
        self._graph_cache = result
        self._graph_version = result.get("version", 0)
        self._cache_ts = now
        return result

    def validate_with_cas(self, action, project_id):
        """校验时带图版本，执行时 compare-and-swap。"""
        graph = self._get_graph(project_id)
        validate_version = self._graph_version

        # ... 校验逻辑 ...

        # 执行时检查版本没变
        current_version = gov_api("GET", f"/api/wf/{project_id}/summary").get("version", 0)
        if current_version != validate_version:
            # 图在校验期间被修改了 → 刷新重试
            self._graph_cache = None
            return ValidationResult(rejected=True, reason="graph_version_conflict", retryable=True)

        return validation_result
```

## 十八、与 v5.1 的关系

v6 不是推倒重来，而是在 v5.1 基础上加一层 **代码控制层**：

```
v5.1:  Gateway → [AI 自由操作] → 结果
v6:    Gateway → [Executor 代码] → [AI 结构化输出] → [4层校验 + 独立证据采集 + 图CAS] → 结果

新增的是中间的代码控制层 + 验收图集成 + 证据采集 + 状态机，不改底层服务。
```

## 十九、实施路线（终版）

### P0 — 核心框架 + 地基加固

| 步骤 | 内容 | 依赖 | 来源 |
|------|------|------|------|
| 1 | ai_lifecycle.py: AILifecycleManager | 无 | 原设计 |
| 2 | ai_output_parser.py: JSON 提取 + schema_version | 无 | 原设计 + 评审#5 |
| 3 | role_permissions.py: 角色权限矩阵 | 无 | 原设计 |
| 4 | graph_validator.py: 验收图约束 + 版本CAS | Gov API | 原设计 + 评审#7 |
| 5 | evidence_collector.py: Executor 独立采集 | 无 | 评审#2 |
| 6 | task_state_machine.py: 显式状态枚举+转换规则 | 无 | 评审#4 |
| 7 | decision_validator.py: 4层分层校验 | 2,3,4 | 原设计 + 评审#6 |
| 8 | context_assembler.py: 预算化上下文组装 | 无 | 原设计 + 评审#10 |
| 9 | task_orchestrator.py: handle_user_message | 1,7,8 | 原设计 |

### P1 — 闭环 + 可靠性

| 步骤 | 内容 | 依赖 | 来源 |
|------|------|------|------|
| 10 | handle_dev_complete + 独立证据校验 | 5,9 | 原设计 + 评审#2 |
| 11 | Coordinator eval 自动触发 | 10 | 原设计 |
| 12 | 错误分类重试策略 | 7,9 | 评审#8 |
| 13 | 对话历史持久化 | 8 | 原设计 |
| 14 | 记忆写入治理 (去重/可信度/TTL) | dbservice | 评审#9 |
| 15 | 自动归档 (记忆 + 上下文) | 10,14 | 原设计 |
| 16 | propose_node 校验 | 4 | 原设计 |
| 17 | task file → DB+Redis 驱动 | 6 | 评审#1 |

### P2 — 增强

| 步骤 | 内容 | 依赖 | 来源 |
|------|------|------|------|
| 18 | 执行沙箱 (隔离workspace/命令白名单) | 9 | 评审#3 |
| 19 | 多角色并行 | 9 | 原设计 |
| 20 | 任务依赖链 (dev→tester→qa 自动) | 10 | 原设计 |
| 21 | 人工审批对象 (approval_id/scope) | 9 | 评审#12 |
| 22 | Plan 层 (请求→计划→任务) | 9 | 评审#11 |
| 23 | 观测性 (trace_id + replay) | 9 | 评审#13 |

### 评审建议采纳总结

| 评审# | 建议 | 采纳 | 落地章节 |
|-------|------|------|---------|
| 1 | task file → DB+Redis | ✅ P1 #17 | §12 状态机 |
| 2 | 证据独立采集 | ✅ P0 #5 | §11 证据采集 |
| 3 | 执行沙箱 | ✅ P2 #18 | 后置（当前单机足够） |
| 4 | 显式状态机 | ✅ P0 #6 | §12 状态机 |
| 5 | schema_version | ✅ P0 #2 | §12.3 Task 字段 |
| 6 | validator 拆层 | ✅ P0 #7 | §13 分层校验 |
| 7 | graph CAS | ✅ P0 #4 | §17 图版本一致性 |
| 8 | 错误分类重试 | ✅ P1 #12 | §14 重试策略 |
| 9 | 记忆写入治理 | ✅ P1 #14 | §15 记忆治理 |
| 10 | context budget | ✅ P0 #8 | §16 预算与确定性 |
| 11 | Plan 层 | ✅ P2 #22 | 后置 |
| 12 | 审批对象 | ✅ P2 #21 | 后置 |
| 13 | replay/审计链 | ✅ P2 #23 | 后置 |
