# codexcli

`codexcli` is an LLM-driven CLI workbench. The CLI itself does not own decision-making intelligence. It provides a controlled runtime that collects context, delegates reasoning to a large model, executes approved tools, and returns observable results to the user.

## Architecture

The project is organized around six layers:

1. Interaction layer: CLI entrypoints, user input, progress output, and result rendering.
2. Session orchestration layer: task lifecycle, context assembly, and model/tool loop coordination.
3. Model integration layer: prompt packaging, model invocation, and structured response parsing.
4. Tool execution layer: shell, file, git, search, and other callable capabilities.
5. Safety layer: policy checks, confirmations, and execution boundaries.
6. Observability layer: logs, events, trace records, and execution summaries.

Detailed design is in [docs/architecture.md](/opt/repos/testcode/docs/architecture.md).

## Project Layout

```text
src/codexcli/
  interaction/     CLI input/output
  orchestration/   session state and agent loop
  model/           LLM adapter and protocol
  tools/           executable tool registry
  safety/          guardrails and policy checks
  observability/   logging and event capture
```

## Quick Start

```bash
python -m codexcli "summarize this repository"
```

The current implementation is a scaffold. It wires the architecture together and can run a minimal request flow with a stub model and toolchain.

## Connect To The Local LLM Proxy

If you want `codexcli` to use the OpenAI-compatible proxy running in `/opt/repos/test`, start that project first and then set:

```bash
export CODEXCLI_MODEL_BASE_URL=http://127.0.0.1:3000
export CODEXCLI_MODEL_NAME=gpt-5.4
python -m codexcli "summarize this repository"
```

Behavior:

- If `CODEXCLI_MODEL_BASE_URL` is not set, `codexcli` keeps using `StubModelClient`
- If `CODEXCLI_MODEL_BASE_URL` is set, `codexcli` sends requests to `POST /v1/chat/completions`
- The proxy in `/opt/repos/test` remains responsible for the real upstream base URL and API key
- The real-model path now supports tool-call loops: the model can request built-in tools, receive tool results, and continue until it produces a final answer
