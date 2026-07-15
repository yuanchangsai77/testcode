from __future__ import annotations

import argparse
import os

from .config import load_dotenv, load_runtime_config
from .context import ExplicitContextLoader, ProjectRulesLoader, WorkspaceSummaryLoader
from .interaction.cli import CLI
from .interaction.presenter import ConsolePresenter
from .mcp.client import TransportBackedMCPClient, UnsupportedMCPClient
from .mcp.discovery import MCPDiscoveryService
from .mcp.manager import MCPManager
from .mcp.provider import MCPResourceProvider, MCPToolProvider
from .mcp.transport import SSETransport, StdioTransport, StreamableHttpTransport
from .model.client import OpenAICompatibleModelClient, StubModelClient
from .model.types import ModelClientConfig
from .observability.logger import InMemoryLogger
from .orchestration.engine import ExecutionEngine
from .safety.guardrails import Guardrails
from .safety.policy import DefaultPolicy
from .sessions import SessionStore
from .tools.builtin import build_builtin_registry
from .tools.builtin_provider import BuiltinToolProvider
from .tools.registry import ToolRegistry
from .skills.registry import SkillRegistry
from .skills.loader import SkillContextLoader
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


def create_model_client(logger, config=None) -> StubModelClient | OpenAICompatibleModelClient:
    config = config or load_runtime_config()
    if not config.model_base_url:
        return StubModelClient()

    model_config = ModelClientConfig(
        base_url=config.model_base_url,
        model=config.model_name,
        timeout=config.model_timeout,
    )
    return OpenAICompatibleModelClient(config=model_config, logger=logger)


def create_app(mode: str | None = None, workspace_root: str | Path | None = None) -> CLI:
    root = Path(workspace_root or os.getcwd()).resolve()
    config = load_runtime_config(mode=mode, cwd=root)
    logger = InMemoryLogger()
    policy = DefaultPolicy(mode=config.mode)
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
    workspace_summary_loader = WorkspaceSummaryLoader(logger=logger)
    explicit_context_loader = ExplicitContextLoader(logger=logger)
    skill_loader = SkillContextLoader(registry=skills_registry, logger=logger)

    # Initialize tools via providers. MCP is wired as an optional provider layer
    # so discovery/runtime boundaries exist even before concrete transports land.
    providers = [BuiltinToolProvider(logger)]
    mcp_tool_provider = None
    mcp_manager = None
    if config.mcp_servers:
        mcp_manager = MCPManager(
            configs={server.name: server for server in config.mcp_servers},
            client_factory=create_mcp_client,
            logger=logger,
        )
        mcp_discovery = MCPDiscoveryService(
            manager=mcp_manager,
            logger=logger,
            cache_path=root / ".testcode" / "mcp-discovery-cache.json",
        )
        mcp_tool_provider = MCPToolProvider(
            configs=config.mcp_servers,
            discovery=mcp_discovery,
            manager=mcp_manager,
            logger=logger,
        )
    tools = ToolRegistry(logger=logger)
    for provider in providers:
        for tool in provider.get_tools():
            tools.register(tool)
    if mcp_tool_provider is not None:
        tools.attach_provider(mcp_tool_provider)
    if mcp_manager is not None:
        tools.attach_state("mcp_manager", mcp_manager, persistent=True)

    model = create_model_client(logger, config=config)
    presenter = ConsolePresenter(tool_result_summarizer=tools.summarize_result)
    engine = ExecutionEngine(
        model=model,
        tools=tools,
        guardrails=guardrails,
        logger=logger,
        context_loaders=[project_rules_loader, workspace_summary_loader, explicit_context_loader, skill_loader],
        approval_callback=presenter.confirm_tool_action,
        progress_reporter=presenter,
    )
    engine.resource_providers = []
    if mcp_manager is not None:
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

    session_store = SessionStore()
    return CLI(engine=engine, presenter=presenter, logger=logger, session_store=session_store)


def main() -> None:
    try:
        parser = argparse.ArgumentParser(description="testcode: LLM-driven CLI workbench scaffold")
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
            app = create_app(mode=args.mode, workspace_root=cwd)
            if session_id is not None:
                resumed_session = app.load_session(session_id) or resumed_session

        if args.once:
            prompt = initial_prompt or input("testcode> ").strip()
            if not prompt:
                return
            metadata = {}
            if resumed_session is not None:
                metadata["conversation"] = list(resumed_session.messages)
                metadata["session_id"] = resumed_session.session_id
                metadata["active_skills"] = list(getattr(resumed_session, "active_skills", []))
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
                        ExecutionSummary(final_message="Interrupted", tool_results=[]),
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
