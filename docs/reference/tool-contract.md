# 参考：Tool 字段契约

## 文档职责

本文档只定义 tool 字段契约，回答：

- 哪些字段进入模型上下文候选
- 哪些字段只用于日志、runtime 或用户摘要
- 新增 tool 时信息应该放在 `output`、`metadata` 还是 `summarizer`

本文档不负责说明整体架构、roadmap 或 MCP/Skill 的专项模块拆分；那些内容分别在
`docs/architecture.md`、`docs/roadmap.md`、`docs/extensions/mcp-integration.md`、
`docs/extensions/skill-system.md` 中展开。

本文档定义 `testcode` tool 字段的用途和流向。新增内置 tool、Skill 派生 tool 或 MCP tool 适配层时，先按这里的契约决定信息应该放在哪里。

## 字段流向

| 字段或概念 | 模型上下文候选 | 用户可见 | 日志 | 主要作用 |
| --- | --- | --- | --- | --- |
| `ToolDefinition.name` | 是 | 间接 | 否 | 模型可调用的工具名。 |
| `ToolDefinition.description` | 是 | 否 | 否 | 告诉模型何时使用工具。 |
| `ToolDefinition.arguments` | 是 | 否 | 否 | 人类可读的参数说明。 |
| `ToolDefinition.input_schema` | 是 | 否 | 否 | API tool schema 和本地参数校验。 |
| `ToolDefinition.risk_level` | 是 | 否 | 否 | policy/approval 判断，也提示模型风险。 |
| `ToolAction.arguments` | 是，作为有界 history args | 审批时可见 | 是 | 短参数原样投影；长字符串只保留长度和摘要哈希。 |
| `ToolResult.output` | 是；经统一结果包装后进入 history | fallback 可见 | 是 | 给模型继续推理的有界结果。 |
| `ToolResult.success` | 是，转成 status | 是 | 是 | 表示工具是否成功。 |
| `ToolResult.error_code` | 是，转成 status | 是 | 是 | 稳定错误恢复、policy 判断和展示。 |
| `ToolResult.metadata` | 默认否；仅特殊字段进入 history | 默认否 | 是 | 给 runtime、测试、日志使用的结构化数据。 |
| `metadata["action_arguments"]` | 是，特殊例外 | 否 | 是 | engine 附加，用于 session history 和重复调用诊断。 |
| `SimpleTool.summarizer` | 否 | 是 | 否 | 只生成用户 run summary，不写入 tool result。 |

## 放置原则

模型可能需要继续工作的短结果放 `ToolResult.output`。`ToolRegistry` 在结果离开工具时统一
执行字节预算、UTF-8 安全裁剪和截断标记，再写入 session history。工具仍可实施更小的
领域限制，但不得依赖各自实现来满足全局输出上限。

统一包装会在 metadata 中记录原始/可见字节数、摘要哈希和 `truncated` 状态。完整的跨消息
`ContextPackager` 仍负责 conversation、Skill、显式上下文和 checkpoint 的总预算；单项工具
结果包装是它之前的第一道稳定边界。

大输出不要只依赖 `output` 承载。工具应在 `metadata` 中保留 source reference、路径、大小、truncated 状态、artifact id 或 run id，让 packager 可以用摘要进入 prompt，并在需要时回查完整内容。

程序需要结构化处理，但模型不一定需要的内容放 `ToolResult.metadata`。普通 metadata 不会进入 prompt；不要依赖模型能看到它。

只为了用户 run summary 的内容放 `SimpleTool.summarizer`。不要把展示摘要写进 `output` 或 `metadata`。

工具参数和 schema 会进入模型上下文。参数说明应短而准确，不要把长文档塞进 `description`、`arguments` 或 `input_schema`。

## 实际案例：`read_file`

`read_file` 是当前内置工具之一，定义在 `src/testcode/tools/builtins/read_file.py`。它的契约可以作为新增读取类工具的参考。

工具定义：

- `name` 是 `read_file`，进入模型上下文，模型通过这个名字发起调用。
- `description` 是 “Read a UTF-8 text file from the workspace.”，只用于告诉模型何时使用该工具。
- `arguments` 描述 `path` 和 `max_bytes`，给模型理解参数含义。
- `input_schema` 要求 `path` 必填，`max_bytes` 可选，并通过 `additionalProperties: false` 拒绝额外参数。
- 未显式设置 `risk_level`，使用 `SimpleTool` 默认值 `read`。

一次成功调用示例：

```python
ToolAction(
    name="read_file",
    arguments={"path": "README.md", "max_bytes": 4096},
)
```

工具读取文件后返回：

```python
ToolResult(
    name="read_file",
    success=True,
    output="<README.md 的文本内容>",
    metadata={
        "path": "/home/changsai/testcode/README.md",
        "bytes": 4096,
        "truncated": True,
    },
)
```

字段流向：

- `output` 放文件内容，因为模型可能需要继续基于文件内容推理；当前实现会进入 session history，
  随后由 `ContextPackager` 纳入统一预算并成为可裁剪候选内容。
- `metadata.path`、`metadata.bytes`、`metadata.truncated` 给 runtime、日志和 summarizer 使用；普通 metadata 不会进入模型上下文。
- `metadata.evidence` 是成功执行后发布给 runtime 的通用证据类型列表。工具声明自身可能产生的证据，
  registry 只在成功结果上附加声明值；聚合型工具也可以为已确认的部分结果显式发布证据。完成门禁只能
  消费这些语义类型，不能按工具名推断交付事实。
- 当前通用类型有四种：`read` 表示观察了当前工作区状态，`workspace_change` 表示本任务确实完成过
  工作区修改，`test` 表示当前 revision 的验证成功，`artifact` 表示某个引用已被明确确认为交付物。
  `read` 和 `test` 只在产生它们的 revision 有效；`workspace_change` 和 immutable `artifact` 是任务级
  累积事实，不因后续普通观察 revision 自动消失。
- `metadata.invalidates_evidence` 撤销当前 revision 上指定类型的旧证据；测试工具失败时 registry 自动
  撤销 `test`。成功的 write、execute、destructive 动作会保守推进观察 revision，即使它没有声明
  `workspace_change`。`changed_files` 和 `artifact_refs` 仅用于摘要、恢复与回查，不能自动产生 artifact
  或完成证据；交付物必须显式发布 `artifact` evidence。
- `read_file_summary()` 基于 metadata 生成用户 run summary，例如 `read /home/changsai/testcode/README.md (4096 bytes truncated)`；这个摘要不写回 `ToolResult.output`。
- 如果读取的是二进制文件，工具返回 `success=False`、`error_code="binary_file"`，`output` 放模型可见的失败原因，`metadata` 保留路径和文件大小供日志/摘要使用。

## 新增 Tool Checklist

新增 tool 时逐项确认：

1. 模型是否需要看到这个信息？
   - 是：短结果放 `ToolResult.output`，大结果放摘要或截断内容，并在 `metadata` 提供可回查 source reference。
   - 否：不要放 `output`。
2. 工具成功时能证明哪类事实：`read`、`workspace_change`、`test` 或 `artifact`？不要把动作名称当证据。
3. 如果工具会写工作区，是否只在真实写入成功后发布 `workspace_change`？
4. runtime、测试或日志是否需要结构化字段？
   - 是：放 `ToolResult.metadata`。
   - 否：不要放 `metadata`。
5. 用户 run summary 是否需要更短展示？
   - 是：给 `SimpleTool` 配 `summarizer`。
   - 否：presenter fallback 到 `output` 截断。
6. 这个工具的风险是什么？
   - 设置 `risk_level`：`read`、`write`、`execute`、`test`、`network` 或 `destructive`。
7. 参数是否必须严格校验？
   - 写 `input_schema`，通常使用 `additionalProperties: false`。

## 当前边界

当前实现中，`ToolRegistry` 先统一包装结果；`SessionContext.add_tool_result()` 只把有界
`output`、状态、`error_code` 和特殊的 `metadata["action_arguments"]` 写入 session history。
其中长 action argument 不会重复内联，避免失败的大型 diff 或文档在每轮 prompt 中倍增；完整参数
写入当前 run 的 artifact，history 和事件日志只保留字符数、哈希与 `action_artifact_ref`。checkpoint
保存任务身份、工作区 revision、类型化证据和实际交付 artifact，不把动作输入误当成任务产物。
`ModelPromptBuilder` 再把 session history 放进模型输入。

事件日志是轻量索引，不直接承载完整参数或输出。每个 run 只归档首个完整模型请求，后续正常请求与
响应只记录大小、用量和 SHA-256 指纹；小型工具参数和结果直接内联，大型正文经脱敏后内容寻址，
只在当前 run 的 artifact archive 中保存一次。action reference 由统一日志入口生成，Engine、history 和
ToolResult 复用该引用，不能各自重复创建 artifact。事件通过带 `$type: artifact_ref`、
`schema_version`、字符数和哈希的 envelope 引用正文。`run.finish` 只保存终态、统计、blocker 和
checkpoint，不再次复制全部 ToolResult。

session history 和 tool result 随后进入 `ContextPackager`，由 packager 生成预算内上下文。因此新增 tool
不应假设 `output` 必然完整进入最终 prompt。

`ConsolePresenter` 展示 run summary 时，通过 `ToolRegistry.summarize_result()` 调用 tool 本地 summarizer。这个 summarizer 不属于 tool schema，不进入 model prompt，不进入 `ToolResult.metadata`。
