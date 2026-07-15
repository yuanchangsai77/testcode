# MCP PR Review Remediation Plan

## 背景

当前 MCP changes 已覆盖 stdio、Streamable HTTP、旧版 HTTP+SSE、工具发现、资源读取和会话恢复，完整测试可以通过。PR 级审查仍发现五个需要在合入前关闭的边界问题。本文件作为本轮修复的施工基线；实现和测试应与这里的决策保持一致。

## 修复范围

### 1. 模型侧工具名兼容

问题：MCP 工具名允许 `.` 且可能超过模型 function tool 的长度限制。直接使用 `<prefix>__<remote_name>` 会让整个模型请求因单个非法工具名失败。

决策：

- 合法且不超过 64 字符的稳定名保持不变，避免破坏已有配置和会话。
- 包含非法字符或超过长度限制时，将非法字符替换为 `_`，并追加基于原始稳定名生成的短哈希。
- 生成结果只允许 ASCII 字母、数字、下划线和短横线，长度不超过 64。
- 实际 MCP 调用、风险覆盖和日志继续使用远端原始工具名。

验收：带点号、空格、超长名称的工具均能注册为合法且确定的模型工具名；不同原始名称不会因规范化静默覆盖。

### 2. 旧版 SSE endpoint 信任边界

问题：服务端 `endpoint` 事件可以指定任意绝对 URL，客户端随后会向该地址发送请求并转发配置 headers，形成 SSRF 和跨源凭据转发风险。

决策：

- endpoint 可以是相对 URI 或与初始 SSE URL 同源的绝对 URI。
- scheme、host 或有效端口不同均视为跨源并拒绝。
- endpoint 解析失败时以 `mcp_protocol_error` 结束初始化，不发送后续 POST。

验收：同源绝对/相对 endpoint 正常工作；跨域、跨端口及 HTTP/HTTPS 降级 endpoint 被拒绝，且没有发起 POST。

### 3. 多服务器资源命名空间

问题：资源 URI 只在当前 provider 实现中被当作全局 ID；多个服务器暴露相同 URI 时，读取会静默命中配置顺序中的第一个服务器。

决策：

- 对 runtime 暴露 `mcp-resource://<encoded-server>/<encoded-resource-id>` 形式的稳定资源 ID。
- descriptor metadata 保留原始 URI。
- 读取时按稳定 ID 定位 descriptor，并将原始 resource ID 发送给对应服务器。

验收：两个服务器可以同时暴露相同 URI；列表 ID 唯一，且分别读取到各自内容。

### 4. transport EOF 快速失败

问题：stdio stdout 或 SSE stream 意外 EOF 时，reader 静默退出，等待中的请求只能等到默认 read timeout。

决策：

- 非主动关闭导致的 EOF 必须向等待队列投递 transport-closed 信号。
- 主动 `close()` 不产生错误信号，避免污染后续生命周期。
- manager 收到该错误后继续沿用现有的一次性重连策略。

验收：stdio/SSE 意外 EOF 立即返回 `mcp_transport_closed`，不等待 read timeout；主动关闭保持安静。

### 5. 协议版本协商

问题：客户端请求 `2025-03-26`，但会无条件接受服务器返回的任意版本并发送 initialized 通知。

决策：

- 明确维护客户端支持版本集合，当前仅包含 `2025-03-26`。
- 服务端遗漏版本或返回不支持版本时关闭 transport，并抛出 `mcp_protocol_error`。
- 只有版本校验通过后才发送 initialized 通知。

验收：支持版本初始化成功；缺失或不支持版本时连接关闭且不发送 initialized。

## 完成标准

- 上述五项均有针对性回归测试。
- 完整 pytest 套件通过。
- `git diff --check` 与 Python compileall 通过。
- 不改变无关功能，不提交或推送 Git changes。
