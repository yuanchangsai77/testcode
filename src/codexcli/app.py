from __future__ import annotations

import argparse
import os

from .interaction.cli import CLI
from .interaction.presenter import ConsolePresenter
from .model.client import OpenAICompatibleModelClient, StubModelClient
from .observability.logger import InMemoryLogger
from .orchestration.engine import ExecutionEngine
from .safety.guardrails import Guardrails
from .safety.policy import DefaultPolicy
from .tools.builtin import build_builtin_registry
from .types import UserRequest


def create_model_client() -> StubModelClient | OpenAICompatibleModelClient:
    base_url = os.getenv("CODEXCLI_MODEL_BASE_URL", "").strip()
    if not base_url:
        return StubModelClient()

    model = os.getenv("CODEXCLI_MODEL_NAME", "gpt-5.4").strip() or "gpt-5.4"
    return OpenAICompatibleModelClient(base_url=base_url, model=model)


def create_app() -> CLI:
    logger = InMemoryLogger()
    policy = DefaultPolicy()
    guardrails = Guardrails(policy=policy, logger=logger)
    tools = build_builtin_registry(logger=logger)
    model = create_model_client()
    engine = ExecutionEngine(model=model, tools=tools, guardrails=guardrails, logger=logger)
    presenter = ConsolePresenter()
    return CLI(engine=engine, presenter=presenter)


def main() -> None:
    parser = argparse.ArgumentParser(description="codexcli: LLM-driven CLI workbench scaffold")
    parser.add_argument("prompt", nargs="+", help="Task to send into the CLI workbench")
    args = parser.parse_args()

    request = UserRequest(prompt=" ".join(args.prompt), cwd=os.getcwd())
    app = create_app()
    app.run(request)
