from __future__ import annotations

import argparse
import os

from . import __version__
from .capabilities.mcp_source import MCPToolboxSource
from .capabilities.local_source import LocalToolboxSource, skill_toolbox_specs, subagent_toolbox_spec
from .capabilities.tools import build_warehouse_tools
from .capabilities.warehouse import CapabilityWarehouse
from .config import load_dotenv, load_runtime_config
from .context import ExplicitContextLoader, ProjectRulesLoader, WorkspaceSummaryLoader
from .interaction.cli import CLI
from .interaction.presenter import ConsolePresenter
from .interaction.tui import TUIConsolePresenter, should_use_tui
from .intent import RequestIntentClassifier
from .mcp.client import TransportBackedMCPClient, UnsupportedMCPClient
from .mcp.discovery import MCPDiscoveryService
from .mcp.manager import MCPManager
from .mcp.provider import MCPResourceProvider
from .mcp.transport import SSETransport, StdioTransport, StreamableHttpTransport
from .model.client import OpenAICompatibleModelClient, StubModelClient
from .model.types import ModelClientConfig
from .observability.logger import InMemoryLogger
from .orchestration.engine import ExecutionEngine
from .orchestration.subagent_runner import SubagentExecutionGrant, SubagentRunner
from .orchestration.subagents import SubagentCoordinator
from .project import ProjectDetector
from .safety.guardrails import Guardrails
from .safety.policy import DefaultPolicy
from .safety.content import build_content_safety_interceptors
from .sessions import SessionClusterStore, SessionImageStore, SessionStore
from .tools.builtin_provider import BuiltinToolProvider
from .tools.registry import ToolRegistry
from .skills.registry import SkillRegistry
from .types import ExecutionSummary, UserRequest
from pathlib import Path


load_dotenv()


def create_mcp_client(server):
    if server.transport == "stdio":
        return TransportBackedMCPClient(config=server, transport=StdioTransport(config=server))
    if server.transport == "streamable_http":
        return TransportBackedMCPClient(config=server, transport=StreamableHttpTransport(config=server))
    if server.transport == "sse":
        return TransportBackedMCPClient(config=server, transport=SSETransport(config=server))
    return UnsupportedMCPClient(config=server)


def create_model_client(
    logger,
    config=None,
    *,
    timeout: float | None = None,
) -> StubModelClient | OpenAICompatibleModelClient:
    config = config or load_runtime_config()
    if not config.model_base_url:
        return StubModelClient()

    model_config = ModelClientConfig(
        base_url=config.model_base_url,
        model=config.model_name,
        timeout=config.model_timeout if timeout is None else timeout,
    )
    return OpenAICompatibleModelClient(config=model_config, logger=logger)


def create_app(
    mode: str | None = None,
    workspace_root: str | Path | None = None,
    *,
    interactive: bool = False,
    background: bool = False,
    subagent_grant: SubagentExecutionGrant | None = None,
) -> CLI:
    root = Path(workspace_root or os.getcwd()).resolve()
    config = load_runtime_config(mode=mode, cwd=root)
    delegated_write = subagent_grant is not None and subagent_grant.is_runner_issued()
    runtime_mode = "auto" if delegated_write and config.mode == "confirm" else config.mode
    session_store = SessionStore()
    subagent_coordinator = SubagentCoordinator(
        session_store=session_store,
        cluster_store=SessionClusterStore(),
        image_store=SessionImageStore(),
    )
    subagent_runner = SubagentRunner(
        coordinator=subagent_coordinator,
        runtime_factory=lambda session, grant: create_app(
            mode=config.mode,
            workspace_root=session.cwd,
            background=True,
            subagent_grant=grant,
        ),
    )
    logger = InMemoryLogger()
    policy = DefaultPolicy(mode=runtime_mode)
    guardrails = Guardrails(policy=policy, logger=logger)

    # Resolve skill registry directories
    builtins_dir = Path(__file__).parent / "skills" / "builtins"
    global_dir = Path.home() / ".testcode" / "skills"
    project_dir = root / ".testcode" / "skills"

    skills_registry = SkillRegistry(
        builtins_dir=builtins_dir,
        global_dir=global_dir,
        project_dir=project_dir,
    )
    skills_registry.scan_metadata()

    project_rules_loader = ProjectRulesLoader(logger=logger)
    intent_classifier = RequestIntentClassifier()
    project_detector = ProjectDetector()
    workspace_summary_loader = WorkspaceSummaryLoader(
        logger=logger,
        intent_classifier=intent_classifier,
        project_detector=project_detector,
    )
    explicit_context_loader = ExplicitContextLoader(logger=logger)

    # Only builtin and warehouse-navigation tools are initially visible. External
    # MCP/Skill capabilities remain in the warehouse until explicitly activated.
    builtin_provider = BuiltinToolProvider(
        logger,
        limits=config.limits,
        project_detector=project_detector,
        include_specialized=False,
    )
    providers = [builtin_provider]
    mcp_manager = None
    mcp_discovery = None
    local_specs = list(
        skill_toolbox_specs(
            skills_registry,
            tools_by_skill=builtin_provider.get_skill_tools(),
        )
    )
    if not background:
        local_specs.append(subagent_toolbox_spec())
    capability_sources = [LocalToolboxSource(tuple(local_specs))]
    if config.mcp_servers:
        def create_configured_mcp_client(server):
            client = create_mcp_client(server)
            if isinstance(client, TransportBackedMCPClient):
                client.max_tools_per_server = config.limits.mcp_tools_per_server
            return client

        mcp_manager = MCPManager(
            configs={server.name: server for server in config.mcp_servers},
            client_factory=create_configured_mcp_client,
            logger=logger,
        )
        mcp_discovery = MCPDiscoveryService(
            manager=mcp_manager,
            logger=logger,
            cache_path=root / ".testcode" / "mcp-discovery-cache.json",
            max_tools_per_server=config.limits.mcp_tools_per_server,
        )
        capability_sources.append(
            MCPToolboxSource(
                configs=config.mcp_servers,
                discovery=mcp_discovery,
                manager=mcp_manager,
                logger=logger,
            )
        )
    tools = ToolRegistry(
        logger=logger,
        max_output_bytes=config.limits.tool_output_bytes,
        interceptors=build_content_safety_interceptors(logger),
    )
    for provider in providers:
        for tool in provider.get_tools():
            tools.register(tool)
    capability_warehouse = CapabilityWarehouse(
        sources=capability_sources,
        registry=tools,
        logger=logger,
        max_active_capabilities=config.limits.active_capabilities,
    )
    for tool in build_warehouse_tools(capability_warehouse):
        tools.register(tool)
    tools.attach_state("capability_warehouse", capability_warehouse, persistent=True)
    tools.attach_state("subagent_coordinator", subagent_coordinator, persistent=True)
    tools.attach_state("subagent_runner", subagent_runner, persistent=True)
    if mcp_manager is not None:
        tools.attach_state("mcp_manager", mcp_manager, persistent=True)

    model = create_model_client(
        logger,
        config=config,
        timeout=(
            min(config.model_timeout, config.orchestration.subagent_model_timeout)
            if background
            else None
        ),
    )
    presenter_type = TUIConsolePresenter if interactive and should_use_tui() else ConsolePresenter
    presenter = presenter_type(tool_result_summarizer=tools.summarize_result)
    engine = ExecutionEngine(
        model=model,
        tools=tools,
        guardrails=guardrails,
        logger=logger,
        context_loaders=[project_rules_loader, workspace_summary_loader, explicit_context_loader],
        capability_warehouse=capability_warehouse,
        approval_callback=None if background else presenter.confirm_tool_action,
        progress_reporter=None if background else presenter,
        max_model_retries=(
            min(
                config.model_retry.max_retries,
                config.orchestration.subagent_max_model_retries,
            )
            if background
            else config.model_retry.max_retries
        ),
        model_retry_delays=config.model_retry.delays,
        max_turns=config.orchestration.max_turns,
        mcp_server_count=sum(server.enabled for server in config.mcp_servers),
        intent_classifier=intent_classifier,
    )
    engine.resource_providers = []
    if mcp_manager is not None and mcp_discovery is not None:
        engine.resource_providers.append(
            MCPResourceProvider(
                configs=config.mcp_servers,
                discovery=mcp_discovery,
                manager=mcp_manager,
                logger=logger,
            )
        )
    # Store skills registry on engine for CLI visibility
    engine.skills_registry = skills_registry
    presenter.engine = engine

    return CLI(
        engine=engine,
        presenter=presenter,
        logger=logger,
        session_store=session_store,
        subagent_coordinator=subagent_coordinator,
        subagent_runner=subagent_runner,
        subagent_grant=subagent_grant,
    )


def main() -> None:
    try:
        parser = argparse.ArgumentParser(description="testcode: LLM-driven CLI workbench scaffold")
        parser.add_argument(
            "--version",
            action="version",
            version=f"testcode {__version__}",
        )
        parser.add_argument("prompt", nargs="*", help="Task to send into the CLI workbench")
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single turn and exit instead of entering long conversation mode.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List saved conversations. You can optionally pick one to resume.",
        )
        parser.add_argument(
            "--resume",
            nargs="?",
            help="Resume a saved conversation by session id.",
        )
        parser.add_argument(
            "--last",
            action="store_true",
            help="Resume the most recently updated saved conversation.",
        )
        parser.add_argument(
            "--mode",
            choices=["readonly", "confirm", "auto"],
            default=os.getenv("TESTCODE_MODE", "confirm").strip() or "confirm",
            help="Safety mode for tool execution.",
        )
        parser.add_argument(
            "--context",
            action="append",
            default=[],
            help="Add a workspace file, directory, or glob as explicit context for this run.",
        )
        args = parser.parse_args()

        interactive = not args.once and not args.list
        if interactive:
            app = create_app(mode=args.mode, interactive=True)
        else:
            app = create_app(mode=args.mode)
        initial_prompt = " ".join(args.prompt).strip() or None

        resume_requested = args.resume is not None
        if resume_requested and args.last:
            raise SystemExit("Use either --resume or --last, not both.")

        resumed_session = None
        if resume_requested:
            if args.resume:
                resumed_session = app.load_session(args.resume)
                if resumed_session is None:
                    raise SystemExit(f"Unknown session id: {args.resume}")
            else:
                resumed_session = app.choose_session()
                if resumed_session is None:
                    return
        elif args.last:
            resumed_session = app.latest_session()
            if resumed_session is None:
                raise SystemExit("No saved sessions found.")
        elif args.list:
            app.presenter.show_session_list(app.list_sessions())
            return

        cwd = resumed_session.cwd if resumed_session is not None else os.getcwd()
        if Path(cwd).resolve() != Path(os.getcwd()).resolve():
            tools = getattr(getattr(app, "engine", None), "tools", None)
            reset_state = getattr(tools, "reset_state", None)
            if callable(reset_state):
                reset_state()
            session_id = resumed_session.session_id if resumed_session is not None else None
            if interactive:
                app = create_app(mode=args.mode, workspace_root=cwd, interactive=True)
            else:
                app = create_app(mode=args.mode, workspace_root=cwd)
            if session_id is not None:
                resumed_session = app.load_session(session_id) or resumed_session

        if args.once:
            prompt = initial_prompt or input("testcode> ").strip()
            if not prompt:
                return
            session_store = getattr(app, "session_store", None)
            if resumed_session is None and session_store is not None:
                resumed_session = session_store.create(cwd=cwd)
            metadata = {}
            if resumed_session is not None:
                metadata["conversation"] = list(resumed_session.messages)
                metadata["session_id"] = resumed_session.session_id
                metadata["active_capability_ids"] = list(
                    getattr(resumed_session, "active_capability_ids", [])
                )
                metadata["session_trace"] = list(getattr(resumed_session, "trace", [])[-6:])
                metadata["resume_state"] = getattr(resumed_session, "resume_state", None)
            metadata["context_paths"] = list(args.context)
            request = UserRequest(prompt=prompt, cwd=cwd, metadata=metadata)
            try:
                summary = app.run(request)
            except KeyboardInterrupt:
                if resumed_session is not None:
                    app.persist_run(
                        resumed_session,
                        prompt,
                        ExecutionSummary(
                            final_message="Interrupted",
                            tool_results=[],
                            outcome="interrupted",
                        ),
                        status="closed",
                        close_runtime=True,
                    )
                raise
            if resumed_session is not None:
                app.persist_run(
                    resumed_session,
                    prompt,
                    summary,
                    status="closed",
                    close_runtime=True,
                )
            return

        app.chat(
            cwd=cwd,
            initial_prompt=initial_prompt,
            conversation=resumed_session.messages if resumed_session is not None else None,
            session_id=resumed_session.session_id if resumed_session is not None else None,
            context_paths=list(args.context),
        )
    except KeyboardInterrupt:
        print()
        return
