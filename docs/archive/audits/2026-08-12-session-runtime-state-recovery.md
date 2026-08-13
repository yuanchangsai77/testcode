# Session Runtime 状态一致性与恢复机制审计

## 文档状态

本文是 2026-08-12 针对一次真实主会话故障的工程复盘，记录当时的执行证据、机制性根因和治理建议。
本文不定义当前稳定行为，也不把候选字段或方案冻结为接口；现行契约仍以
[Agent 执行循环](../../core/agent-loop.md)、[Shell 会话生命周期](../../core/shell-session-lifecycle.md)、
[Tool 字段契约](../../reference/tool-contract.md)和[配置参考](../../reference/configuration.md)为准。

审计对象：

- 会话：`20260812014149749448-440cdfeb`
- 首次运行：`2026-08-12T02-15-15.089373+00-00`
- 恢复运行：`2026-08-12T02-39-19.479206+00-00`
- 用户目标：在 `workspaces/` 下创建 AI 留学申请平台项目并形成架构方案

本文关注的不是某一种 patch 文本是否兼容，而是哪些 runtime 机制使一次局部工具失败扩散成整个任务
失控。

## 1. 总体结论

本次故障的主要性质是 **runtime 状态一致性失败**，不是单独的模型输出错误或编辑工具错误。

四个关键机制互相放大：

1. **恢复时丢失显式执行进度，却保留隐式 Shell 状态。** 首次运行已经创建目录并改变 Shell cwd，
   但模型超时后的错误摘要丢弃了成功工具结果；相同 session 恢复时仍复用了 Shell cwd。
2. **没有 runtime 所有的任务状态机。** 当前进度主要依赖模型从自然语言 conversation、history 和
   tool output 中重新推断，无法稳定回答“已经完成什么、当前阶段是什么、下一步应做什么”。
3. **上下文只增不减。** 失败动作的完整参数、输出和模型自述持续进入后续 prompt，没有跨消息总预算、
   语义折叠或 artifact 引用机制，导致延迟、超时和模型退化概率同步上升。
4. **完成判定与恢复依据过度信任模型文本。** 非契约文本 `Dictionary` 被解析为正常终态；runtime 虽将
   outcome 聚合为 `stalled`，却仍把 `Dictionary` 保存为 final message 和 open issue，覆盖了真实 blocker。

表面上的 patch 方言不匹配只是这一链路中最先暴露的接口不一致。即使单独兼容该方言，隐藏状态、恢复
丢失、上下文膨胀和完成误判仍会在其他工具上复现。

## 2. 真实运行复盘

### 2.1 首次运行已经产生部分工作区状态

首次运行实际执行了以下有效动作：

1. 多次读取 workspace 和示例项目。
2. 创建 `workspaces/study-abroad/docs`、`src` 和 `tests`。
3. 执行 `cd /home/changsai/testcode/workspaces/study-abroad`。
4. 创建 `docs/architecture`、`docs/design`、`src/testcode` 等子目录。

之后模型服务发生连续超时。该运行共有：

- 19 次模型请求；
- 10 次有效响应；
- 9 次超时；
- 有效响应累计约 117,499 prompt tokens；
- 最终连续 8 次请求均在 60 秒截止时间后失败。

CLI 捕获 `RuntimeError` 后创建了新的 `ExecutionSummary`，并把 `tool_results` 设为空。因此首次运行在
session trace 中显示没有工具，尽管工作区和 Shell 都已经发生真实变化。

### 2.2 恢复运行继承了错误的状态组合

用户输入“继续”后，恢复运行获得了：

- conversation 中的原始长需求；
- 上一次“Model API is unavailable”的最终文本；
- 压缩后的 run outcome 和通用 recovery hint；
- 同一 session 保留的 ToolRegistry 状态和持久化 Shell 进程。

它没有获得：

- 首次运行成功工具的结构化清单；
- 已创建目录的 checkpoint；
- Shell 当前 cwd；
- 当前任务阶段；
- 尚未生成任何文件这一完成差距。

因此恢复后的模型重新开始检查目录。结构化工具仍以请求工作区
`/home/changsai/testcode` 为路径基准，而 Shell 已位于
`/home/changsai/testcode/workspaces/study-abroad`。同一个相对路径在两类工具中被解释成不同位置，产生
“目录存在”和“目录不存在”两组表面冲突的证据。

### 2.3 恢复运行没有形成有效收敛

恢复运行共有：

- 23 次模型请求；
- 18 次有效响应；
- 5 次超时；
- 有效响应累计约 214,653 prompt tokens；
- 4 次失败的 patch 调用；
- 最终产物仍然只有空目录。

模型请求中的用户区文本从约 800 字符增长至约 21,631 字符；单次 prompt 从约 8,372 tokens 增长至
约 14,705 tokens。最后一次模型响应只有两个 completion tokens，内容为 `Dictionary`。

解析器将该文本解释为 `done=true`。执行引擎随后根据历史未解决工具失败把 outcome 聚合为 `stalled`，
但 final message 仍为 `Dictionary`。SessionStore 又直接使用 final message 构造 open issue，最终恢复状态
变成：

```text
last_outcome: stalled
open_issue: Dictionary
```

真实的路径状态冲突、编辑协议不匹配和未生成文件均未进入下一次恢复的权威 blocker。

## 3. 故障传播链

```text
首轮创建目录并改变 Shell cwd
  ↓
模型连续超时
  ↓
CLI 错误摘要丢弃成功 ToolResult
  ↓
session 恢复看不到已完成工作
  ↓
同一 session 的隐藏 Shell cwd 继续存活
  ↓
结构化工具与 Shell 使用不同相对路径基准
  ↓
模型收到互相矛盾的观察结果并重新探索
  ↓
失败动作参数、输出和模型自述持续进入 history
  ↓
prompt 膨胀、响应变慢、超时和退化增加
  ↓
异常终态 Dictionary 被 runtime 接受
  ↓
模型文本覆盖真实 blocker，污染后续恢复
```

## 4. 机制性缺口

### RT-STATE-001：异常终止丢弃部分成功结果

**严重度：P0**

前台 CLI 将广泛的 `RuntimeError` 统一包装为“Model API is unavailable”，同时把 `tool_results` 清空。

影响：

- 工作区真实状态与 session trace 分裂；
- 已完成动作无法进入恢复 checkpoint；
- 工具调用记录和 Shell 状态的生命周期不一致；
- parser、engine 或 tool runtime 的内部错误也可能被误报为模型服务不可用。

需要保持的不变量：

> run 异常终止不能抹除此前已经确认成功的工具结果；错误摘要必须在保留部分进度的基础上追加，而不是
> 替换执行事实。

### RT-STATE-002：隐式工具状态跨 run 保留但不可见

**严重度：P0**

相同 session 的 ToolRegistry 状态会跨多次 engine execute 保留，Shell cwd 因此跨 run 存活。模型提示却
描述为 Shell 状态仅在当前 run 内保留，恢复上下文也没有投影真实 cwd。

影响：

- 模型依据错误的生命周期契约规划路径；
- session 恢复不具备确定性；
- 环境变量、cwd 等隐藏状态可能影响远离其创建点的后续操作；
- 结构化工具和 Shell 出现双重坐标系。

需要保持的不变量：

> 任何会影响工具语义且跨模型轮次或 run 存活的状态，都必须由 runtime 明确拥有，并作为结构化事实
> 投影给模型；否则该状态必须在边界处重置。

### RT-STATE-003：缺少 runtime 所有的任务状态机

**严重度：P0**

当前 `SessionContext` 保存模型消息、工具结果和自然语言 history，但不保存目标、阶段、交付条件、已完成
产物、未解决 blocker 和下一允许动作。

结果是 runtime 只能通过提示词要求模型“不要重复读取”，无法确定性判断：

- 目录是否已经确认；
- 是否已经产生用户要求的文件；
- 当前应继续探索、修改、验证还是结束；
- 某次新调用是否在推进同一计划。

重复动作去重和 progress guard 只能抑制完全相同的调用，不能替代任务状态机。

### RT-CTX-001：跨消息上下文没有总预算和语义归约

**严重度：P0**

当前只对单个 ToolResult output 设置字节上限；conversation、history、工具参数、项目规则、工具定义和
恢复信息没有统一总预算。每轮 prompt 会重放当前 run 的全部 history。

特别是 ToolResult history 会携带完整 `action_arguments`。对于创建大文档的失败动作，完整文档内容会
再次进入下一轮模型输入。失败次数越多，上下文越大。

需要保持的不变量：

> 模型上下文应由 runtime 根据任务状态生成有界投影，而不是把事件日志直接拼接成 prompt。大参数和
> 大输出应以 artifact/reference 保存，prompt 只包含摘要、稳定事实和按需回查入口。

### RT-ERR-001：错误码没有完整恢复语义

**严重度：P1**

当前错误码主要表达失败类型和部分停止条件，没有统一描述：

- 是否可以原样重试；
- 重试前必须改变什么；
- 推荐的下一动作；
- 连续发生多少次后终止；
- 是否使 read state、cwd 或其他 checkpoint 失效。

`invalid_patch` 不属于不可重试集合，工具输出又只说明“没有 changed files”。runtime 因此允许模型持续
生成同类失败动作，而不能识别这是稳定的协议级不匹配。

需要保持的不变量：

> 恢复策略属于 runtime 控制面，不能完全依赖模型阅读自由文本错误后自行决定。

### RT-PROTOCOL-001：提示契约、传输约束和解析容错互相冲突

**严重度：P0**

系统提示要求模型始终返回严格 JSON，但模型客户端只发送普通 messages 和 native tools，没有请求强制
JSON schema 输出。解析器又为了兼容普通文本，将任意非空文本当作最终完成。

这形成了三层不一致：

1. prompt 声称严格；
2. transport 没有强制；
3. parser 按宽松模式接受。

因此 `Dictionary` 能合法穿过 runtime。单纯继续加强提示词不能闭合该边界。

### RT-COMPLETE-001：模型完成声明没有交付门禁

**严重度：P0**

当前 `done=true` 且无 action 会直接结束循环。runtime 不验证：

- 用户要求的文件是否存在；
- 是否发生预期工作区变更；
- 是否还有未解决工具错误；
- 最终文本是否具有基本有效性；
- 请求特定的完成证据是否满足。

`_aggregate_outcome` 可以在事后把结果改成 `stalled`，但不会重新生成与真实 blocker 一致的 final message。
因此 outcome、用户消息和 resume state 仍然分裂。

### RT-RESUME-001：恢复状态以最终文本代替结构化 blocker

**严重度：P0**

只要 run 不是 `completed`，SessionStore 就把 `trace.final_message` 作为 open issue。它没有选择最近的未解决
工具失败、任务完成差距或 runtime exception。

模型生成的异常文本因此可以覆盖控制面事实，并在下一次恢复时继续影响模型。

需要保持的不变量：

> open issue 必须来自 runtime 计算的结构化 blocker；模型文本只能作为用户说明或低信任补充，不能成为
> 恢复控制面的唯一依据。

### RT-RETRY-001：重试只有单请求边界，没有 run 级熔断

**严重度：P1**

当前模型调用支持单轮最多 7 次重试，但缺少：

- 整个 run 的墙钟时间预算；
- 总请求数和总 token 预算；
- 连续超时熔断；
- 服务健康状态；
- 重试前的上下文压缩或降级；
- 根据请求规模和模型能力调整策略。

首次运行最后连续 8 个 60 秒请求全部失败。重试没有增加新信息，却显著延长冻结时间。

### RT-OBS-001：model attempt 被统计为语义 turn

**严重度：P1**

日志分组把每条 `model.request` 当作新 turn，超时后的 retry 也产生新的空 turn。首次运行记录 19 turns，
实际是 10 个响应加 9 个超时；恢复运行记录 23 turns，实际是 18 个响应加 5 个超时。

这会扭曲：

- 无进展回合判断；
- 运行效率指标；
- 故障复盘中的模型行为；
- 未来基于 turn count 的预算与告警。

应明确区分 semantic turn、model attempt、tool phase 和 approval wait。

### RT-MODEL-001：只有 HTTP 兼容，没有 Agent 语义能力协商

**严重度：P1**

当前客户端把 OpenAI-compatible chat completion 视为统一模型接口，但不同模型在以下方面可能不同：

- native tool call 稳定性；
- strict JSON/schema 支持；
- 长上下文退化行为；
- 并行工具行为；
- 编辑协议偏好；
- 超时和首 token 延迟特征。

本次实际模型多次生成与工具契约不同的编辑文本，最终又返回 `Dictionary`。这不能只归因于模型质量；
runtime 在启动时没有能力探测、模型 profile 或协议降级策略。

## 5. 建议的目标运行模型

### 5.1 Runtime-owned Task Checkpoint

每个 run 应维护结构化任务 checkpoint，至少表达：

```text
objective
phase
confirmed_facts
completed_actions
artifacts
unmet_deliverables
blocking_conditions
next_allowed_actions
workspace_revision
tool_runtime_state_projection
```

工具成功后由 runtime 更新 checkpoint；模型只能提出计划和候选动作，不能单方面宣告执行事实。

### 5.2 Prompt Context Package

模型输入应由有界 packager 生成，而不是重放事件日志：

```text
稳定项目规则
+ 当前目标和阶段
+ 最近确认事实
+ 未解决 blocker
+ 产物摘要与引用
+ 最近少量原始交互
+ 当前可见工具
```

以下内容默认不重复内联：

- 已归档的长用户原文；
- 大型 patch/diff 参数；
- 重复目录列表；
- 已被新状态取代的错误；
- 模型重复自述。

### 5.3 统一路径与工具状态语义

可接受的设计至少有一种：

1. 每个 run 开始时把 Shell cwd 重置到请求 cwd；
2. Shell 状态允许跨 run，但每轮都把真实 cwd/env projection 注入 runtime facts；
3. 所有工具统一以显式 `cwd_id` 或 workspace-relative path 解析，不再依赖隐藏进程 cwd。

无论采用哪种方式，prompt、文档、实现和恢复状态必须使用相同生命周期语义。

### 5.4 结构化错误恢复合同

ToolResult 或相邻控制面对象应能够表达：

```text
category
retryability
required_state_change
recommended_next_action
invalidated_state
attempt_limit
blocker_summary
```

runtime 应根据合同决定是否允许再次调用、是否必须换策略以及何时停止，而不是只把错误文本交给模型。

### 5.5 完成门禁与单一终态提交

模型 `done=true` 只表示“建议结束”。最终提交前由 runtime 验证：

- 任务交付谓词；
- 必要 artifact；
- 未解决错误；
- 最终输出协议和基本质量；
- 请求要求的验证证据。

同一个不可变终态对象应一次性投影到：

- run log；
- session trace；
- resume state；
- 用户摘要；
- cluster/public state（若存在）。

其中 `final_message`、`outcome`、`blockers` 和 `evidence` 必须来自同一次终态计算。

### 5.6 模型适配与运行预算

模型 adapter 应持有明确 profile：

```text
structured_output_mode
native_tool_call_mode
context_budget
recommended_timeout
parallel_tool_support
known_protocol_constraints
```

运行预算需要同时约束：

- 单请求超时；
- 连续超时次数；
- run 总时间；
- semantic turns；
- model attempts；
- 总 token；
- 无工作区进展回合数。

达到熔断条件后应保存 partial checkpoint，并提供结构化可恢复结果，而不是继续发送相同大请求。

## 6. 治理优先级

### P0：先闭合状态一致性

1. 异常运行保留已成功 ToolResult 和工作区 checkpoint。
2. 统一或显式投影 Shell cwd，消除跨 run 隐藏状态。
3. 建立 runtime-owned task checkpoint 和 unmet deliverables。
4. 最终完成增加交付门禁，拒绝无效终态文本。
5. open issue 改为结构化 blocker，不再直接复制模型 final message。
6. 终态只计算一次并一致投影到所有存储。

### P1：控制退化和资源放大

1. 引入跨消息 ContextPackager 和 artifact 引用。
2. 给错误码增加 retryability 与 recovery contract。
3. 增加 run 级时间、token、无进展和连续超时熔断。
4. 区分 semantic turn、model attempt、tool action 和 approval wait。
5. 增加模型 profile、能力协商和协议降级策略。

### P2：体验与诊断增强

1. 在 TUI 中显示当前 task phase、partial artifacts、真实 Shell cwd 和 blocker。
2. 恢复会话时展示 checkpoint 差异，而不是只显示上一条 assistant 文本。
3. 增加“每单位工作区进展消耗的 tokens/attempts”指标。
4. 对长审批等待执行状态重读和动作再验证。

## 7. 验收场景

后续机制变更至少应覆盖以下端到端场景：

1. 创建两个目录后模型超时；恢复时无需重新探索，能够从第三个交付项继续。
2. 上一 run 的 Shell 曾 `cd child`；恢复后模型明确知道真实 cwd，或 Shell 已确定性重置。
3. 模型连续产生同类协议错误；runtime 在有限次数后结构化停止，不允许无限变体重试。
4. 失败动作携带大型文档参数；后续 prompt 只出现摘要和 artifact 引用，不重复全文。
5. 模型返回 `Dictionary`、乱码、空泛将来时或与交付事实冲突的 `done=true`；完成门禁拒绝提交。
6. runtime 异常发生在多个成功工具之后；run trace、resume state 和工作区 checkpoint 仍保留成功事实。
7. 最终 outcome、final message、open issue 和 blocker 在 run/session/cluster 各投影中保持一致。
8. 连续模型超时触发 run 级熔断，保存 partial checkpoint，且日志不把 retry 统计为新语义 turn。

## 8. 最终判断

本次事件说明当前 runtime 已经具备模型调用、工具执行、安全审批、会话保存和局部纠偏等组件，但这些
组件尚未围绕一个单一、可信、结构化的任务状态组合起来。

只要执行进度仍主要存在于模型自然语言历史中，而 Shell、能力和 workspace 观察状态又以各自生命周期
隐式存活，任何局部失败都可能演化成：

```text
状态分裂 → 重复探索 → 上下文膨胀 → 模型退化 → 错误终态 → 恢复污染
```

因此治理重点应从“让模型更听提示词”转向“让 runtime 成为任务状态、恢复语义和完成判定的唯一权威”。

## 9. 2026-08-12 修复进展

本审计之后已完成两阶段根因治理：

- runtime-owned checkpoint、结构化 blocker 和统一 terminal summary 已贯通前台、session 与 subagent；
- Shell cwd 已成为显式恢复事实，异常不再丢弃部分成功结果；
- completion gate 已拒绝协议占位符，并按 required evidence 检查修改、测试和 artifact 交付；
- ContextPackager 已按优先级限制最终 messages 字符预算，长 action 参数不再重复内联；
- run 已具备墙钟、model attempt 和连续超时熔断，retry 与 semantic turn 分开统计；
- model adapter 已提供最小 capability profile，明确 structured output、native tool call 和 context budget。

仍未关闭的范围是 token 精确预算、完整语义摘要、通用 artifact 回查协议、服务健康共享和按模型进行
在线能力探测。这些属于后续增强，不再阻塞本次状态一致性主链路。

后续设计复核又关闭了两项状态一致性缺口：checkpoint 现在以 task id 和 workspace root 隔离恢复，
不再因上一 run 未完成而自动继承；完成事实改为带 workspace revision 的类型化 evidence ledger，后续
写入会使旧读取与测试证据失效。内置工具和 subagent handoff 通过通用 evidence 类型接入，完成策略
不再识别 `patch`、`run_tests` 等具体工具名。上下文改为带优先级的结构化分段，日志中的大值引用也
使用版本化 artifact envelope 并在单次 run 内按内容哈希复用。

根因治理完成后的验证结果：源码编译通过，完整测试套件 `488 passed`，diff whitespace 检查通过；新增
覆盖包括跨任务 checkpoint 隔离、显式继续恢复、扩展工具通用 evidence、写入后旧测试失效、旧会话
schema 安全迁移、并发 subagent 验证污染、必需上下文分段保留和大型 artifact 内容去重。后续审查又
补充了 `done=true` 同轮工具动作的终态门禁、失败验证撤销、执行副作用失效、changed-files/artifact
分离以及 subagent 类型化 evidence 直传。

最终 evidence 语义收口为两类有效期：`read`、`test` 只对当前 workspace revision 有效；
`workspace_change`、显式确认的 immutable `artifact` 是当前 task 的累计事实。Artifact reference 只负责
回查，不再自动升级为交付证据；no-change 完成必须同时具备当前 read evidence 和明确解释。
