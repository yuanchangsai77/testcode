from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..orchestration.session import SessionContext
from ..types import ModelReply, ToolAction


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
                actions=[ToolAction(name="workspace_summary", arguments={"cwd": session.request.cwd})],
                done=False,
            )

        summary = session.tool_results[-1].output
        return ModelReply(
            message=(
                "codexcli architecture scaffold is ready. "
                f"Initial workspace inspection: {summary}"
            ),
            done=True,
        )


class OpenAICompatibleModelClient:
    """Minimal OpenAI-compatible chat client backed by the local proxy."""

    def __init__(self, base_url: str, model: str = "gpt-5.4", timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def respond(self, session: SessionContext) -> ModelReply:
        messages = self._build_messages(session)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        data = self._post_json(f"{self.base_url}/v1/chat/completions", payload)
        message = data["choices"][0]["message"]["content"]
        return self._parse_reply(self._normalize_content(message))

    def _build_messages(self, session: SessionContext) -> list[dict[str, object]]:
        system_lines = [
            "You are the model integration layer for codexcli.",
            "You must decide whether to answer directly or request tool calls.",
            "Always respond with strict JSON.",
            "Use exactly this schema:",
            '{"message":"string","done":true|false,"actions":[{"name":"tool_name","arguments":{"key":"value"}}]}',
            "Rules:",
            "- If you need more local context, set done to false and include one or more tool actions.",
            "- If you can answer the user, set done to true.",
            "- Do not use markdown fences.",
            "- Only use tool names from the provided tool list.",
            "- Keep message concise and user-facing.",
        ]

        user_lines = [
            f"Current working directory: {session.request.cwd}",
            f"User request: {session.request.prompt}",
            "Available tools:",
        ]

        for tool in session.available_tools:
            user_lines.append(f"- {tool.name}: {tool.description}")
            for name, description in tool.arguments.items():
                user_lines.append(f"  argument {name}: {description}")

        if session.history:
            user_lines.append("Session history:")
            user_lines.extend(f"- {item}" for item in session.history)

        return [
            {"role": "system", "content": "\n".join(system_lines)},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model request failed with HTTP {error.code}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Model request failed: {error.reason}") from error

        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Model response was not valid JSON: {body}") from error

        if "choices" not in data:
            raise RuntimeError(f"Model response missing choices: {data}")

        return data

    def _normalize_content(self, content: object) -> str:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        texts.append(text)
            return "\n".join(texts)

        raise RuntimeError(f"Unsupported model content format: {content!r}")

    def _parse_reply(self, content: str) -> ModelReply:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            candidate = self._extract_first_json_object(content)
            if candidate is None:
                return ModelReply(message=content, done=True)
            payload = json.loads(candidate)

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError(f"Model response missing message: {payload}")

        done = bool(payload.get("done", False))
        raw_actions = payload.get("actions", [])
        if not isinstance(raw_actions, list):
            raise RuntimeError(f"Model actions must be a list: {payload}")

        actions: list[ToolAction] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                raise RuntimeError(f"Model action must be an object: {item!r}")

            name = item.get("name")
            arguments = item.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"Model action missing tool name: {item!r}")
            if not isinstance(arguments, dict):
                raise RuntimeError(f"Model action arguments must be an object: {item!r}")

            actions.append(ToolAction(name=name, arguments=arguments))

        if actions and done:
            done = False

        return ModelReply(message=message, actions=actions, done=done)

    def _extract_first_json_object(self, content: str) -> str | None:
        start = content.find("{")
        if start < 0:
            return None

        depth = 0
        in_string = False
        escape = False

        for index in range(start, len(content)):
            char = content[index]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return content[start : index + 1]

        return None
