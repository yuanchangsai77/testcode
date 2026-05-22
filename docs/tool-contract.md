# Tool Contract

本文档定义 `testcode` tool 字段的用途和流向。新增内置 tool、Skill 派生 tool 或 MCP tool 适配层时，先按这里的契约决定信息应该放在哪里。

## 字段流向

| 字段或概念 | 进入模型上下文 | 用户可见 | 日志 | 主要作用 |
| --- | --- | --- | --- | --- |
| `ToolDefinition.name` | 是 | 间接 | 否 | 模型可调用的工具名。 |
| `ToolDefinition.description` | 是 | 否 | 否 | 告诉模型何时使用工具。 |
| `ToolDefinition.arguments` | 是 | 否 | 否 | 人类可读的参数说明。 |
| `ToolDefinition.input_schema` | 是 | 否 | 否 | API tool schema 和本地参数校验。 |
| `ToolDefinition.risk_level` | 是 | 否 | 否 | policy/approval 判断，也提示模型风险。 |
| `ToolAction.arguments` | 是，作为 history args | 审批时可见 | 是 | 模型请求工具时给出的参数。 |
| `ToolResult.output` | 是 | fallback 可见 | 是 | 给模型继续推理的主要结果。 |
| `ToolResult.success` | 是，转成 status | 是 | 是 | 表示工具是否成功。 |
| `ToolResult.error_code` | 是，转成 status | 是 | 是 | 稳定错误恢复、policy 判断和展示。 |
| `ToolResult.metadata` | 默认否 | 默认否 | 是 | 给 runtime、测试、日志使用的结构化数据。 |
| `metadata["action_arguments"]` | 是，特殊例外 | 否 | 是 | engine 附加，用于 session history 和重复调用诊断。 |
| `SimpleTool.summarizer` | 否 | 是 | 否 | 只生成用户 run summary，不写入 tool result。 |

## 放置原则

模型需要继续工作的内容放 `ToolResult.output`。

程序需要结构化处理，但模型不一定需要的内容放 `ToolResult.metadata`。普通 metadata 不会进入 prompt；不要依赖模型能看到它。

只为了用户 run summary 的内容放 `SimpleTool.summarizer`。不要把展示摘要写进 `output` 或 `metadata`。

工具参数和 schema 会进入模型上下文。参数说明应短而准确，不要把长文档塞进 `description`、`arguments` 或 `input_schema`。

## 新增 Tool Checklist

新增 tool 时逐项确认：

1. 模型是否需要看到这个信息？
   - 是：放 `ToolResult.output`。
   - 否：不要放 `output`。
2. runtime、测试或日志是否需要结构化字段？
   - 是：放 `ToolResult.metadata`。
   - 否：不要放 `metadata`。
3. 用户 run summary 是否需要更短展示？
   - 是：给 `SimpleTool` 配 `summarizer`。
   - 否：presenter fallback 到 `output` 截断。
4. 这个工具的风险是什么？
   - 设置 `risk_level`：`read`、`write`、`execute`、`test`、`network` 或 `destructive`。
5. 参数是否必须严格校验？
   - 写 `input_schema`，通常使用 `additionalProperties: false`。

## 当前边界

`SessionContext.add_tool_result()` 只把 `output`、状态、`error_code` 和特殊的 `metadata["action_arguments"]` 写入 session history。`ModelPromptBuilder` 再把 session history 放进模型输入。

`ConsolePresenter` 展示 run summary 时，通过 `ToolRegistry.summarize_result()` 调用 tool 本地 summarizer。这个 summarizer 不属于 tool schema，不进入 model prompt，不进入 `ToolResult.metadata`。
