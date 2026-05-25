# testcode Build Roadmap

本文档用于指挥 `testcode` 从当前 CLI agent scaffold 演进为一个可扩展的智能脚手架：先具备稳定的多轮对话和本地工具执行能力，再接入标准 Skill、外部 MCP，最后扩展到 team/subagent 和 A2A。

## 版本快照

本文档是活文档，可以持续调整优先级和实现顺序。已经形成阶段性能力边界时，在 `docs/versions/` 下新建版本快照文件并固化下来，例如：

- `docs/versions/v0.1.md`

版本快照默认不回改。`v0.1` 记录当时已有功能；后续 `v0.2`、`v0.3` 等文件记录相对前一版本新增、优化或删除的内容。

## 目标边界

当前优先级不是先做完整产品外壳，而是把核心 agent runtime 做稳：

- 多轮对话中，模型能可靠调用工具读取、搜索、修改、运行测试。
- 文件修改必须可审计、可回滚、可验证，不能靠整文件覆盖碰运气。
- 工具系统要能承载内置工具、Skill 派生上下文、外部 MCP tools。
- 后续 team/subagent/A2A 都应该复用同一套 session、tool、policy、logger 基础设施。

## 当前状态

已具备：

- CLI 单轮和多轮对话。
- session 保存、列表、resume、latest。
- OpenAI-compatible 模型客户端和 stub 模型客户端。
- 原生 `message.tool_calls` 解析。
- JSON content 格式的模型 action fallback。
- 内置工具 registry、结构化 `ToolDefinition.input_schema`、`risk_level`。
- 文件读取、目录列表、文件信息、文本搜索、文件查找。
- shell 执行、测试运行、unified diff patch。
- git 只读工具：status、diff、show。
- `readonly`、`confirm`、`auto` 三种运行模式。
- write/execute/test/destructive 风险审批。
- 明显危险 shell 命令识别。
- run 日志写入 `.testcode/runs/`。
- pytest 覆盖了核心 engine、model、policy、tools 的一部分路径。

仍存在的关键缺口：

- 模型协议已有原生 tool calling，但协议提示、响应清洗和错误恢复还需要继续打磨。
- 修改前读取、hash 防覆盖、diff preview、测试反馈闭环还不完整。
- 上下文收集仍偏被动，缺少项目探测、AGENTS 规则、git/test 自动摘要。
- 没有 Skill 加载机制。
- 没有 MCP server/tool 接入机制。
- 没有 subagent/team/A2A 编排模型。
- 敏感文件和日志脱敏仍不完整。

## P0：稳定核心编码 CLI

目标：让 `testcode` 能在多轮对话里稳定完成小型真实代码任务。

### P0.1 模型协议打磨

- 继续保留原生 `message.tool_calls` 和 JSON content fallback。
- 清理模型输出中的协议噪声，避免 `<think>` 等内容进入用户可见的中间消息。
- 强化响应校验：缺 `choices`、缺 `message`、空 content、非法 tool name、非法 arguments 都返回可读错误。
- 对模型网络错误、代理断连、非法响应形状提供稳定错误信息。
- 增加测试：JSON fallback、原生 tool_calls、非法响应、混合异常、代理断连。

### P0.2 可靠编辑工作流

进展：

- 已完成已有文件修改前必须先 `read_file`；新增文件仍允许直接 patch。
- 已完成本次 `execute()` 运行内记录已读取文件的 path、mtime、sha256。
- 已完成 patch 前重新计算 hash/mtime；文件被外部修改时返回 `file_changed_since_read`。
- 已完成 patch 结果 metadata 返回 preview diff、changed files、行数统计。
- 已完成 patch 文件数和 diff 行数上限。
- 已补充未读拒绝、读后外部修改拒绝、上下文不匹配、创建文件、多文件修改、路径越界和超限拒绝测试。

剩余：

- session 持久化层尚未保存 read state，跨 run resume 后仍需要重新读取目标文件。
- `patch` 仍偏底层，依赖模型手写 unified diff；后续需要结构化编辑工具或由 runtime 自动生成 diff，降低 `patch_syntax_error` 重试失败率。

原始待办：

- 模型 patch 文件前必须先 `read_file` 目标文件。
- session 记录已读取文件的 path、mtime、hash。
- patch 前重新计算 hash，文件被外部修改时拒绝并要求重新读取。
- patch 应用前生成 preview diff。
- patch 应用后返回 changed files、diff summary、行数统计。
- 改进 patch 失败诊断：区分 diff 语法错误、上下文不匹配、路径越界、文件状态不符。
- 限制单次 patch 的最大文件数和最大 diff 行数。
- 增加测试：未读拒绝、读后外部修改拒绝、上下文不匹配、创建文件、多文件修改、超限拒绝。

### P0.3 验证闭环

进展：

- 已完成 `run_tests` 输出测试通过、失败或超时状态，并写回 session history。
- 已完成失败测试输出进入模型下一轮上下文，允许模型继续修复。
- 已完成连续失败测试轮次上限，避免无限 fix loop。
- 已完成 run summary 展示测试状态、退出码、输出行数和耗时。
- 已增加 fake model e2e：第一次测试失败，第二轮读取失败输出后继续修复成功。

剩余：

- 暂无。

### P0.4 安全补齐

进展：

- 已完成非交互模式下默认拒绝需要确认的操作，并有测试覆盖。
- 已完成本次 `execute()` 内记住同一 tool/risk 的 approval。
- 已通过 workspace path resolve 防止 symlink 跳出 workspace。
- 已完成敏感文件规则：`.env`、私钥、`.pem`、token 类文件默认不直接返回内容。
- 已完成日志中对 token-like value 和敏感 env key 脱敏。

剩余：

- 可继续扩充敏感文件模式和 symlink 写入专项 fixture 覆盖。

### P0.5 工具输出质量

进展：

- 已完成 `git_status` 输出清理：用户可见输出不再嵌入底层 `exit_code/stdout/stderr` 包装。
- 已完成 `git_status`、`git_diff`、`git_show` 的稳定 error_code 和结构化 metadata 补齐。
- 已补充 git 工具测试，覆盖 clean repo、dirty repo、non-git repo，以及无效 revision。
- 已完成 run summary 关键工具结果简洁摘要，优先使用结构化 metadata，避免直接展示大段 stdout/stderr/diff。

剩余：

- 暂无。

原始待办：

- 清理 `git_status` 输出，避免把底层 `exit_code/stdout` 嵌入用户可见摘要。
- 明确区分工具的 user-facing output 和结构化 metadata。
- 为 `git_status`、`git_diff`、`git_show` 补稳定 error_code 和 metadata。
- run summary 展示关键工具结果时做简洁摘要。
- 增加 clean repo、dirty repo、non-git repo 的 git 工具测试。

## P1：主动上下文收集

目标：减少模型盲目探索，让每次请求一开始就拿到高价值上下文。

### P1.1 项目探测

- 检测 `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`。
- 推断语言、包管理器、常见测试命令。
- 不读取大文件全文，只返回摘要。
- 增加 Python、Node、Rust、Go fixture 测试。

### P1.2 项目规则加载

- 从 cwd 向上查找 `AGENTS.md`。
- 支持多层规则，近目录优先。
- 只加载与当前任务相关的规则，避免一次性塞满上下文。
- 读取 README 和 `docs/architecture.md` 的前 N 字符或摘要。
- 增加多层规则冲突测试。

### P1.3 Git 和 Workspace 摘要

- 收集当前分支、git status、working tree diff 摘要、最近 commit。
- 生成目录树摘要，忽略 `.git`、venv、`node_modules`、缓存目录。
- 缓存 workspace summary，文件变化后失效。
- 对大目录做数量和深度限制。

### P1.4 显式上下文

- CLI 增加 `--context path`，支持多个路径。
- 路径必须通过 workspace 安全检查。
- 记录 context 来源，让模型区分自动上下文和用户指定上下文。
- 支持文件、目录、glob 的受控展开。

## P2：Skill 系统

目标：像 Codex/Claude Code 一样支持标准 Skill 和项目/用户自定义 Skill。

### P2.1 Skill 格式

- 定义 Skill 目录结构：
  - `SKILL.md`：主要说明。
  - `skill.toml` 或 frontmatter：name、description、triggers、version。
  - 可选 `assets/`、`scripts/`、`references/`。
- 支持项目 Skill：`.testcode/skills/`。
- 支持用户全局 Skill：`~/.testcode/skills/`。
- 支持内置标准 Skill。

### P2.2 Skill 发现和加载

- 启动时只读取 Skill metadata，不读取所有正文。
- 根据用户请求、显式 `/skill` 命令、trigger 关键词选择相关 Skill。
- 被选中的 Skill 才加载 `SKILL.md`。
- Skill 引用额外文件时按需读取，避免上下文膨胀。
- 记录本轮加载了哪些 Skill。

### P2.3 Skill 对工具和上下文的影响

- Skill 可以提供额外上下文、操作流程、验证命令建议。
- Skill 不直接绕过 policy，也不能自动获得更高权限。
- Skill scripts 如需执行，必须转换为普通 tool action 并走审批。
- 增加测试：自动触发、显式触发、未触发不加载、Skill script 需要审批。

## P3：MCP 接入

目标：把外部 MCP server 暴露的 tools/resources 纳入同一套 agent runtime。

### P3.1 MCP 配置

- 支持全局配置：`~/.testcode/config.toml`。
- 支持项目配置：`.testcode/config.toml`。
- 定义 MCP server 配置：name、command、args、env、transport、enabled。
- 配置优先级：CLI 参数 > env > 项目配置 > 全局配置 > 默认值。

### P3.2 MCP Client Runtime

- 启动并管理 stdio MCP server。
- 拉取 MCP tools，转换为内部 `ToolDefinition`。
- 拉取 MCP resources，作为显式上下文来源。
- MCP tool 执行结果转换为统一 `ToolResult`。
- MCP 调用同样走 policy、approval、logger。
- MCP server 崩溃时返回可恢复错误，不拖垮主流程。

### P3.3 MCP 安全模型

- 为 MCP tool 定义默认 risk：未知工具默认 `confirm` 或 `network`。
- 配置允许用户为具体 MCP tool 覆盖 risk level。
- MCP resource 读取要走敏感信息保护和长度限制。
- 日志记录 server name、tool name、耗时、错误码。

## P4：Team / Subagent

目标：先实现本地 subagent 编排，再扩展到远程 A2A。

### P4.1 本地 Subagent

- 定义 `AgentSpec`：name、role、model、tools、cwd、context budget、mode。
- parent agent 可以发起 `delegate_task`。
- subagent 拥有独立 session、tool history、logger run id。
- subagent 最终只向 parent 返回摘要、关键发现、修改文件、验证结果。
- parent 可以限制 subagent 只读、只搜索、或允许 patch。

### P4.2 Team 编排

- 支持预设 team roles：planner、coder、reviewer、tester。
- 支持串行委派和有限并行委派。
- 防止多个 subagent 同时写同一文件：基于 file lock 或 patch 前 hash。
- team run summary 展示每个 agent 的任务、结果、耗时。

### P4.3 A2A 协议准备

- 抽象 agent message：task、context、artifact、tool result、final answer。
- 抽象 agent capability：tools、skills、workspace access、risk policy。
- 本地 subagent 先使用同一消息结构，为远程 A2A 做兼容层。

## P5：A2A 远程 Agent

目标：允许 `testcode` 与外部 agent 互相发现能力、委派任务、交换结果。

- 定义远程 agent 配置：endpoint、auth、capabilities、timeout。
- 支持 capability discovery。
- 支持 task submit、status poll 或 streaming events。
- 支持 artifact 传递：diff、日志、测试结果、摘要。
- 远程 agent 返回的修改必须以 patch/artifact 形式进入本地审批流。
- A2A 网络调用默认需要确认或显式配置允许。
- 增加失败降级：远程不可用时 parent agent 能继续本地执行或向用户报告。

## P6：交互体验和产品化

目标：在核心能力稳定后，再补齐易用性和分发。

### P6.1 CLI 体验

- `/help`、`/status`、`/sessions`、`/mode`、`/skill`、`/mcp`。
- Ctrl+C 中断当前模型或工具并保存 session。
- readline 历史和多行输入。
- TTY 彩色输出，非 TTY 自动关闭 ANSI。
- 工具 start/finish 进度展示和耗时展示。

### P6.2 配置命令

- `testcode config get key`。
- `testcode config set key value`。
- `testcode config list`。
- `testcode config path`。
- 非法配置返回清晰错误。

### P6.3 分发

- `--version`。
- zsh/fish completion。
- README 增加 editable install、模型配置、Skill、MCP、subagent 示例。
- release checklist。

## P7：可观测性和质量门禁

目标：让每次运行都能复盘，后续改动有稳定回归保护。

### P7.1 日志和诊断

- logger 创建 run id，session 记录 run id。
- 工具、模型、MCP、subagent 都记录耗时。
- `testcode logs --last`、`--run <id>`、`--session <id>`。
- details.log 顶部写失败诊断摘要：最后一个 model error、tool error、policy block。
- 日志写入失败不影响主流程。

### P7.2 上下文预算和摘要

- 定义 max input chars 或 token 近似预算。
- conversation、tool results、workspace summary、Skill 内容按优先级裁剪。
- 超预算时先压缩旧 tool output，再压缩旧 conversation。
- session schema 增加 `summary` 字段。
- 达到阈值后触发历史摘要，resume 时优先加载 summary。

### P7.3 测试和质量工具

- 继续补关键测试，不急于先重排目录。
- 覆盖模型协议、工具、policy、CLI、Skill、MCP、subagent。
- 配置 ruff。
- 视复杂度再加入 mypy 或 pyright。
- 增加 `make test` 或等价脚本。
- CI 运行 lint、typecheck、test。

## 暂缓或不优先

这些能力可以做，但不应阻塞当前目标：

- 结构化 replacement patch：unified diff 暂时够用。
- 流式 token 输出：体验优化，等 tool calling 稳定后再做。
- token usage 展示：有价值，但低于可靠编辑和 MCP/Skill。
- review 模式：可以后置到核心编辑闭环之后。
- session rename/tag/fork：产品化功能，后置。
- zsh/fish completion：分发阶段再做。
- 大规模测试目录重构：先补关键测试，稳定后再整理。

## 近期推荐执行顺序

1. P0.5 工具输出质量，先让模型看到干净、稳定的工作区状态。
2. P1.1-P1.3 主动上下文收集，建立项目、Git、Workspace 摘要。
3. P1.2 项目规则加载，支持 `AGENTS.md` 和按需规则文件。
4. P1.4 显式上下文，支持用户指定文件或目录进入本轮上下文。
5. P2 Skill 系统，先做最小发现、触发和 `SKILL.md` 加载。
6. P0.2-P0.3 可靠编辑和验证闭环。
7. P3 MCP 接入。
8. P4 本地 subagent/team。
9. P5 A2A 远程 agent。
10. P6/P7 体验、配置、日志、质量门禁持续补齐。

## 第一阶段验收标准

完成 P0 后，`testcode` 应至少具备：

- 能通过原生 tool calling 调用内置工具。
- 能安全读取、搜索、修改当前 workspace 内文件。
- 修改文件前必须读文件，读后外部修改会被拒绝。
- 所有写操作都通过 patch，并展示 diff summary。
- shell 和测试执行默认需要审批。
- 能运行测试命令，并把失败信息反馈给模型继续修复。
- 能识别并拒绝明显危险操作。
- 敏感文件读取和日志输出有基础保护。
- README 能说明如何运行、配置模型、执行一次真实代码修改任务。
