# codexcli Core Architecture

## 1. Product Positioning

`codexcli` is a large-model-driven CLI workbench. It is not an autonomous decision engine. Its responsibility is to:

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

- `src/codexcli/interaction/cli.py`
- `src/codexcli/interaction/presenter.py`

### 3.2 Session Orchestration Layer

Responsibilities:

- create and manage a session for each task
- assemble history, environment metadata, and tool availability
- run the think-act-observe loop between model and tools
- stop when the model reaches a terminal answer or a policy blocks progress

Core files:

- `src/codexcli/orchestration/session.py`
- `src/codexcli/orchestration/engine.py`

### 3.3 Model Integration Layer

Responsibilities:

- translate session state into model input
- invoke the model provider
- parse structured actions from model output
- support streaming and provider replacement

Core files:

- `src/codexcli/model/protocol.py`
- `src/codexcli/model/client.py`

### 3.4 Tool Execution Layer

Responsibilities:

- expose callable tools to the orchestration engine
- execute concrete operations such as shell, file, and search tasks
- return normalized results to the orchestration loop

Core files:

- `src/codexcli/tools/base.py`
- `src/codexcli/tools/registry.py`
- `src/codexcli/tools/builtin.py`

### 3.5 Safety Layer

Responsibilities:

- validate whether a requested action is allowed
- require explicit approval for risky operations
- keep runtime boundaries independent from model judgment

Core files:

- `src/codexcli/safety/policy.py`
- `src/codexcli/safety/guardrails.py`

### 3.6 Observability Layer

Responsibilities:

- record model calls, tool invocations, and errors
- provide traceability for debugging and auditing
- emit execution summaries for the CLI and future telemetry backends

Core files:

- `src/codexcli/observability/events.py`
- `src/codexcli/observability/logger.py`

## 4. Runtime Flow

1. The user submits a task through the CLI.
2. The interaction layer creates a request object.
3. The orchestration layer opens a session and assembles context.
4. The model layer receives the session prompt and returns either:
   - a final answer
   - one or more tool actions
5. Each requested tool action is checked by the safety layer.
6. Allowed actions are executed by the tool layer.
7. Tool results are logged by the observability layer and added back into session state.
8. The orchestration loop continues until a final answer is produced.
9. The interaction layer renders the answer and execution summary.

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
