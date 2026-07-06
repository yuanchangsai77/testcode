from __future__ import annotations

import json
import re
from html import unescape

from ..types import ModelReply, ToolAction
from .types import CleanedContent


class ModelReplyParser:
    def __init__(self, logger=None) -> None:
        self.logger = logger

    def parse_response(self, data: dict[str, object], *, allowed_tool_names: set[str] | None = None) -> ModelReply:
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
            actions = self.parse_tool_calls(raw_tool_calls, allowed_tool_names=allowed_tool_names)
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
        return self.parse_reply(content, allowed_tool_names=allowed_tool_names)

    def parse_tool_calls(
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

    def parse_reply(self, content: str, *, allowed_tool_names: set[str] | None = None) -> ModelReply:
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
                if self._looks_like_json_reply(content):
                    return ModelReply(
                        message=(
                            "Model response was invalid JSON. Return strict JSON with "
                            "message, done, and actions fields only."
                        ),
                        done=False,
                        metadata={"invalid_reply_json": True},
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

    def _normalize_nullable_content(self, content: object) -> str:
        if content is None:
            return ""
        return self._normalize_content(content)

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
        unescaped = unescape(without_tool_attrs)
        
        # Clean up lines to avoid duplicate consecutive empty lines while preserving line breaks
        lines = unescaped.splitlines()
        cleaned_lines = []
        for line in lines:
            line_str = line.strip()
            if line_str:
                cleaned_lines.append(line_str)
            else:
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
        message = "\n".join(cleaned_lines).strip()
        
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

    def _looks_like_json_reply(self, content: str) -> bool:
        without_think = re.sub(
            r"^\s*(?:<think\b[^>]*>.*?</think>\s*)+",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return without_think.lstrip().startswith("{")

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
