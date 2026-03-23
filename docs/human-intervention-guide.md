# 人工介入流程与边界

## 一、哪些操作需要人工介入

### 必须人工（AI 不可自主执行）

| 操作 | 原因 | 介入方式 |
|------|------|---------|
| 密码/Token 重置 | 安全敏感 | 人类运行 init_project.py |
| refresh_token revoke/rotate | 撤销凭证 | 人类调 /api/token/revoke |
| release-gate 最终确认 | 发布决策 | 人类在 Telegram 确认 |
| rollback 执行 | 回退有风险 | 人类在 Telegram 确认 |
| 删除节点/项目 | 不可逆 | 人类直接操作 |
| Scheduled Task 权限授权 | 平台限制 | 人类点 "Always allow" |

### 建议人工（AI 可执行但应先确认）

| 操作 | 原因 | 确认方式 |
|------|------|---------|
| 新节点创建 | 架构决策 | AI 提议 → Telegram 通知人类 → 人类回复确认 |
| baseline 批量状态变更 | 影响范围大 | AI 列出变更清单 → 人类确认 |
| 跨项目操作 | 影响其他项目 | Telegram 通知人类确认 |

### AI 可自主执行

| 操作 | 条件 |
|------|------|
| verify-update (testing/t2_pass) | 有测试证据 |
| qa_pass | 有 e2e 证据 + artifacts 通过 |
| coverage-check | 随时可跑 |
| context save/load | 自动 |
| 记忆写入 | 自动 |
| 消息回复 | 非敏感内容 |

## 二、Scheduled Task 中的人工介入

### 触发条件

Task 遇到以下情况时停止自动处理，通知人类：

```python
HUMAN_REQUIRED_KEYWORDS = ["紧急", "urgent", "人工", "manual", "帮我", "help me"]
DANGEROUS_KEYWORDS = ["rollback", "delete", "revoke", "release", "deploy", "回滚", "删除", "发布"]
```

### 通知格式

```
[需要人工介入]
原因: 用户请求涉及发布操作
用户消息: "帮我把 amingClaw 发布一下"
建议操作: 人类确认后执行 POST /api/wf/amingClaw/release-gate

请在 Telegram 回复 "确认" 或 "取消"
```

### 人工确认流程

```
Task 检测到需要人工介入
    │
    ▼
回复 Telegram: "[需要人工介入] 原因 + 建议"
    │
    ▼
消息不 ACK（留在队列里）
    │
    ▼
下一次 Task 触发:
    检查队列 → 发现未 ACK 消息 → 检查是否有人类的确认回复
    │
    ├── 人类回复 "确认" → 执行操作 → ACK 两条消息
    ├── 人类回复 "取消" → 跳过 → ACK 两条消息
    └── 无回复 → 再次提醒（最多 3 次后自动取消）
```

## 三、验收中的人工介入

### 哪些验收需要人工

| 节点类型 | 自动验收？ | 人工要求 |
|---------|----------|---------|
| 有单元测试的 | ✅ tester 自动 | QA 可自动（有 e2e_report） |
| 基础设施变更 | ✅ tester 自动 | QA 需人工确认服务正常 |
| Scheduled Task 行为 | ❌ | 必须人工：发消息 → 看回复 → 确认 ACK |
| UI/Telegram 交互 | ❌ | 必须人工：看截图 → 确认 |
| 安全相关 (token/auth) | ❌ | 必须人工验证 |

### 人工验收标记

verify-update 的 evidence 中加 `manual_verified` 字段：

```json
{
  "type": "e2e_report",
  "producer": "qa-agent",
  "tool": "manual_e2e",
  "summary": {
    "passed": 1,
    "failed": 0,
    "manual_verified": true,
    "verified_by": "human",
    "verification_method": "telegram_message_test",
    "notes": "发消息测试，回复正确，ACK 正常，不重复"
  }
}
```

### 人工验收流程

```
AI 完成实现
    │
    ▼
AI 通过 Telegram 通知人类:
  "L9.9 Scheduled Task 管理已实现，需要人工验收：
   1. 请在 Telegram 发送一条测试消息
   2. 等待 1 分钟看是否收到回复
   3. 确认回复内容正确
   4. 确认消息没有重复
   5. 回复 '验收通过' 或 '验收失败: 原因'"
    │
    ▼
人类测试并回复
    │
    ├── "验收通过" → AI 提交 verify-update (qa_pass)
    │   evidence: {manual_verified: true, verified_by: "human"}
    │
    └── "验收失败: xxx" → AI 修复 → 重新请求验收

## 四、边界总结

```
完全自动化:
  代码测试 → verify-update → coverage-check → 记忆写入

需要人工确认:
  新节点创建 → baseline → 跨项目操作

必须人工执行:
  Token 管理 → 发布确认 → Scheduled Task 授权

人工验收:
  UI 交互 → Telegram 行为 → 安全功能
```
