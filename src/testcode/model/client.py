from __future__ import annotations

import json
import http.client
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import unescape

from ..orchestration.session import SessionContext
from ..types import ModelReply, ToolAction, ToolDefinition


@dataclass(frozen=True, slots=True)
class ModelClientConfig:
    base_url: str
    model: str = "gpt-5.4"
    timeout: float = 60.0


@dataclass(frozen=True, slots=True)
class CleanedContent:
    message: str
    thinking: str = ""
    had_protocol_tags: bool = False


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

    def __init__(
        self,
        base_url: str | None = None,
        model: str = "gpt-5.4",
        timeout: float = 60.0,
        logger=None,
        config: ModelClientConfig | None = None,
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

    def respond(self, session: SessionContext) -> ModelReply:
        messages = self._build_messages(session)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        tools = self._build_tools(session.available_tools)
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
        reply = self._parse_response(data, allowed_tool_names={tool.name for tool in session.available_tools})
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
            "- If native tool calls are available, use the API tool_calls field.",
            "- If you answer in content, use only the strict JSON schema above for tool actions.",
            "- Never emit XML, HTML, <invoke>, <tool_call>, or <parameter> tags.",
            "- Do not use markdown fences.",
            "- Only use tool names from the provided tool list.",
            "- Keep message concise and user-facing.",
            "- Do not repeat the same tool call if the session history already contains the needed result.",
            "- If a tool result has error_code path_outside_workspace, explain that the path is outside the current workspace and ask the user to switch cwd or provide an in-workspace path.",
            "- If a tool result has error_code approval_required, explain that the tool needs approval instead of retrying it.",
            "- Do not retry a failed tool call with the same arguments unless the user gives new information.",
            "Available tools:",
        ]
        system_lines.extend(self._format_tool_definitions(session))

        user_lines = [
            f"Current working directory: {session.request.cwd}",
            f"User request: {session.request.prompt}",
        ]

        if session.history:
            user_lines.append("Session history:")
            user_lines.extend(f"- {item}" for item in session.history)

        return [
            {"role": "system", "content": "\n".join(system_lines)},
            *self._format_conversation_messages(conversation),
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _format_conversation_messages(self, conversation: object) -> list[dict[str, object]]:
        if not isinstance(conversation, list):
            return []

        messages: list[dict[str, object]] = []
        for item in conversation:
            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
                continue

            messages.append({"role": role, "content": content})

        return messages

    def _format_tool_definitions(self, session: SessionContext) -> list[str]:
        lines: list[str] = []
        for tool in sorted(session.available_tools, key=lambda item: item.name):
            lines.append(f"- {tool.name}: {tool.description}")
            lines.append(f"  risk: {tool.risk_level}")
            for name in sorted(tool.arguments):
                lines.append(f"  argument {name}: {tool.arguments[name]}")
        return lines

    def _build_tools(self, definitions: list[ToolDefinition]) -> list[dict[str, object]]:
        tools: list[dict[str, object]] = []
        for definition in sorted(definitions, key=lambda item: item.name):
            parameters = definition.input_schema or self._schema_from_arguments(definition)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": parameters,
                    },
                }
            )
        return tools

    def _schema_from_arguments(self, definition: ToolDefinition) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                name: {"type": "string", "description": description}
                for name, description in sorted(definition.arguments.items())
            },
            "required": [],
            "additionalProperties": False,
        }

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
        except http.client.RemoteDisconnected as error:
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "reason": str(error)},
                )
            raise RuntimeError(f"Model request failed: {error}") from error

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

    def _parse_response(self, data: dict[str, object], *, allowed_tool_names: set[str] | None = None) -> ModelReply:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(f"Model response missing choices: {data}")

        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError(f"Model choice must be an object: {first!r}")

        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"Model response missing message: {data}")

        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            if not isinstance(raw_tool_calls, list):
                raise RuntimeError(f"Model tool_calls must be a list: {message!r}")
            content = self._normalize_nullable_content(message.get("content"))
            cleaned = self._clean_content(content)
            actions = self._parse_tool_calls(raw_tool_calls, allowed_tool_names=allowed_tool_names)
            return ModelReply(
                message=cleaned.message or "Model requested tool calls.",
                actions=actions,
                done=False,
                metadata=self._cleaned_metadata(cleaned),
            )

        if "content" not in message:
            raise RuntimeError(f"Model response missing content: {message!r}")

        content = self._normalize_content(message.get("content"))
        if not content.strip():
            raise RuntimeError(f"Model response content was empty: {message!r}")
        return self._parse_reply(content, allowed_tool_names=allowed_tool_names)

    def _normalize_nullable_content(self, content: object) -> str:
        if content is None:
            return ""
        return self._normalize_content(content)

    def _parse_tool_calls(
        self,
        raw_tool_calls: list[object],
        *,
        allowed_tool_names: set[str] | None = None,
    ) -> list[ToolAction]:
        actions: list[ToolAction] = []
        for item in raw_tool_calls:
            if not isinstance(item, dict):
                raise RuntimeError(f"Model tool_call must be an object: {item!r}")

            if item.get("type", "function") != "function":
                raise RuntimeError(f"Unsupported tool_call type: {item!r}")

            function = item.get("function")
            if not isinstance(function, dict):
                raise RuntimeError(f"Model tool_call missing function: {item!r}")

            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"Model tool_call missing function name: {item!r}")
            self._validate_tool_name(name, allowed_tool_names)

            raw_arguments = function.get("arguments", {})
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"Model tool_call arguments were not valid JSON: {item!r}") from error
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise RuntimeError(f"Model tool_call arguments must be an object: {item!r}")

            if not isinstance(arguments, dict):
                raise RuntimeError(f"Model tool_call arguments must decode to an object: {item!r}")

            actions.append(ToolAction(name=name, arguments=arguments))

        return actions

    def _parse_reply(self, content: str, *, allowed_tool_names: set[str] | None = None) -> ModelReply:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            content_actions = self._parse_content_tool_calls(content, allowed_tool_names=allowed_tool_names)
            if content_actions:
                cleaned = self._clean_content(content)
                return ModelReply(
                    message=cleaned.message or "Model requested tool calls.",
                    actions=content_actions,
                    done=False,
                    metadata=self._cleaned_metadata(cleaned),
                )

            candidate = self._extract_first_json_object(content)
            if candidate is None:
                cleaned = self._clean_content(content)
                return ModelReply(
                    message=cleaned.message or content,
                    done=True,
                    metadata=self._cleaned_metadata(cleaned),
                )
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                if self.logger is not None:
                    self.logger.record(
                        "model.invalid_reply_json",
                        {"content": content, "candidate": candidate},
                    )
                cleaned = self._clean_content(content)
                return ModelReply(
                    message=cleaned.message or content,
                    done=True,
                    metadata=self._cleaned_metadata(cleaned),
                )

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
            self._validate_tool_name(name, allowed_tool_names)
            if not isinstance(arguments, dict):
                raise RuntimeError(f"Model action arguments must be an object: {item!r}")

            actions.append(ToolAction(name=name, arguments=arguments))

        if actions and done:
            done = False

        cleaned = self._clean_content(message)
        return ModelReply(
            message=cleaned.message or message,
            actions=actions,
            done=done,
            metadata=self._cleaned_metadata(cleaned),
        )

    def _validate_tool_name(self, name: str, allowed_tool_names: set[str] | None) -> None:
        if allowed_tool_names is not None and name not in allowed_tool_names:
            raise RuntimeError(f"Model requested unknown tool: {name}")

    def _parse_content_tool_calls(
        self,
        content: str,
        *,
        allowed_tool_names: set[str] | None = None,
    ) -> list[ToolAction]:
        tool_matches = list(re.finditer(r'(?:<[\w:.-]+[^>]*\btool="([^"]+)"[^>]*>|tool="([^"]+)")', content))
        actions: list[ToolAction] = []
        for index, match in enumerate(tool_matches):
            name = match.group(1) or match.group(2)
            self._validate_tool_name(name, allowed_tool_names)
            end = tool_matches[index + 1].start() if index + 1 < len(tool_matches) else len(content)
            block = content[match.end() : end]
            arguments = {}
            for parameter in re.finditer(
                r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>',
                block,
                flags=re.DOTALL,
            ):
                arguments[parameter.group(1)] = unescape(parameter.group(2)).strip()
            actions.append(ToolAction(name=name, arguments=arguments))
        return actions

    def _clean_content(self, content: str) -> CleanedContent:
        thinking_parts = [
            unescape(match.group(1)).strip()
            for match in re.finditer(r"<think\b[^>]*>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
            if match.group(1).strip()
        ]
        without_think = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
        without_parameters = re.sub(
            r"<parameter\s+name=\"[^\"]+\"\s*>.*?</parameter>",
            "",
            without_think,
            flags=re.DOTALL | re.IGNORECASE,
        )
        without_tags = re.sub(r"</?[\w:.-]+[^>]*>", "", without_parameters)
        without_tool_attrs = re.sub(r'-?\s*tool="[^"]+"\s*>?', "", without_tags)
        message = " ".join(unescape(without_tool_attrs).split())
        return CleanedContent(
            message=message,
            thinking="\n".join(thinking_parts),
            had_protocol_tags=self._has_protocol_tags(content),
        )

    def _has_protocol_tags(self, content: str) -> bool:
        return re.search(
            r"</?(?:think|invoke|tool_call|[\w.-]+:tool_call|parameter)\b|tool=\"[^\"]+\"",
            content,
            flags=re.IGNORECASE,
        ) is not None

    def _cleaned_metadata(self, cleaned: CleanedContent) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if cleaned.thinking:
            metadata["thinking"] = cleaned.thinking
        if cleaned.had_protocol_tags:
            metadata["cleaned_protocol_tags"] = True
        return metadata

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
