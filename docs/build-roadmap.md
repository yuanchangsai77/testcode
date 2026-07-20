# testcode Build Roadmap

## 文档职责

本文档负责：

- 说明当前系统处于什么阶段
- 记录还缺哪些能力
- 定义推荐实现顺序和阶段验收标准

本文档不负责替代架构设计或专项接口说明。具体边界以其他文档为准：

- 总体架构：`docs/architecture.md`
- 扩展点抽象：`docs/runtime-extensibility.md`
- Skill 设计：`docs/skill-system.md`
- MCP 设计：`docs/mcp-integration.md`
- tool 字段契约：`docs/tool-contract.md`

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

## 长任务上下文原则

长任务的准确性不能靠把所有历史和环境信息都塞进 prompt。runtime 应保存完整事实，prompt 只注入继续当前任务所需的最小充分上下文。

- 全量对话过程、完整工具结果、原始事件和 artifacts 必须持久化保存；剪枝只发生在模型调用前的 context packaging 阶段。
- 剪枝逻辑必须模块化，不能散落在 session store、context loaders、prompt builder 和各个 tool 里。
- Hot context：本轮必须使用的信息，包括当前目标、当前阶段、下一步、最近失败、待修改文件状态、最新测试结果。
- Warm summary：压缩后的历史结论，包括已调查路径、已做决策、已完成修改、已排除方向。
- Cold archive：完整原始记录，包括 events、details、完整工具输出、patch preview、测试日志和文件 read state；需要细节时通过工具或日志回查。
- Checkpoint 是持久化事实来源，不等于 prompt 全量注入。resume prompt 应注入 checkpoint 的裁剪恢复包，并带上来源、run id、时间点、文件 hash 或测试命令等可核验引用。
- 上下文裁剪必须可观察：被摘要、被省略、被截断的内容要在日志和 prompt 标记中说明，避免模型误以为上下文完整。
- 早期实现应先建立稳定接口和透传行为，再逐步启用预算、摘要和裁剪策略，避免为了过早优化上下文而增加后续重构成本。

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
- 项目规则、workspace summary、显式 `--context` 上下文加载。
- Skill metadata 扫描、能力仓库目录展示、instructions 按需激活及 active skill 注入 prompt。
- 内置、用户全局、项目级 Skill 目录。
- run 日志写入 `.testcode/runs/`。
- pytest 覆盖了核心 engine、model、policy、tools、context、Skill、MCP、能力仓库和 CLI 的关键路径；当前 276 个用例通过。

仍存在的关键缺口：

- 模型协议已有原生 tool calling，但协议提示、响应清洗和错误恢复还需要继续打磨。
- session 持久化层尚未保存完整长任务状态：tool history、read state、任务计划、检查点、审批上下文、测试状态。
- 长任务仍缺少明确的 plan/task 状态机，无法稳定表达 pending、in_progress、blocked、verified、done。
- 模型请求仍是非流式；长任务的可取消、可恢复和用户可观察进度还需要增强。
- 上下文收集已有基础，但还缺统一 token/字符预算、分层记忆、历史摘要、旧工具输出压缩和 resume 恢复包。
- Skill 已有最小可用链路，但缺少 references/assets/scripts 的按需加载和 Skill script 审批执行模型。
- MCP 已具备三种 transport、tool/resource 协议主链路、缓存、重连、安全映射和专项测试；后续重点是公网兼容性与 resource context packaging。
- 没有 subagent/team/A2A 编排模型。
- 敏感文件、日志脱敏和外部路径授权仍需持续补强。

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

进展：

- 已完成 `WorkspaceSummaryLoader`，在每次执行前注入 bounded workspace summary。
- 已检测 `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`。
- 已推断 Python、Node.js、Rust、Go 及常见测试命令。
- 已补充 Python、Node、Go marker 和目录摘要测试。

剩余：

- 继续增强 package-manager 细节，例如 uv/poetry/pnpm/yarn 等。
- 增加 Rust fixture 测试。

原始待办：

- 检测 `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`。
- 推断语言、包管理器、常见测试命令。
- 不读取大文件全文，只返回摘要。
- 增加 Python、Node、Rust、Go fixture 测试。

### P1.2 项目规则加载

进展：

- 已完成 `ProjectRulesLoader`，从 cwd 向上查找 `AGENTS.md`。
- 已支持以 `.git`、`pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod` 作为项目边界。
- 已按从根到近目录的顺序注入规则，近目录规则在 prompt 中靠后，可覆盖上层规则。
- 已限制单个规则文件读取大小，避免大文件撑爆上下文。
- 已补充多层规则、截断、prompt 注入和 app 装配测试。

剩余：

- 暂未做按任务选择额外规则文件；当前只加载 `AGENTS.md`。
- 暂未建立 README、架构文档等高价值文档的按需摘要机制。

原始待办：

- 从 cwd 向上查找 `AGENTS.md`。
- 支持多层规则，近目录优先。
- 只加载与当前任务相关的规则，避免一次性塞满上下文。
- 读取 README 和 `docs/architecture.md` 等高价值文档时，优先生成预算内摘要和来源引用，避免简单塞入前 N 字符。
- 增加多层规则冲突测试。

### P1.3 Git 和 Workspace 摘要

进展：

- 已收集当前分支、短状态、最近 commit。
- 已生成目录树摘要，忽略 `.git`、venv、`node_modules`、缓存目录和 `.testcode`。
- 已对目录摘要做深度和条目数限制。
- 已补充 clean git repo、目录忽略、截断和 prompt 注入测试。

剩余：

- 暂未收集 working tree diff 的预算内摘要。
- 暂未做 workspace summary 缓存和文件变化失效。

原始待办：

- 收集当前分支、git status、working tree diff 摘要、最近 commit；diff 只进入摘要，完整 diff 留给按需工具读取。
- 生成目录树摘要，忽略 `.git`、venv、`node_modules`、缓存目录。
- 缓存 workspace summary，文件变化后失效。
- 对大目录做数量和深度限制。

### P1.4 显式上下文

进展：

- 已完成 CLI `--context` 参数，支持多次传入。
- 已完成 `ExplicitContextLoader`，支持 workspace 内文件、目录和 glob。
- 已对路径做 workspace 边界检查，拒绝越界路径。
- 已限制显式上下文文件数量和单文件读取大小，二进制文件不注入。
- 已记录 explicit context 来源，并在 prompt 中单独展示。
- 已补充 loader、prompt、越界拒绝、截断、二进制拒绝和 CLI dispatch 测试。

剩余：

- 暂未支持更复杂的 include/exclude glob 规则。
- 暂未做 context token 预算统一裁剪；后续并入 P7.2。用户显式提供的上下文也应受总预算约束，超出时保留来源清单和摘要。

原始待办：

- CLI 增加 `--context path`，支持多个路径。
- 路径必须通过 workspace 安全检查。
- 记录 context 来源，让模型区分自动上下文和用户指定上下文。
- 支持文件、目录、glob 的受控展开。

## P2：Skill 系统

（详细方案设计已完成，见 [docs/skill-system.md](skill-system.md) 与 [docs/runtime-extensibility.md](runtime-extensibility.md)）

目标：像 Codex/Claude Code 一样支持标准 Skill 和项目/用户自定义 Skill。


### P2.1 Skill 格式

进展：

- 已定义 `SKILL.md` + frontmatter 格式，支持 name、description、triggers、version。
- 已支持内置 Skill：`src/testcode/skills/builtins/`。
- 已支持用户全局 Skill：`~/.testcode/skills/`。
- 已支持项目 Skill：`.testcode/skills/`。

剩余：

- 尚未支持 `assets/`、`scripts/`、`references/` 的标准化加载和生命周期。
- 尚未定义 Skill 版本冲突、同名覆盖和来源优先级的用户可见诊断。

### P2.2 Skill 发现和加载

进展：

- 已实现启动时 metadata 扫描，避免一次性加载所有正文。
- 已通过 `SkillToolboxSource` 将 Skill metadata 暴露为能力仓库目录。
- 已实现打开 toolbox 只返回 instructions manifest，显式激活后才加载正文。
- 已实现 active skills 跨多轮 session 传递。
- `SkillContextLoader` 及 trigger/`/skill` 匹配仍作为兼容组件和测试路径保留，但当前 `create_app()` 主链路不注册该 loader。

剩余：

- 尚未形成 `/skill` CLI 命令体验；当前通过能力仓库工具打开和激活 Skill。
- 尚未实现 Skill 引用额外文件时的按需读取。
- 尚未实现 Skill 内容的独立预算裁剪和过长内容摘要。

### P2.3 Skill 对工具和上下文的影响

- 已完成 Skill instructions 注入 system prompt。
- 已明确 Skill 只能提供上下文和流程建议，不绕过 policy，不自动获得更高权限。
- 已补充兼容 trigger loader、能力仓库激活、prompt 注入和跨轮保留测试。

剩余：

- Skill scripts 还没有正式执行模型；后续必须转换为普通 tool action，并走同一套审批、日志和风险策略。
- Skill 派生 tool 还没有接入 `ToolProvider`。

## P2.5：正式长任务跑通

目标：在接入 MCP 和 subagent 之前，先让单 agent 能稳定跑完可中断、可恢复、可复盘的长编码任务。

### P2.5.0 Context Packaging 边界

目标：在真正做复杂剪枝前，先建立一个独立的“模型注入前”层，集中处理 prompt context 的选择、排序、裁剪和审计。

- 新增 `ContextPackager` 或等价模块，输入为 session 状态、checkpoint/archive 索引、candidate context 和 source references，输出为 `PromptContextPackage`。
- `SessionStore` 只负责完整保存，不负责裁剪。
- `ContextLoader` 只负责发现和产生候选上下文，不直接决定最终 prompt 预算。
- `ModelPromptBuilder` 只负责把 `PromptContextPackage` 渲染成 provider messages，不内置复杂剪枝策略。
- 初版 `ContextPackager` 可以只做透传、分组、来源标记和总字符统计，不急于做复杂摘要。
- 后续预算策略、Hot/Warm/Cold 分层、摘要和裁剪都收敛到这一层；完整原始内容按需读取，不要求 packager 常驻持有。
- 增加单元测试：完整对话被保存、packaging 后只注入预算内上下文、被省略内容带 source reference。

### P2.5.1 Session Checkpoint

- session schema 增加 runtime state：任务计划、当前阶段、tool history 摘要、最近失败原因、最近测试结果。
- 保存 read state：path、mtime、sha256，使 resume 后能判断是否仍可继续 patch，或明确要求重新读取。
- 保存 active skills、context sources、run ids、关键 artifact 摘要，并把完整工具输出和原始事件保留在 cold archive。
- 中断、模型错误、工具错误、审批拒绝时都写入 checkpoint。
- resume 时只将 checkpoint 的最小恢复包注入 prompt，并清楚区分历史事实、当前状态和下一步建议。
- prompt 中的恢复包必须带来源引用，例如 run id、文件路径、mtime/hash、测试命令或 artifact id，便于按需回查完整记录。

### P2.5.2 Task Plan 状态机

- 引入轻量 task plan：pending、in_progress、blocked、verified、done。
- 每次 run summary 展示当前阶段、已完成事项、阻塞原因、建议下一步。
- 连续无进展、重复读取、重复失败测试时更新状态，而不是只返回散落的工具错误。
- 支持用户在长会话中查看当前任务状态，例如 `/status` 或等价 CLI 命令。

### P2.5.3 长任务恢复和验证闭环

- Ctrl+C、模型 API 失败、工具超时后，保留可恢复状态。
- 恢复后优先读取 checkpoint 和最新 workspace summary，再决定是否继续。
- 对涉及文件修改的长任务，最终必须有明确验证结果：测试通过、测试失败、未运行测试及原因。
- run details 顶部增加失败诊断摘要和下一步恢复建议。

### P2.5.4 上下文预算前置版

- 在完整 P7.2 前先在 `ContextPackager` 内做最小预算保护：限制 conversation、tool results、Skill、explicit context 的总字符量。
- 旧工具输出优先压缩为摘要，保留最新失败测试、最新 patch、关键错误和可回查来源。
- 超预算时写入日志和 prompt 标记，避免静默丢失关键上下文。
- 预算策略默认遵循 Hot context > Warm summary > Cold archive 索引，不把 cold archive 全量注入模型。

## P3：MCP 接入

目标：把外部 MCP server 暴露的 tools/resources 纳入同一套 agent runtime。

详细设计见 [docs/mcp-integration.md](mcp-integration.md)。P3 的实现应以该文档中的模块拆分和边界为准，避免把 transport、server lifecycle、tool adaptation 和 resource indexing 混写成单个 provider。

### P3.1 MCP 配置

- 支持全局配置：`~/.testcode/config.toml`。
- 支持项目配置：`.testcode/config.toml`。
- 定义统一 MCP server 配置：name、transport、enabled、tool_name_prefix、risk_overrides、timeout、read_timeout。
- `stdio` 使用 `command`、`args`、`env`。
- `streamable_http` 和 `sse` 使用 `url`、`headers`。
- 配置优先级：CLI 参数 > env > 项目配置 > 全局配置 > 默认值。
- `tool_name_prefix` 默认取 server name；允许显式覆盖。
- 启动阶段校验最终稳定 tool id 全局唯一；命名冲突时拒绝注册冲突项，并输出可诊断错误。
- 支持 `risk_overrides`，用于覆盖具体 MCP tool 的默认风险级别。
- 支持 `${VAR}` 形式的环境变量展开，并对敏感值脱敏。

### P3.2 MCP Client Runtime

- 先定义统一 `MCPTransport` 抽象，再分别接入 `stdio`、`streamable_http`、`sse`。
- 当前已具备 `stdio`、`streamable_http`、`sse` 主链路、专项 observability、磁盘 discovery cache 与一次性失效重连。
- 已将运行时拆为 `MCPTransport`、`MCPClient`、`MCPManager`、`MCPDiscoveryService`、adapter、`MCPToolboxSource`、`MCPResourceProvider`，分别负责消息传输、协议调用、生命周期管理、懒发现与缓存、schema/result 适配、按需激活和 resource 入口。
- `MCPToolProvider` 只作为兼容性直接注册接口保留；当前 `create_app()` 主链路使用 `MCPToolboxSource`。
- 拉取 MCP tools，转换为内部 `ToolDefinition`。
- 拉取 MCP resources 的索引和元数据，作为可按需读取的上下文来源；通过独立 `MCPResourceProvider` 暴露，不和 tool provider 混写。
- MCP tool 执行结果转换为统一 `ToolResult`。
- MCP 调用同样走 policy、approval、logger。
- MCP server 崩溃时返回可恢复错误，不拖垮主流程。
- 同一 runtime 内复用已建立的 server client；关闭 session/runtime 时统一清理。
- app 启动默认不强依赖所有 server 在线；新 server 可走懒发现、缓存快照和显式刷新策略。
- URL 型 server 记录 `timeout`、`read_timeout`、HTTP/SSE 错误，并统一做 secret redaction。

### P3.3 MCP 安全模型

- 为 MCP tool 定义 capability traits，再映射到默认 risk；未知工具默认 `confirm`。
- traits 至少覆盖：本地读、本地写、执行代码、网络访问、远端状态变更、凭证使用、长耗时。
- 配置允许用户为具体 MCP tool 覆盖 risk level。
- MCP resource 读取要走敏感信息保护和长度限制。
- 日志记录 server name、tool name、稳定 id、traits、映射后的 risk、是否 override、耗时、错误码。
- 不允许 MCP 通过专用通道绕过现有 `ToolRegistry`、policy 或 approval。
- URL query、headers、env 中的 key/token 必须脱敏后再进入日志。

### P3.4 能力仓库与渐进激活

目标设计见 [docs/capability-warehouse.md](capability-warehouse.md)。MCP、Skill、插件和单体工具统一作为仓库资产，不再默认把全部外部能力注册到当前模型工具列表。

- 核心工作台只常驻文件、shell、编辑、询问和仓库操作等少量基础能力。
- MCP server 和 Skill 作为工具箱，默认只暴露外层名称、描述、类型和能力标签。
- 打开工具箱时按需获取受限 manifest，不立即注入全部 schema 或正文。
- 只把当前步骤需要的少量叶子工具、Skill 指令或引用放入 activation set。
- `ToolRegistry` 只承载核心工具与当前激活集；仓库总目录由独立组件维护。
- 激活按 turn/run/session 管理范围，并受数量、schema 字符数、TTL 和回收策略约束。
- 未打开的 MCP 不连接、不 discovery、不报错，也不影响当前任务。
- 激活不绕过 policy、approval、logger 和结果裁剪。

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
- Ctrl+C 已支持中断当前输入、模型或工具并保留 session 流程。
- 已实现 TTY UTF-8 输入、光标编辑、视觉折行和 resize 重绘；持久化输入历史仍待实现。
- 已支持 TTY 彩色输出、全宽边框和窄屏状态栏；非 TTY 保留纯流式输入回退。
- 普通屏幕模式下，部分终端缩放时可能保留上边框回流行；后续需在“实时单下边框”与完整 TUI 之间选择结构性方案。
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
- 定义分层记忆：Hot context、Warm summary、Cold archive。
- conversation、tool results、workspace summary、Skill 内容、explicit context、MCP resources 按优先级裁剪。
- 超预算时先把旧 tool output 压缩为带来源引用的摘要，再压缩旧 conversation。
- session schema 增加 `summary` 和 `archive_index` 字段；summary 用于 prompt，archive_index 用于按需回查完整记录。
- 达到阈值后触发历史摘要，resume 时优先加载最小恢复包，而不是完整历史。

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
- token usage 展示：有价值，但低于长任务 checkpoint、上下文预算和恢复能力。
- review 模式：可以后置到核心编辑闭环之后。
- session rename/tag/fork：产品化功能，后置。
- zsh/fish completion：分发阶段再做。
- 大规模测试目录重构：先补关键测试，稳定后再整理。

## 近期推荐执行顺序

1. 对齐文档和实现状态，补一个 `docs/versions/v0.2.md` 快照，固化当前 P0/P1/P2 最小可用边界。
2. P2.5.0 Context packaging 边界，先建立独立注入前层，初版只做透传、分组、来源标记和统计。
3. P2.5.1 Session checkpoint，先解决中断和 resume 后状态丢失。
4. P2.5.2 Task plan 状态机，让长任务有明确阶段、阻塞原因和下一步。
5. P2.5.3 长任务恢复和验证闭环，保证中断、模型失败、测试失败后能继续。
6. P2.5.4 上下文预算前置版，在独立 packager 内建立 Hot/Warm/Cold 分层，避免长会话 prompt 失控。
7. P2 剩余项：Skill references/assets/scripts 和 `/skill` 交互体验。
8. P3 MCP 接入。
9. P4 本地 subagent/team。
10. P5 A2A 远程 agent。
11. P6/P7 体验、配置、日志、质量门禁持续补齐。

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

## 第二阶段验收标准

完成 P2.5 后，`testcode` 应至少具备：

- 一个长任务可以被中断、保存、恢复，并清楚说明恢复点。
- resume 后不依赖模型猜测历史，prompt 中有最小恢复包、任务状态、关键工具结果摘要和可回查来源。
- 文件修改任务恢复后能判断已读文件是否仍然安全，或要求重新读取。
- 每次 run summary 都展示当前阶段、完成事项、阻塞原因、验证状态和下一步。
- 长对话不会无限塞入旧历史；完整事实保存在 archive，prompt 只放预算内摘要；超预算时有可观察的摘要和裁剪记录。
- 涉及代码修改的任务最终明确给出测试通过、测试失败或未运行测试的原因。
