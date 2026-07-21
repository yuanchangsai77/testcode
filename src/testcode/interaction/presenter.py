from __future__ import annotations

import json
import re
import sys
from html import unescape

from ..types import ExecutionSummary, SessionRecord, StoredSession, ToolResult, UserRequest
from .input import PromptBox, StatusBar
from .terminal import Spinner, colored_border


class ConsolePresenter:
    max_tool_output = 120

    def __init__(self, tool_result_summarizer=None) -> None:
        self.tool_result_summarizer = tool_result_summarizer
        self.status_bar = StatusBar()
        self.prompt_box = PromptBox(self.status_bar)

    def _print(self, value: str = "") -> None:
        print(value)

    def _print_many(self, values: list[str]) -> None:
        for value in values:
            print(value)

    def show_start(self, request: UserRequest) -> None:
        pass

    def show_summary(self, summary: ExecutionSummary) -> None:
        thinking = self._extract_thinking(summary.final_message)
        
        GRAY = "\033[90m"
        RESET = "\033[0m"
        
        if thinking:
            self._print(f"\n {GRAY}› thinking:{RESET}")
            indented_thinking = "\n".join(f"   {line}" for line in thinking.splitlines())
            self._print(f"{GRAY}{indented_thinking}{RESET}")
            
        response_text = self._display_text(summary.final_message)
        try:
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.padding import Padding

            console = Console(file=getattr(self, "_output", sys.stdout))
            console.print(Padding(Markdown(response_text), (0, 0, 0, 3)))
            self._print()
        except ImportError:
            indented_response = "\n".join(f"   {line}" for line in response_text.splitlines())
            self._print(f"{indented_response}\n")

    def _summarize_tool_result(self, result: ToolResult) -> str:
        if self.tool_result_summarizer is not None:
            summary = self.tool_result_summarizer(result)
            if isinstance(summary, str) and summary.strip() and summary != result.output:
                text = self._summarize_tool_output(summary)
                return f"{result.error_code}: {text}" if result.error_code else text

        if not result.success:
            if result.error_code:
                return f"{result.error_code}: {self._summarize_tool_output(result.output)}"
            return self._summarize_tool_output(result.output)

        return self._summarize_tool_output(result.output)

    def confirm_tool_action(self, action, reason: str) -> bool:
        CYAN = "\033[1;36m"
        YELLOW = "\033[1;33m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        self._print(f"\n {YELLOW}•{RESET} {BOLD}Requesting permission for:{RESET} {CYAN}{action.name}{RESET}")
        self._print(f"   {BOLD}Reason:{RESET} {reason}")
        
        if action.arguments:
            arguments = json.dumps(action.arguments, ensure_ascii=False)
            if len(arguments) > 80:
                arguments = json.dumps(action.arguments, ensure_ascii=False, indent=2)
            indented_args = "\n".join(f"      {line}" for line in arguments.splitlines())
            self._print(f"   {BOLD}Arguments:{RESET}\n{indented_args}")
            
        if action.name == "patch" and isinstance(action.arguments.get("diff"), str):
            self._print(f"   {BOLD}Patch Preview:{RESET}")
            indented_diff = "\n".join(f"      {line}" for line in action.arguments["diff"].splitlines())
            self._print(indented_diff)

        self._print(f"   {BOLD}Do you want to proceed?{RESET}")

        engine = getattr(self, "engine", None)
        choice = self.prompt_box.read_selection(engine=engine, options=("Yes", "No"))
        return choice in {"1", "y", "yes"}

    def _summarize_tool_output(self, output: str) -> str:
        single_line = " ".join(str(output).split())
        if len(single_line) <= self.max_tool_output:
            return single_line
        return f"{single_line[: self.max_tool_output - 3]}..."

    def _display_text(self, value: str) -> str:
        without_think = re.sub(r"<think\b[^>]*>.*?</think>", "", str(value), flags=re.DOTALL | re.IGNORECASE)
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
        return "\n".join(cleaned_lines).strip()

    def _extract_thinking(self, value: str) -> str:
        parts = [
            unescape(match.group(1)).strip()
            for match in re.finditer(r"<think\b[^>]*>(.*?)</think>", str(value), flags=re.DOTALL | re.IGNORECASE)
            if match.group(1).strip()
        ]
        return "\n".join(parts)

    def show_session_state(self, session: StoredSession, resumed: bool, engine=None) -> None:
        # Standard ANSI colors
        CYAN = "\033[1;36m"
        GREEN = "\033[1;32m"
        YELLOW = "\033[1;33m"
        MAGENTA = "\033[1;35m"
        GRAY = "\033[90m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        logo = f"""
{CYAN}  _            _                 _                 
 | |_ ___  ___| |_ ___  ___   __| | ___ 
 | __/ _ \\/ __| __/ __|/ _ \\ / _` |/ _ \\
 | ||  __/\\__ \\ || (__| (_) | (_| |  __/
  \\__\\___||___/\\__\\___|\\___/ \\__,_|\\___|{RESET}
"""
        import sys
        import platform
        import os

        # Try to detect if we are in a virtual environment
        venv_name = None
        if sys.prefix != sys.base_prefix:
            venv_name = os.path.basename(sys.prefix)
        else:
            for possible_venv in (".venv", "venv", "env"):
                if os.path.isdir(os.path.join(session.cwd, possible_venv)):
                    venv_name = possible_venv
                    break

        env_str = f"Python {sys.version.split()[0]}"
        if venv_name:
            env_str += f" ({venv_name})"
        env_str += f" on {platform.system()}"

        action_str = "Resumed" if resumed else "Started"
        session_id_colored = f"{YELLOW}{session.session_id}{RESET}"

        # Safety Mode Coloring
        mode_val = "confirm"
        if engine and hasattr(engine, "guardrails") and hasattr(engine.guardrails, "policy"):
            mode_val = getattr(engine.guardrails.policy, "mode", "confirm")

        if mode_val == "auto":
            mode_colored = f"{MAGENTA}auto{RESET} (Tool calls run automatically)"
        elif mode_val == "readonly":
            mode_colored = f"{GREEN}readonly{RESET} (No write operations allowed)"
        else:
            mode_colored = f"{YELLOW}confirm{RESET} (Tool calls require approval)"

        # Border Line
        border = colored_border()

        output = [
            logo,
            border,
            f" {GRAY}›{RESET} {BOLD}Workspace:{RESET}   {session.cwd}",
            f" {GRAY}›{RESET} {BOLD}Session:{RESET}     {action_str} - {session_id_colored}",
            f" {GRAY}›{RESET} {BOLD}Safety Mode:{RESET} {mode_colored}",
            f" {GRAY}›{RESET} {BOLD}System:{RESET}      {env_str}",
            border,
        ]

        # Separate immediately usable runtime components from lazily activated
        # capabilities so the startup summary does not imply everything is loaded.
        if engine:
            loaders_count = len(getattr(engine, "context_loaders", []))
            tools_count = len(getattr(engine.tools, "_tools", {})) if hasattr(engine, "tools") else 0
            skills_count = len(getattr(engine.skills_registry, "_skills", {})) if hasattr(engine, "skills_registry") else 0
            mcp_server_count = getattr(engine, "mcp_server_count", 0)
            loader_label = "Context Loader" if loaders_count == 1 else "Context Loaders"
            tool_label = "Tool" if tools_count == 1 else "Tools"
            skill_label = "Skill" if skills_count == 1 else "Skills"
            mcp_label = "MCP Server" if mcp_server_count == 1 else "MCP Servers"

            output.extend(
                [
                    f" {GRAY}›{RESET} {BOLD}Runtime:{RESET}",
                    f"   {GREEN}•{RESET} {BOLD}{loaders_count}{RESET} {loader_label}",
                    f"   {GREEN}•{RESET} {BOLD}{tools_count}{RESET} {tool_label}",
                    f" {GRAY}›{RESET} {BOLD}Capability Catalog:{RESET}",
                    f"   {GREEN}•{RESET} {BOLD}{skills_count}{RESET} {skill_label}",
                    f"   {GREEN}•{RESET} {BOLD}{mcp_server_count}{RESET} {mcp_label}",
                    border,
                ]
            )

        output.extend(
            [
                f"  {GRAY}Type \"exit\" or \"quit\" to end the session.{RESET}",
                "",
            ]
        )
        self._print_many(output)

    def show_session_list(self, sessions: list[SessionRecord]) -> None:
        if not sessions:
            self._print("[testcode] no saved sessions")
            return

        self._print("[testcode] saved sessions:")
        for index, session in enumerate(sessions, start=1):
            preview = session.preview or "(no user messages yet)"
            self._print(
                f"{index}. {session.session_id} | {session.status} | "
                f"{session.updated_at} | {session.message_count} messages"
            )
            self._print(f"  cwd: {session.cwd}")
            self._print(f"  preview: {preview}")

    def show_thinking_start(self) -> Spinner:
        spinner = Spinner(message="Model is thinking...", interruptible=True)
        spinner.start()
        return spinner

    def show_thinking_end(self, spinner: Spinner) -> None:
        spinner.stop()
        self.clear_running_status_bar(0)

    def model_started(self) -> Spinner:
        return self.show_thinking_start()

    def model_finished(self, handle: Spinner) -> None:
        self.show_thinking_end(handle)

    def model_retrying(
        self,
        handle: Spinner,
        retry: int,
        max_retries: int,
        status: str,
        delay_seconds: float,
    ) -> None:
        handle.update_message(
            f"{status} — retrying {retry}/{max_retries} in {delay_seconds:g}s..."
        )

    def show_tool_start(self, action_name: str) -> Spinner:
        YELLOW = "\033[1;33m"
        CYAN = "\033[1;36m"
        RESET = "\033[0m"
        prefix = f" {YELLOW}•{RESET}"
        message = f"{CYAN}{action_name}{RESET} -> Executing"
        spinner = Spinner(message=message, prefix=prefix)
        spinner.start()
        return spinner

    def show_tool_end(self, spinner: Spinner, action, result: ToolResult) -> None:
        spinner.stop()
        
        # Color codes
        GREEN = "\033[1;32m"
        RED = "\033[1;31m"
        CYAN = "\033[1;36m"
        RESET = "\033[0m"

        args_str = self._format_args(action.arguments)
        args_display = f"({args_str})" if args_str else ""
        
        output_summary = self._summarize_tool_result(result)
        
        if result.success:
            self._print(f" {GREEN}•{RESET} {CYAN}{action.name}{RESET}{args_display} -> {output_summary}")
        else:
            self._print(f" {RED}•{RESET} {CYAN}{action.name}{RESET}{args_display} -> {RED}{output_summary}{RESET}")

    def show_tool_skipped(self, action, reason: str) -> None:
        YELLOW = "\033[1;33m"
        CYAN = "\033[1;36m"
        RESET = "\033[0m"

        args_str = self._format_args(action.arguments)
        args_display = f"({args_str})" if args_str else ""
        self._print(f" {YELLOW}•{RESET} {CYAN}{action.name}{RESET}{args_display} -> {YELLOW}{reason}{RESET}")

    def tool_started(self, action_name: str) -> Spinner:
        return self.show_tool_start(action_name)

    def tool_finished(self, handle: Spinner, action, result: ToolResult) -> None:
        self.show_tool_end(handle, action, result)

    def tool_aborted(self, handle: Spinner) -> None:
        handle.stop()

    def tool_skipped(self, action, reason: str) -> None:
        self.show_tool_skipped(action, reason)

    def _format_args(self, arguments: dict) -> str:
        if not arguments:
            return ""
        parts = []
        for k, v in arguments.items():
            v_str = str(v)
            if len(v_str) > 40:
                v_str = v_str[:37] + "..."
            if " " in v_str or v_str.startswith("{") or v_str.startswith("["):
                parts.append(f"{k}=\"{v_str}\"")
            else:
                parts.append(f"{k}={v_str}")
        return ", ".join(parts)

    def show_status_bar(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> None:
        self.status_bar.show(
            engine=engine,
            active_tasks_count=active_tasks_count,
            is_running=is_running,
            left_override=left_override,
        )

    def show_help(self) -> None:
        CYAN = "\033[1;36m"
        BOLD = "\033[1m"
        RESET = "\033[0m"
        GRAY = "\033[90m"
        
        self._print(f"\n{BOLD}testcode CLI Workbench Shortcuts & Commands:{RESET}")
        self._print(f"  {CYAN}/help, ?{RESET}       Show this help message")
        self._print(f"  {CYAN}/tasks{RESET}         List active background tasks")
        self._print(f"  {CYAN}/skills{RESET}        List all scanned skills and metadata")
        self._print(f"  {CYAN}/mode [mode]{RESET}  Show or change safety mode ({GRAY}readonly{RESET}/{GRAY}confirm{RESET}/{GRAY}auto{RESET})")
        self._print(f"  {CYAN}exit, quit{RESET}     Exit the current workbench session\n")

    def show_tasks(self) -> None:
        self._print("\nNo active background tasks running in this session.\n")

    def show_skills(self, engine) -> None:
        CYAN = "\033[1;36m"
        BOLD = "\033[1m"
        RESET = "\033[0m"
        
        skills = {}
        if engine and hasattr(engine, "skills_registry") and engine.skills_registry:
            skills = getattr(engine.skills_registry, "_skills", {})
            
        self._print(f"\n{BOLD}Scanned Skill Registry:{RESET}")
        if not skills:
            self._print("  No skills found in registry.")
        else:
            for name, meta in skills.items():
                version = getattr(meta, "version", "0.1.0")
                desc = getattr(meta, "description", "")
                triggers = getattr(meta, "triggers", [])
                self._print(f"  {CYAN}• {name}{RESET} (v{version}) - {desc}")
                if triggers:
                    self._print(f"    Triggers: {', '.join(triggers)}")
        self._print()

    def show_or_change_mode(self, engine, mode_arg: str | None = None) -> None:
        GREEN = "\033[1;32m"
        YELLOW = "\033[1;33m"
        RED = "\033[1;31m"
        RESET = "\033[0m"
        BOLD = "\033[1m"
        
        if not engine or not hasattr(engine, "guardrails") or not hasattr(engine.guardrails, "policy"):
            self._print("Engine policy configuration is not available.\n")
            return
            
        policy = engine.guardrails.policy
        
        if not mode_arg:
            current_mode = getattr(policy, "mode", "confirm")
            self._print(f"\nCurrent safety mode: {BOLD}{current_mode}{RESET}\n")
            return
            
        if mode_arg not in {"readonly", "confirm", "auto"}:
            self._print(f"\n{RED}Error:{RESET} Invalid mode '{mode_arg}'. Use readonly, confirm, or auto.\n")
            return
            
        policy.mode = mode_arg
        self._print(f"\nSafety mode successfully updated to: {BOLD}{mode_arg}{RESET}\n")

    def show_input_border(self) -> None:
        self.prompt_box.show_border()

    def clear_previous_status_bar(self) -> None:
        import sys
        if sys.stdout.isatty():
            # Move up 3 lines, carriage return, clear the entire line, and move back down 3 lines
            sys.stdout.write("\r\033[3A\r\033[2K\033[3B\r")
            sys.stdout.flush()

    def show_interrupted(self) -> None:
        RED = "\033[1;31m"
        RESET = "\033[0m"
        self._print(f"\n {RED}⎿  Interrupted · What should testcode CLI do instead?{RESET}\n")

    def clear_running_status_bar(self, tools_count: int) -> None:
        self.status_bar.clear_running(tools_count)

    def prompt_input(self, engine=None) -> str:
        return self.prompt_box.prompt_input(engine=engine)
