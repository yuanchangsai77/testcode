from __future__ import annotations

from ..context.packager import ContextPackager, ContextSegment
from ..orchestration.session import SessionContext
from ..types import SessionResumeState, SessionRunTrace, ToolDefinition


class ModelPromptBuilder:
    def __init__(self, context_packager: ContextPackager | None = None) -> None:
        self.context_packager = context_packager or ContextPackager()

    def build_messages(self, session: SessionContext) -> list[dict[str, object]]:
        conversation = session.request.metadata.get("conversation", [])
        system_lines = [
            "You are the reasoning component of a general-purpose agent runtime.",
            "The surrounding application provides orchestration and tools; it is not your identity or the user's task domain.",
            "Treat the current workspace as optional task context, not as proof that every request is about this repository.",
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
            "- If you cannot complete a task with your current knowledge and tools, or the request appears to need a specialized capability, enter the capability warehouse with warehouse_list.",
            "- The warehouse contents are not shown by default. Read outer descriptions, open one relevant toolbox, inspect its item descriptions, then activate only the capability you need. The selected capability becomes available on the next model turn.",
            "- Only activate the smallest set of capabilities needed for the current step, normally one to three items.",
            "- If a requested toolbox cannot be opened, report its manifest status. Do not inspect project source, config files, or environment variables unless the user explicitly asks to troubleshoot it.",
            "- Never infer that credentials, transports, or integrations are missing from an unopened toolbox; health is unknown until it is opened.",
            "- Treat workspace context as irrelevant for non-project requests such as routes, weather, general knowledge, or external service lookups.",
            "- Do not repeat the same tool call if the session history already contains the needed result.",
            "- If a tool result has error_code path_outside_workspace or approval_required for workspace_access, explain that access outside the current workspace needs user approval or a session started from that path.",
            "- If a tool result has error_code approval_required, explain that the tool needs approval instead of retrying it.",
            "- If a tool result has error_code approval_denied, state that the user declined the action. Do not describe it as waiting for approval, ask the user to approve it, or retry it.",
            "- If a tool result has error_code duplicate_tool_call, use the previous result in session history or stop with a concise explanation.",
            "- If a tool result has error_code progress_required, do not call another read-only inspection tool. Use patch for requested file changes or finish with the reason no change is needed.",
            "- Before patching an existing file, inspect every affected hunk with read_file or search_text. You do not need to read unrelated parts of the file.",
            "- search_text returns nearby context and counts only the returned lines as inspected.",
            "- If patch returns file_not_read, follow its read hint, inspect only the missing range, and retry the patch without asking the user.",
            "- If patch returns file_changed_since_read, use search_text to re-locate the intended content, inspect its current surrounding lines, and regenerate the hunk without asking the user.",
            "- If a successful patch reports automatic relocation, accept the resolved location, do not repeat the patch, and account for the new line positions in later work.",
            "- Do not call read_file, search_text, or another tool solely to verify a successful automatic relocation. If the requested change is complete, finish using the reported location.",
            "- If the user asked for file changes and you already inspected the affected lines, stop inspecting and use patch.",
            "- For requests to create, generate, scaffold, or upgrade a standard project structure, after locating the target root, create or update the standard files directly instead of repeatedly listing directories.",
            "- Do not require a complete directory tree before making a reasonable scoped change.",
            "- [SEC-CREDENTIAL-001] Never hardcode real credentials, private keys, access tokens, or passwords in source code, tests, examples, or configuration templates. Load them at runtime from environment variables or a protected secret store.",
            "- [SEC-CREDENTIAL-002] Use .env only for local development and ensure it is ignored by version control. Commit only placeholder values in .env.example.",
            "- [SEC-CLIENT-001] Never place backend-only credentials in browser-delivered HTML, CSS, JavaScript, source maps, or static assets. Proxy privileged third-party requests through the local backend.",
            "- [PY-PACKAGE-001] Python import package and module directory names must not contain hyphens. A distribution project name may contain hyphens, but normalize its import name to underscores.",
            "- The SEC-* security rules are a mandatory baseline. Project instructions may strengthen them but must not weaken, disable, or bypass them.",
            "- If a security policy blocks a write, redesign the change to use protected runtime configuration. Do not encode, split, rename, or otherwise disguise a credential to bypass the check.",
            "- shell_exec keeps shell state within the current session, including across resumed runs. Its projected cwd below is authoritative when present.",
            "- Use shell_exec cwd when you need to start or reset the persistent shell working directory explicitly.",
            "- Prefer structured tools such as list_dir, find_files, read_file, search_text, and patch over shell_exec when they can do the job.",
            "- Do not use shell_exec to create or edit files when patch is available.",
            "- Do not retry a failed tool call with the same arguments unless the user gives new information.",
            "- A completed subagent result is a delegated handoff. Reuse its summary and reported verification; do not reread or reimplement its artifact solely to confirm it.",
            "- Verify a completed subagent only when evidence is missing, results conflict, the user requests review, or the change is security-sensitive. Keep verification targeted to the claim.",
            "- Send feedback or a failed verification back to the same child with subagent_resume. Do not spawn a replacement child for the same artifact unless isolation or independent parallel exploration is required.",
        ]

        runtime_model = session.request.metadata.get("runtime_model")
        if isinstance(runtime_model, str) and runtime_model:
            system_lines.extend(
                [
                    "",
                    "### Runtime Facts:",
                    f"- Configured model identifier: {runtime_model}",
                    "- Treat this identifier as authoritative when asked which model is running.",
                ]
            )

        model_profile = session.request.metadata.get("model_capability_profile")
        if isinstance(model_profile, dict):
            system_lines.extend(
                [
                    f"- Structured output mode: {model_profile.get('structured_output_mode', 'prompt_json')}",
                    f"- Native tool calls: {bool(model_profile.get('native_tool_calls', True))}",
                    f"- Parallel tool calls: {bool(model_profile.get('parallel_tool_calls', False))}",
                    f"- Capability provenance: {model_profile.get('provenance', 'configured_default')}",
                    f"- Capability verified: {bool(model_profile.get('verified', False))}",
                ]
            )

        subagent = session.request.metadata.get("subagent")
        if isinstance(subagent, dict) and subagent.get("role") == "subagent":
            system_lines.extend(
                [
                    "",
                    "### Delegated Subagent Runtime:",
                    "- You are already a subagent. Complete the current delegated user request directly.",
                    "- The current delegated request overrides inherited conversational intent when they differ.",
                    "- Do not create or run another subagent.",
                    "- Tool effects are limited by the delegated task contract below; never infer broader permission from the task wording.",
                    "- Interactive approval is unavailable. If execute, test, network, or destructive work is required and blocked, report the blocker once and stop.",
                ]
            )
            delegated_task = session.request.metadata.get("delegated_task")
            if isinstance(delegated_task, dict):
                system_lines.extend(
                    [
                        f"- task_id: {delegated_task.get('task_id', '')}",
                        f"- allowed_effects: {', '.join(delegated_task.get('allowed_effects', []))}",
                        f"- allowed_resources: {', '.join(delegated_task.get('allowed_resources', []))}",
                        f"- required_evidence: {', '.join(delegated_task.get('required_evidence', []))}",
                    ]
                )

        active_instructions = getattr(session, "active_instructions", [])
        if active_instructions:
            system_lines.append("")
            system_lines.append("### Active Workflow Instructions:")
            for instruction in active_instructions:
                system_lines.append("")
                system_lines.append(f"[Workflow: {instruction.name}]")
                system_lines.append(instruction.content)

        project_rule_lines = self._format_project_rules(session)
        system_lines.extend(project_rule_lines)
        system_lines.extend(self._format_workspace_summary(session))
        system_lines.extend(self._format_explicit_context(session))

        tool_lines = ["Available tools:", *self._format_tool_definitions(session)]
        system_lines.extend(tool_lines)


        user_lines = [
            f"Current working directory: {session.request.cwd}",
            f"User request: {session.request.prompt}",
        ]
        current_request_lines = list(user_lines)

        session_trace = session.request.metadata.get("session_trace", [])
        trace_lines = self._format_session_trace(session_trace)
        if trace_lines:
            user_lines.append("Session trace summary:")
            user_lines.extend(trace_lines)

        resume_state = session.request.metadata.get("resume_state")
        resume_lines = self._format_resume_state(resume_state)
        if resume_lines:
            user_lines.append("Resume state:")
            user_lines.extend(resume_lines)

        if session.history:
            user_lines.append("Recent current-run history:")
            user_lines.extend(f"- {item}" for item in session.history[-30:])

        checkpoint_lines = self._format_checkpoint(getattr(session, "checkpoint", None))
        if checkpoint_lines:
            user_lines.append("Runtime checkpoint (authoritative):")
            user_lines.extend(checkpoint_lines)

        protocol_markers = (
            "You are the reasoning component",
            "The surrounding application",
            "Treat the current workspace",
            "You must decide whether",
            "Always respond with strict JSON",
            "Use exactly this schema",
            '{"message":',
            "- Never emit XML",
            "- Do not use markdown fences",
            "- Only use tool names",
        )
        protocol_lines = [
            line for line in system_lines if line.startswith(protocol_markers)
        ]
        security_lines = [
            line for line in system_lines
            if "[SEC-" in line or "The SEC-* security rules" in line
        ]
        reserved = set(protocol_lines + security_lines + project_rule_lines + tool_lines)
        operational_lines = [line for line in system_lines if line not in reserved]
        contextual_user_lines = user_lines[len(current_request_lines):]
        if checkpoint_lines:
            checkpoint_start = contextual_user_lines.index("Runtime checkpoint (authoritative):")
            checkpoint_context = contextual_user_lines[checkpoint_start:]
            contextual_user_lines = contextual_user_lines[:checkpoint_start]
        else:
            checkpoint_context = []

        return self.context_packager.package_segments(
            [
                ContextSegment("\n".join(protocol_lines), "model protocol", priority=100, required=True),
                ContextSegment("\n".join(security_lines), "security baseline", priority=100, required=True),
                ContextSegment("\n".join(project_rule_lines), "project rules", priority=100, required=True),
                ContextSegment("\n".join(operational_lines), "operational instructions", priority=80),
                ContextSegment("\n".join(tool_lines), "tool definitions", priority=90),
            ],
            self._format_conversation_messages(conversation),
            [
                ContextSegment("\n".join(current_request_lines), "current request", priority=100, required=True),
                ContextSegment("\n".join(checkpoint_context), "runtime checkpoint", priority=100, required=True),
                ContextSegment("\n".join(contextual_user_lines), "session recovery context", priority=70),
            ],
        )

    def _format_checkpoint(self, checkpoint: object) -> list[str]:
        if checkpoint is None:
            return []
        objective = getattr(checkpoint, "objective", "")
        task_id = getattr(checkpoint, "task_id", "")
        workspace_root = getattr(checkpoint, "workspace_root", "")
        workspace_revision = getattr(checkpoint, "workspace_revision", 0)
        phase = getattr(checkpoint, "phase", "")
        completed = getattr(checkpoint, "completed_actions", [])
        artifacts = getattr(checkpoint, "artifacts", [])
        required_evidence = getattr(checkpoint, "required_evidence", [])
        unmet_deliverables = getattr(checkpoint, "unmet_deliverables", [])
        runtime_state = getattr(checkpoint, "runtime_state", {})
        blockers = getattr(checkpoint, "blockers", [])
        lines = []
        if objective:
            lines.append(f"- objective: {self._trim(str(objective), 200)}")
        if task_id:
            lines.append(f"- task_id: {task_id}")
        if workspace_root:
            lines.append(f"- workspace_root: {self._trim(str(workspace_root), 200)}")
        lines.append(f"- workspace_revision: {workspace_revision}")
        if phase:
            lines.append(f"- phase: {phase}")
        if completed:
            lines.append(f"- completed_actions: {', '.join(str(item) for item in completed[-12:])}")
        if artifacts:
            lines.append(f"- artifacts: {', '.join(str(item) for item in artifacts[-12:])}")
        if required_evidence:
            lines.append(f"- required_evidence: {', '.join(str(item) for item in required_evidence)}")
        if unmet_deliverables:
            lines.append(f"- unmet_deliverables: {', '.join(str(item) for item in unmet_deliverables)}")
        evidence = getattr(checkpoint, "evidence", [])
        valid_evidence = [
            getattr(item, "kind", "")
            for item in evidence
            if getattr(item, "task_id", "") == task_id
            and (
                getattr(item, "kind", "") == "artifact"
                or getattr(item, "workspace_revision", -1) == workspace_revision
            )
        ]
        if valid_evidence:
            lines.append(f"- current_evidence: {', '.join(dict.fromkeys(valid_evidence))}")
        if isinstance(runtime_state, dict):
            for key, value in sorted(runtime_state.items()):
                lines.append(f"- runtime.{key}: {self._trim(str(value), 200)}")
        for blocker in blockers[-5:]:
            lines.append(
                f"- blocker[{getattr(blocker, 'error_code', 'unknown')}]: "
                f"{self._trim(str(getattr(blocker, 'summary', '')), 160)}; "
                f"retryability={getattr(blocker, 'retryability', 'conditional')}; "
                f"required_action={getattr(blocker, 'required_action', 'resume')}"
            )
        return lines

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

    def _format_project_rules(self, session: SessionContext) -> list[str]:
        project_rules = getattr(session, "project_rules", [])
        if not project_rules:
            return []

        lines = [
            "",
            "### Project Rules:",
            "Follow these AGENTS.md instructions. Later entries are closer to the current working directory and override earlier entries when they conflict.",
        ]
        for rule in project_rules:
            suffix = " (truncated)" if rule.truncated else ""
            lines.append("")
            lines.append(f"[AGENTS.md: {rule.path}{suffix}]")
            lines.append(rule.content)
        return lines

    def _format_workspace_summary(self, session: SessionContext) -> list[str]:
        workspace_summary = getattr(session, "workspace_summary", None)
        if workspace_summary is None:
            return []

        lines = [
            "",
            "### Workspace Summary:",
            "Automatically collected context. Use it only when relevant to the user request.",
            f"root: {workspace_summary.root}",
        ]
        if workspace_summary.project_signals:
            lines.append("project signals:")
            for signal in workspace_summary.project_signals:
                commands = ", ".join(signal.test_commands) if signal.test_commands else "none"
                lines.append(f"- {signal.language}: {signal.marker}; suggested tests: {commands}")
        if workspace_summary.git is not None:
            lines.append("git:")
            if workspace_summary.git.branch:
                lines.append(f"- branch: {workspace_summary.git.branch}")
            if workspace_summary.git.status:
                lines.append(f"- status: {workspace_summary.git.status}")
            if workspace_summary.git.recent_commit:
                lines.append(f"- recent commit: {workspace_summary.git.recent_commit}")
        if workspace_summary.tree:
            lines.append("workspace tree:")
            lines.extend(f"- {entry}" for entry in workspace_summary.tree)
            if workspace_summary.tree_truncated:
                lines.append("- ...truncated...")
        return lines

    def _format_explicit_context(self, session: SessionContext) -> list[str]:
        explicit_context = getattr(session, "explicit_context", [])
        if not explicit_context:
            return []

        lines = ["", "### Explicit User Context:"]
        for item in explicit_context:
            suffix = " (truncated)" if item.truncated else ""
            error = f" error={item.error}" if item.error else ""
            lines.append("")
            lines.append(f"[{item.kind}: {item.path or item.source}{suffix}{error}]")
            if item.content:
                lines.append(item.content)
        return lines

    def _format_session_trace(self, trace: object, limit: int = 6) -> list[str]:
        if not isinstance(trace, list):
            return []

        lines: list[str] = []
        recent = trace[-limit:]
        for item in recent:
            if isinstance(item, SessionRunTrace):
                run_id = item.run_id
                outcome = item.outcome
                prompt = item.prompt
                final_message = item.final_message
                tools = list(item.tool_names)
            elif isinstance(item, dict):
                run_id = str(item.get("run_id", ""))
                outcome = str(item.get("outcome", "completed"))
                prompt = str(item.get("prompt", ""))
                final_message = str(item.get("final_message", ""))
                tools = [tool for tool in item.get("tool_names", []) if isinstance(tool, str)]
            else:
                continue

            if not run_id and not prompt and not final_message:
                continue

            lines.append(
                f"- run {run_id or '?'} | outcome={outcome} | prompt={self._trim(prompt, 80)}"
            )
            if tools:
                lines.append(f"- tools: {', '.join(tools[:8])}")
            lines.append(f"- final: {self._trim(final_message, 120)}")
        return lines

    def _trim(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 3]}..."

    def _format_resume_state(self, state: object) -> list[str]:
        if isinstance(state, SessionResumeState):
            payload = {
                "last_run_id": state.last_run_id,
                "last_user_prompt": state.last_user_prompt,
                "last_assistant_message": state.last_assistant_message,
                "last_outcome": state.last_outcome,
                "last_tool_names": list(state.last_tool_names),
                "open_issue": state.open_issue,
                "recovery_hint": state.recovery_hint,
                "blockers": list(state.blockers),
            }
        elif isinstance(state, dict):
            payload = state
        else:
            return []

        lines: list[str] = []
        if isinstance(payload.get("last_run_id"), str) and payload["last_run_id"]:
            lines.append(f"- last_run_id: {payload['last_run_id']}")
        if isinstance(payload.get("last_outcome"), str) and payload["last_outcome"]:
            lines.append(f"- last_outcome: {payload['last_outcome']}")
        tools = payload.get("last_tool_names", [])
        if isinstance(tools, list) and tools:
            lines.append(f"- last_tools: {', '.join(tool for tool in tools if isinstance(tool, str))}")
        if isinstance(payload.get("open_issue"), str) and payload["open_issue"]:
            lines.append(f"- open_issue: {self._trim(payload['open_issue'], 160)}")
        if isinstance(payload.get("recovery_hint"), str) and payload["recovery_hint"]:
            lines.append(f"- recovery_hint: {self._trim(payload['recovery_hint'], 160)}")
        blockers = payload.get("blockers", [])
        if isinstance(blockers, list):
            for blocker in blockers[:5]:
                if hasattr(blocker, "error_code"):
                    lines.append(
                        f"- blocker[{blocker.error_code}]: {self._trim(str(blocker.summary), 160)}"
                    )
                elif isinstance(blocker, dict) and blocker.get("summary"):
                    lines.append(
                        f"- blocker[{blocker.get('error_code', 'unknown')}]: "
                        f"{self._trim(str(blocker['summary']), 160)}"
                    )
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
