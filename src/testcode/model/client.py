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
    """Minimal OpenAI-compatible chat client backed by the local proxy."""

    def __init__(self, base_url: str, model: str = "gpt-5.4", timeout: float = 60.0, logger=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.logger = logger

    def respond(self, session: SessionContext) -> ModelReply:
        messages = self._build_messages(session)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        url = f"{self.base_url}/v1/chat/completions"
        if self.logger is not None:
            self.logger.record(
                "model.request",
                {
                    "url": url,
                    "model": self.model,
                    "messages": messages,
                },
            )
        data = self._post_json(url, payload)
        if self.logger is not None:
            self.logger.record("model.response", data)
        message = data["choices"][0]["message"]["content"]
        reply = self._parse_reply(self._normalize_content(message))
        if self.logger is not None:
            self.logger.record(
                "model.parsed_reply",
                {
                    "message": reply.message,
                    "done": reply.done,
                    "actions": [
                        {"name": action.name, "arguments": action.arguments}
                        for action in reply.actions
                    ],
                },
            )
        return reply

    def _build_messages(self, session: SessionContext) -> list[dict[str, object]]:
        conversation = session.request.metadata.get("conversation", [])
        system_lines = [
            "You are the model integration layer for testcode.",
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
            "- Do not repeat the same tool call if the session history already contains the needed result.",
            "- If a tool result has error_code path_outside_workspace, explain that the path is outside the current workspace and ask the user to switch cwd or provide an in-workspace path.",
            "- If a tool result has error_code approval_required, explain that the tool needs approval instead of retrying it.",
            "- Do not retry a failed tool call with the same arguments unless the user gives new information.",
        ]

        user_lines = [
            f"Current working directory: {session.request.cwd}",
            f"User request: {session.request.prompt}",
            "Available tools:",
        ]

        if conversation:
            user_lines.append("Conversation history:")
            for item in conversation:
                if isinstance(item, dict):
                    role = str(item.get("role", "unknown"))
                    content = str(item.get("content", ""))
                    user_lines.append(f"- {role}: {content}")

        for tool in session.available_tools:
            user_lines.append(f"- {tool.name}: {tool.description}")
            user_lines.append(f"  risk: {tool.risk_level}")
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
            if self.logger is not None:
                self.logger.record(
                    "model.http_error",
                    {"url": url, "status": error.code, "body": body},
                )
            raise RuntimeError(f"Model request failed with HTTP {error.code}: {body}") from error
        except TimeoutError as error:
            if self.logger is not None:
                self.logger.record(
                    "model.timeout",
                    {"url": url, "timeout": self.timeout},
                )
            raise RuntimeError(f"Model request timed out after {self.timeout:g} seconds") from error
        except urllib.error.URLError as error:
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "reason": str(error.reason)},
                )
            raise RuntimeError(f"Model request failed: {error.reason}") from error

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
