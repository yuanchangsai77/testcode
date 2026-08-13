# 核心运行时：Agent 执行循环

## 文档职责

本文档说明当前模型—工具循环如何推进、纠正重复动作和停止。它不定义终端渲染、
工具字段或上下文来源；这些内容分别属于
[TUI 当前行为](../interaction/tui-current.md)、[Tool 字段契约](../reference/tool-contract.md)、
[项目感知](project-awareness.md)和[演进路线图](../roadmap.md)。

## 基本流程

每次 run 创建独立的 session context 和 runtime-owned task checkpoint，并按以下顺序循环：

1. 加载项目规则、相关 workspace 摘要和显式上下文。
2. 只通过已持久化的 `active_capability_ids` 恢复 capability activation。
3. 将当前可见工具和 session context 发送给模型。
4. 将回复解析为最终回答或工具动作。
5. 对工具执行可见性、预检、安全审批和实际调用。
6. 先把成功动作、artifact、结构化 blocker 和工具运行态投影写入 checkpoint，再生成有界
   session history 供下一轮模型使用。

模型以 `done: true` 且不再请求工具表示建议完成；runtime completion gate 会拒绝空值和协议
占位符，最终 outcome、blocker 和 checkpoint 由 runtime 统一计算。新激活的能力从下一模型轮次
起可见，不能在发起激活的同一轮立即调用。

完成门禁位于唯一终态提交路径。即使模型在 `done: true` 的同一回复中请求工具，runtime 也必须先
执行工具、更新 checkpoint、重新计算 unmet evidence，再决定是否完成。

模型请求失败时按配置的重试次数和延迟重试；单次任务的最大模型轮数也由配置控制。具体
默认值和硬上限见[配置参考](../reference/configuration.md)。

每次请求前还会检查 run 总墙钟、model attempt 总数和连续超时数。任一预算耗尽即打开熔断器，
以 `exhausted` terminal summary 保存当前 checkpoint；retry 只增加 attempt，不增加 semantic turn。

## 重复动作处理

运行时为工具名和规范化参数生成动作指纹，并保存已完成结果。同一写入世代内再次请求
完全相同的动作时：

- 不重复执行工具。
- 返回带 `duplicate=true` 的合成结果，并引用上一次结果。
- 重复次数超过内部限制，或上一次结果本身不可重试时，返回
  `duplicate_tool_call`。

成功执行可能修改工作区的 `write`、`execute`、`test` 或 `destructive` 动作会开启新的
工作区世代并清空旧动作指纹，因为之前读取的文件状态可能已经失效。修改后重新读取因此是
合法的新动作，而不是重复调用；同一个修改动作本身仍会记录在新世代中，避免重复执行。

## 修改任务的进度纠正

当请求被识别为文件修改任务，模型第二次请求完全相同的只读上下文工具时，默认进度策略
会追加 `progress_required`：

- 提醒模型使用 session history 中已有结果。
- 要求下一步进入修改，或明确解释为什么不需要修改并结束。
- 同一写入世代只发送一次恢复提示。

该策略只针对修改意图和只读上下文工具。纯审查、解释或诊断请求不会因为重复读取而被
强制进入写入。

## 停止条件

除模型正常完成外，当前循环还会在以下情况下停止：

- 达到配置的最大模型轮数。
- 连续三轮都产生失败的测试结果。
- 连续两轮只产生不可重试结果，无法继续推进。
- 用户中断当前 run。

运行摘要使用结构化终态区分 `completed`、`blocked`、`stalled`、`runtime_error`、
`interrupted` 和 `exhausted`。权限等待、策略阻断或连续不可重试结果不能记录为完成。

凭据写入被内容安全策略阻断时，即使模型同时声明完成，运行时也会要求模型先改用安全
方案，不能把被阻断的写入当成成功结束。

所有停止路径都产生同一种 terminal summary。它保留截止该时刻已确认成功的工具结果，并携带
runtime 计算的 outcome、结构化 blocker、task checkpoint、artifact 和可见工具运行态。前台异常
包装、session trace、resume state 与 subagent handoff 都消费这份 summary，不能重新从模型最后
一句文本推断控制面状态。

## 当前边界

- 动作去重仍只在当前 run 和写入世代内维护；未完成任务的 checkpoint 跨 run 保存确认进度，但尚未恢复完整
  read-state hash。
- 当前 conversation 和工具结果先由 `ContextPackager` 做总字符预算和近期消息选择。长 action
  argument 不再重复内联，只保留长度和摘要哈希；结构化优先级、语义摘要与按需 artifact 回查仍需
  继续增强。
- checkpoint 已记录 required evidence 和 unmet deliverables。修改请求默认要求 workspace change；模型
  若判断现状已经满足，必须先取得当前 revision 的 read evidence，再给出明确的 no-change 理由。更复杂
  的领域交付谓词仍需逐类扩展。
- checkpoint 的恢复以 task id 和 workspace root 为边界，不能仅凭上一 run 未完成就继承。证据账本记录
  证据类型、生产者和 workspace revision；写入推进 revision，旧的读取与测试证据随即失效。完成门禁
  只消费通用证据类型，不识别具体工具名。调用方可以显式传入原 task id；交互入口中的纯“继续”或
  “resume”请求也会恢复最近的未完成任务，其他新请求默认建立新任务。
- `changed_files` 只是变更资源摘要，不是 immutable artifact。测试失败会撤销当前 revision 的 test
  evidence；可能产生工作区副作用的成功 execute/write/destructive 动作至少推进观察 revision，使旧
  read/test 失效，但不会抹掉本任务已经发生的 workspace change 或已确认的 immutable artifact。
- 终端中的 Working、审批、取消和运行中输入由 TUI 事件层处理，不属于执行循环的业务
  状态。
