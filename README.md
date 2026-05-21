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

Detailed design is in [docs/architecture.md](/opt/repos/testcode/docs/architecture.md).

## Project Layout

```text
src/testcode/
  interaction/     CLI input/output
  orchestration/   session state and agent loop
  model/           LLM adapter and protocol
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
