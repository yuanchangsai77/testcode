# 审批代理与委托授权

## 文档职责

本文定义审批请求在会话间传递、代理和验证的通用机制。它回答：

- 子会话需要审批时，审批请求如何安全地从子会话传递到主会话再到用户
- 审批决策如何携带可验证的身份和授权信息返回
- 未来如何扩展到跨终端、跨设备审批
- 为什么不能用"换一种工具"绕过审批

当前各层审批实现见[执行安全](execution-safety.md)；子会话与集群见[Subagent 会话
集群](subagent-session-clusters.md)；未来跨设备授权语义见[未来平台安全与授权](../future/security-and-authorization.md)。

## 1. 核心原则

审批的本质是**人的判断**，不能也不应该被自动化替代。

三个不变量：

1. **[不变量] 审批是人做出的安全决策，任何自动化替代审批等同于提权。**
   高安全性操作可以直接放行是因为它们本身就不需要审批，不是因为"系统代替人审批了"。
   危险性操作不能用换工具、编码、拆分参数等方式规避审批流。

2. **[不变量] 审批请求是带身份和上下文的、可传递的、可验证的消息。**
   审批不只是"问用户是否允许"，它携带：谁在请求、为哪个会话、要执行什么动作、
   风险等级是什么、在当前委派链中的位置。审批决策同样携带：谁批准的、批准什么、
   批准范围、有效期。接收方可以验证这些信息未被篡改。

3. **[不变量] 审批决策的传递链与工作区的信任边界一致。**
   审批只在可验证的会话、集群和信任域内传递。不能从低信任域向高信任域"提权"，
   也不能将审批用于超出原始请求范围的资源或操作。

## 2. 当前审批链路

当前主会话的审批路径：

```text
Engine._execute()
  → guardrails.check(action) → PolicyDecision
  → 如需审批: approval_callback(action, reason)
  → CLI/Presenter.confirm_tool_action() → TUI 用户交互
  → 返回 True/False/None → Engine 继续或阻断
```

关键接口：

| 层次 | 接口 | 职责 |
| --- | --- | --- |
| `DefaultPolicy.evaluate()` | 返回 `PolicyDecision(allowed, requires_confirmation, reason, risk_level)` | 判断是否需要审批 |
| `ExecutionEngine._approval_decision()` | 调用 `approval_callback` 或返回 `None` | 桥接 policy 到交互层 |
| `ConsolePresenter.confirm_tool_action()` | TUI 交互，返回 `True/False` | 用户确认 |
| `ExecutionEngine._execute_action()` | 审批通过后执行，失败生成 `approval_denied`/`approval_required` | 执行与错误码 |

当前子会话在后台运行时的审批路径：

```text
SubagentRunner._run_one()
  → runtime_factory(child) → 创建 CLI(background=True)
  → create_app(background=True) → approval_callback=None
  → Engine 遇到需要审批的动作 → _approval_decision() 返回 None
  → 生成 ToolResult(error_code="approval_required")
  → 子会话进入 blocked
  → Runner 将 blocked 结果写入公共状态
```

子会话的 `approval_callback` 被设为 `None`，因此任何需要审批的动作都直接进入
`blocked`。这是当前行为的正确设计——后台子会话不能让用户交互，也不应该允许
子会话自行决定审批。

## 3. 问题：为什么需要审批代理

当前子会话遇到审批时的行为是"直接 blocked"，这有几个问题：

1. **子会话无法完成需要审批的任务。** 例如用户让子会话执行 `shell_exec` 在
   `confirm` 模式下需要审批，但子会话永远无法获得审批。

2. **blocked 结果对主会话不够透明。** 主会话只知道子会话 blocked，但不知道
   它是因为什么动作、什么参数、什么风险等级被阻塞的，无法做有意义的决策。

3. **没有传递路径。** 即使主会话想让用户审批子会话的某个动作，也没有结构化的
   方式把审批请求从子会话传到主会话，再传到用户终端，再把审批结果传回去。

审批代理解决的是：**让审批请求在会话链中安全传递，让审批决策在会话链中安全返回。**

## 4. 控制面模型

审批数据属于可信运行时控制面，不属于模型协作消息。集群公共状态可以保存审批对象的引用和
脱敏展示摘要，但原始参数、认证材料和授权凭据必须进入独立的 `ApprovalStore`。模型只能看到：

- 请求标识、风险、动作类别和脱敏资源范围；
- 当前状态及用户可理解的原因；
- 不包含凭据的验证结果。

模型不能读取签名、密钥、完整敏感参数，也不能创建批准结果。需要人工审批的请求由运行时自动
路由到可交互终端；模型可以补充风险说明或建议拒绝，但不能批准、静默吞掉或改写请求。

## 5. 可验证对象

### 5.1 ApprovalRequest

审批请求由执行动作的 runtime 生成并写入可信存储，至少包含：

| 字段 | 语义 |
| --- | --- |
| `request_id`、`nonce` | 全局唯一标识和一次性随机挑战 |
| `requester` | workload、session、cluster、attempt 和委派链身份 |
| `audience` | 唯一可消费该决定的 runtime 或信任域 |
| `action_digest` | 对规范化工具名、参数、cwd、工作区和风险的摘要 |
| `display` | 面向用户的脱敏说明，不作为执行参数 |
| `policy_snapshot` | 触发审批的策略版本、风险和原因 |
| `issued_at`、`expires_at` | 请求时限 |
| `key_id`、`algorithm`、`signature` | 请求签发方及其认证签名 |

参数必须先按稳定格式规范化再计算摘要。原始参数加密保存或由发起 runtime 持有；不能把命令、
header、token 等原样放进模型可读公共状态。

### 5.2 ApprovalDecision

用户操作生成的是不可变的审批决定，而不是由主会话模型生成的“授权令牌”：

| 字段 | 语义 |
| --- | --- |
| `decision_id`、`request_id`、`nonce` | 与唯一请求及挑战绑定 |
| `decision` | `approved` 或 `denied` |
| `principal` | 完成人工确认的用户主体 |
| `auth_context` | 终端、认证强度和交互时间等可审计上下文 |
| `audience`、`action_digest` | 防止跨 runtime、跨参数串用 |
| `scope` | 工具、资源、风险和次数边界；默认严格单次 |
| `issued_at`、`expires_at` | 极短有效期 |
| `issuer`、`key_id`、`algorithm`、`signature` | 可信审批服务的签发信息 |

拒绝决定不需要再次审批，也不能转换成批准。会话 ID 不是用户身份；终端只采集用户选择，真正的
决定由可信审批服务按认证上下文签发。

### 5.3 验证与消费

执行前必须验证签名和信任链、签发方、受众、请求及 nonce、动作摘要、策略版本、期限和 scope。
批准决定通过后，还必须在可信存储中原子地从 `pending` 转为 `consumed`；默认只能消费一次。
并发请求、进程重启和消息重放都不能让同一决定执行两次。验证失败是 `approval_invalid`，用户拒绝
是 `approval_denied`，超时是 `approval_expired`，三者不得混用。

## 6. 端到端流程

```text
子会话 runtime → policy 要求人工审批
  → runtime 签发 ApprovalRequest 并写入 ApprovalStore
  → 公共状态仅发布 request_id 和脱敏摘要
  → 成员进入 waiting_approval，保留原动作 checkpoint

Approval Router → 根据 audience 和 presence 自动选择可信交互端
  → 终端重新读取 ApprovalStore 中的请求并校验签名
  → 向用户展示固定动作摘要，采集批准或拒绝
  → Approval Service 签发 ApprovalDecision

子会话 runtime → 收到决定通知后重新读取可信存储
  → 校验并原子消费决定
  → 仅对原 checkpoint 中 action_digest 完全一致的动作执行一次
  → 记录审计事件并继续；拒绝、过期或取消则结束等待
```

主会话和模型不在授权链上。当前只有 TUI 时，Router 把请求送到该 TUI；未来增加其他终端只替换
presence 与传输适配器，不改变请求、决定、验证和消费语义。

## 7. 与当前实现的集成边界

第一阶段实现时需要新增：

- 独立 `ApprovalStore`，不复用模型可读的 shared state 保存秘密或签名对象；
- `ApprovalRouter`、`ApprovalService` 和终端 `ApprovalPresenter`；
- `waiting_approval` 状态及可恢复的原动作 checkpoint；
- runtime 侧请求签发、决定验证和原子消费；
- 审计事件：请求、投递、用户决定、验证、消费、拒绝、过期和取消。

公共状态只新增 `approval_pending`、`approval_resolved` 的脱敏投影。无需提供
`subagent_approve` 工具；模型调用工具不是用户认证，也不应成为批准入口。拒绝可由用户、策略或
超时直接产生，不需要额外授权。

当前实现保持 `approval_callback=None → approval_required → blocked`，直到上述控制面完整落地。
不能先接受未签名 token、把哈希当签名，或把主会话 ID 当作用户身份来做兼容旁路。

## 8. 安全与恢复要求

- 认证密钥由进程凭据或系统密钥库管理，支持轮换；文档不固定具体算法。
- 所有时间判断使用可信时钟，并限制最大有效期和时钟偏差。
- checkpoint 只保存规范化动作引用，不保存可被模型修改后重新绑定的可执行文本。
- 用户中断、会话取消或委派链变化会撤销未消费决定。
- 日志仅记录摘要、主体标识和状态，不记录原始敏感参数、签名或认证凭据。
- 跨设备传输必须同时验证设备、用户主体和目标 runtime，TLS 不能替代对象级签名。

## 9. 第一阶段验收范围

- 请求和决定对象具有真实签名字段、主体、受众、nonce、动作摘要和期限。
- 模型不可生成、修改或读取批准凭据，也不能阻止运行时呈现强制审批。
- 同一批准在并发、重启和重放场景下最多消费一次。
- 参数变化、委派链变化、过期、撤销和错误受众全部拒绝执行。
- 公共状态和日志不暴露原始敏感参数。
- TUI 完成一次子会话请求、用户批准、子会话恢复的端到端测试。
