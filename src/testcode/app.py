from __future__ import annotations

import argparse
import os

from .config import load_dotenv, load_runtime_config
from .interaction.cli import CLI
from .interaction.presenter import ConsolePresenter
from .model.client import OpenAICompatibleModelClient, StubModelClient
from .model.types import ModelClientConfig
from .observability.logger import InMemoryLogger
from .orchestration.engine import ExecutionEngine
from .safety.guardrails import Guardrails
from .safety.policy import DefaultPolicy
from .sessions import SessionStore
from .tools.builtin import build_builtin_registry
from .types import UserRequest


load_dotenv()


def create_model_client(logger) -> StubModelClient | OpenAICompatibleModelClient:
    config = load_runtime_config()
    if not config.model_base_url:
        return StubModelClient()

    model_config = ModelClientConfig(
        base_url=config.model_base_url,
        model=config.model_name,
        timeout=config.model_timeout,
    )
    return OpenAICompatibleModelClient(config=model_config, logger=logger)


def create_app(mode: str | None = None) -> CLI:
    config = load_runtime_config(mode=mode)
    logger = InMemoryLogger()
    policy = DefaultPolicy(mode=config.mode)
    guardrails = Guardrails(policy=policy, logger=logger)
    tools = build_builtin_registry(logger=logger)
    model = create_model_client(logger)
    presenter = ConsolePresenter(tool_result_summarizer=tools.summarize_result)
    engine = ExecutionEngine(
        model=model,
        tools=tools,
        guardrails=guardrails,
        logger=logger,
        approval_callback=presenter.confirm_tool_action,
    )
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

        if args.once:
            prompt = initial_prompt or input("testcode> ").strip()
            if not prompt:
                return
            metadata = {}
            if resumed_session is not None:
                metadata["conversation"] = list(resumed_session.messages)
                metadata["session_id"] = resumed_session.session_id
            request = UserRequest(prompt=prompt, cwd=cwd, metadata=metadata)
            app.run(request)
            return

        app.chat(
            cwd=cwd,
            initial_prompt=initial_prompt,
            conversation=resumed_session.messages if resumed_session is not None else None,
            session_id=resumed_session.session_id if resumed_session is not None else None,
        )
    except KeyboardInterrupt:
        print()
        return
