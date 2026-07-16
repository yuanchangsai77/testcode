# testcode Runtime Extensibility Design Document

## Document Scope

本文档只定义通用扩展点及其边界：

- `ContextLoader`
- `ToolProvider`
- `ResourceProvider`

它回答“哪些扩展点存在、边界在哪里、为什么这样拆”。

本文档不负责展开某一个具体扩展系统的全部实现细节：

- MCP 的 transport、discovery、risk、lifecycle 见 `docs/mcp-integration.md`
- Skill 的目录结构、匹配和注入流程见 `docs/skill-system.md`
- 能力仓库、工具箱分层、渐进暴露和按需激活见 `docs/capability-warehouse.md`
- 总体 runtime 分层见 `docs/architecture.md`

`ToolProvider` 的现有接口描述反映当前直接注册模型。目标架构中，外部来源先进入能力仓库，只有被选中的叶子能力才通过 provider/adapter 进入当前激活集；该演进以 `docs/capability-warehouse.md` 为准。

To support features like the **Skill System (P2)**, **Project Rules (P1.2)**, **Explicit Context (P1.4)**, and **MCP Integration (P3)** without bloating the core execution loop, we introduce three generic extension interfaces into the `testcode` runtime:
1. **`ContextLoader`**: Hook interface for loading dynamic context, rules, summaries, explicit user context, and skills at the start of a run. Loaders provide candidate context and archive references; they should not treat the prompt as long-term storage or own final pruning policy.
2. **`ToolProvider`**: Hook interface for registering external tools while reusing the same registry, policy, approval, and logging path as built-in tools. Providers should expose registration-ready tool handles, not own long-lived remote discovery policy.
3. **`ResourceProvider`**: Hook interface for exposing indexed, on-demand context sources such as MCP resources without forcing them through the tool registration path.

---

## 1. Context Extension: `ContextLoader` Interface

### Interface Definition
We define the interface using Python protocols or abstract base classes:

```python
from typing import Protocol
from ..types import UserRequest
from .session import SessionContext

class ContextLoader(Protocol):
    """Extension hook to collect candidate instructions, metadata, or workspace context before execution."""
    
    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        """Executed once at the beginning of the ExecutionEngine run."""
        ...
```

### Implementing Concrete Loaders

Using this single abstraction, we can cleanly isolate and implement different roadmap features:

1. **`ProjectRulesLoader`** (For P1.2: Project Rules - `AGENTS.md`)
   - Traverses directories upwards from `request.cwd` to the nearest project boundary.
   - Project boundaries are detected using `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`, or `go.mod`.
   - Loads bounded rule content defensively and records source metadata for packaging.
2. **`WorkspaceSummaryLoader`** (For P1.1/P1.3: Project, Git, and Workspace Summary)
   - Detects common project markers and suggested test commands.
   - Collects read-only git branch/status/latest commit information.
   - Builds a bounded directory tree while ignoring cache/build folders.
3. **`ExplicitContextLoader`** (For P1.4: Explicit Context - `--context`)
   - Expands file globs/directories requested via `--context`.
   - Reads workspace-contained text files and bounded directory listings as candidate context.
   - Refuses out-of-workspace paths and binary files.
4. **`SkillContextLoader`** (For P2: Skill System)
   - Matches `request.prompt` using `SkillRegistry`.
   - Adds active skill metadata and candidate guidance into session properties.

Context loaders should return or record enough source metadata for recovery and audit: path, run id, hash, truncation state, or artifact id. Complete raw content belongs in logs or cold archives and should be loaded into prompt only on demand.

A separate context packaging layer should sit after loaders and before `ModelPromptBuilder`. That layer owns ordering, budget accounting, clipping, summarization, and prompt-facing omission markers. This keeps pruning replaceable and prevents each loader from growing its own incompatible trimming logic.

### Integrating into the Engine

We modify `ExecutionEngine` to execute all registered loaders before entering the loop:

```python
class ExecutionEngine:
    def __init__(
        self,
        model,
        tools,
        guardrails,
        logger,
        context_loaders: list[ContextLoader] | None = None,
        approval_callback=None,
    ) -> None:
        ...
        self.context_loaders = context_loaders or []

    def execute(self, request: UserRequest) -> ExecutionSummary:
        ...
        session = SessionContext(request=request, available_tools=self.tools.definitions())
        
        # Execute all context loaders
        for loader in self.context_loaders:
            loader.load_context(request, session)
            
        # Continue loop...
```

---

## 2. Tool Extension: `ToolProvider` Interface

Currently, tools are hardcoded and loaded via `build_builtin_registry()`. To easily support **MCP (Model Context Protocol)** or other external toolsets, we introduce a `ToolProvider` interface.

### Interface Definition

```python
class ToolProvider(Protocol):
    """Interface to provide registration-ready tools to the runtime."""
    
    def get_tools(self) -> list[Tool]:
        """Returns a list of tools to be registered (implementing the Tool protocol)."""
        ...
```

This abstraction is intentionally narrow. A `ToolProvider` should not become the owner of remote connection lifecycle, retries, lazy discovery policy, or resource indexing. Those concerns belong in dedicated runtime services and adapters that the provider depends on.

> [!NOTE]
> **Execution Order of ContextLoaders**: Loaders should be executed in order of specificity:
> 1. Project policy loaders, such as `ProjectRulesLoader`.
> 2. Automatic workspace loaders, such as `WorkspaceSummaryLoader`.
> 3. User-explicit parameter loaders, such as `ExplicitContextLoader`.
> 4. Dynamic trigger-based loaders, such as `SkillContextLoader`.


### Implementations

1. **`BuiltinToolProvider`**: Returns the standard file, search, git, and shell tools.
2. **`MCPToolProvider`**: Reads tool descriptors from a dedicated MCP discovery service, adapts them into `testcode` tools, and returns them. It does not directly own transport selection, connection lifecycle, or resource indexing.

### Integrating into the Registry

During application creation in `src/testcode/app.py`:

```python
def create_app(mode: str | None = None) -> CLI:
    ...
    # Load all providers
    providers: list[ToolProvider] = [
        BuiltinToolProvider(logger),
        MCPToolProvider(logger, config_path=...)
    ]
    
    tools = ToolRegistry(logger=logger)
    for provider in providers:
        for tool in provider.get_tools():
            tools.register(tool)
    ...
```

Recommended internal layering for MCP:

- `MCPToolProvider`: registration only
- `MCPDiscoveryService`: lazy refresh, cached tool/resource descriptors, provider-facing snapshot API
- `MCPManager`: shared server lifecycle and client cache
- `MCPClient`: per-server protocol operations such as `initialize`, `tools/list`, and `tools/call`
- `MCPTransport`: transport abstraction, with `stdio` as the first implementation
- adapter layer: converts MCP tool schemas and results into `testcode` `Tool` / `ToolResult`

This split matters because process lifecycle, lazy discovery policy, schema adaptation, and runtime registration change at different rates. A single monolithic provider would technically work for a demo, but it would couple transport concerns to runtime composition and make later support for multiple transports, resource indexing, startup isolation, and reconnection logic harder to test and evolve.

---

## 3. Resource Extension: `ResourceProvider` Interface

MCP resources and future external artifacts should not be smuggled through `ToolProvider` or ad hoc `ContextLoader` branches. They are a distinct extension surface: indexed, queryable, and loaded on demand.

### Interface Definition

```python
class ResourceProvider(Protocol):
    """Interface to provide indexed, on-demand context sources."""

    def list_resources(self) -> list[ResourceDescriptor]:
        """Returns resource descriptors, not full resource bodies."""
        ...

    def read_resource(self, resource_id: str) -> ResourceContent:
        """Loads one resource body for downstream filtering, clipping, and packaging."""
        ...
```

Expected boundary:

- `ResourceProvider` exposes metadata and fetch primitives only.
- `ContextLoader` or a future resource-aware selector may choose which descriptors become candidate context.
- `ContextPackager` still owns prompt budget, clipping, summaries, and omission markers.
- Full resource bodies remain outside the prompt until explicitly selected.

---

## 4. Advantages of This Design

* **Decoupled Execution Loop**: [ExecutionEngine](orchestration/engine.py) does not need to know about files, markdown frontmatter, git branches, or MCP transport protocols. It only orchestrates prompt, tool execution, safety, and loop termination.
* **Easy Testing**: Each `ContextLoader`, `ToolProvider`, `ResourceProvider`, MCP client, and adapter can be tested in isolation without running a full LLM session.
* **Observe and Log**: We can log context loading events and MCP runtime events to keep trace logs structured and searchable.
* **Prompt Discipline**: Extension hooks can add candidate context, but the context packaging layer applies the runtime budget and provides source references for omitted or summarized content.
* **Composable Growth Path**: The same extension boundary supports built-in tools today, MCP tools next, future external resources, and later skill-derived or subagent-exposed capabilities without changing the engine contract.

See [docs/mcp-integration.md](mcp-integration.md) for the concrete MCP module split, lifecycle rules, risk mapping, and test plan.
