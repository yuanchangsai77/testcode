# MCP Integration Design

## 文档职责

本文档只负责 MCP 专项设计，重点回答：

- MCP 在 `testcode` 中通过哪些模块接入
- discovery、transport、client、manager、adapter、provider 如何分层
- MCP tool 和 resource 如何复用现有 runtime 的 policy、approval、logger、prompt discipline

本文档不重复解释整个 runtime 分层，也不替代通用扩展点定义：

- 总体架构看 `docs/architecture.md`
- 通用扩展点看 `docs/runtime-extensibility.md`
- roadmap 优先级与阶段目标看 `docs/build-roadmap.md`
- tool 字段放置规则看 `docs/tool-contract.md`

本文档定义 `testcode` 中 MCP 接入的目标边界、模块拆分、运行时职责和实施顺序。重点不是“把外部 tool 接进来能跑”，而是让 MCP 能稳定复用现有 runtime 的 tool、policy、logger、session 和 prompt 约束，同时为后续 `stdio`、`streamable_http`、`sse` 和 URL 型服务预留稳定演进空间。

当前仓库实现状态：

- 已有 `ToolProvider`，并已补齐 `ResourceProvider` 扩展面。
- 已补齐 `src/testcode/mcp/` 模块骨架，包括 `config`、`types`、`client`、`manager`、`discovery`、`adapter`、`provider`。
- `app.py` 已装配 MCP tool/resource provider，并将 manager 纳入统一生命周期清理。
- 已实现 `stdio`、`streamable_http` 与 `sse` 的最小可用 transport 和 client 协议调用主链路。
- 已实现内存与磁盘 discovery cache、一次性失效重连、MCP 专项事件、基础 capability traits 和未知工具默认确认。
- 后续仍需扩充 traits 语义，并将 resource provider 接入完整的候选上下文选择与预算流程。

## 1. 目标

MCP 接入应满足以下目标：

- 不修改 `ExecutionEngine` 的核心职责边界。
- 不引入“外部 tool 特殊通道”；MCP tool 必须走同一套 tool registry、policy、approval、logger。
- 让 transport、connection lifecycle、schema adaptation、resource indexing 解耦。
- 第一阶段优先交付最小可用的 MCP tool 接入；resources/prompts 采用延后接入策略。
- 后续支持多个 server、不同 transport、risk override、resource indexing 和更细粒度缓存，而不要求重写主流程。

## 2. 非目标

第一阶段不做以下内容：

- 不把 MCP resources 全量注入 prompt。
- 不在第一阶段同时实现所有 transport；先打稳统一抽象和 `stdio` 主路径。
- 不让 MCP 绕过现有安全模型。
- 不在 `ModelPromptBuilder` 中加入 MCP 专用分支逻辑。
- 不把 server 进程管理和 tool adapter 写死在单一 provider 中。

## 3. 现有架构约束

当前仓库已经具备 MCP 接入所需的主要扩展边界：

- `ToolProvider` 负责发现并返回可注册工具，定义在 `src/testcode/orchestration/ext.py`。
- `ResourceProvider` 应作为并列扩展面承载 MCP resource index 和按需读取，而不是塞进 tool provider 或 prompt builder。
- `ToolRegistry` 负责注册、schema 校验、执行和结果记录，定义在 `src/testcode/tools/registry.py`。
- `DefaultPolicy` 负责按 `risk_level` 决定允许、确认或阻断，定义在 `src/testcode/safety/policy.py`。
- `OpenAICompatibleModelClient` 只依赖 `session.available_tools` 生成 native tool schema，不区分工具来源，定义在 `src/testcode/model/client.py`。
- `app.py` 是 runtime composition root，适合装配多个 provider。

因此，MCP 应该以“discovery service + tool provider + resource provider + manager/client/transport + adapter”的形式接入，而不是直接侵入 engine、prompt builder 或 model client。

## 4. 设计原则

### 4.1 组合优先

MCP server 不是新的执行主线，而是新的 tool 来源。组合点在 application assembly，而不是 orchestration loop。

### 4.2 协议与运行时分离

MCP 协议交互负责：

- 建连
- 初始化
- `tools/list`
- `tools/call`
- `resources/list`
- `resources/read`

`testcode` runtime 负责：

- tool 注册
- risk/policy
- approval
- logging
- prompt context discipline

### 4.3 显式命名空间与稳定标识

多个 MCP server 可能暴露同名 tool。内部注册时必须做显式命名空间隔离，推荐格式：

`<server_name>__<tool_name>`

例如：

- `github__search_repositories`
- `filesystem__read_file`
- `amap__maps_text_search`

这样可以避免与内置 tool 和其他 MCP tool 冲突，也让日志和审批提示更容易理解。

命名规则必须进一步写死：

- 内部稳定 id 使用 `<tool_name_prefix>__<mcp_tool_name>`。
- `tool_name_prefix` 默认等于 `server_name`，仅允许在配置中显式覆盖。
- provider 注册阶段必须校验稳定 id 全局唯一；与内置 tool、其他 MCP tool 或同 server 重复时直接拒绝注册该 tool，并写入用户可见诊断与日志。
- 日志和审批提示可以额外展示 `server_name`、原始 `tool_name` 和稳定 id，但模型看到和 runtime 执行的名称必须始终使用稳定 id。
- 不允许在 prompt 层再引入一套临时别名，否则会破坏恢复、日志检索和 `risk_overrides` 的一致性。

### 4.4 渐进式接入

优先顺序应是：

1. 统一配置模型
2. 统一 transport 接口
3. `stdio` transport
4. `streamable_http` transport
5. `sse` transport
6. discovery cache 和懒刷新策略
7. `tools/list`
8. `tools/call`
9. risk mapping / override
10. observability
11. `resources/list` 索引
12. resource read 进入 candidate context

不要一开始把 tools、resources、prompts、sampling 一次性做全。

## 5. 模块拆分

建议新增如下模块层次：

```text
src/testcode/mcp/
  __init__.py
  config.py        MCP server 配置模型与加载
  types.py         server/tool/resource 的内部类型
  transport.py     transport 协议和 stdio/http/sse transport
  client.py        单 server MCP client，负责协议调用
  manager.py       多 server 生命周期管理、缓存和关闭
  discovery.py     tool/resource descriptor 的懒发现、缓存与刷新策略
  adapter.py       MCP schema -> testcode Tool / Resource adapter
  provider.py      MCPToolProvider / MCPResourceProvider，负责注册入口
```

职责划分：

- `config.py`
  - 解析全局/项目配置中的 MCP server 定义。
  - 处理环境变量展开和敏感字段脱敏。
  - 不处理连接逻辑。
- `transport.py`
  - 抽象消息收发。
  - 第一阶段先实现 `stdio`，并预留 `streamable_http` 与 `sse` 的统一接口。
- `client.py`
  - 面向单个 server 的 MCP 会话。
  - 负责 `initialize`、`tools/list`、`tools/call`、可选的 `resources/list` / `resources/read`。
- `manager.py`
  - 缓存已启动 server client。
  - 统一关闭进程和清理状态。
  - 管理 discovery、provider 和 tool runtime 共用的连接。
- `discovery.py`
  - 维护按 server 划分的 tool/resource descriptor 缓存。
  - 定义懒加载、刷新、失效和降级策略。
  - 保证 app 启动不因为远端 server 暂时不可用而整体失败。
- `adapter.py`
  - 将 MCP tool schema 转换为 `SimpleTool` 或等价 `Tool` 实现。
  - 将 resource metadata 转换为后续上下文索引对象。
- `provider.py`
  - 从 discovery service 读取快照。
  - 返回已适配的 tool 列表和 resource provider 入口。

## 6. 运行时数据流

在当前实现里，数据流先落成了组合骨架：

1. `load_runtime_config()` 读取 `.testcode/config.toml` 中的 `[[mcp.servers]]`。
2. `app.py` 基于这些配置创建 `MCPManager`、`MCPDiscoveryService` 和 `MCPToolProvider`。
3. `MCPToolProvider.get_tools()` 从 discovery snapshot 读取 descriptor，并通过 adapter 生成内部 `Tool`。
4. 当 transport 尚未实现时，discovery 会记录 `mcp_server_unavailable` 级别的受控错误，provider 返回空集合，builtin tools 仍可继续工作。

### 6.1 Tool Discovery

应用启动或建 app 时：

1. `app.py` 加载 runtime config。
2. `MCPDiscoveryService` 读取启用的 server 配置和本地缓存元数据。
3. `MCPToolProvider` 从 discovery service 获取当前可注册的 descriptor snapshot。
4. adapter 将每个 MCP tool descriptor 转换为内部 `Tool`。
5. `ToolRegistry` 注册这些 tool。

关键约束：

- app 启动期默认不强制连接所有远端 server。
- 对没有缓存的新 server，可选择延迟到首次相关操作或显式刷新时再执行 `initialize` / `tools/list`。
- discovery 失败只影响对应 server，不应拖垮整个 CLI 启动。

### 6.3 Composition Root Wiring

MCP 必须只在 application composition root 装配，不允许由 engine 内部懒创建。推荐装配顺序：

1. 加载 runtime config，得到 `mcp_servers`
2. 创建 `MCPManager`
3. 创建 `MCPDiscoveryService`
4. 创建 `MCPToolProvider`
5. 与 `BuiltinToolProvider` 一起注册到同一个 `ToolRegistry`

这样能保证：

- engine 只面对统一的 `ToolRegistry`
- policy、approval、logger 不需要感知 MCP 来源
- transport/client/discovery 的演进不会污染 orchestration loop

### 6.2 Tool Execution

模型调用某个 MCP tool 时：

1. `ExecutionEngine` 像执行普通 tool 一样请求 `ToolRegistry.execute()`。
2. `ToolRegistry` 完成参数校验。
3. policy 按该 tool 的 `risk_level` 判断是否允许或需要审批。
4. MCP tool adapter 在 `run()` 中通过 `MCPManager` 找到目标 server client；如 descriptor 过期，可触发一次受控 refresh。
5. client 调用 `tools/call`。
6. adapter 将返回内容标准化为 `ToolResult`。
7. 结果继续进入 session history、logger 和 summary 流程。

这条链路中，engine 不应知道 MCP transport 细节。

## 7. 配置设计

### 7.1 配置来源

建议与 roadmap 保持一致：

- 全局配置：`~/.testcode/config.toml`
- 项目配置：`.testcode/config.toml`
- 环境变量作为补充覆盖
- CLI 参数保留最高优先级

### 7.2 统一 `MCPServerConfig`

推荐内部配置模型不要为每种 transport 设计完全不同的数据结构，而是先统一成一份 `MCPServerConfig`，再由 transport 层校验自身必需字段。

统一字段建议包含：

- `name`
- `transport`
- `enabled`
- `tool_name_prefix`
- `risk_overrides`
- `timeout`
- `read_timeout`
- `headers`

按 transport 分流的字段：

- `stdio`: `command`、`args`、`env`
- `streamable_http`: `url`、`headers`
- `sse`: `url`、`headers`

建议约束：

- `name` 必填，且在配置内唯一。
- `transport` 必填，允许值为 `stdio`、`streamable_http`、`sse`。
- `command` 对 `stdio` 必填。
- `url` 对 `streamable_http` 和 `sse` 必填。
- `args` 仅 `stdio` 使用。
- `env` 仅 `stdio` 使用。
- `headers` 对 URL 型 transport 可选。
- `enabled` 默认 `true`。
- `tool_name_prefix` 可选；未设置时默认使用 `name`，但最终生成的稳定 id 必须通过全局唯一性校验。
- `risk_overrides` 用于覆盖单个 MCP tool 的默认风险级别。
- `timeout` 表示连接或首包超时。
- `read_timeout` 表示已建立连接后的读取超时，尤其用于长连接或流式读取。

### 7.3 配置示例

`stdio` 示例：

```toml
[[mcp.servers]]
name = "github"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
enabled = true
tool_name_prefix = "github"
timeout = 30

[mcp.servers.env]
GITHUB_TOKEN = "${GITHUB_TOKEN}"

[mcp.servers.risk_overrides]
search_repositories = "network"
create_issue = "write"
```

`streamable_http` 示例：

```toml
[[mcp.servers]]
name = "amap"
transport = "streamable_http"
url = "https://mcp.amap.com/mcp?key=${AMAP_MCP_KEY}"
timeout = 60
read_timeout = 300
enabled = true
tool_name_prefix = "amap"
```

`sse` 示例：

```toml
[[mcp.servers]]
name = "amap_sse"
transport = "sse"
url = "https://mcp.amap.com/sse?key=${AMAP_MCP_KEY}"
timeout = 60
read_timeout = 300
enabled = false
tool_name_prefix = "amap"
```

如果需要兼容编辑器生态里常见的 JSON 结构，也建议统一成一份 transport-aware 配置，而不是为 `streamable_http` 和 `sse` 分裂出两套模型。例如：

```json
{
  "mcpServers": {
    "amap": {
      "transport": "streamable_http",
      "url": "https://mcp.amap.com/mcp?key=YOUR_KEY",
      "timeout": 60,
      "read_timeout": 300
    },
    "amap_sse": {
      "transport": "sse",
      "url": "https://mcp.amap.com/sse?key=YOUR_KEY",
      "timeout": 60,
      "read_timeout": 300,
      "enabled": false
    }
  }
}
```

### 7.4 高德这类 URL 型 MCP 的设计约定

像高德这样的服务会直接提供 URL，而不是本地拉起一个进程。这类 server 不应被当作“特殊 MCP”，而应落在统一 transport 模型内：

- `https://mcp.amap.com/mcp?...` 归类为 `streamable_http`
- `https://mcp.amap.com/sse?...` 归类为 `sse`

当前设计判断：

- `streamable_http` 应作为 URL 型 MCP 的主推路径
- `sse` 作为兼容或补充路径

工程原因：

- `streamable_http` 更容易统一请求/响应生命周期和普通 HTTP 超时模型
- `sse` 更依赖长连接、持续读取和断流重连策略
- 两者都应共用同一个 `MCPClient` 接口和同一套错误码映射，不应在 provider 层分叉逻辑

高德这类 URL 配置还有两个额外约束：

- URL 中可能包含 `key`；日志、details 和错误输出必须脱敏
- `timeout` 与 `read_timeout` 必须分离，否则排查连接失败和读流超时会混在一起

### 7.5 环境变量展开与脱敏

建议支持 `${VAR_NAME}` 形式的环境变量展开，尤其用于：

- URL 中的 API key
- headers 中的 bearer token
- stdio server 的 env 注入

展开规则建议：

- 仅在 runtime config 加载阶段展开
- 未设置的变量保留为空字符串或返回配置错误，由配置校验层决定
- logger 中写配置时，对 `key`、`token`、`authorization` 等敏感字段做脱敏

## 8. Transport 抽象

建议抽象统一 transport 接口，而不是为每种 transport 暴露不同 client API。

推荐职责：

- `connect()`
- `close()`
- `request()` 或等价的单次请求调用
- 对需要流式读取的 transport，内部自行处理消息帧和读取循环

### 8.1 `StdioTransport`

职责：

- 启动本地子进程
- 通过 stdin/stdout 收发 MCP 消息
- 捕获进程退出和 stderr 异常

特点：

- 生命周期管理最复杂
- 最适合作为首个实现，因为它能先打稳 manager/client 的重连和关闭逻辑

### 8.2 `StreamableHttpTransport`

职责：

- 向远端 MCP endpoint 发起 HTTP 请求
- 处理状态码、响应体和协议错误
- 支持认证 header 和超时配置

特点：

- 更适合作为 URL 型 MCP 的主路径
- 连接语义更清晰，便于统一首包超时和请求失败日志

### 8.3 `SseTransport`

职责：

- 建立 SSE 长连接
- 读取事件流
- 处理断流、心跳和读取超时

特点：

- 更轻量
- 但生命周期更偏“流”，因此必须单独定义读取超时、断流重连和事件解析规则

## 9. Timeout、重连与可用性语义

建议一开始就明确区分以下超时：

- `timeout`
  - 建连、首包或单次请求启动阶段的超时
- `read_timeout`
  - 建立连接后的读取超时

推荐规则：

- `stdio` 通常主要使用 `timeout`；如未来需要，也可对 stdout 读取增加 `read_timeout`
- `streamable_http` 同时支持 `timeout` 和 `read_timeout`
- `sse` 强烈建议支持 `read_timeout`

重连策略建议：

- provider 阶段发现失败时，不应让整个 app 启动失败；可跳过该 server 并记录错误
- tool 调用阶段如发现 client 已失效，可由 manager 尝试一次透明重建
- 连续重建失败后返回稳定错误码，不在 engine 内无限重试

## 10. Discovery 与 Client 协议边界

`MCPDiscoveryService` 与 `MCPClient` 不能混成一个“万能 provider helper”。

`MCPDiscoveryService` 负责：

- 决定何时连 server 做 `initialize` / `tools/list` / `resources/list`
- 缓存 descriptor snapshot
- 控制启动期、运行期和刷新时机的失败降级策略
- 向 provider 暴露稳定的只读快照接口

`MCPClient` 负责：

- 屏蔽 transport 差异
- 暴露协议操作
- 返回标准化内部类型
- 将协议和 transport 错误分级后上抛

`MCPClient` 对上层只暴露协议语义，例如：

- `initialize()`
- `list_tools()`
- `call_tool()`
- `list_resources()`
- `read_resource()`

关键要求：

- provider 和 adapter 不应直接感知 HTTP、SSE、stdio 的底层细节
- provider 也不应自行决定“何时探测 server”；它只消费 discovery snapshot
- `MCPClient` 返回的应是标准化内部类型，而不是原始 transport payload
- 错误应尽量在 client 层完成分级，再上抛到 adapter 或 manager

## 11. Tool 适配规则

### 11.1 名称

内部名称：

`<tool_name_prefix>__<mcp_tool_name>`

显示给模型和日志时都使用该名称。

如生成后的名称冲突：

- 默认行为是拒绝注册冲突项，不做静默覆盖。
- 日志需写清冲突 server、原始 tool 名和目标稳定 id。
- CLI summary 应提示用户通过修改 `tool_name_prefix` 或禁用冲突 server 解决。

### 11.2 描述

优先使用 MCP tool 的 description。必要时仅做轻微清洗，不要在 provider 内拼接大量说明文字。

### 11.3 参数 Schema

MCP 的 input schema 原则上应直接映射到 `ToolDefinition.input_schema`，仅在以下情况下做本地补强：

- 缺失 `type: object` 时补齐包装
- 缺失 `additionalProperties` 时保持宽松，不擅自收紧
- 非法 schema 时拒绝注册该 tool，并记录日志

### 11.4 输出

MCP tool 返回内容后，适配器应按 `docs/tool-contract.md` 的约定决定：

- 模型继续推理需要的短结果，进入 `ToolResult.output`
- 结构化、审计或调试字段，进入 `ToolResult.metadata`
- `ToolResult.output` 最多保留 100,000 字符，超出部分截断，并在输出中明确标记
- 超限的完整结果经过脱敏后写入 run artifact；结构化内容和远端 metadata 同样受大小限制

建议 metadata 至少包含：

- `server_name`
- `remote_tool_name`
- `duration_ms`
- `transport`
- `truncated`
- `artifact_path`

## 12. 风险模型

MCP 不能使用“因为是外部系统所以全部自由放行”的策略。先做 capability traits，再折叠为现有 `risk_level` 更稳。

建议内部先抽象 traits：

- `reads_local_data`
- `writes_local_data`
- `executes_code`
- `uses_network`
- `mutates_remote_state`
- `uses_credentials`
- `long_running`

然后再做统一折叠：

- 纯只读本地或只读远程查询，可映射到较低风险。
- 涉及写文件、执行命令、远端状态变更、凭证使用的能力，默认提升到 `confirm` 或更高策略。
- 服务端返回的 tool annotations 属于不可信提示，只能用于提高风险，不能把未知能力降为免审批的 `read`。
- `risk_overrides` 是显式覆盖入口，但应覆盖的是最终策略，不应跳过 trait 推断和审计记录。

默认建议如下：

- 未知 MCP tool：`confirm`
- 明显查询类：`read` 或 `network`
- 明显创建/修改类：`write`
- 明显执行远程动作类：`execute`
- 明显删除或不可逆修改类：`destructive`

推荐做两级映射：

1. 启发式默认映射
   - 依据 tool 名称和 description 中的动词，如 `get`、`list`、`search`、`create`、`update`、`delete`
2. 用户显式覆盖
   - 通过配置对具体 `server/tool` 定义风险级别

启发式只用于默认值，最终以显式配置为准。

日志应至少记录：

- 原始 MCP tool
- traits 推断结果
- 映射后的 `risk_level`
- 是否用户 override

## 13. 生命周期管理

MCP 最大的工程风险不在 schema，而在连接和进程生命周期。

建议规则：

- 一个 app/runtime 实例内，同一 server 只建立一个 client。
- 多轮会话中优先复用已建立的 client。
- `ToolRegistry.reset_state()` 或 app 关闭时，由 `MCPManager` 统一关闭 transport/client。
- server 崩溃后，不复用失效 client；下一次发现或调用时允许重建。
- stdio、Streamable HTTP 和 SSE 都应接受单条 JSON-RPC 消息或 batch，并按请求 id 选择对应响应。
- project MCP 配置、Skill 目录和 discovery cache 必须绑定请求或 resumed session 的 workspace，而不是启动命令所在目录。
- 单个 server 最多暴露 256 个 tools、1,000 个 resources，单个 descriptor 最大 100,000 字符；超限项丢弃并记录诊断事件。
- transport 在 JSON 解析前将单条 HTTP、SSE 或 stdio 消息限制为 10 MiB，避免远端异常响应导致无界内存占用。

连接状态不应散落在 provider 和单个 tool 实例里；应由 manager 集中管理。

manager 还应承担多 transport 共性治理：

- client 缓存键统一按 `server_name`
- 失效 client 的摘除和重建
- shutdown 时按 transport 类型调用对应 `close()`

## 14. Resources 接入策略

resources 应视为“按需上下文来源”，不是 tool 的附属输出，并应通过 `ResourceProvider` 进入统一扩展面。

第一阶段建议：

- 只做 `resources/list` 的索引与 metadata 收集
- 由 `MCPResourceProvider` 暴露 descriptor 与按需读取能力
- 不自动把 resource 正文注入 prompt
- 不在 model tool schema 中直接暴露所有 resource

后续阶段再做：

- `MCPResourceIndexLoader` 或等价 `ContextLoader`
- 按需 `resources/read`
- 敏感内容检查
- 预算裁剪
- source reference 标记
- 与 `ContextPackager` 集成

这样才能与项目现有的 bounded context 目标一致。

## 15. Observability

MCP 相关日志建议至少记录：

- `mcp.server.start`
- `mcp.server.ready`
- `mcp.server.error`
- `mcp.tools.discovered`
- `mcp.tool.call`
- `mcp.tool.result`
- `mcp.resource.indexed`

关键字段：

- `server_name`
- `transport`
- `tool_name`
- `duration_ms`
- `success`
- `error_code`

建议额外记录 transport 相关字段：

- `timeout`
- `read_timeout`
- `reconnect_attempted`
- `http_status`
- `stream_closed_by_peer`

URL 型 transport 还应对以下字段默认脱敏：

- query string 中的 `key`、`token`
- `Authorization`
- 自定义认证 header

不要把大块远端返回内容原样灌入顶层日志字段；应使用结构化 metadata 和必要裁剪。

## 16. 错误码建议

建议标准错误码：

- `mcp_server_unavailable`
- `mcp_initialize_failed`
- `mcp_tool_list_failed`
- `mcp_tool_call_failed`
- `mcp_invalid_schema`
- `mcp_protocol_error`
- `mcp_transport_timeout`
- `mcp_transport_read_timeout`
- `mcp_transport_connect_failed`
- `mcp_transport_closed`
- `mcp_http_error`
- `mcp_sse_stream_error`

建议分层：

- transport 层负责底层连接和读写错误
- client 层负责协议交互错误
- adapter 层负责 schema 和结果适配错误

这样排查时能快速判断故障点是在网络、协议还是本地映射。

## 17. 测试策略

最小测试集应覆盖：

### 17.1 配置

- 加载全局和项目配置
- server 去重
- 无效 transport 拒绝
- risk override 解析
- `${VAR}` 展开
- URL query 中敏感 key 脱敏
- header token 脱敏

### 17.2 Provider / Discovery

- provider 发现多个 server tool
- 同名 tool 正确加前缀
- 非法 schema tool 被跳过并记录
- URL 型 server 与 stdio server 可同时存在

### 17.3 Transport / Client

- `stdio` server 启动、关闭、异常退出
- `streamable_http` 请求成功、HTTP 失败、超时
- `sse` 建连成功、断流、读取超时
- manager 对失效 client 的一次性重建

### 17.4 Execution

- MCP tool 参数校验复用 `ToolRegistry`
- 成功调用返回统一 `ToolResult`
- 远端报错映射为稳定 `error_code`
- policy/approval 正常生效

### 17.5 Lifecycle

- 多次调用复用同一 client
- reset/close 会关闭 transport
- server 崩溃后可恢复重建

### 17.6 Future Resource Path

- resource index 不直接进入 prompt
- resource read 经由单独上下文路径接入

## 18. 实施顺序

推荐按以下顺序落地：

1. 定义 `mcp` 配置模型和内部类型
2. 定义统一 transport 接口
3. 实现 `stdio` transport
4. 实现单 server client
5. 实现 `MCPManager`
6. 实现 MCP tool adapter
7. 实现 `MCPToolProvider`
8. 在 `app.py` 中装配 provider
9. 补齐 policy、logger、tests
10. 实现 `streamable_http`
11. 实现 `sse`
12. 后续再做 resource indexing

如果目标包含高德这类 URL 型 MCP，建议阶段拆得更细：

1. P3.1a：统一配置模型与校验
2. P3.1b：transport 接口与 `stdio`
3. P3.2a：client/manager/provider/adapter 主链路
4. P3.2b：`streamable_http`
5. P3.2c：`sse`
6. P3.3：resource indexing 与 context integration

当前代码进度可对应到：

- 已完成：三种 transport、真实 MCP 协议 client、manager/discovery/adapter/provider、tool/resource 主链路、稳定命名、冲突拒绝、缓存、一次性重连、专项事件和安全默认值
- 待增强：真实公网 server 的兼容性矩阵、更丰富的 traits、resource candidate context 与统一 token 预算

## 19. 结论

对当前项目而言，正确的 MCP 方案不是“在 engine 里加对 MCP 的特殊判断”，而是：

- 在 composition root 增加 provider
- 在 tool layer 增加 adapter
- 在 runtime 内增加 manager/client/transport
- 在 context layer 后续增加 resource indexing

这条路线与当前仓库的模块化方向、可扩展性目标和复用现有安全/日志/模型工具链的原则一致，而且能把第一阶段复杂度控制在可验证的范围内。
