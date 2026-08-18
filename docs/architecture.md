# testcode 核心架构

## 文档职责

本文档只回答三个问题：

- `testcode` runtime 由哪些层组成
- 每一层的职责边界是什么
- 关键对象和数据流如何协作

本文档不负责展开专项实现细节。相关内容请分别查看：

- 演进顺序与阶段目标：`docs/roadmap.md`
- 跨项目目标状态与多设备方向：`docs/future/README.md`
- Agent 循环与停止条件：`docs/core/agent-loop.md`
- 执行授权与内容安全：`docs/core/execution-safety.md`
- 项目规则、探测与测试解析：`docs/core/project-awareness.md`
- 扩展点抽象：`docs/extensions/runtime-interfaces.md`
- MCP 接入专项：`docs/extensions/mcp-integration.md`
- Skill 专项：`docs/extensions/skill-system.md`
- tool 字段契约：`docs/reference/tool-contract.md`

## 1. 产品定位

`testcode` is a large-model-driven CLI workbench. It is not an autonomous decision engine. Its responsibility is to:

- receive user intent from the command line
- collect execution context from the local environment
- assemble task-relevant context for an LLM, with budgeted packaging as the target boundary
- convert model output into executable actions
- execute actions through controlled tools
- expose progress, logs, and results back to the user

The system's intelligence comes from the model. The CLI is the runtime shell that makes that intelligence operable, safe, and observable.

## 2. 设计原则

- Thin control plane: keep the CLI framework focused on coordination, not reasoning.
- Clear boundary of responsibility: model decides, runtime executes.
- Structured execution: every model instruction is normalized into typed actions.
- Safety first: tool execution must pass policy checks before running.
- Full observability: every turn, tool call, and result is recorded as events.
- Replaceable components: model providers, tools, and policies are pluggable.
- Bounded context: persist complete facts for audit and recovery, but send only the smallest useful working set to the model.

## 3. 分层架构

Current source layout:

```text
src/testcode/
  app.py             application composition and CLI argument dispatch
  config.py          .env loading and runtime configuration
  context/           project rules, workspace summaries, and explicit context loaders
  interaction/       CLI input/output and presentation
  orchestration/     session context, progress protocol, and model/tool execution loop
  model/             provider client, prompt builder, reply parser, model types
  sessions/          persisted conversation storage
  tools/             tool protocol, registry, shared helpers, built-in tools
  safety/            policy evaluation and guardrail logging
  observability/     events and run logs
  types.py           cross-layer request, reply, tool, summary, and session records
```

`src/testcode/session_store.py` has been removed. Session persistence now lives only under `src/testcode/sessions/`.

### 3.0 Application Composition

Responsibilities:

- load the source checkout's `.env` before runtime objects are assembled
- create the logger, policy, guardrails, tool registry, model client, presenter, engine, and session store
- parse CLI flags and choose single-turn, chat, list, resume, or latest-session mode
- collect explicit context arguments such as `--context`
- keep object wiring separate from lower-level implementation details

Core files:

- `src/testcode/app.py`
- `src/testcode/__main__.py`
- `src/testcode/config.py`

Composition structure:

- `config.py` owns `.env` loading and `RuntimeConfig`.
- `app.py` owns dependency wiring and command-line dispatch.
- `__main__.py` remains the package execution shim.

### 3.1 Interaction Layer

Responsibilities:

- accept user requests from CLI arguments or interactive input
- present task status and incremental updates
- render final answers and execution summaries
- handle cancellations and user confirmations

Core files & folders:

- `src/testcode/interaction/cli.py`
- `src/testcode/interaction/presenter.py`
- `src/testcode/interaction/commands/` (decoupled slash command subsystem, including `base.py`, `session_cmds.py`, `sys_cmds.py`, and package factory)


### 3.2 Session Orchestration Layer

Responsibilities:

- create the per-run `SessionContext`
- assemble bounded history, environment metadata, and tool availability
- run the think-act-observe loop between model and tools
- stop when the model reaches a terminal answer or a policy blocks progress
- avoid repeating successful duplicate tool calls within the same run

Core files:

- `src/testcode/orchestration/session.py`
- `src/testcode/orchestration/engine.py`
- `src/testcode/orchestration/control.py`

Orchestration structure:

- `session.py` owns the in-memory context passed to the model during one run.
- `engine.py` owns the model/tool loop, policy checks, approvals, and duplicate action skipping.
- `control.py` owns run budget/circuit-breaker policy and completion evidence evaluation.
- `progress.py` defines optional progress events so terminal rendering stays outside the execution engine.
- Long-lived conversation persistence is not part of this layer; it is handled by `sessions/store.py`.

循环推进、重复动作纠正和停止条件的当前行为见
[Agent 执行循环](core/agent-loop.md)。

### 3.2.1 Context Assembly Layer

Responsibilities:

- load project rules before model invocation and load workspace summaries only for project-relevant requests
- keep context gathering independent from packaging and prompt rendering
- collect user-selected context paths, workspace summaries, active workflow instructions, checkpoints, and archive references as candidate context
- bound context size and keep paths inside the active workspace
- keep complete raw artifacts in logs or archives for later inspection instead of treating prompt context as storage

Core files:

- `src/testcode/context/project_rules.py`
- `src/testcode/context/workspace.py`
- `src/testcode/context/explicit.py`
- `src/testcode/context/packager.py`

Context structure:

- `ProjectRulesLoader` loads `AGENTS.md` from the current path up to the nearest project boundary. Project boundaries are detected from `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`, or `go.mod`.
- `WorkspaceSummaryLoader` first decides whether workspace context is relevant, then detects common project markers, suggested test commands, git branch/status/latest commit, and a bounded workspace tree. Explicit enablement, selected context paths, clear repository terms, code-action plus code-target intent (including mixed Chinese/English prompts), or an explicit code path enable the summary. Ambiguous words such as `code`, `test`, or `project` alone do not.
- `ExplicitContextLoader` expands CLI-provided `--context` files, directories, and globs under the workspace. It refuses out-of-workspace paths and binary files, clips individual reads defensively, and records source metadata for packaging.
- `ContextPackager` sits after loaders and before provider transport. It accepts labeled context segments with explicit priority and required/truncatable policy, preserves protocol, security, project-rule, current-request and checkpoint segments first, selects recent conversation within a character budget, clips oversized sections at line boundaries where possible, and reports package statistics.

The orchestration layer treats these as ordinary `ContextLoader` implementations. It does not know how rules, summaries, or explicit files are discovered.

The target long-task design uses a three-tier memory model:

- Hot context: the current goal, phase, next action, recent failures, relevant file state, and latest verification result.
- Warm summary: compressed history of decisions, completed work, investigated paths, and resolved errors.
- Cold archive: full events, tool outputs, patches, test logs, and read-state records stored on disk and referenced by id, path, hash, or run id.

The current packaging layer consumes checkpoint facts and recent history within a hard character budget. A later semantic summarizer should turn older history into warm summaries, while cold archive content remains loaded only on demand.

Packaging boundaries:

- `SessionStore` persists conversation/session metadata and bounded runtime checkpoint projections. A future archive store should hold full tool history, read-state hashes and stable content-addressed references.
- `ContextLoader` implementations discover candidate context and source metadata.
- `ContextPackager` owns pre-injection pruning, recent-message selection and character budget accounting.
- `ModelPromptBuilder` groups candidate context, then delegates final message bounding to `ContextPackager`.

More aggressive semantic pruning, content-addressed source references and token-aware accounting should be added behind the same interface.

### Task identity and evidence ledger

Recovery is scoped to a runtime task, not merely to the latest incomplete session run. Every checkpoint carries a
stable `task_id`, its workspace root, a monotonic workspace revision, and a bounded typed evidence ledger. A new
user request starts a new task unless the caller explicitly supplies the previous task id (or marks the request as a
continuation); an incomplete outcome alone is never sufficient authority to inherit completion evidence.

Tools publish semantic evidence kinds such as `workspace_change`, `test`, `read`, and `artifact` through the common
`ToolResult` contract. Completion policy consumes those evidence kinds and does not depend on concrete tool names.
Every confirmed workspace mutation advances the checkpoint revision. Observation and verification evidence is valid
only for the revision at which it was produced, so a later write automatically makes an earlier test or read stale.
Successful write/execute/destructive effects conservatively advance the observation revision even when they do not
claim a completed workspace change, and a failed verification revokes test evidence for the current revision.
Workspace-change and explicitly confirmed immutable-artifact evidence are cumulative task facts; only read and test
evidence require an exact current-revision match. A no-change completion requires both a clear explanation and current
read evidence.
Subagent handoff evidence follows the same contract and is rebound to the parent task only when the runner accepts the
child's bounded terminal result.

Context assembly and packaging are described here only as architecture boundaries. The reusable extension hooks themselves belong to `docs/extensions/runtime-interfaces.md`.
项目相关性判断、规则加载、项目探测和测试命令解析的当前行为见
[项目感知](core/project-awareness.md)。

### 3.3 Model Integration Layer

Responsibilities:

- translate session state into model input
- invoke the model provider
- parse structured actions from model output
- support provider replacement through a small `respond(session)` protocol boundary
- correlate every transport attempt with a unique request id and a declared client timeout

Core files:

- `src/testcode/model/protocol.py`
- `src/testcode/model/client.py`
- `src/testcode/model/prompt.py`
- `src/testcode/model/parser.py`
- `src/testcode/model/types.py`

Model structure:

- `client.py` owns OpenAI-compatible provider transport, request/response logging, and provider-level error handling.
- `prompt.py` owns message and native tool schema construction. Complex context budgeting belongs in the context packaging layer, not directly in provider-specific prompt rendering.
- `parser.py` owns native tool call parsing, JSON fallback parsing, and protocol-noise cleanup.
- `types.py` owns model-specific configuration and parser helper types.
- `ModelCapabilityProfile` declares the adapter's structured-output mode, native/parallel tool behavior and context budget. It also records whether the values are configured assumptions or verified capabilities; HTTP compatibility alone does not imply these semantic capabilities.

Important boundaries:

- `ModelClientConfig` lives in `model/types.py`, not in `model/client.py`.
- Prompt construction is accessed through `ModelPromptBuilder`.
- Reply parsing is accessed through `ModelReplyParser`.
- `OpenAICompatibleModelClient` coordinates those collaborators but does not expose parser or prompt compatibility methods.
- The model client depends on a replaceable model-access endpoint, which may be a direct provider, compatible gateway,
  or local inference framework. Transport implementations own protocol conversion, cancellation propagation, and
  diagnostics while treating valid model content as opaque. See
  [模型传输接口契约](reference/model-transport-contract.md).
- JSON and SSE are interchangeable model transport modes. The synchronous orchestration boundary still receives one
  validated `ModelReply`; optional stream observers may consume deltas without driving tools from partial output.

### 3.4 Tool Execution Layer

Responsibilities:

- expose callable tools to the orchestration engine
- execute concrete operations such as shell, file, and search tasks
- return normalized results to the orchestration loop
- validate tool arguments against each tool's input schema before execution
- keep tool paths bounded to the request workspace by default

Core files:

- `src/testcode/tools/base.py`
- `src/testcode/tools/registry.py`
- `src/testcode/tools/builtin_provider.py`
- `src/testcode/tools/builtin_provider.py` (built-in provider and standalone registry factory)
- `src/testcode/tools/shared.py`
- `src/testcode/tools/builtins/`

Tool structure:

- `base.py` defines the common tool protocol, `SimpleTool`, and `ToolContext`.
- `registry.py` owns registration, default tool exposure, schema validation, and normalized result logging.
- `shared.py` contains reusable helpers for JSON-style schemas, workspace path resolution, subprocess execution, output clipping, and result retargeting.
- `builtin_provider.py` supplies built-in tools to the application composition root.
- Built-in composition has one owner in `builtin_provider.py`; tests reuse its standalone registry factory.
- `builtins/<tool>.py` contains one concrete tool per file. Each module exports a `tool()` factory and keeps the tool's `run()` implementation local.

Core built-in tools:

- `list_dir`, `read_file`, `file_info`
- `find_files`, `search_text`
- `git_status`, `git_diff`
- `shell_exec`
- `patch`

High-frequency, low-cost read-only inspection remains in the core workbench. Specialized built-in tools
remain implemented under `builtins/`, while application composition groups them into progressively
disclosed Skill toolboxes: `pytest-helper` supplies `run_tests`; `git-helper` supplies `git_show` and
workflow instructions. Tool providers own implementations, and Skills only reference reusable
capabilities. The standalone registry factory includes both core and specialized tools for direct
execution tests.

This section describes where tool execution lives. Field-level placement rules for `ToolResult.output`, `metadata`, summarizers, and prompt visibility belong to `docs/reference/tool-contract.md`.

### 3.5 Safety Layer

Responsibilities:

- validate whether a requested action is allowed
- require explicit approval for risky operations
- keep runtime boundaries independent from model judgment
- surface approval requests through the interaction layer

Core files:

- `src/testcode/safety/policy.py`
- `src/testcode/safety/guardrails.py`
- `src/testcode/safety/content/`
- `src/testcode/safety/secret_patterns.py`
- `src/testcode/safety/redaction.py`

安全模式、审批、危险命令识别和凭据写入阻断的当前行为见
[执行安全](core/execution-safety.md)。

### 3.6 Observability Layer

Responsibilities:

- record a compact event index for model calls, tool invocations, and errors
- provide traceability for debugging and auditing
- emit execution summaries for the CLI and future telemetry backends

`events.jsonl` is the only ordered run event source. Events contain identities, outcomes, sizes, hashes and artifact
references, not repeated raw model messages or tool payloads. A run archives its initial redacted model request once;
later request and successful response events keep fingerprints and usage only. Small tool arguments and results stay
inline in their compact event; larger bodies are stored once in the run artifact archive using content-addressed
deduplication, and the same action reference is reused by tool history and results. Error bodies remain diagnosable through bounded
error events or artifacts. `details.log` is a human-readable index derived from compact events; it must not replay every
event payload or duplicate terminal tool outputs.

Core files:

- `src/testcode/observability/events.py`
- `src/testcode/observability/logger.py`

### 3.7 Configuration and Persistence

Responsibilities:

- load model connection settings from the source checkout's `.env` and process
  environment variables
- load runtime policy and MCP settings from global/project `config.toml`
- keep application assembly separate from configuration parsing
- persist resumable CLI conversations independently from the interaction loop
- list, load, resume, and close stored conversations
- persist conversation state and a bounded derived resume summary without
  reinjecting the full trace

Core files:

- `src/testcode/config.py`
- `src/testcode/sessions/__init__.py`
- `src/testcode/sessions/store.py`
- `src/testcode/sessions/cluster.py`
- `src/testcode/orchestration/subagents.py`
- `src/testcode/orchestration/subagent_runner.py`

Configuration structure:

- `.env` loading only fills missing environment variables. It resolves relative
  to the installed/source package checkout, not to an arbitrary target workspace.
- `RuntimeConfig` normalizes model connection settings, retry policy, runtime limits, safety mode, and MCP servers.
- `~/.testcode/config.toml` supplies user defaults; `.testcode/config.toml` overrides them for the current project.
- Configurable limits have internal hard caps. Exceeding a cap is a startup error rather than a silent fallback; see `docs/reference/configuration.md`.

Persistence structure:

- `SessionStore` derives its storage root from the package/source checkout and
  writes JSON files under that root's `.testcode/sessions/`; it does not derive
  this path from the active target workspace.
- Stored sessions are bounded recovery snapshots, not transcript archives. They include recent conversation, recent
  lightweight run summaries, the complete lightweight run-id index, active Skill/capability ids and one authoritative derived resume checkpoint.
  Older raw process detail remains addressable through run ids and artifacts rather than being copied into the snapshot.
- Subagent session clusters add explicit parent/child launch provenance. Session images are immutable launch inputs, while the cluster public state stores only bounded member status, findings, verification results, blockers, and artifact references. Members do not gain direct access to each other's conversation or tool history; see [Subagent 会话集群](core/subagent-session-clusters.md).
- `SubagentRunner` atomically claims ready members and executes them concurrently through separately composed runtimes. Model-visible spawn, run-ready, and status tools expose this lifecycle without turning the public state into a direct message bus.
- The latest resume checkpoint includes schema version, task identity, workspace root/revision, objective, phase,
  completed actions, typed evidence, artifact references, required evidence, unmet deliverables, blockers and bounded
  runtime-state projections. Historical trace entries do not copy checkpoints. Full tool history and content hashes
  remain run archive concerns.
- Corrupt session files are skipped when listing sessions.

## 4. Runtime Flow

1. The user submits a task through the CLI.
2. `app.py` loads configuration, wires the runtime objects, and chooses the requested CLI mode.
3. The interaction layer creates a `UserRequest`.
4. The orchestration layer creates a `SessionContext` with available tool definitions and prior conversation metadata.
5. Registered context loaders add candidate project rules, relevant workspace summaries, explicit context, and source metadata to the session. The capability warehouse restores explicitly active Skill guidance and tool capabilities. Non-project external or general-knowledge requests skip the workspace tree, Git status, and test signals unless explicitly enabled.
6. `ModelPromptBuilder` groups the current context and `ContextPackager` produces bounded provider messages according to the active model profile.
7. `OpenAICompatibleModelClient` invokes the provider, or `StubModelClient` is used when no model base URL is configured.
8. `ModelReplyParser` normalizes the provider response into either:
   - a final answer
   - one or more tool actions
9. Each requested tool action is checked by the safety layer.
10. If an action needs approval, the CLI asks the user before execution.
11. Allowed or approved actions are executed by the tool layer.
12. Tool results are logged by the observability layer and added back into session state.
13. Successful duplicate tool actions in the same run are skipped to avoid repeated side effects.
14. The completion gate checks required evidence; the loop continues until delivery is supported, a blocker is reached, or wall-clock/attempt/timeout budgets open the circuit.
15. The interaction layer renders the answer and execution summary.
16. In chat mode, `SessionStore` persists the updated conversation and last run id.

## 5. 仓库脚手架选择

This repository uses Python for the initial scaffold because:

- it is fast to bootstrap for CLI and orchestration code
- it makes interfaces and typed protocol models easy to express
- it keeps the architecture readable while remaining runnable

The code is intentionally minimal. It establishes the system boundaries and the request loop, leaving room for future provider integrations and richer tool implementations.

## 6. 后续扩展点

- additional model providers behind `ModelClient`
- semantic context summarization, token-aware budgeting, and content-addressed archive retrieval
- richer terminal UI with streaming updates
- resource-aware context selection and budgeted packaging
- additional capability warehouse sources, Skill assets, and automatic TTL/LRU release policies (see [能力仓库](extensions/capability-warehouse.md))
- broader MCP server compatibility and resource context integration (see [MCP 集成](extensions/mcp-integration.md))
- local subagents, team workflows, and remote A2A agents
