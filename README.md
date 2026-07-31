# testcode

`testcode` is an LLM-driven CLI workbench. The CLI itself does not own decision-making intelligence. It provides a controlled runtime that collects context, delegates reasoning to a large model, executes approved tools, and returns observable results to the user.

## Architecture

The project is organized around six layers:

1. Interaction layer: CLI entrypoints, user input, progress output, and result rendering.
2. Session orchestration layer: task lifecycle, context assembly, and model/tool loop coordination.
3. Model integration layer: prompt construction, model invocation, and structured response parsing.
4. Tool execution layer: shell, file, git, search, and other callable capabilities.
5. Safety layer: policy checks, confirmations, and execution boundaries.
6. Observability layer: logs, events, trace records, and execution summaries.

Detailed design is in [docs/architecture.md](docs/architecture.md).

## Documentation Map

每类事实只指定一份主文档，其他文档只链接引用，避免分别维护同一份状态或契约。
已完成计划不作为长期文档保留：稳定行为进入功能说明，未完成事项进入路线图，实施过程
由 Git 历史追溯。

| 类型 | 主文档 | 唯一职责 | 不负责 |
| --- | --- | --- | --- |
| 使用入口 | 本文档 | 安装、运行、模型接入与常用命令 | 内部对象设计和实施计划 |
| 当前架构 | [总体架构](docs/architecture.md) | runtime 分层、对象职责和当前数据流 | 专项行为和优先级 |
| 当前功能 | [Agent 执行循环](docs/core/agent-loop.md) | 模型—工具循环、重复动作纠正和停止条件 | TUI 渲染和长任务 roadmap |
| 当前功能 | [执行安全](docs/core/execution-safety.md) | 风险模式、审批、危险动作和写入内容检查 | Shell 隔离和工具字段定义 |
| 当前功能 | [项目感知](docs/core/project-awareness.md) | 项目规则、workspace 摘要、项目探测和测试命令解析 | 通用扩展接口 |
| 配置参考 | [配置参考](docs/reference/configuration.md) | 配置来源、覆盖顺序、默认值和硬上限 | MCP 协议语义 |
| 字段参考 | [工具契约](docs/reference/tool-contract.md) | Tool 定义、结果、metadata 和摘要的字段流向 | 工具实现教程 |
| 扩展设计 | [运行时扩展](docs/extensions/runtime-interfaces.md) | ContextLoader、ToolProvider、ResourceProvider 通用边界 | MCP、Skill 的内部设计 |
| 扩展设计 | [能力仓库](docs/extensions/capability-warehouse.md) | 工具箱、渐进披露、激活和回收策略 | transport 或 Skill 文件格式 |
| 专项设计 | [MCP 集成](docs/extensions/mcp-integration.md) | MCP transport、discovery、协议、安全和生命周期 | 全局 roadmap |
| 专项设计 | [Skill 系统](docs/extensions/skill-system.md) | Skill 格式、来源、箱内资产及激活语义 | 通用仓库策略 |
| 当前交互 | [TUI 当前行为](docs/interaction/tui-current.md) | 终端交互、输入编辑、重绘和兼容性边界 | runtime orchestration |
| 交互路线图 | [TUI 路线图](docs/interaction/tui-roadmap.md) | TUI 未完成阶段和验收标准 | 当前交互细节和产品优先级 |
| 当前功能 | [Shell 生命周期](docs/core/shell-session-lifecycle.md) | 串行 Bash 的保留、中断、清理和安全边界 | OS 级隔离 |
| 总路线图 | [演进路线图](docs/roadmap.md) | 当前基线、未完成事项、优先级和验收标准 | 已完成施工历史和专项契约 |
| 版本快照 | [v0.2](docs/versions/v0.2.md)、[v0.1](docs/versions/v0.1.md) | 发布时点的不可变能力记录 | 当前状态 |

建议阅读顺序：本文档 → 配置参考 → 总体架构 → 路线图；需要开发某个子系统时，再进入对应专题文档。

## Project Layout

```text
src/testcode/
  config.py         Runtime configuration and .env loading
  interaction/     CLI input/output
  orchestration/   session state and agent loop
  model/           LLM prompt, parser, adapter, and protocol
  sessions/        Stored conversation persistence
  tools/           executable tool registry
  safety/          guardrails and policy checks
  observability/   logging and event capture
```

## Quick Start

Requirements: Python 3.11 or newer.

Create a project-local virtual environment and install `testcode` in editable
mode:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Run a single request:

```bash
.venv/bin/testcode --once "summarize this repository"
```

When developing without an editable install, use the source-tree form:

```bash
PYTHONPATH=src python3 -m testcode --once "summarize this repository"
```

The current implementation supports a structured local tool loop for file
inspection, search, shell execution, patching, test commands, read-only Git
inspection, Skill activation, and on-demand MCP tool activation.

Long conversation mode:

```bash
PYTHONPATH=src python3 -m testcode
```

List saved conversations:

```bash
PYTHONPATH=src python3 -m testcode --list
```

Choose a saved conversation interactively:

```bash
PYTHONPATH=src python3 -m testcode --resume
```

Resume the most recent conversation:

```bash
PYTHONPATH=src python3 -m testcode --last
```

Resume a specific conversation by id:

```bash
PYTHONPATH=src python3 -m testcode --resume 20260331041240862413-588cbf9b
```

Single turn only:

```bash
PYTHONPATH=src python3 -m testcode --once "summarize this repository"
```

Add explicit context files, directories, or globs:

```bash
PYTHONPATH=src python3 -m testcode --context README.md --context "docs/*.md" "summarize these docs"
```

At the start of each run, `testcode` also injects bounded context from
project `AGENTS.md` rules, common project markers, git status, and a compact
workspace tree.

Choose a safety mode:

```bash
PYTHONPATH=src python3 -m testcode --mode readonly "summarize this repository"
PYTHONPATH=src python3 -m testcode --mode confirm "edit a file after approval"
PYTHONPATH=src python3 -m testcode --mode auto "apply low-risk file edits automatically"
```

## Connect To An OpenAI-Compatible Endpoint

Start your OpenAI-compatible endpoint and configure the `.env` file in the
`testcode` source tree with its base URL:

```env
TESTCODE_MODEL_BASE_URL=http://127.0.0.1:3000
TESTCODE_MODEL_NAME=gpt-5.4
TESTCODE_MODEL_TIMEOUT=60
TESTCODE_MODE=confirm
```

Behavior:

- `testcode` automatically loads `.env` beside the source checkout; an arbitrary
  target workspace's `.env` is not loaded automatically
- If `TESTCODE_MODEL_BASE_URL` is still not set, `testcode` keeps using `StubModelClient`
- If `TESTCODE_MODEL_BASE_URL` is set, `testcode` sends requests to `POST /v1/chat/completions`
- `TESTCODE_MODEL_TIMEOUT` controls the model request timeout in seconds and defaults to 60
- `TESTCODE_MODE` controls tool safety and defaults to `confirm`
- The configured endpoint remains responsible for its upstream credentials and provider-specific authentication
- The real-model path now supports tool-call loops: the model can request built-in tools, receive tool results, and continue until it produces a final answer
- Each run automatically writes observability logs under `.testcode/runs/<timestamp>/`, including `events.jsonl` and a layered `details.log`

## Configuration

模型连接信息继续放在 `.env`；运行策略和 MCP 服务放在 `~/.testcode/config.toml` 或项目的
`.testcode/config.toml`。项目配置覆盖全局同名项。完整示例、参数中文说明和内部硬上限见
[配置参考](docs/reference/configuration.md)。

Run:

```bash
.venv/bin/testcode "summarize this repository"
```

If you pass an initial prompt without `--once`, `testcode` answers that prompt and then stays in interactive conversation mode. Type `exit` or `quit` to leave.

Interactive conversations are saved under the package/source checkout's
`.testcode/sessions/` directory; this is not the active target workspace when
the two differ. Use `--list` to inspect saved session ids,
`--resume <session_id>` to continue a specific conversation, or `--last` to
reopen the most recently updated one.

If you prefer interactive selection, run `PYTHONPATH=src python3 -m testcode --resume` without an id and pick a numbered session from the list.

For bash completion of `testcode` flags, source [`contrib/testcode-completion.bash`](contrib/testcode-completion.bash) from the repository root:

```bash
source contrib/testcode-completion.bash
```

## Core Tools

The built-in tool set exposes structured schemas, risk levels, stable error
codes, and workspace-bounded path handling:

- `list_dir`, `read_file`, `file_info`: read-only workspace file inspection
- `find_files`, `search_text`: bounded file and text search
- `shell_exec`: execute a command in the workspace
- `patch`: apply a validated unified diff in the workspace
- `apply_change`: deprecated compatibility editing tool; new workflows should prefer `patch`
- `run_tests`: execute a test command with captured output and duration
- `git_status`, `git_diff`, `git_show`: read-only git inspection
- `warehouse_list`, `toolbox_open`, `capability_activate`,
  `capability_release`, `capability_status`: inspect and manage on-demand
  Skill/MCP capabilities

Application composition currently registers `apply_change` for compatibility. It is deprecated and should not be used for new workflows.

### Shell 会话与中断

每个交互会话最多维护一个串行的 Bash 会话，以保留工作目录和环境变量。
在 POSIX/Linux 环境中，该 Bash 在独立进程组中运行：正常退出、输入阶段的 Ctrl+C、
执行阶段的 Ctrl+C 以及命令超时时，testcode 会终止整个进程组，而不是只终止 Bash
主进程。因此由该会话启动的普通后台子进程也会一并停止。超时后会立即重置为干净的
Bash，后续命令继续在该新 Bash 中执行。
完整的终止时机、并发边界和安全边界见
[Shell 会话生命周期](docs/core/shell-session-lifecycle.md)。

这是一种进程生命周期管理机制，不是操作系统级沙盒：命令仍以启动 testcode 的用户
权限执行。对不可信代码或需要限制文件、网络和资源访问的任务，应在容器或系统级
沙盒中运行 testcode。

Concrete tool implementations live under `src/testcode/tools/builtins/`.
Each built-in tool is described in its own module and exported through a
`tool()` factory. Shared helpers for schema creation, workspace path resolution,
process execution, and output clipping live in `src/testcode/tools/shared.py`.
`src/testcode/tools/builtin_provider.py` supplies built-ins to application composition. `src/testcode/tools/builtin.py` remains a legacy registry assembly helper.

Risky tools such as `shell_exec` and `patch` require interactive approval in
the default `confirm` mode before execution. In `readonly` mode only read tools
can run. In `auto` mode read and write tools can run without confirmation while
execute, test, network, and destructive actions still require approval. Dangerous
shell commands such as `rm -rf`, `git reset --hard`, and `git clean -fd` are
classified as destructive. If the same successful tool call is requested again
within one run, the orchestration layer skips the duplicate instead of asking for
approval and executing it again.

The model ends a run by returning `done: true`. There is no separate `finish` tool.

## Development

Create a project-local virtual environment and install the package plus test
dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
```

Run the test suite:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```
