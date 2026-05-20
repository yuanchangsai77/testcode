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

- create and manage a session for each task
- assemble history, environment metadata, and tool availability
- run the think-act-observe loop between model and tools
- stop when the model reaches a terminal answer or a policy blocks progress

Core files:

- `src/testcode/orchestration/session.py`
- `src/testcode/orchestration/engine.py`

### 3.3 Model Integration Layer

Responsibilities:

- translate session state into model input
- invoke the model provider
- parse structured actions from model output
- support streaming and provider replacement

Core files:

- `src/testcode/model/protocol.py`
- `src/testcode/model/client.py`

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

## 4. Runtime Flow

1. The user submits a task through the CLI.
2. The interaction layer creates a request object.
3. The orchestration layer opens a session and assembles context.
4. The model layer receives the session prompt and returns either:
   - a final answer
   - one or more tool actions
5. Each requested tool action is checked by the safety layer.
6. If an action needs approval, the CLI asks the user before execution.
7. Allowed or approved actions are executed by the tool layer.
8. Tool results are logged by the observability layer and added back into session state.
9. Successful duplicate tool actions in the same run are skipped to avoid repeated side effects.
10. The orchestration loop continues until a final answer is produced.
11. The interaction layer renders the answer and execution summary.

## 5. Repository Scaffold Decision

This repository uses Python for the initial scaffold because:

- it is fast to bootstrap for CLI and orchestration code
- it makes interfaces and typed protocol models easy to express
- it keeps the architecture readable while remaining runnable

The code is intentionally minimal. It establishes the system boundaries and the request loop, leaving room for future provider integrations and richer tool implementations.

## 6. Future Extension Points

- real model providers such as OpenAI-compatible backends
- persistent conversation storage
- approval workflows for destructive tools
- richer terminal UI with streaming updates
- plugin-based external tool discovery
- distributed execution or remote agents
