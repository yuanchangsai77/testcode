from __future__ import annotations

import http.client
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..orchestration.session import SessionContext
from ..types import ModelReply, ToolAction
from . import types as model_types
from .parser import ModelReplyParser
from .prompt import ModelPromptBuilder


class StubModelClient:
    """
    Minimal placeholder model client.

    The first turn asks for a workspace summary tool. The second turn returns
    a final answer informed by the observed tool output.
    """

    def respond(self, session: SessionContext) -> ModelReply:
        if not session.tool_results:
            return ModelReply(
                message="I need workspace context before answering.",
                actions=[ToolAction(name="list_dir", arguments={"path": "."})],
                done=False,
            )

        summary = session.tool_results[-1].output
        return ModelReply(
            message=(
                "testcode architecture scaffold is ready. "
                f"Initial workspace inspection: {summary}"
            ),
            done=True,
        )


class OpenAICompatibleModelClient:
    """OpenAI-compatible chat client backed by the configured model proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str = "gpt-5.4",
        timeout: float = 60.0,
        logger=None,
        config: model_types.ModelClientConfig | None = None,
        prompt_builder: ModelPromptBuilder | None = None,
        parser: ModelReplyParser | None = None,
    ) -> None:
        if config is not None:
            base_url = config.base_url
            model = config.model
            timeout = config.timeout
        if base_url is None:
            raise ValueError("base_url is required")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.logger = logger
        self.prompt_builder = prompt_builder or ModelPromptBuilder()
        self.parser = parser or ModelReplyParser(logger=logger)
        self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def respond(self, session: SessionContext) -> ModelReply:
        messages = self.prompt_builder.build_messages(session)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        tools = self.prompt_builder.build_tools(session.available_tools)
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/v1/chat/completions"
        if self.logger is not None:
            self.logger.record(
                "model.request",
                {
                    "url": url,
                    "model": self.model,
                    "messages": messages,
                    "tools": [tool["function"]["name"] for tool in tools],
                },
            )
        data = self._post_json(url, payload)
        if self.logger is not None:
            self.logger.record("model.response", data)
        reply = self.parser.parse_response(
            data,
            allowed_tool_names={tool.name for tool in session.available_tools},
        )
        if self.logger is not None:
            self.logger.record(
                "model.parsed_reply",
                {
                    "message": reply.message,
                    "done": reply.done,
                    "metadata": reply.metadata,
                    "actions": [
                        {"name": action.name, "arguments": action.arguments}
                        for action in reply.actions
                    ],
                },
            )
        return reply

    def _post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        deadline = time.monotonic() + self.timeout
        try:
            with self._open_url(request, timeout=self.timeout) as response:
                body = self._read_response(response, deadline).decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if self.logger is not None:
                self.logger.record(
                    "model.http_error",
                    {"url": url, "status": error.code, "body": body},
                )
            message = f"Model request failed with HTTP {error.code}: {body}"
            if error.code in {408, 429} or 500 <= error.code <= 599:
                raise model_types.ModelServiceError(message) from error
            raise RuntimeError(message) from error
        except TimeoutError as error:
            if self.logger is not None:
                self.logger.record(
                    "model.timeout",
                    {"url": url, "timeout": self.timeout},
                )
            raise model_types.ModelTimeoutError(
                f"Model request timed out after {self.timeout:g} seconds"
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                if self.logger is not None:
                    self.logger.record(
                        "model.timeout",
                        {"url": url, "timeout": self.timeout},
                    )
                raise model_types.ModelTimeoutError(
                    f"Model request timed out after {self.timeout:g} seconds"
                ) from error
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "reason": str(error.reason)},
                )
            raise model_types.ModelConnectionError(
                f"Model request failed: {error.reason}"
            ) from error
        except http.client.RemoteDisconnected as error:
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "reason": str(error)},
                )
            raise model_types.ModelConnectionError(f"Model request failed: {error}") from error

        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            if self.logger is not None:
                self.logger.record("model.invalid_json", {"url": url, "body": body})
            raise RuntimeError(f"Model response was not valid JSON: {body}") from error

        if "choices" not in data:
            if self.logger is not None:
                self.logger.record("model.invalid_shape", {"url": url, "body": data})
            raise RuntimeError(f"Model response missing choices: {data}")

        return data

    def _open_url(self, request: urllib.request.Request, *, timeout: float):
        hostname = urllib.parse.urlsplit(request.full_url).hostname or ""
        if self._is_loopback_host(hostname):
            return self._direct_opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def _read_response(self, response, deadline: float) -> bytes:
        read1 = getattr(response, "read1", None)
        if not callable(read1):
            if time.monotonic() >= deadline:
                raise TimeoutError("model request deadline exceeded")
            return response.read()

        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model request deadline exceeded")
            self._set_response_timeout(response, remaining)
            chunk = read1(65_536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _set_response_timeout(self, response, timeout: float) -> None:
        candidates = [
            getattr(response, "fp", None),
            getattr(getattr(response, "fp", None), "raw", None),
        ]
        for candidate in candidates:
            sock = getattr(candidate, "_sock", None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(timeout)
                return

    def _is_loopback_host(self, hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

__all__ = [
    "OpenAICompatibleModelClient",
    "StubModelClient",
]
