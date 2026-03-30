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
    base_url = os.getenv("CODEXCLI_MODEL_BASE_URL", "").strip()
    if not base_url:
        return StubModelClient()

    model = os.getenv("CODEXCLI_MODEL_NAME", "gpt-5.4").strip() or "gpt-5.4"
    return OpenAICompatibleModelClient(base_url=base_url, model=model, logger=logger)


def create_app() -> CLI:
    logger = InMemoryLogger()
    policy = DefaultPolicy()
    guardrails = Guardrails(policy=policy, logger=logger)
    tools = build_builtin_registry(logger=logger)
    model = create_model_client(logger)
    engine = ExecutionEngine(model=model, tools=tools, guardrails=guardrails, logger=logger)
    presenter = ConsolePresenter()
    return CLI(engine=engine, presenter=presenter, logger=logger)


def main() -> None:
    try:
        parser = argparse.ArgumentParser(description="codexcli: LLM-driven CLI workbench scaffold")
        parser.add_argument("prompt", nargs="*", help="Task to send into the CLI workbench")
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single turn and exit instead of entering long conversation mode.",
        )
        args = parser.parse_args()

        app = create_app()
        cwd = os.getcwd()
        initial_prompt = " ".join(args.prompt).strip() or None

        if args.once:
            prompt = initial_prompt or input("codexcli> ").strip()
            if not prompt:
                return
            request = UserRequest(prompt=prompt, cwd=cwd)
            app.run(request)
            return

        app.chat(cwd=cwd, initial_prompt=initial_prompt)
    except KeyboardInterrupt:
        print()
        return
