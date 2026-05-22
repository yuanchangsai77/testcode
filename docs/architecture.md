# testcode Core Architecture

## 1. Product Positioning

`testcode` is a large-model-driven CLI workbench. It is not an autonomous decision engine. Its responsibility is to:

- receive user intent from the command line
- collect execution context from the local environment
- package that context for an LLM
- convert model output into executable actions
- execute actions through controlled tools
- expose progress, logs, and results back to the user

The system's intelligence comes from the model. The CLI is the runtime shell that makes that intelligence operable, safe, and observable.

## 2. Design Principles

- Thin control plane: keep the CLI framework focused on coordination, not reasoning.
- Clear boundary of responsibility: model decides, runtime executes.
- Structured execution: every model instruction is normalized into typed actions.
- Safety first: tool execution must pass policy checks before running.
- Full observability: every turn, tool call, and result is recorded as events.
- Replaceable components: model providers, tools, and policies are pluggable.

## 3. Layered Architecture

Current source layout:

```text
src/testcode/
  app.py             application composition and CLI argument dispatch
  config.py          .env loading and runtime configuration
  interaction/       CLI input/output and presentation
  orchestration/     session context and model/tool execution loop
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

- load `.env` before runtime objects are assembled
- create the logger, policy, guardrails, tool registry, model client, presenter, engine, and session store
- parse CLI flags and choose single-turn, chat, list, resume, or latest-session mode
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

Core files:

- `src/testcode/interaction/cli.py`
- `src/testcode/interaction/presenter.py`

### 3.2 Session Orchestration Layer

Responsibilities:

- create the per-run `SessionContext`
- assemble history, environment metadata, and tool availability
- run the think-act-observe loop between model and tools
- stop when the model reaches a terminal answer or a policy blocks progress
- avoid repeating successful duplicate tool calls within the same run

Core files:

- `src/testcode/orchestration/session.py`
- `src/testcode/orchestration/engine.py`

Orchestration structure:

- `session.py` owns the in-memory context passed to the model during one run.
- `engine.py` owns the model/tool loop, policy checks, approvals, duplicate action skipping, and terminal summary.
- Long-lived conversation persistence is not part of this layer; it is handled by `sessions/store.py`.

### 3.3 Model Integration Layer

Responsibilities:

- translate session state into model input
- invoke the model provider
- parse structured actions from model output
- support provider replacement through a small `respond(session)` protocol boundary

Core files:

- `src/testcode/model/protocol.py`
- `src/testcode/model/client.py`
- `src/testcode/model/prompt.py`
- `src/testcode/model/parser.py`
- `src/testcode/model/types.py`

Model structure:

- `client.py` owns OpenAI-compatible provider transport, request/response logging, and provider-level error handling.
- `prompt.py` owns message and native tool schema construction.
- `parser.py` owns native tool call parsing, JSON fallback parsing, and protocol-noise cleanup.
- `types.py` owns model-specific configuration and parser helper types.

Important boundaries:

- `ModelClientConfig` lives in `model/types.py`, not in `model/client.py`.
- Prompt construction is accessed through `ModelPromptBuilder`.
- Reply parsing is accessed through `ModelReplyParser`.
- `OpenAICompatibleModelClient` coordinates those collaborators but does not expose parser or prompt compatibility methods.

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
- `src/testcode/tools/builtin.py`
- `src/testcode/tools/shared.py`
- `src/testcode/tools/builtins/`

Tool structure:

- `base.py` defines the common tool protocol, `SimpleTool`, and `ToolContext`.
- `registry.py` owns registration, default tool exposure, schema validation, and normalized result logging.
- `shared.py` contains reusable helpers for JSON-style schemas, workspace path resolution, subprocess execution, output clipping, and result retargeting.
- `builtin.py` assembles the default registry only.
- `builtins/<tool>.py` contains one concrete tool per file. Each module exports a `tool()` factory and keeps the tool's `run()` implementation local.

Built-in tools:

- `list_dir`, `read_file`, `file_info`
- `find_files`, `search_text`
- `shell_exec`, `run_tests`
- `git_status`, `git_diff`, `git_show`
- `patch`

`apply_change` remains available internally as a deprecated tool, but it is not exposed in the default tool list.

### 3.5 Safety Layer

Responsibilities:

- validate whether a requested action is allowed
- require explicit approval for risky operations
- keep runtime boundaries independent from model judgment
- surface approval requests through the interaction layer

Core files:

- `src/testcode/safety/policy.py`
- `src/testcode/safety/guardrails.py`

### 3.6 Observability Layer

Responsibilities:

- record model calls, tool invocations, and errors
- provide traceability for debugging and auditing
- emit execution summaries for the CLI and future telemetry backends

Core files:

- `src/testcode/observability/events.py`
- `src/testcode/observability/logger.py`

### 3.7 Configuration and Persistence

Responsibilities:

- load runtime configuration from `.env` and environment variables
- keep application assembly separate from configuration parsing
- persist resumable CLI conversations independently from the interaction loop
- list, load, resume, and close stored conversations

Core files:

- `src/testcode/config.py`
- `src/testcode/sessions/__init__.py`
- `src/testcode/sessions/store.py`

Configuration structure:

- `.env` loading only fills missing environment variables.
- `RuntimeConfig` normalizes model base URL, model name, timeout, and safety mode.
- Invalid or missing numeric timeout values fall back to the default.

Persistence structure:

- `SessionStore` writes JSON files under `.testcode/sessions/`.
- Stored sessions include cwd, timestamps, status, messages, and run ids.
- Corrupt session files are skipped when listing sessions.

## 4. Runtime Flow

1. The user submits a task through the CLI.
2. `app.py` loads configuration, wires the runtime objects, and chooses the requested CLI mode.
3. The interaction layer creates a `UserRequest`.
4. The orchestration layer creates a `SessionContext` with available tool definitions and prior conversation metadata.
5. The model layer builds messages through `ModelPromptBuilder`.
6. `OpenAICompatibleModelClient` invokes the provider, or `StubModelClient` is used when no model base URL is configured.
7. `ModelReplyParser` normalizes the provider response into either:
   - a final answer
   - one or more tool actions
8. Each requested tool action is checked by the safety layer.
9. If an action needs approval, the CLI asks the user before execution.
10. Allowed or approved actions are executed by the tool layer.
11. Tool results are logged by the observability layer and added back into session state.
12. Successful duplicate tool actions in the same run are skipped to avoid repeated side effects.
13. The orchestration loop continues until a final answer is produced or a stop condition is reached.
14. The interaction layer renders the answer and execution summary.
15. In chat mode, `SessionStore` persists the updated conversation and last run id.

## 5. Repository Scaffold Decision

This repository uses Python for the initial scaffold because:

- it is fast to bootstrap for CLI and orchestration code
- it makes interfaces and typed protocol models easy to express
- it keeps the architecture readable while remaining runnable

The code is intentionally minimal. It establishes the system boundaries and the request loop, leaving room for future provider integrations and richer tool implementations.

## 6. Future Extension Points

- additional model providers behind `ModelClient`
- more robust prompt budgeting and context assembly
- reliable edit workflow with read-before-patch, hash checks, diff preview, and test feedback
- approval workflows for destructive tools
- richer terminal UI with streaming updates
- skill-based context loading
- MCP-backed external tool discovery
- local subagents, team workflows, and remote A2A agents
