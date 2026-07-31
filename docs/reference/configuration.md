# 参考：配置

## 文档职责

本文档是运行配置的唯一参考：说明配置文件位置、覆盖顺序、各参数的中文含义、默认值和内部硬上限。
MCP 服务器字段的 transport 语义仍以 [MCP 集成](../extensions/mcp-integration.md) 为准。

## 配置来源与优先级

模型地址、模型名、模型超时和运行模式从 `testcode` 源码根目录的 `.env`
或同名环境变量读取；已有环境变量不会被 `.env` 覆盖。当前实现不会自动加载任意目标
工作区中的 `.env`。以已安装包运行时，优先使用进程环境变量，避免依赖安装目录中的
`.env`。

命令行 `--mode` 会覆盖 `TESTCODE_MODE`。其余模型连接项目前没有命令行覆盖。

运行策略与 MCP 服务从以下 TOML 文件读取：

1. `~/.testcode/config.toml`：用户全局默认值。
2. `.testcode/config.toml`：当前项目覆盖同名配置。

同名 MCP server 以项目条目整体替换全局条目，不做字段级合并。

不要把密钥写入 TOML；MCP 的 URL、headers 和 env 字段可用 `${NAME}` 引用环境变量。

## 完整示例

```toml
[model.retry]
# 模型首次请求失败后最多重试几次；0 表示不重试。
# 默认 7，内部硬上限 20。
max_retries = 7
# 每次重试前等待的秒数；次数超过数组长度时重复最后一个值。
# 每项范围 0–60 秒，数组最多 20 项。
delays = [0.5, 1, 1.5, 2, 3, 5, 8]

[orchestration]
# 一次任务中模型最多可继续思考或调用工具的轮数。
# 默认 100，内部硬上限 500。
max_turns = 100

[limits]
# 每个 MCP 服务最多发现的工具数。默认 256，内部硬上限 1024。
mcp_tools_per_server = 256
# 同时激活的 MCP/Skill 能力数。默认 8，内部硬上限 32。
active_capabilities = 8
# shell/search 等命令工具保留的输出字节数。默认 32000，内部硬上限 1048576。
tool_output_bytes = 32000
# read_file 默认读取的字节数。默认 64000，内部硬上限 1048576。
read_file_bytes = 64000
# list_dir 默认返回的目录条目数。默认 200，内部硬上限 2000。
list_dir_entries = 200
# find_files/search_text 默认返回的结果数。默认 200，内部硬上限 2000。
search_results = 200

[[mcp.servers]]
name = "amap"
description = "Search places, geocode addresses, and plan routes."
transport = "streamable_http"
url = "https://mcp.amap.com/mcp?key=${AMAP_MCP_KEY}"
enabled = true
timeout = 60
read_timeout = 300
```

## 参数行为

内部硬上限是防止单次任务无限占用时间、内存或模型上下文的保护边界。TOML 值超出上限时，程序会在启动时明确报错，不会静默回退或忽略。

`list_dir`、`find_files`、`search_text` 和 `read_file` 允许模型在单次调用中给出数量参数，但仍会被内部硬上限截断；这里的配置值决定未传参数时的默认返回量。

`tool_output_bytes` 限制命令和文本搜索的保留输出；`read_file_bytes` 独立限制文件读取，避免一项配置意外放大所有上下文来源。

## 环境变量

源码 checkout 中的 `.env` 只需要保留一组当前使用的模型连接配置：

```env
TESTCODE_MODEL_BASE_URL=http://127.0.0.1:3000
TESTCODE_MODEL_NAME=gpt-5.4
TESTCODE_MODEL_TIMEOUT=60
TESTCODE_MODE=confirm
```

`TESTCODE_MODE` 可选值为 `readonly`、`confirm`、`auto`。MCP 的敏感值应通过系统环境变量提供，例如 `AMAP_MCP_KEY`。

## MCP server 字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 必填 | server 稳定名称，也是默认工具名前缀。 |
| `description` | `""` | 外层工具箱用途描述；应说明何时打开该能力。 |
| `capabilities` | `[]` | 外层目录标签，不等于已激活叶子能力。 |
| `transport` | 必填 | `stdio`、`streamable_http` 或 `sse`。 |
| `enabled` | `true` | 是否加入运行时配置。 |
| `tool_name_prefix` | `name` | 模型侧稳定工具名前缀。 |
| `risk_overrides` | `{}` | 按远端工具名覆盖风险等级。 |
| `timeout` | `30` | 建连和普通请求超时秒数。 |
| `read_timeout` | `300` | HTTP/SSE 长读取超时秒数。 |
| `command` | `""` | `stdio` 必填命令。 |
| `args` | `[]` | `stdio` 命令参数。 |
| `env` | `{}` | `stdio` 子进程环境变量。 |
| `url` | `""` | HTTP/SSE transport 必填 URL。 |
| `headers` | `{}` | HTTP/SSE 请求头。 |

`risk_overrides` 的值只能是 `read`、`write`、`execute`、`test`、`network`、`destructive` 或 `confirm`。transport 的协议行为、安全边界和生命周期仍以 [MCP 集成](../extensions/mcp-integration.md) 为准。

每个 server 还支持以下环境变量覆盖，其中 `<NAME>` 是 server name 转成大写并将非字母数字字符替换为下划线：

```text
TESTCODE_MCP_<NAME>_TRANSPORT
TESTCODE_MCP_<NAME>_TOOL_NAME_PREFIX
TESTCODE_MCP_<NAME>_COMMAND
TESTCODE_MCP_<NAME>_URL
TESTCODE_MCP_<NAME>_TIMEOUT
TESTCODE_MCP_<NAME>_READ_TIMEOUT
TESTCODE_MCP_<NAME>_ENABLED
```

headers、env、args 和 `risk_overrides` 不提供同名环境变量整体覆盖；其中的字符串值可以使用 `${VAR}` 展开敏感信息。
