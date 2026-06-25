from __future__ import annotations

from ..orchestration.session import SessionContext
from ..types import ToolDefinition


class ModelPromptBuilder:
    def build_messages(self, session: SessionContext) -> list[dict[str, object]]:
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
            "- If a tool result has error_code path_outside_workspace or approval_required for workspace_access, explain that access outside the current workspace needs user approval or a session started from that path.",
            "- If a tool result has error_code approval_required, explain that the tool needs approval instead of retrying it.",
            "- If a tool result has error_code duplicate_tool_call, use the previous result in session history or stop with a concise explanation.",
            "- If a tool result has error_code progress_required, do not call another read-only inspection tool. Use patch for requested file changes or finish with the reason no change is needed.",
            "- If the user asked for file changes and you already know the target project root and relevant files, stop inspecting and use patch.",
            "- For requests to create, generate, scaffold, or upgrade a standard project structure, after locating the target root, create or update the standard files directly instead of repeatedly listing directories.",
            "- Do not require a complete directory tree before making a reasonable scoped change.",
            "- shell_exec keeps shell state within the current run, including cd and exported environment variables.",
            "- Use shell_exec cwd when you need to start or reset the persistent shell working directory explicitly.",
            "- Prefer structured tools such as list_dir, find_files, read_file, search_text, and patch over shell_exec when they can do the job.",
            "- Do not retry a failed tool call with the same arguments unless the user gives new information.",
        ]

        active_skills = getattr(session, "active_skills", [])
        if active_skills:
            system_lines.append("")
            system_lines.append("### Active Skill Guidelines:")
            for skill in active_skills:
                system_lines.append("")
                system_lines.append(f"[Skill: {skill.metadata.name}]")
                system_lines.append(skill.content)

        system_lines.append("Available tools:")
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

    def build_tools(self, definitions: list[ToolDefinition]) -> list[dict[str, object]]:
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
