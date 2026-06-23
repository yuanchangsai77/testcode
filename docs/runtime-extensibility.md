# testcode Runtime Extensibility Design Document

To support features like the **Skill System (P2)**, **Project Rules (P1.2)**, **Explicit Context (P1.4)**, and **MCP Integration (P3)** without bloating the core execution loop, we introduce two generic extension interfaces into the `testcode` runtime:
1. **`ContextLoader`**: Hook interface for loading dynamic context, rules, and skills at the start of a run.
2. **`ToolProvider`**: Hook interface for registering external tools (e.g., from MCP servers).

---

## 1. Context Extension: `ContextLoader` Interface

### Interface Definition
We define the interface using Python protocols or abstract base classes:

```python
from typing import Protocol
from ..types import UserRequest
from .session import SessionContext

class ContextLoader(Protocol):
    """Extension hook to inject custom instructions, metadata, or workspace context before execution."""
    
    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        """Executed once at the beginning of the ExecutionEngine run."""
        ...
```

### Implementing Concrete Loaders

Using this single abstraction, we can cleanly isolate and implement different roadmap features:

1. **`SkillContextLoader`** (For P2: Skill System)
   - Matches `request.prompt` using `SkillRegistry`.
   - Injects active skills into `session.metadata` or custom session properties.
2. **`ProjectRulesLoader`** (For P1.2: Project Rules - `AGENTS.md`)
   - Traverses directories upwards from `request.cwd` to find `AGENTS.md`.
   - Loads its content and appends it to system instructions.
3. **`ExplicitContextLoader`** (For P1.4: Explicit Context - `--context`)
   - Expands file globs/directories requested via `--context`.
   - Reads files and prepends them as initial system/user knowledge.

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
    """Interface to provide a collection of executable tools to the runtime."""
    
    def get_tools(self) -> list[Tool]:
        """Discovers and returns a list of tools to be registered (implementing the Tool protocol)."""
        ...
```

> [!NOTE]
> **Execution Order of ContextLoaders**: Loaders should be executed in order of specificity:
> 1. General workspace/project loaders (like `ProjectRulesLoader` loading `AGENTS.md`).
> 2. Dynamic, trigger-based loaders (like `SkillContextLoader`).
> 3. User-explicit parameter loaders (like `ExplicitContextLoader` loading files/directories specified in CLI arguments).


### Implementations

1. **`BuiltinToolProvider`**: Returns the standard file, search, git, and shell tools.
2. **`MCPToolProvider`**: Connects to the configured stdio MCP servers, translates their tool schemas to `testcode`'s custom tool interface, and returns them.

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

---

## 3. Advantages of This Design

* **Decoupled Execution Loop**: [ExecutionEngine](orchestration/engine.py) does not need to know about files, markdown frontmatter, git branches, or MCP transport protocols. It only orchestrates prompt, tool execution, safety, and loop termination.
* **Easy Testing**: Each `ContextLoader` and `ToolProvider` can be tested in complete isolation without running a full LLM session.
* **Observe and Log**: We can log context loading events (e.g. `context.loaded` event with payload `{"loader": "SkillContextLoader", "duration_ms": 12}`) to keep trace logs structured and searchable.
