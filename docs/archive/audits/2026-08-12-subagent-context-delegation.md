# Subagent 上下文、公共空间与委托权限安全审查

## 文档状态

本文是已归档的工程复盘与安全分析，记录 2026-08-12 针对 `testcode` harness 的审查证据、风险判断
和修复过程。对应修复提交为 `66f7d14`。本文不再定义当前行为，也不作为未完成工作的权威清单；
现行运行时契约以[Subagent 会话集群](../../core/subagent-session-clusters.md)、
[Agent 执行循环](../../core/agent-loop.md)、[执行安全](../../core/execution-safety.md)和
[审批代理与委托授权](../../core/approval-delegation.md)为准，后续优先级以[项目路线图](../../roadmap.md)为准。

文中候选字段仅用于保留当时的分析上下文，不冻结接口。

截至 2026-08-12，本审查推动的修复已覆盖：显式副作用与资源合同、只读 policy 强制、后台权限
派发前准入、运行期能力快照、公共状态降权与折叠、结构化 blocker、输出重复隔离、完成证据校验、
校验后统一日志终态、主循环未解决失败汇总、批量 partial 语义、无集群空状态、模型身份事实、根成员
生命周期、SessionStore 锁/revision/原子替换和继承上下文上限。审批代理、语义级离题检测、artifact
所有权/完整性仓库以及全链路 token/日志硬预算仍属于后续增强，不应被视为已经完成。

审查基于 2026-08-11 的真实运行记录：

- 主会话：`20260811070346188569-816917fd`
- 文档审查子会话：`20260811071841752743-c784ea9d`
- 测试执行子会话：`20260811071841754349-460cd3ef`
- 集群：`cluster-20260811T071841-f2a79c35268f`

本文关注以下问题：

1. 主会话历史、恢复状态和能力如何进入子会话。
2. 集群公共空间中的内容由谁产生、以什么信任级别进入模型上下文。
3. 父会话批准运行子会话后，实际授予了什么权限，权限是否受任务意图约束。
4. run、session、cluster 和 public state 如何记录同一次执行的结果。
5. 模型异常输出如何被检测、隔离、持久化和恢复。

## 1. 总体结论

当前实现已经具备会话隔离、attempt 原子提交、工作区边界和结构化公共状态的基本框架，但以下
三个边界仍没有闭合：

1. **上下文边界没有按信任级别隔离。** 模型生成的公共状态摘要被重新包装为 `system` 消息，
   使一个子会话可以通过公共空间影响其他子会话的高优先级上下文。
2. **委托授权没有绑定任务允许的副作用。** runner 签发的授权绑定了会话、attempt 和工作区，
   但没有绑定“本任务只读”或允许修改的资源；明确禁止修改的任务仍具备自动 `patch` 能力。
3. **结果状态没有单一权威提交点。** 子 runtime 先完成日志，runner 再做后置完成校验，导致同一
   attempt 可以同时显示为 run `completed`、session `blocked`、cluster `blocked` 和 public state
   `stalled`。

这三项会互相放大：错误上下文会诱发错误工具调用或异常输出，过宽授权允许错误调用产生真实副作用，
而分裂的状态记录又可能把未完成或异常执行显示为成功。

## 2. 真实运行复盘

### 2.1 主会话

主会话暴露了以下问题：

- 首轮回答错误地自称 Claude，但模型请求日志记录的实际模型为
  `deepseek-ai/deepseek-v4-flash-0731`，说明运行时模型身份没有作为可信事实进入回答边界。
- 工具试用轮中，父会话在当前 run 激活了 `pytest-helper`、`git-helper` 和 `subagents`，但随后创建的
  子会话没有继承这些运行期能力。
- 用户选择 `A+B` 后，模型只执行目录和文件定位就返回 `done=true`，一段明显异常且仍使用将来时的
  文本被记录为 `completed`。
- 用户输入“继续”后，运行时只对“继续”做意图分类，没有继承前一轮的修改任务、开放 blocker 和
  验证要求。
- 最后一轮修改了分类器和测试，却没有运行最终测试；由于最后一个工具结果是成功的 `patch`，整轮
  仍被记录为 `completed`。
- 最终回复再次出现重复、多语言碎片和乱码，但没有触发输出质量隔离。

修复前的全量测试结果为 `434 passed, 1 failed`。当时的失败用例说明新增的中文否定逻辑会把
“无需修改源码，直接写报告”整体判成只读，忽略后半句仍要求产生写入结果。该问题已随本轮修复，
当前工作区全量测试结果为 `455 passed`。

### 2.2 文档审查子会话

原任务要求读取文档、输出不超过 300 字的摘要，并明确禁止修改文件。

实际执行链：

1. 子会话成功调用 `read_file` 并生成了可用摘要。
2. 意图分类器把“禁止修改任何文件”中的“修改”识别为正向文件修改意图。
3. 修改任务进度策略触发 `progress_guard`。
4. 模型按照护栏允许的路径解释并输出结果，但 `progress_guard` 仍作为最后一个失败工具结果存在，
   因而 run 被记为 `stalled`。
5. 父会话恢复同一子会话，要求解释任务性质；子 runtime 本身返回 `completed`。
6. runner 在 runtime 日志已经 finalize 后再次按任务文本分类，并因缺少成功 `patch` 把结果改为
   `stalled/blocked`。

最终同一 attempt 出现冲突状态：

| 投影 | 记录结果 |
| --- | --- |
| 子会话 run trace | `completed` |
| 子会话 resume state | `completed` |
| 子会话 status | `blocked` |
| cluster member | `blocked` |
| public state metadata | `stalled` |

此外，摘要实际超过用户要求的 300 字，但完成校验没有检查交付约束，只检查是否存在 `patch`。

### 2.3 测试执行子会话

原任务要求运行一个 pytest 文件并报告数量与耗时。

实际执行链：

1. 父会话本轮已激活专用 `run_tests` 能力，但子会话没有继承，模型只能选择 `shell_exec`。
2. 后台子会话没有交互审批通道；`shell_exec` 属于 `execute`，因此返回 `approval_required`。
3. 运行时没有立即用结构化 blocker 收敛任务，而是继续调用模型。
4. 模型最终生成大量重复噪声，并混入与任务无关的 Interactive Brokers、现金和货币基金内容。
5. runner 使用模型最终文本作为 blocker 摘要，真正的 `approval_required`、命令和风险信息没有进入
   公共交付。
6. 噪声被写入 messages、run trace、resume state、open issue 和 cluster public state，随后又作为
   上下文注入其他子会话。

本地历史中未发现该金融内容在模型响应前出现。仅凭当前证据不能断言上游发生跨用户泄漏，但可以
确认模型服务发生了严重离题或上下文污染，而 harness 没有检测、隔离和停止传播。

## 3. 当前数据流与信任错位

当前主要路径可以概括为：

```text
主会话 conversation / persisted capability ids
  → subagent_spawn(inherit)
  → child.messages + child.active_capability_ids
  → runner 读取 cluster.shared_state[-20:]
  → 拼成 role=system 的公共状态消息
  → 子模型请求
  → 模型文本 + ToolResult
  → runtime logger finalize
  → runner completion check
  → session / cluster / public state 分别持久化
```

其中存在四次信任升级：

1. 父会话的全部历史默认进入独立子任务，而不是按任务需要选择。
2. 模型生成的公共摘要被提升为 `system` 消息。
3. 父会话批准“运行子会话”被解释为整个工作区内的结构化写入授权，即使子任务明确只读。
4. 模型最终文本被当作 blocker、open issue 和恢复依据，即使它与结构化工具结果冲突。

## 4. 漏洞与实现缺口

### SG-CTX-001：公共状态的系统级提示注入

**严重度：P0**

公共状态由子模型产生，却以 `role=system` 注入其他子模型。恶意、被提示注入或发生退化的子会话
可以借此影响兄弟会话。`_public_context` 接收 `session_id` 却没有使用，也没有过滤当前作者、任务、
attempt 或已过期 revision。

需要保持的不变量：

> 模型生成内容、外部内容和其他 Agent 内容只能作为不可信数据进入上下文，不能因进入公共空间而
> 自动升级为系统指令。

### SG-CTX-002：旧 attempt 与兄弟状态未折叠

**严重度：P1**

公共空间保持 append-only 是合理的，但 prompt 消费端只是平铺最近 20 条摘要，没有携带 revision、
attempt、supersedes 或 validation 状态。恢复后的子会话会同时看到自己的旧 blocker、兄弟噪声和
当前新任务，无法判断哪条仍然有效。

### SG-CTX-003：默认 inherit 携带无关完整历史

**严重度：P2**

文档阅读和单文件测试都是边界清晰的独立任务，但默认使用 `inherit`，复制了父会话此前全部对话。
公开 spawn 接口又不能为 `fresh` 子会话动态指定能力，因此模型常在“继承过多上下文”和“缺少能力”
之间二选一。

### SG-AUTH-001：委托授权未绑定任务副作用

**严重度：P0**

`SubagentExecutionGrant` 只绑定身份、attempt 和 workspace root，没有绑定：

- 任务是否只读；
- 允许的风险类型；
- 允许修改的路径；
- 允许调用的能力；
- 最大调用次数或期限；
- 用户明确禁止的动作。

因此“禁止修改任何文件”当前只是一条模型提示，不是运行时授权约束。

### SG-AUTH-002：不可执行任务缺少派发前准入

**严重度：P1**

当前安全策略明确规定后台子会话不能自动执行 `execute`、`test`、`network` 和 `destructive` 动作。
该策略符合现行安全设计；问题是调度器仍允许把必需这些动作的任务交给后台子会话，也没有审批代理
或父会话回退路径。

结果是任务在运行后才以 `approval_required` 失败，浪费模型调用并产生无意义恢复回合。

### SG-CAP-001：同一 run 的能力激活不能被继承

**严重度：P1**

运行期能力保存在当前 warehouse；`subagent_spawn` 通过 session id 从磁盘重新加载父会话。磁盘会话
只持有此前持久化的 session-scope 能力，无法看到本轮刚激活的 run-scope 能力。这与“inherit 复制
创建时已激活能力”的文档语义不一致。

### SG-STATE-001：完成状态没有单一提交点

**严重度：P0**

runtime 在 runner 完成后置校验前 finalize 日志。runner 修改 summary 后，logger 的
`last_run_summary` 已经冻结，导致 trace 与 cluster 不一致。

状态提交必须满足：

> 同一 attempt 的最终 outcome、摘要和证据只计算一次，并由一次事务或同一不可变结果对象投影到
> run、session、cluster、public state 和 resume state。

### SG-STATE-002：主运行只用最后工具结果决定 outcome

**严重度：P0**

前序 `subagent_blocked`、失败测试或开放 blocker 可以被后续成功 read/patch 掩盖。模型返回
`done=true` 也没有任务级完成证据检查。这使 `completed` 失去审计价值。

### SG-STATE-003：根成员永久处于 running

**严重度：P2**

cluster 创建时把主成员设为 `running`，当前没有发现与主 run 生命周期同步的更新路径。于是会同时
出现主 session `active`、last run `completed`、root member `running` 和 child `blocked`。

### SG-COMP-001：完成校验以工具名代替交付证据

**严重度：P1**

文件修改任务只认可成功的 `patch`。这会拒绝合法的其他交付方式或“检查后无需修改”，也会在意图
误判时诱导只读任务写文件。相反，测试任务无需 `run_tests`，阅读任务无需读取目标，字数、格式和
artifact 存在性也不校验。

### SG-COMP-002：进度护栏的允许路径无法成功结束

**严重度：P1**

`progress_guard` 告诉模型可以“解释为什么无需修改并结束”，但自身是失败结果。模型按说明结束时，
最后结果仍是 `progress_required`，最终 outcome 会被记为 `stalled`。

### SG-BLOCK-001：blocker 丢失结构化原因

**严重度：P0**

子会话 blocked 时，runner 主要发布模型最终文本，而不是失败 ToolResult 的结构化原因。实际的
`approval_required` 被噪声覆盖，父会话无法判断应等待审批、改由主会话执行、缩小任务还是取消。

### SG-OUT-001：异常模型输出被当作可信状态传播

**严重度：P0**

parser 对部分非严格协议文本采用兼容性接受；运行时又缺少重复度、相关性和证据一致性检查。异常
文本会进入所有恢复和协作投影，没有 quarantine 状态，也没有保留“原始响应异常、结构化 blocker
仍有效”的区分。

### SG-BATCH-001：批量结果缺少 partial 和 unresolved 语义

**严重度：P1**

只要一个子会话 blocked，整个 `subagent_run_ready` 返回失败；没有 ready 成员时，`all([])` 又会让
空结果返回成功。前者掩盖部分成功，后者可能掩盖仍存在的 blocked 成员。

### SG-HEALTH-001：能力健康与业务调用失败混为一谈

**严重度：P2**

无集群时查询 status、用户拒绝审批、任务 blocker 或参数错误都会把 toolbox 标成 degraded。这些
结果不代表能力实现或连接不健康。服务健康、调用结果和任务状态需要三个独立维度。

### SG-PERSIST-001：普通会话写入没有并发保护

**严重度：P2**

cluster store 使用锁和原子替换，SessionStore 则直接覆盖 JSON 和 trace。主 TUI、runner 和恢复流程
并发保存关联状态时，存在消息、能力、trace 或 resume state 被旧快照覆盖的风险。

### SG-BUDGET-001：上下文与日志无统一预算

**严重度：P2**

完整 conversation 和工具输出在每一模型轮次重复进入 prompt。目标主运行的 20 次模型响应累计约
`502,975` prompt tokens，单次最高约 `34,655`。九个 run 的 `events.jsonl` 和 `details.log` 合计已
超过 12 MB；details 又在 Turns 和 Timeline 中重复展开同一内容。

上下文膨胀会增加成本，也会让早期异常文本反复出现并提高后续退化概率。

## 5. 目标信任模型

上下文应按来源和权威级别分层，而不是都转换成聊天消息：

| 层级 | 内容 | 权威性 | 模型能否改写 |
| --- | --- | --- | --- |
| Runtime Policy | 系统安全规则、真实执行边界 | 权威 | 否 |
| Task Contract | 用户目标、允许副作用、资源范围、所需证据 | 权威 | 否，只能请求澄清 |
| Runtime Facts | session、attempt、权限、工具结果、权威状态 | 权威 | 否 |
| User Conversation | 用户原始对话 | 用户输入 | 否 |
| Delegated Context | 父会话选择的有界任务背景 | 非权威背景 | 否 |
| Shared Observations | 其他成员的发现和摘要 | 不可信证据 | 可以质疑、验证 |
| Model Working Text | 推理摘要和候选结论 | 不可信提议 | 是 |

公共状态只能进入 `Shared Observations`，不得进入 `Runtime Policy`。即使使用 OpenAI-compatible
messages 表示，也应放在明确标注为不可信数据的 user/tool 内容区，并通过结构化封装转义潜在指令。

## 6. 目标任务与授权契约

建议把自然语言任务与运行时可执行合同分开。字段名仅为候选：

```text
DelegatedTask
  task_id
  parent_session_id
  child_session_id
  attempt
  objective
  context_policy
  allowed_effects
  allowed_resources
  required_evidence
  budgets
  approval_policy
```

其中：

- `context_policy` 决定使用 fresh、选择性继承、镜像或显式上下文引用。
- `allowed_effects` 明确 read/write/test/execute/network，而不是由模型从文字猜测后扩大。
- `allowed_resources` 把权限限制到具体目录、文件或 artifact namespace。
- `required_evidence` 描述完成所需的读取、修改、测试、artifact 或结构化回答证据。
- `budgets` 限制模型回合、token、工具次数、日志和 wall time。
- `approval_policy` 表示遇到额外权限时应阻塞、路由审批、回退父会话还是取消。

最终有效授权必须是以下范围的交集：

```text
effective_grant =
  user_authority
  ∩ parent_delegated_scope
  ∩ task.allowed_effects
  ∩ task.allowed_resources
  ∩ current_policy
```

任何自然语言分类、模型建议、恢复或公共状态都只能进一步收紧，不能扩大这个交集。

## 7. 目标公共状态模型

公共空间继续保持 append-only，但应区分权威状态和不可信内容：

```text
SharedStateEntry
  entry_id
  cluster_id
  author_session_id
  task_id
  attempt
  revision
  kind
  lifecycle_state
  trust_class
  summary
  evidence_refs
  supersedes
  validation_state
  created_at
```

消费端必须：

1. 按 task、author 和 attempt 分组。
2. 使用 revision 和 supersedes 折叠旧投影。
3. 默认不把当前成员自己的旧最终摘要重新注入。
4. 只选与当前任务相关的兄弟发现。
5. 将模型摘要标为 untrusted，不提升为 system 指令。
6. 对异常输出只暴露结构化错误类别，不传播原始噪声。
7. 对 artifact 引用校验存在性、所有权、完整性和授权，而不只检查相对路径形式。

## 8. 目标完成与状态提交流程

推荐顺序：

```text
子 runtime 执行
  → 收集 ToolResult 和模型候选回答
  → 输出协议与质量检查
  → Task Contract 证据校验
  → 计算唯一 FinalAttemptResult
  → 原子提交 attempt 终态
  → 从同一结果投影 run/session/cluster/public/resume
  → logger finalize
```

`FinalAttemptResult` 至少包含：

- outcome；
- 用户可见摘要；
- 结构化 blocker；
- changed files；
- verifications；
- artifact refs；
- unresolved requirements；
- output validation 状态；
- attempt 和 revision。

logger 不应在完成校验之前冻结 trace；resume state 也不能再从最终自然语言文本反推开放问题。

## 9. 异常输出隔离

模型输出进入持久化和公共空间前至少检查：

- 协议是否符合当前模式；
- 重复片段比例是否异常；
- 是否包含明显无关主题漂移；
- 是否声称使用了不存在的工具或证据；
- 是否与 ToolResult、Task Contract 或状态机冲突；
- 是否满足明确长度和格式约束。

异常时应生成运行时拥有的结构化结果：

```text
outcome: model_output_invalid
public_summary: 子会话模型输出未通过质量检查
blocker: 原始有效工具错误或“需要重新生成”
raw_response_ref: 仅审计可见的受控 artifact
```

原始异常文本不能进入兄弟会话上下文、resume open issue 或面向用户的默认摘要。

## 10. 修复优先级

### P0：先关闭信任与授权漏洞

1. 公共状态不再以 system 消息注入。
2. 委派合同增加只读/写入等显式 allowed effects，纯阅读任务在 policy 层禁止 patch。
3. blocker 从 ToolResult 和运行时状态生成，不使用未经验证的模型文本覆盖。
4. 完成校验前不 finalize logger，统一 FinalAttemptResult。
5. 主运行 outcome 汇总所有 unresolved requirements，不再只看最后工具结果。
6. 增加异常输出 quarantine，停止污染 public state 和 resume state。

### P1：修复调度、能力和恢复

1. 派发前检查任务是否需要后台无法取得的权限。
2. 将当前运行期能力快照显式传给 child，或允许 spawn 合同声明最小能力集合。
3. 为测试、读取、修改和 artifact 任务定义最小证据契约。
4. 公共状态按 revision、attempt 和 supersedes 折叠。
5. 批量执行返回 completed/blocked/failed 的分组结果和 unresolved 总览。
6. “继续”从结构化 workflow checkpoint 恢复目标和验收条件。

### P2：改善状态、健康与成本

1. 无集群 status 返回空快照，不视为能力失败。
2. 分离 capability health、invocation outcome 和 task state。
3. 明确 root member 与主 run 的生命周期关系。
4. SessionStore 使用锁、revision 和原子替换。
5. 对 conversation、tool history、public state、日志和模型请求实施统一预算。
6. 将实际模型身份作为可信 runtime fact 提供给模型和用户界面。

## 11. 必需的端到端测试

当前单元测试无法覆盖真实链路。至少需要增加：

1. 只读子任务即使模型请求 patch，也在运行时被拒绝。
2. 公共状态中的伪 system 指令不能改变兄弟会话 policy 或任务。
3. 本轮 activate 后立即 spawn，子会话获得声明的最小能力。
4. 需要 test/execute 且无审批代理的任务在派发前返回可执行性错误。
5. runtime completed、runner 校验失败后，run/session/cluster/public/resume 五处状态一致。
6. 前序 blocker 后执行成功 read/patch，主 run 仍保持 blocked 或 partial。
7. progress guard 后合法解释无需修改可以完成。
8. 测试任务没有测试证据时不能 completed。
9. 一个子会话成功、一个 blocked 时返回 partial；没有 ready 但存在 blocker 时不能返回空成功。
10. 旧 attempt 公共状态不会作为当前事实注入。
11. 重复噪声、离题内容和证据冲突进入 quarantine，不传播到其他会话。
12. 并发保存 session 不丢失消息、能力、trace 和 cluster 关联。
13. “继续”恢复未完成任务的 required evidence 和开放 blocker。
14. 用户询问模型身份时，回答与 runtime 配置一致或明确表示不可确认。

## 12. 非目标

本审查不建议：

- 通过放宽 `execute/test` 审批来绕过后台权限问题；
- 让父模型或子模型自行签发审批结果；
- 删除 append-only 公共状态历史；
- 要求主会话重新实现每个成功子任务；
- 用更多自然语言提示代替运行时授权和状态机；
- 把所有模型异常都解释为上游安全事件。

目标是在保留会话集群、并行 runner 和有界 handoff 设计的前提下，把上下文、授权、证据和状态的
权威边界补完整。
