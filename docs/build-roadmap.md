# testcode Build Roadmap

本文档用于指挥 `testcode` 从当前 scaffold 演进为成熟编码 CLI。组织方式是“大 TODO / 小 TODO”：大 TODO 表示阶段目标，小 TODO 表示可落地的工程任务。

## 使用方式

- 每个大 TODO 完成后，应该能带来一个可体验、可验证的能力增量。
- 小 TODO 尽量保持可独立提交、可测试、可回滚。
- 优先完成“可靠改代码”和“安全执行”相关任务，再扩展 UI 和生态。
- 每个阶段结束时补测试、更新 README，并记录已知限制。

## 大 TODO 1：强化模型协议

目标：让模型调用从“自定义 JSON 文本约定”升级为稳定、可恢复、可观测的 agent protocol。

当前状态：

- 模型请求会把完整工具描述放在稳定 system prompt 前缀中，动态的 cwd、当前请求、conversation history 和 session history 放在后续 user message 中。
- 多轮对话每次请求只包含一份完整工具描述，不会把历史轮次的工具描述累积进 conversation。
- 工具描述按工具名和参数名稳定排序，便于 provider 对重复前缀做 prompt/KV cache。

小 TODO：

- 拆分 provider 接口。
  - 定义 `ModelClient` protocol，包含 `respond(session)`。
  - 将 `StubModelClient` 和 `OpenAICompatibleModelClient` 都挂到同一接口。
  - 为 provider 初始化增加配置对象，避免直接读散落的 env。
  - 测试：stub provider 和 OpenAI-compatible provider 都能被 `create_app` 注入。
- 支持原生 tool calling。
  - 在请求 payload 中加入 `tools` 定义。
  - 将内部 `ToolDefinition` 转换为 OpenAI-compatible tool schema。
  - 解析 `tool_calls` 为 `ToolAction`。
  - 保留当前 JSON content 解析作为 fallback。
  - 测试：content JSON、tool_calls、混合异常三种响应。
- 强化响应解析。
  - 校验 `choices`、`message`、`content`、`tool_calls` 字段。
  - 对空响应返回可读错误。
  - 对非法 action name 返回可读错误。
  - 对非法 arguments 类型返回可读错误。
  - 测试：缺字段、空 content、非法 JSON、非法 action。
- 增加请求可靠性。
  - 增加 timeout 配置。
  - 增加最多 N 次重试。
  - 只对网络错误和 5xx 重试。
  - 不对 4xx、认证错误、schema 错误重试。
  - 日志记录每次尝试、失败原因和最终错误。
- 支持流式输出。
  - 增加 `stream` 配置开关。
  - 定义 stream chunk 数据结构。
  - presenter 增加 token 增量展示方法。
  - 非交互模式允许关闭流式展示。
  - 测试：模拟 chunk 拼接为完整消息。
- 记录模型用量。
  - 从响应中提取 usage。
  - 记录 prompt tokens、completion tokens、total tokens。
  - 记录模型名称、base_url、耗时。
  - 在 run summary 中展示简短用量。
  - 对缺失 usage 的 provider 保持兼容。
- 增加上下文预算。
  - 定义 max input chars 或 token 近似预算。
  - 为 conversation、tool results、workspace summary 设置优先级。
  - 超预算时先压缩旧 tool output。
  - 再压缩旧 conversation。
  - 测试：超长历史不会生成无限 payload。
- 增加历史摘要。
  - 在 session 中保存 `summary` 字段。
  - 达到阈值后触发摘要请求。
  - 摘要失败时保留原历史并记录 warning。
  - resume 时优先加载 summary。
  - 测试：多轮会话生成并使用摘要。

## 大 TODO 2：建设可靠工具系统

目标：让 CLI 具备真实编码助手需要的文件、搜索、shell、git、测试工具能力。

当前状态：

- 已建立结构化工具协议：`ToolDefinition.input_schema`、`risk_level`，以及 `ToolResult.error_code`、`metadata`。
- 已实现 registry 参数校验：未知工具、缺必填参数、未知参数都会返回稳定错误码。
- 已实现 workspace 路径解析 helper：相对路径基于 request cwd，默认拒绝 workspace 外路径。
- 已按“一个 tool 一个文件”拆分内置工具，具体实现位于 `src/testcode/tools/builtins/`。
- 已实现文件、搜索、shell、patch、测试运行、git 只读工具的第一版。
- 已废弃 `apply_change`，默认不暴露给模型。
- 已接入 CLI 审批：`shell_exec`、`patch` 等风险工具在执行前需要用户确认。
- 已增加重复成功 tool action 跳过逻辑，避免同一 run 内重复执行相同副作用命令。
- 已建立 pytest 覆盖，当前命令：`PYTHONPATH=src .venv/bin/python -m pytest -q`。

仍待补齐：

- `patch` 目前支持 unified diff，结构化 replacement 尚未实现。
- `patch` 返回 changed files，diff 摘要仍可继续增强。
- `run_tests` 已记录 exit_code、stdout、stderr、耗时，仍需补 pytest 成功/失败 fixture 的更细测试。
- git 只读工具已有非 git、有修改仓库测试，仍需补干净仓库测试。

小 TODO：

- 重构工具定义。
  - 为 `ToolDefinition` 增加 `input_schema`。
  - 为 `ToolDefinition` 增加 `risk_level`。
  - 为 `ToolResult` 增加 `error_code`、`metadata`。
  - 让 registry 在执行前校验必填参数。
  - 测试：缺参数、未知参数、未知工具。
- 增加 workspace 路径工具。
  - 增加路径解析 helper。
  - 支持相对路径基于 request cwd。
  - 拒绝默认写出 workspace。
  - 对路径不存在、目录、文件分别返回稳定错误码。
  - 测试：相对路径、绝对路径、`..`、不存在路径。
- 拆分只读文件工具。
  - `list_dir`：列目录，限制最大条目数。
  - `read_file`：读文本文件，限制最大字节数。
  - `file_info`：返回大小、mtime、类型。
  - 对二进制文件返回拒绝或摘要。
  - 测试：空目录、大文件、二进制文件。
- 增加搜索工具。
  - `search_text` 封装 `rg`。
  - `find_files` 封装文件 glob 或 `rg --files`。
  - 限制最大结果数。
  - 返回文件、行号、匹配片段。
  - 测试：有匹配、无匹配、结果截断。
- 增加 shell 工具。
  - 输入包含 command、cwd、timeout。
  - 默认风险等级为 execute。
  - 捕获 stdout、stderr、exit_code。
  - 增加超时杀进程。
  - 测试：成功命令、失败命令、超时命令。
- 增加 patch 工具。
  - 接受 unified diff 或结构化 replacement。
  - 应用前验证目标文件存在状态。
  - 应用前验证上下文匹配。
  - 应用后返回 changed files 和 diff 摘要。
  - 测试：成功 patch、上下文不匹配、创建文件、修改多文件。
- 废弃 `apply_change`。
  - 标记为 deprecated。
  - 默认不暴露给模型。
  - README 迁移到 patch 工具。
  - 测试确认默认工具列表不包含 `apply_change`。
- 增加测试运行工具。
  - 输入 test command。
  - 执行走 shell 底层但单独风险等级。
  - 记录 exit_code、stdout、stderr、耗时。
  - 对输出做最大长度截断。
  - 测试：pytest 成功和失败输出。
- 增加 git 只读工具。
  - `git_status` 返回 porcelain 和当前分支。
  - `git_diff` 返回 working tree diff。
  - `git_show` 查看 commit 或文件版本。
  - 只读 git 工具不需要审批。
  - 测试：非 git 仓库、干净仓库、有修改仓库。

## 大 TODO 3：完善安全与审批

目标：让 runtime 独立于模型判断，能够阻止危险操作并要求用户确认。

小 TODO：

- 定义运行模式。
  - `readonly`：只允许 read 工具。
  - `confirm`：write、execute 需要确认。
  - `auto`：允许低风险 write，execute 仍可配置。
  - CLI 增加 `--mode` 参数。
  - 测试：三种模式下同一 action 的 decision。
- 定义风险等级。
  - read：文件读取、搜索、git status。
  - write：patch、scratchpad。
  - execute：shell、test。
  - network：外部请求。
  - destructive：删除、reset、强制覆盖。
  - 测试：所有内置工具都有风险等级。
- 接入策略判断。
  - registry 执行前统一调用 guardrails。
  - guardrails 收到 action、tool definition、cwd。
  - policy 返回 allowed、requires_confirmation、reason。
  - blocked result 写回 session。
  - 测试：被拒绝动作不会执行 handler。
- 增加确认流。
  - presenter 展示 action、参数、风险原因。
  - 用户可输入 yes/no。
  - 支持本次会话记住同类 action。
  - 非交互模式默认拒绝需要确认的操作。
  - 测试：确认、拒绝、非交互拒绝。
- 危险命令识别。
  - 识别 `rm -rf`。
  - 识别 `git reset --hard`。
  - 识别 `git clean -fd`。
  - 识别 shell 重定向覆盖敏感路径。
  - 测试：危险命令被标记 destructive。
- 路径边界保护。
  - 统一 resolve 路径。
  - 检查 resolve 后是否在 workspace 内。
  - 对软链接跳出 workspace 的路径拒绝写入。
  - 对系统目录写入直接拒绝。
  - 测试：`..`、symlink、绝对路径。
- 敏感信息保护。
  - 定义 sensitive file patterns：`.env`、`id_rsa`、`.pem`。
  - 默认不把敏感文件内容返回给模型。
  - 用户显式要求时仍需要确认。
  - 日志中对疑似 secret 做脱敏。
  - 测试：敏感文件读取被拦截或脱敏。

## 大 TODO 4：提升代码修改能力

目标：让 `testcode` 能稳定完成小型真实代码任务，而不是只写入完整文件。

小 TODO：

- 建立 edit workflow。
  - 要求模型修改前必须读取目标文件。
  - session 记录已读取文件和版本信息。
  - patch 工具检查目标文件是否已被读取。
  - 未读取直接返回可恢复错误。
  - 测试：未读文件 patch 被拒绝。
- 增加 diff 展示。
  - patch 应用前生成 preview diff。
  - presenter 展示 changed files。
  - 对大 diff 做截断并提示日志路径。
  - run summary 包含修改文件列表。
  - 测试：diff summary 稳定。
- 防止覆盖并发修改。
  - 读取文件时记录 mtime 和 hash。
  - patch 前重新计算 hash。
  - hash 不一致时拒绝并提示重新读取。
  - 对 git dirty 状态给模型提示。
  - 测试：读后外部修改会拒绝 patch。
- 改进 patch 失败诊断。
  - 返回失败文件、失败 hunk、附近上下文。
  - 区分语法错误和上下文不匹配。
  - 给模型下一步建议：重新读取或缩小 patch。
  - 测试：错误 patch 有稳定 error_code。
- 控制变更规模。
  - 配置最大单次文件数。
  - 配置最大 diff 行数。
  - 超限时要求模型拆分。
  - 对生成文件和大文件单独限制。
  - 测试：超大 patch 被拒绝。
- 增加计划机制。
  - 对复杂任务先输出 plan。
  - plan 包含目标文件、验证命令、风险。
  - 用户可配置是否必须确认 plan。
  - 日志记录 plan。
  - 测试：需要 plan 的任务会先停在确认点。
- 建立验证闭环。
  - 修改后建议或自动运行相关测试。
  - 测试结果追加到 session。
  - 失败结果反馈给模型继续修复。
  - 限制 fix loop 最大轮数。
  - 测试：fake model 可完成失败后修复流程。
- 增加 review 模式。
  - CLI 支持 `--review` 或 `/review`。
  - 收集 diff。
  - 要求模型按 finding 输出文件和行号。
  - 不允许 review 模式自动修改文件。
  - 测试：review 模式只读。

## 大 TODO 5：增强上下文收集

目标：让 runtime 主动提供高价值上下文，减少模型盲目探索。

小 TODO：

- 增加项目探测器。
  - 检测 `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`。
  - 返回语言、框架线索、包管理器。
  - 不读取大文件内容。
  - 测试：Python、Node、Rust、Go fixture。
- 识别测试命令。
  - Python：pytest、unittest、tox。
  - Node：npm test、pnpm test、yarn test。
  - Rust：cargo test。
  - Go：go test ./...。
  - 测试：根据文件存在情况选择命令。
- 读取项目规则。
  - 从 cwd 向上查找 `AGENTS.md`。
  - 支持多层规则，近目录优先。
  - 读取 README 的前 N 字符。
  - 读取 docs/architecture 等候选文档摘要。
  - 测试：多层规则冲突时选择最近规则。
- 收集 git 上下文。
  - 当前分支。
  - git status。
  - working tree diff 摘要。
  - 最近 commit 信息。
  - 测试：非 git 仓库优雅降级。
- 生成 workspace summary。
  - 目录树限制深度和数量。
  - 标记关键文件。
  - 标记忽略目录：`.git`、`node_modules`、venv。
  - 缓存 summary，文件变化后失效。
  - 测试：大目录不会超预算。
- 支持显式上下文。
  - CLI 增加 `--context path`。
  - 支持多个 context。
  - 记录 context 来源。
  - 路径必须通过安全检查。
  - 测试：指定文件被加入模型输入。

## 大 TODO 6：改进交互体验

目标：让 CLI 在长任务、工具调用、失败恢复时保持清晰可控。

小 TODO：

- 改造 presenter。
  - 增加 `show_model_delta`。
  - 增加 `show_tool_start`。
  - 增加 `show_tool_finish`。
  - 增加 `show_confirmation_request`。
  - 测试：输出不包含异常 traceback。
- 增加工具进度显示。
  - 显示工具名和简短参数。
  - 成功显示耗时。
  - 失败显示 error_code。
  - 被拒绝显示 policy reason。
  - 测试：四种状态输出。
- 增加彩色输出。
  - 检测是否 TTY。
  - TTY 下启用颜色。
  - 非 TTY 自动关闭颜色。
  - 增加 `--no-color`。
  - 测试：非 TTY 输出无 ANSI。
- 增加交互命令。
  - `/help` 列命令。
  - `/status` 显示 session 和 mode。
  - `/exit` 退出。
  - `/sessions` 列会话。
  - `/mode` 切换模式。
  - 测试：每个 slash command 不进入模型调用。
- 改善输入能力。
  - 支持多行输入结束符。
  - 接入 readline 历史。
  - 历史文件写到 `.testcode/history` 或用户目录。
  - 保留简单 input fallback。
  - 测试：无 readline 环境可运行。
- 增加取消处理。
  - Ctrl+C 中断当前模型或工具。
  - 保存 session 当前状态。
  - 提示用户可 resume。
  - 对正在执行的子进程发 terminate。
  - 测试：模拟 KeyboardInterrupt。

## 大 TODO 7：升级会话和记忆系统

目标：让会话不只是聊天记录，而是可恢复的工程任务状态。

小 TODO：

- 扩展 session schema。
  - 增加 `version`。
  - 增加 `summary`。
  - 增加 `tags`。
  - 增加 `workspace_fingerprint`。
  - 增加迁移函数处理旧格式。
  - 测试：旧 session 能加载。
- 增加 run 关联。
  - session 记录 run ids。
  - logger 接收 session id。
  - `testcode --list` 显示最近 run。
  - run 目录中写入 session id。
  - 测试：一次 chat 生成 session-run 关联。
- 增加会话管理命令。
  - `--list` 增加过滤 cwd。
  - 增加搜索关键字。
  - 增加 rename。
  - 增加 tag。
  - 增加 fork。
  - 测试：rename/tag/fork 不破坏消息。
- 增加 checkpoint。
  - 每次工具调用后保存 checkpoint。
  - checkpoint 记录 pending action。
  - 异常退出后可恢复到最近 checkpoint。
  - resume 时提示上次中断状态。
  - 测试：模拟工具异常后 checkpoint 存在。
- 增加记忆层。
  - 项目记忆写 `.testcode/memory.md`。
  - 用户记忆写全局目录。
  - 读取记忆前做长度限制。
  - 支持禁用记忆。
  - 测试：项目记忆优先于用户记忆。

## 大 TODO 8：产品化配置与分发

目标：让项目能被稳定安装、配置、升级和跨项目使用。

小 TODO：

- 增加配置加载。
  - 全局配置：`~/.testcode/config.toml`。
  - 项目配置：`.testcode/config.toml`。
  - env 覆盖配置文件。
  - CLI 参数覆盖 env。
  - 测试：四层优先级正确。
- 定义配置 schema。
  - provider base_url。
  - model name。
  - timeout。
  - mode。
  - max turns。
  - 测试：非法配置有清晰错误。
- 增加 config 命令。
  - `testcode config get key`。
  - `testcode config set key value`。
  - `testcode config list`。
  - `testcode config path`。
  - 测试：读写全局配置。
- 增加初始化流程。
  - 首次运行检测缺失配置。
  - 提示设置 provider。
  - 可跳过，继续 stub mode。
  - 生成 `.env.example` 或 config 示例。
  - 测试：无配置时不崩溃。
- 完善分发。
  - 增加 `--version`。
  - 补 zsh completion。
  - 补 fish completion。
  - README 增加 pip install editable。
  - 增加 release checklist。

## 大 TODO 9：增强可观测性

目标：让每次运行都能被复盘、调试和审计。

小 TODO：

- 增加 run id。
  - logger 创建 run id。
  - presenter 启动时显示 run id。
  - session 记录 run id。
  - logs 命令按 run id 查询。
  - 测试：run id 在事件和 summary 中一致。
- 记录耗时。
  - 工具执行前记录 start。
  - 工具执行后记录 finish。
  - 模型请求记录耗时。
  - session 总耗时。
  - 测试：耗时字段存在且为数字。
- 增加日志命令。
  - `testcode logs --last`。
  - `testcode logs --run <id>`。
  - `testcode logs --session <id>`。
  - 支持输出 events 或 details。
  - 测试：能读取最近日志。
- 敏感信息脱敏。
  - 对 env key pattern 脱敏。
  - 对 token-like value 脱敏。
  - 对敏感文件内容脱敏。
  - 保留足够调试上下文。
  - 测试：日志中不出现假 secret。
- 失败诊断摘要。
  - 捕获最后一个 model error。
  - 捕获最后一个 tool error。
  - 捕获 policy block。
  - 在 details.log 顶部写 diagnosis。
  - 测试：失败 run 包含 diagnosis。
- 日志写入容错。
  - 写 events 失败不影响主流程。
  - 记录 fallback warning。
  - details.log 写入失败时输出提示。
  - 测试：只读日志目录下 run 仍返回结果。

## 大 TODO 10：建立质量门禁

目标：让后续工程推进有稳定回归保护。

小 TODO：

- 搭建测试分层。
  - `tests/unit` 放纯函数和类测试。
  - `tests/integration` 放工具和 CLI 测试。
  - `tests/e2e` 放 fake model 全流程。
  - 增加 fixture workspace。
  - 文档说明如何运行各层测试。
- 增加模型测试。
  - JSON fallback 解析。
  - tool_calls 解析。
  - invalid response 错误。
  - retry 行为。
  - stream 拼接。
- 增加工具测试。
  - 文件读取。
  - 搜索。
  - patch。
  - shell。
  - git。
  - 测试输出截断。
- 增加安全测试。
  - mode 策略。
  - 危险命令。
  - workspace 边界。
  - 敏感文件。
  - 确认流。
- 增加 CLI 测试。
  - 参数组合。
  - slash commands。
  - session resume。
  - non-interactive 输出。
  - KeyboardInterrupt。
- 增加质量工具。
  - 配置 ruff。
  - 配置 mypy 或 pyright。
  - 配置 pytest coverage。
  - 增加 `make test` 或脚本。
  - CI 运行 lint、typecheck、test。
- 增加验收清单。
  - 每个大 TODO 写完成定义。
  - 每个阶段写 demo 命令。
  - 每个阶段写 rollback 风险。
  - 每个阶段更新 README。

## 推荐推进顺序

1. 大 TODO 2：建设可靠工具系统。
2. 大 TODO 3：完善安全与审批。
3. 大 TODO 4：提升代码修改能力。
4. 大 TODO 5：增强上下文收集。
5. 大 TODO 1：强化模型协议。
6. 大 TODO 6：改进交互体验。
7. 大 TODO 7：升级会话和记忆系统。
8. 大 TODO 9：增强可观测性。
9. 大 TODO 8：产品化配置与分发。
10. 大 TODO 10：建立质量门禁，持续贯穿所有阶段。

## 第一阶段验收标准

完成第一阶段时，`testcode` 应至少具备：

- 能安全读取、搜索、修改当前 workspace 内文件。
- 所有写操作都通过 patch，并展示 diff。
- shell 执行默认需要审批。
- 能运行测试命令并把失败信息反馈给模型。
- 能识别并拒绝明显危险操作。
- 有覆盖工具、策略、patch、CLI 关键路径的测试。
- README 能说明如何运行、配置模型、执行一次真实代码修改任务。
