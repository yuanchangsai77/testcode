from __future__ import annotations

import argparse
import os
from pathlib import Path

from .interaction.cli import CLI
from .interaction.presenter import ConsolePresenter
from .model.client import OpenAICompatibleModelClient, StubModelClient
from .observability.logger import InMemoryLogger
from .orchestration.engine import ExecutionEngine
from .safety.guardrails import Guardrails
from .safety.policy import DefaultPolicy
from .session_store import SessionStore
from .tools.builtin import build_builtin_registry
from .types import UserRequest


def load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if (
            (value.startswith('"') and value.endswith('"')) or
            (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]

        os.environ[key] = value


load_dotenv()


def create_model_client(logger) -> StubModelClient | OpenAICompatibleModelClient:
    base_url = os.getenv("TESTCODE_MODEL_BASE_URL", "").strip()
    if not base_url:
        return StubModelClient()

    model = os.getenv("TESTCODE_MODEL_NAME", "gpt-5.4").strip() or "gpt-5.4"
    return OpenAICompatibleModelClient(base_url=base_url, model=model, logger=logger)


def create_app() -> CLI:
    logger = InMemoryLogger()
    policy = DefaultPolicy()
    guardrails = Guardrails(policy=policy, logger=logger)
    tools = build_builtin_registry(logger=logger)
    model = create_model_client(logger)
    engine = ExecutionEngine(model=model, tools=tools, guardrails=guardrails, logger=logger)
    presenter = ConsolePresenter()
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
        args = parser.parse_args()

        app = create_app()
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
