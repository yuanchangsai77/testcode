# testcode

`testcode` is an LLM-driven CLI workbench. The CLI itself does not own decision-making intelligence. It provides a controlled runtime that collects context, delegates reasoning to a large model, executes approved tools, and returns observable results to the user.

## Architecture

The project is organized around six layers:

1. Interaction layer: CLI entrypoints, user input, progress output, and result rendering.
2. Session orchestration layer: task lifecycle, context assembly, and model/tool loop coordination.
3. Model integration layer: prompt packaging, model invocation, and structured response parsing.
4. Tool execution layer: shell, file, git, search, and other callable capabilities.
5. Safety layer: policy checks, confirmations, and execution boundaries.
6. Observability layer: logs, events, trace records, and execution summaries.

Detailed design is in [docs/architecture.md](docs/architecture.md).

## Documentation Map

| 目的 | 文档 |
| --- | --- |
| 安装、运行、模型接入与常用命令 | 本文档 |
| 所有运行配置、默认值与硬上限 | [配置参考](docs/configuration.md) |
| runtime 分层、对象职责与数据流 | [总体架构](docs/architecture.md) |
| 当前阶段、未完成事项与验收标准 | [演进路线图](docs/build-roadmap.md) |
| ContextLoader、ToolProvider、ResourceProvider | [运行时扩展](docs/runtime-extensibility.md) |
| 工具箱、渐进披露、激活与回收 | [能力仓库](docs/capability-warehouse.md) |
| MCP transport、discovery、风险与生命周期 | [MCP 集成](docs/mcp-integration.md) |
| Skill 的目录、加载和注入 | [Skill 系统](docs/skill-system.md) |
| Tool 定义、结果、metadata 和摘要的契约 | [工具契约](docs/tool-contract.md) |
| TUI 交互与实现决策 | [TUI 设计](docs/tui_design_and_architecture.md) |
| 版本快照与已完成修复记录 | [版本快照](docs/versions/v0.1.md)、[MCP 修复记录](docs/mcp-pr-review-remediation.md) |

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

```bash
python -m testcode "summarize this repository"
```

The current implementation wires the architecture together and supports a structured local tool loop for file inspection, search, shell execution, patching, test commands, and read-only git inspection.

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

## Connect To The Local LLM Proxy

If you want `testcode` to use the OpenAI-compatible proxy running in `/opt/repos/test`, start that project first and then configure `.env`:

```env
TESTCODE_MODEL_BASE_URL=http://127.0.0.1:3000
TESTCODE_MODEL_NAME=gpt-5.4
TESTCODE_MODEL_TIMEOUT=60
TESTCODE_MODE=confirm
```

Behavior:

- `testcode` automatically loads `.env` from the repository root
- If `TESTCODE_MODEL_BASE_URL` is still not set, `testcode` keeps using `StubModelClient`
- If `TESTCODE_MODEL_BASE_URL` is set, `testcode` sends requests to `POST /v1/chat/completions`
- `TESTCODE_MODEL_TIMEOUT` controls the model request timeout in seconds and defaults to 60
- `TESTCODE_MODE` controls tool safety and defaults to `confirm`
- The proxy in `/opt/repos/test` remains responsible for the real upstream base URL and API key
- The real-model path now supports tool-call loops: the model can request built-in tools, receive tool results, and continue until it produces a final answer
- Each run automatically writes observability logs under `.testcode/runs/<timestamp>/`, including `events.jsonl` and a layered `details.log`

## Configuration

模型连接信息继续放在 `.env`；运行策略和 MCP 服务放在 `~/.testcode/config.toml` 或项目的
`.testcode/config.toml`。项目配置覆盖全局同名项。完整示例、参数中文说明和内部硬上限见
[配置参考](docs/configuration.md)。

Run:

```bash
PYTHONPATH=src python3 -m testcode "summarize this repository"
```

If you pass an initial prompt without `--once`, `testcode` answers that prompt and then stays in interactive conversation mode. Type `exit` or `quit` to leave.

Interactive conversations are saved under `.testcode/sessions/`. Use `--list` to inspect saved session ids, `--resume <session_id>` to continue a specific conversation, or `--last` to reopen the most recently updated one.

If you prefer interactive selection, run `PYTHONPATH=src python3 -m testcode --resume` without an id and pick a numbered session from the list.

For bash completion of `testcode` flags, source [`contrib/testcode-completion.bash`](/opt/repos/testcode/contrib/testcode-completion.bash):

```bash
source /opt/repos/testcode/contrib/testcode-completion.bash
```

## Core Tools

The built-in tool set exposes structured schemas, risk levels, stable error
codes, and workspace-bounded path handling:

- `list_dir`, `read_file`, `file_info`: read-only workspace file inspection
- `find_files`, `search_text`: bounded file and text search
- `shell_exec`: execute a command in the workspace
- `patch`: apply a validated unified diff in the workspace
- `run_tests`: execute a test command with captured output and duration
- `git_status`, `git_diff`, `git_show`: read-only git inspection

`apply_change` is deprecated and is not exposed to the model by default.

### Shell 会话与中断

每个交互会话最多维护一个串行的 Bash 会话，以保留工作目录和环境变量。
在 POSIX/Linux 环境中，该 Bash 在独立进程组中运行：正常退出、输入阶段的 Ctrl+C、
执行阶段的 Ctrl+C 以及命令超时时，testcode 会终止整个进程组，而不是只终止 Bash
主进程。因此由该会话启动的普通后台子进程也会一并停止。超时后会立即重置为干净的
Bash，后续命令继续在该新 Bash 中执行。
完整的终止时机、并发边界和安全边界见[Shell 会话生命周期](docs/shell-session-lifecycle.md)。

这是一种进程生命周期管理机制，不是操作系统级沙盒：命令仍以启动 testcode 的用户
权限执行。对不可信代码或需要限制文件、网络和资源访问的任务，应在容器或系统级
沙盒中运行 testcode。

Concrete tool implementations live under `src/testcode/tools/builtins/`.
Each built-in tool is described in its own module and exported through a
`tool()` factory. Shared helpers for schema creation, workspace path resolution,
process execution, and output clipping live in `src/testcode/tools/shared.py`.
`src/testcode/tools/builtin.py` only assembles the default registry.

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

Create a project-local virtual environment and install test dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest
```

Run the test suite:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```
