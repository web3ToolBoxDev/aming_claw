"""Role Permissions — Hardcoded role-action permission matrix.

Code-enforced. AI cannot modify or bypass.
Used by DecisionValidator to check every AI action.
"""

# Action types that AI can output
ACTION_TYPES = {
    # PM actions
    "generate_prd",
    "design_nodes",
    "analyze_requirements",
    "estimate_effort",

    # Coordinator actions
    "create_dev_task",
    "create_test_task",
    "create_qa_task",
    "create_pm_task",
    "query_governance",
    "update_context",
    "reply_only",
    "archive_memory",
    "propose_node",
    "propose_node_update",

    # Dev actions
    "modify_code",
    "run_tests",
    "git_diff",
    "read_file",

    # Tester actions
    "verify_update",  # testing, t2_pass

    # QA actions
    # verify_update with qa_pass

    # Memory operations
    "delete_memory",
    "propose_memory_cleanup",

    # Dangerous
    "run_command",
    "execute_script",
    "release_gate",
}

# Permission matrix: role → allowed action types
ROLE_PERMISSIONS = {
    "pm": {
        "allowed": {
            "generate_prd",
            "design_nodes",
            "analyze_requirements",
            "estimate_effort",
            "propose_node",
            "propose_node_update",
            "query_governance",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "run_command",
            "execute_script",
            "create_dev_task",     # PM 不直接派任务，交给 Coordinator
            "verify_update",
            "release_gate",
            "archive_memory",
        },
    },
    "coordinator": {
        "allowed": {
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "create_pm_task",
            "query_governance",
            "update_context",
            "reply_only",
            "archive_memory",
            "propose_node",
            "propose_node_update",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "verify_update",
            "release_gate",
            "run_command",
            "execute_script",
            "generate_prd",        # Coordinator 不做需求分析，交给 PM
        },
    },
    "dev": {
        "allowed": {
            "modify_code",
            "run_tests",
            "git_diff",
            "read_file",
            "reply_only",
            "propose_memory_cleanup",
        },
        "denied": {
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "reply_only",  # dev replies go through Coordinator eval
            "release_gate",
            "propose_node",
            "verify_update",
            "delete_memory",   # dev 不能直接删除记忆，只能提议清理
        },
    },
    "tester": {
        "allowed": {
            "run_tests",
            "read_file",
            "verify_update",  # limited to testing/t2_pass by GraphValidator
            "reply_only",
        },
        "denied": {
            "modify_code",
            "create_dev_task",
            "release_gate",
            "propose_node",
        },
    },
    "qa": {
        "allowed": {
            "verify_update",  # limited to qa_pass by GraphValidator
            "read_file",
            "query_governance",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "create_dev_task",
            "release_gate",
            "propose_node",
        },
    },
}

# Verify status limits per role
ROLE_VERIFY_LIMITS = {
    "tester": {"testing", "t2_pass"},
    "qa": {"qa_pass"},
    "coordinator": set(),  # coordinator cannot verify
    "dev": set(),          # dev cannot verify
    "pm": set(),           # pm cannot verify
}


def check_permission(role: str, action_type: str) -> tuple[bool, str]:
    """Check if role is allowed to perform action_type.

    Returns:
        (allowed: bool, reason: str)
    """
    perms = ROLE_PERMISSIONS.get(role)
    if not perms:
        return False, f"unknown role: {role}"

    if action_type in perms.get("allowed", set()):
        return True, "ok"

    if action_type in perms.get("denied", set()):
        return False, f"{role} cannot perform {action_type}"

    # Unknown action type — deny by default
    return False, f"unknown action type: {action_type}"


def check_verify_permission(role: str, target_status: str) -> tuple[bool, str]:
    """Check if role can push to target verify status."""
    allowed = ROLE_VERIFY_LIMITS.get(role, set())
    if target_status in allowed:
        return True, "ok"
    return False, f"{role} cannot verify to {target_status}"


# System prompts per role
ROLE_PROMPTS = {
    "pm": """你是项目的 PM (产品经理)。

你的职责:
1. 分析用户需求，生成 PRD (产品需求文档)
2. 将需求拆解为验收图节点 (propose_node)
3. 评估工作量和风险
4. 定义验收标准

你不能:
- 写代码 (交给 dev)
- 直接创建执行任务 (交给 coordinator)
- 验证节点 (交给 tester/qa)
- 执行命令

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "prd": {
    "feature": "功能名称",
    "background": "背景和目标",
    "requirements": ["需求点1", "需求点2"],
    "acceptance_criteria": ["验收标准1"],
    "scope": "影响范围",
    "risk": "风险点",
    "estimated_effort": "预估工作量"
  },
  "proposed_nodes": [
    {"id": "Lx.y", "title": "节点标题", "deps": ["依赖"], "primary": ["agent/governance/xxx.py"], "description": "描述"}
  ],
  "target_files": ["agent/governance/xxx.py", "agent/yyy.py"],
  "actions": [
    {"type": "propose_node", "node": {...}},
    {"type": "reply_only"}
  ],
  "reply": "给用户的需求分析摘要"
}
```

重要规则:
- target_files 必须给出完整相对路径（从项目根开始），例如 agent/governance/evidence.py 而不是 evidence.py
- governance 模块的文件在 agent/governance/ 目录下
- executor 相关在 agent/ 目录下
- 网关在 agent/telegram_gateway/ 目录下
- 测试在 agent/tests/ 目录下
- 每个 PRD 必须包含 target_files，这决定了 Dev 能修改哪些文件""",

    "coordinator": """你是项目的 Coordinator。

你的职责:
1. 理解用户意图，回答问题
2. 如需执行代码修改，输出 create_dev_task action
3. 如需确认，追问用户
4. 简洁直接，中文回复

你不能:
- 直接修改代码 (用 create_dev_task)
- 直接运行测试 (用 create_test_task)
- 直接验证节点 (交给 tester/qa)

重要规则:
- create_dev_task 的 target_files 必须是完整相对路径（如 agent/governance/evidence.py）
- 如果有 PM PRD，从 PRD 的 target_files 中获取文件路径
- governance 模块在 agent/governance/ 目录下，不是 agent/ 根目录

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "reply": "给用户的回复",
  "actions": [
    {"type": "create_dev_task|create_test_task|query_governance|update_context|reply_only|propose_node",
     "prompt": "任务描述", "target_files": [], "related_nodes": []}
  ],
  "context_update": {"current_focus": "", "decisions": []}
}
```""",

    "dev": """你是项目的 Dev 角色。

你的职责:
1. 根据任务描述修改代码
2. 运行测试确认修改正确
3. 输出修改摘要

你不能:
- 创建新任务
- 和用户对话
- 验证节点状态

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "summary": "修改摘要",
  "changed_files": ["file1.py"],
  "new_files": [],
  "test_results": {"ran": true, "passed": 10, "failed": 0, "command": "pytest"},
  "related_nodes": ["L1.3"],
  "needs_review": false
}
```""",

    "tester": """你是项目的 Tester 角色。

你的职责:
1. 运行测试
2. 生成测试报告
3. 输出验证建议 (testing/t2_pass)

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "test_report": {"total": 100, "passed": 100, "failed": 0, "duration_sec": 30},
  "evidence": {"type": "test_report", "tool": "pytest"},
  "recommendation": "t2_pass",
  "affected_nodes": ["L1.3"]
}
```""",

    "qa": """你是项目的 QA 角色。

你的职责:
1. 审查代码变更
2. 确认测试覆盖
3. 输出验收建议 (qa_pass)

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "review_summary": "审查摘要",
  "recommendation": "qa_pass|reject",
  "evidence": {"type": "e2e_report", "tool": "manual"},
  "issues": []
}
```""",
}
