# 核心运行时：执行安全

## 文档职责

本文档说明当前工具执行的授权、审批、危险动作识别和写入内容检查。它回答：

- 三种安全模式分别允许什么
- 工具风险如何转换为允许、审批或阻断
- 写入内容在执行前接受哪些检查
- 安全检查能覆盖什么，不能替代什么

配置来源和默认值见[配置参考](../reference/configuration.md)，Shell 进程的中断与清理见
[Shell 生命周期](shell-session-lifecycle.md)，工具字段流向见
[Tool 字段契约](../reference/tool-contract.md)。本文档不定义这些领域。

## 执行检查顺序

工具请求按以下顺序进入运行时：

```text
模型请求
  → 工具可见性
  → 参数与写入内容预检
  → risk policy
  → 必要时请求用户审批
  → 工具实现
  → 结果与安全事件
```

激活 Skill 或 MCP 工具只改变模型能否看到该能力，不改变这条执行路径。外部工具仍要经过
统一参数校验、policy、审批和日志记录。

## 安全模式与风险等级

当前风险等级为 `read`、`write`、`execute`、`test`、`network`、
`destructive` 和 `confirm`。

| 模式 | 直接允许 | 需要审批 | 直接阻断 |
| --- | --- | --- | --- |
| `readonly` | `read` | 无 | 其他风险 |
| `confirm` | `read` | `write`、`execute`、`test`、`network`、`destructive`、`confirm` | 未知风险 |
| `auto` | `read`、`write` | `execute`、`test`、`network`、`destructive`、`confirm` | 未知风险 |

`shell_exec` 通常是 `execute`，但明显破坏性的命令会在运行时提升为
`destructive`。当前识别包括递归强制删除、`git reset --hard`、
强制清理目录以及向常见系统目录重定向写入。

非破坏性审批可以在同一次 run 中按“工具名 + 风险等级”复用；破坏性动作每次都需要
单独确认。用户拒绝审批会返回 `approval_denied`，模式直接阻断会返回
`blocked_by_policy`。

## 写入内容检查

风险审批回答“是否允许执行这类动作”，内容检查回答“拟写入内容是否包含高置信度
凭据”。用户批准写入不能绕过内容检查。

当前检查入口包括：

- `patch`：只扫描 unified diff 的新增行，不扫描删除行。
- `apply_change`：扫描拟写入的完整内容；该工具仅为兼容路径。
- `shell_exec`：扫描命令中的字面文本，作为补充防线。

当前高置信度类别包括私钥头、常见供应商令牌、云访问密钥、Bearer Token、URL
内嵌凭据和敏感字段的直接赋值。环境变量引用、`YOUR_API_KEY`、`<api-key>`、
`replace-me` 等明确占位值不会按真实凭据阻断。

命中后，工具不会执行，并返回：

- `error_code`: `blocked_by_security_policy`
- `policy_id`: `SEC-CREDENTIAL-001`
- 命中数量、文件位置和类别

结果、metadata 和日志只记录类别与位置，不回显命中的秘密原文。模型必须改为环境变量
或其他受保护的运行时来源后再继续。

## 安全边界

- Shell 字面扫描无法理解任意编码、子进程或运行时生成的内容，不能证明一条 Shell
  命令不会泄露凭据。
- 进程组清理不是文件系统、网络或资源沙盒。不可信任务仍需运行在容器或系统级沙盒中。
- 内容检查当前保护写入路径，不等同于完整的数据防泄漏系统。
- 日志脱敏是第二道保护，不应代替执行前阻断。
- `apply_change` 仍被注册用于兼容；新工作流应使用 `patch`。

## 可观察性

授权判断记录 `safety.check` 事件；内容扫描记录
`safety.content_scan.completed` 或 `safety.content_scan.blocked`。事件保留工具名、
风险、模式、决策、命中数量和类别，不保存命中的凭据。
