from __future__ import annotations

try:
    import readline
except ImportError:
    pass

from pathlib import Path

from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest
from .presenter import ConsolePresenter


from .commands import SlashCommandRegistry, default_slash_command_registry


class CLI:
    """Thin interaction shell that delegates work to the execution engine."""

    def __init__(
        self,
        engine,
        presenter: ConsolePresenter,
        logger=None,
        session_store=None,
        subagent_coordinator=None,
        subagent_runner=None,
        subagent_grant=None,
        command_registry: SlashCommandRegistry | None = None,
    ) -> None:
        self.engine = engine
        self.presenter = presenter
        self.logger = logger
        self.session_store = session_store
        self.subagent_coordinator = subagent_coordinator
        self.subagent_runner = subagent_runner
        self.subagent_grant = subagent_grant
        self.command_registry = command_registry or default_slash_command_registry()
        if hasattr(self.presenter, "command_registry"):
            self.presenter.command_registry = self.command_registry
        if hasattr(self.presenter, "prompt_box") and hasattr(self.presenter.prompt_box, "_composer"):
            self.presenter.prompt_box._composer.command_registry = self.command_registry
        if hasattr(self.presenter, "_composer"):
            self.presenter._composer.command_registry = self.command_registry
        self.active_session = None


    def run(self, request: UserRequest) -> ExecutionSummary:
        return self._run_once(request)

    def run_background(self, request: UserRequest) -> ExecutionSummary:
        """Execute without terminal rendering, for an isolated subagent worker."""
        if self.subagent_grant is not None:
            self._validate_subagent_grant(request)
        if self.logger is not None:
            self.logger.start_run(request)
        try:
            summary = self.engine.execute(request)
        except Exception:
            if self.logger is not None:
                self.logger.record("run.error", {"message": "subagent execution failed"})
            raise
        if self.logger is not None:
            self.logger.finalize(request, summary)
        return summary

    def _validate_subagent_grant(self, request: UserRequest) -> None:
        grant = self.subagent_grant
        if not grant.is_runner_issued():
            raise RuntimeError("delegated subagent execution grant was not issued by the runner")
        subagent = request.metadata.get("subagent")
        if not isinstance(subagent, dict):
            raise RuntimeError("delegated subagent request is missing its execution identity")
        expected = {
            "session_id": request.metadata.get("session_id"),
            "cluster_id": subagent.get("cluster_id"),
            "parent_session_id": subagent.get("parent_session_id"),
            "attempt": subagent.get("attempt"),
            "workspace_root": str(Path(request.cwd).resolve()),
        }
        actual = {
            "session_id": grant.session_id,
            "cluster_id": grant.cluster_id,
            "parent_session_id": grant.parent_session_id,
            "attempt": grant.attempt,
            "workspace_root": str(Path(grant.workspace_root).resolve()),
        }
        if expected != actual:
            raise RuntimeError("delegated subagent request does not match its execution grant")

    def chat(
        self,
        cwd: str,
        initial_prompt: str | None = None,
        conversation: list[dict[str, str]] | None = None,
        session_id: str | None = None,
        context_paths: list[str] | None = None,
    ) -> None:
        conversation = list(conversation or [])
        session = None
        resumed = bool(conversation)
        if self.session_store is not None:
            if session_id is not None:
                session = self.session_store.load(session_id)
            if session is None:
                session = self.session_store.create(cwd=cwd, messages=conversation)
            else:
                session.cwd = cwd
                session.status = "active"
                session.messages = list(conversation)
                self.session_store.save(session)
            self.prepare_session_runtime(session)
            self.presenter.show_session_state(session, resumed=resumed, engine=self.engine)
            if resumed and hasattr(self.presenter, "show_session_history"):
                self.presenter.show_session_history(session)
        self.active_session = session


        prompt = initial_prompt

        while True:
            session = self.active_session
            if prompt is None:

                try:
                    prompt = self.presenter.prompt_input(engine=self.engine)
                except KeyboardInterrupt:
                    print()
                    self._close_session(session, conversation)
                    return
                except EOFError:
                    print()
                    self._close_session(session, conversation)
                    return

            if not prompt:
                prompt = None
                continue

            if prompt.lower() in {"exit", "quit"}:
                self._close_session(session, conversation)
                return

            if prompt.startswith("/") or prompt in {"?", "？"}:
                should_exit = self.command_registry.execute(
                    self,
                    prompt,
                    session=session,
                    conversation=conversation,
                )
                if should_exit:
                    self._close_session(session, conversation)
                    return
                prompt = None
                continue


            active_capability_ids = []
            if session is not None:
                active_capability_ids = getattr(session, "active_capability_ids", [])

            request = UserRequest(
                prompt=prompt,
                cwd=cwd,
                metadata={
                    "conversation": list(conversation),
                    "session_id": session.session_id if session is not None else None,
                    "active_capability_ids": list(active_capability_ids),
                    "session_trace": list(getattr(session, "trace", [])[-6:]) if session is not None else [],
                    "resume_state": getattr(session, "resume_state", None),
                    "context_paths": list(context_paths or []),
                },
            )
            if session is not None and self.logger is not None and self.session_store is not None:
                # Start early so the run id can be attached to the session before execution.
                # _run_once may call start_run again; the logger treats that as a no-op.
                registered_skills = []
                if hasattr(self.engine, "context_loaders"):
                    for loader in self.engine.context_loaders:
                        if hasattr(loader, "registry"):
                            registered_skills = sorted(loader.registry._skills.keys())
                            break
                self.logger.start_run(request, registered_skills=registered_skills)
                self._attach_last_run_id(session)
                self.session_store.save(session)
            try:
                summary = self._run_once(request)
                conversation.append({"role": "user", "content": prompt})
                conversation.append({"role": "assistant", "content": summary.final_message})
                if session is not None:
                    session.messages = list(conversation)
                    session.status = "active"
                    session.active_capability_ids = list(
                        getattr(summary, "active_capability_ids", [])
                    )
                    run_summary = getattr(self.logger, "last_run_summary", None)
                    if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
                        session.trace.append(run_summary)
                    self._attach_last_run_id(session)
                    self.session_store.save(session)
            except KeyboardInterrupt:
                if session is not None:
                    run_summary = getattr(self.logger, "last_run_summary", None)
                    if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
                        session.trace.append(run_summary)
                    self._attach_last_run_id(session)
                    self.session_store.save(session)
                prompt = None
                continue
            prompt = None

    def list_sessions(self) -> list[SessionRecord]:
        if self.session_store is None:
            return []
        return self.session_store.list_sessions()

    def load_session(self, session_id: str) -> StoredSession | None:
        if self.session_store is None:
            return None
        return self.session_store.load(session_id)

    def prepare_session_runtime(self, session: StoredSession | None) -> None:
        if session is None:
            return
        prepare = getattr(self.engine, "prepare_session_state", None)
        if callable(prepare):
            prepare(
                session.session_id,
                getattr(session, "active_capability_ids", []),
            )

    def latest_session(self) -> StoredSession | None:
        if self.session_store is None:
            return None
        return self.session_store.latest()

    def persist_run(
        self,
        session: StoredSession,
        prompt: str,
        summary: ExecutionSummary,
        *,
        status: str = "active",
        close_runtime: bool = False,
    ) -> None:
        if self.session_store is None:
            return
        session.messages.extend(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": summary.final_message},
            ]
        )
        session.status = status
        session.active_capability_ids = list(
            getattr(summary, "active_capability_ids", [])
        )
        run_summary = getattr(self.logger, "last_run_summary", None)
        if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
            session.trace.append(run_summary)
        self._attach_last_run_id(session)
        try:
            self.session_store.save(session)
            if status == "closed":
                self._print_exit_info(session)
        finally:
            if close_runtime:
                tools = getattr(self.engine, "tools", None)
                reset_state = getattr(tools, "reset_state", None)
                if callable(reset_state):
                    reset_state()

    def choose_session(self, prompt: str = "Select a session to resume") -> StoredSession | None:
        sessions = self.list_sessions()
        if not sessions:
            if self.presenter and hasattr(self.presenter, "_print"):
                self.presenter._print("\nNo saved sessions found.\n")
            return None

        import os
        import sys
        import select
        import tty
        import termios
        import signal

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            self.presenter.show_session_list(sessions)
            while True:
                raw = input(f"{prompt} (Enter to cancel): ").strip()
                if not raw:
                    return None
                if not raw.isdigit():
                    print("[testcode] enter a session number")
                    continue
                index = int(raw)
                if 1 <= index <= len(sessions):
                    return self.load_session(sessions[index - 1].session_id)
                print(f"[testcode] choose a number between 1 and {len(sessions)}")

        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)
        selected = 0
        max_visible = 3
        window_start = 0
        rendered_lines = 0

        def read_key() -> str:
            raw_key = os.read(fd, 1)
            if raw_key != b"\x1b":
                while True:
                    try:
                        return raw_key.decode()
                    except UnicodeDecodeError:
                        raw_key += os.read(fd, 1)
            sequence = "\x1b"
            for _ in range(32):
                readable, _, _ = select.select([fd], [], [], 0.05)
                if not readable:
                    break
                char = os.read(fd, 1).decode(errors="ignore")
                sequence += char
                if len(sequence) == 2 and char not in {"[", "O"}:
                    break
                if len(sequence) >= 3 and "@" <= char <= "~":
                    break
            return sequence

        import unicodedata

        def display_width(value: str) -> int:
            w = 0
            for character in value:
                if unicodedata.combining(character):
                    continue
                w += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
            return w

        def truncate_to_width(value: str, max_w: int) -> str:
            if display_width(value) <= max_w:
                return value
            current_w = 0
            truncated = []
            for character in value:
                char_w = 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
                if current_w + char_w > max_w - 3:
                    break
                truncated.append(character)
                current_w += char_w
            return "".join(truncated) + "..."

        def render_frame():
            nonlocal rendered_lines
            lines = []
            CYAN = "\033[1;36m"
            GREEN = "\033[1;32m"
            YELLOW = "\033[1;33m"
            GRAY = "\033[90m"
            RESET = "\033[0m"
            BOLD = "\033[1m"

            from .terminal import terminal_columns

            term_w = max(40, terminal_columns())
            max_line_len = term_w - 8

            header_text = f"{prompt} (use ↑/↓ keys, Enter to confirm, Esc to cancel):"
            if display_width(header_text) > max_line_len:
                header_text = f"{prompt} (↑/↓ to select, Enter/Esc):"
                if display_width(header_text) > max_line_len:
                    header_text = truncate_to_width(header_text, max_line_len)
            lines.append(f"{BOLD}{header_text}{RESET}")

            total = len(sessions)
            nonlocal window_start
            if selected >= window_start + max_visible:
                window_start = selected - max_visible + 1
            elif selected < window_start:
                window_start = selected
            window_start = max(0, min(window_start, total - max_visible))

            visible_sessions = sessions[window_start : window_start + max_visible]

            if window_start > 0:
                lines.append(f"  {GRAY}▲ ({window_start} more sessions above){RESET}")

            for rel_idx, s in enumerate(visible_sessions):
                actual_idx = window_start + rel_idx
                is_selected = actual_idx == selected

                status_colored = f"{GREEN}[active]{RESET}" if s.status == "active" else f"{GRAY}[closed]{RESET}"
                msg_count = s.message_count if hasattr(s, "message_count") else 0
                updated = s.updated_at if hasattr(s, "updated_at") else "N/A"
                preview = s.preview or "(no messages yet)"

                display_id = s.session_id
                if len(display_id) > 20:
                    display_id = display_id[:8] + "..." + display_id[-8:]

                # Safe title line length calculation & dynamic CJK-aware shortening
                raw_title_len = display_width(display_id) + display_width(s.status) + display_width(updated) + 16
                if raw_title_len > max_line_len:
                    if len(updated) > 10:
                        updated = updated[:10]  # Just YYYY-MM-DD
                    raw_title_len = display_width(display_id) + display_width(s.status) + display_width(updated) + 16
                    if raw_title_len > max_line_len:
                        target_id_w = max(5, max_line_len - display_width(s.status) - display_width(updated) - 20)
                        display_id = truncate_to_width(display_id, target_id_w)

                detail_text = f"Path: {s.cwd} | Preview: \"{preview}\""
                if display_width(detail_text) > max_line_len:
                    detail_text = truncate_to_width(detail_text, max_line_len)

                if is_selected:
                    pointer = f"{CYAN}›{RESET}"
                    card_title = f"{CYAN}{BOLD}{display_id}{RESET} {status_colored} ({msg_count} msgs) · {YELLOW}{updated}{RESET}"
                    card_detail = f"    {BOLD}Path:{RESET} {detail_text[6:]}"
                else:
                    pointer = " "
                    card_title = f"{GRAY}{display_id}{RESET} {status_colored} ({msg_count} msgs) · {GRAY}{updated}{RESET}"
                    card_detail = f"    {GRAY}{detail_text}{RESET}"

                lines.append(f"  {pointer} {card_title}")
                lines.append(card_detail)



            remaining_below = total - (window_start + len(visible_sessions))
            if remaining_below > 0:
                lines.append(f"  {GRAY}▼ ({remaining_below} more sessions below){RESET}")

            if rendered_lines > 0:
                sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")

            for line in lines:
                sys.stdout.write(f"\r\033[2K{line}\n")
            sys.stdout.flush()
            rendered_lines = len(lines)

        try:
            tty.setcbreak(fd)
            render_frame()
            while True:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if not readable:
                    continue
                key = read_key()
                if key in {"\r", "\n"}:
                    if rendered_lines > 0:
                        sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")
                        sys.stdout.flush()
                    return self.load_session(sessions[selected].session_id)
                if key in {"\x1b", "\x03"}:
                    if rendered_lines > 0:
                        sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")
                        sys.stdout.flush()
                    return None
                if key in {"\x1b[A", "\x1bOA", "k"}:
                    selected = (selected - 1) % len(sessions)
                    render_frame()
                elif key in {"\x1b[B", "\x1bOB", "j"}:
                    selected = (selected + 1) % len(sessions)
                    render_frame()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)


    def handle_resume_command(
        self,
        target_id: str | None = None,
        current_session: StoredSession | None = None,
        conversation: list[dict[str, str]] | None = None,
    ) -> StoredSession | None:

        if self.session_store is None:
            if self.presenter and hasattr(self.presenter, "_print"):
                self.presenter._print("[testcode] session store is not available")
            return None

        selected_session: StoredSession | None = None
        if target_id:
            selected_session = self.session_store.load(target_id)
            if selected_session is None:
                if self.presenter and hasattr(self.presenter, "_print"):
                    self.presenter._print(f"[testcode] session '{target_id}' not found")
                return None
        else:
            selected_session = self.choose_session()

        if selected_session is not None:
            if current_session is not None and conversation is not None:
                current_session.messages = list(conversation)
                self.session_store.save(current_session)
            if conversation is not None:
                conversation.clear()
                conversation.extend(selected_session.messages)
            self.active_session = selected_session
            self.prepare_session_runtime(selected_session)
            if self.presenter:
                self.presenter.show_session_state(selected_session, resumed=True, engine=self.engine)
                if hasattr(self.presenter, "show_session_history"):
                    self.presenter.show_session_history(selected_session)
        return selected_session




    def _run_once(self, request: UserRequest) -> ExecutionSummary:

        if self.logger is not None:
            registered_skills = []
            if hasattr(self.engine, "context_loaders"):
                for loader in self.engine.context_loaders:
                    if hasattr(loader, "registry"):
                        registered_skills = sorted(loader.registry._skills.keys())
                        break
            self.logger.start_run(request, registered_skills=registered_skills)
        self.presenter.show_start(request)
        try:
            self.presenter.show_status_bar(engine=self.engine, is_running=True)
            summary = self.engine.execute(request)
            self.presenter.clear_running_status_bar(len(summary.tool_results))
        except KeyboardInterrupt:
            tools_count = 0
            interrupted_results = []
            if hasattr(self.engine, "current_session") and self.engine.current_session:
                tools_count = len(self.engine.current_session.tool_results)
                interrupted_results = list(self.engine.current_session.tool_results)
            cancel_run = getattr(self.engine, "cancel_current_run", None)
            if callable(cancel_run):
                cancel_run()
            else:
                tools = getattr(self.engine, "tools", None)
                reset_state = getattr(tools, "reset_state", None)
                if callable(reset_state):
                    reset_state()
            self.presenter.clear_running_status_bar(tools_count)
            self.presenter.show_interrupted()
            interrupted_summary = ExecutionSummary(
                final_message="Interrupted",
                tool_results=interrupted_results,
                outcome="interrupted",
            )
            if hasattr(self.engine, "_finish"):
                self.engine._finish(interrupted_summary)
            if self.logger is not None:
                self.logger.record("run.interrupted", {"tool_count": tools_count})
                self.logger.finalize(request, interrupted_summary)
            raise KeyboardInterrupt
        except RuntimeError as error:
            self.presenter.clear_running_status_bar(0)
            if self.logger is not None:
                self.logger.record("run.error", {"message": str(error)})
            summary = ExecutionSummary(
                final_message=(
                    "Model API is unavailable right now. "
                    f"{error}. You can keep this session open and try again later."
                ),
                tool_results=[],
                outcome="runtime_error",
            )
        self.presenter.show_summary(summary)
        if self.logger is not None:
            self.logger.finalize(request, summary)
        return summary

    def _close_session(self, session, conversation: list[dict[str, str]]) -> None:
        try:
            if session is not None and self.session_store is not None:
                session.status = "closed"
                run_summary = getattr(self.logger, "last_run_summary", None)
                if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
                    session.trace.append(run_summary)
                self._attach_last_run_id(session)
                session.messages = list(conversation)
                self.session_store.save(session)
                self._print_exit_info(session)
            else:
                self._print_exit_info(None)
        finally:
            tools = getattr(self.engine, "tools", None)
            reset_state = getattr(tools, "reset_state", None)
            if callable(reset_state):
                reset_state()

    def _print_exit_info(self, session: StoredSession | None) -> None:
        import json
        from pathlib import Path

        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        has_usage = False

        if session is not None and self.logger is not None and getattr(self.logger, "base_dir", None) is not None:
            run_ids = list(session.run_ids)
            current_run_id = getattr(self.logger, "run_id", None)
            if current_run_id and current_run_id not in run_ids:
                run_ids.append(current_run_id)

            for run_id in run_ids:
                run_dir = Path(self.logger.base_dir) / run_id
                events_file = run_dir / "events.jsonl"
                if events_file.exists():
                    try:
                        with events_file.open("r", encoding="utf-8") as f:
                            for line in f:
                                if not line.strip():
                                    continue
                                event = json.loads(line)
                                if event.get("name") == "model.response":
                                    payload = event.get("payload", {})
                                    usage = payload.get("usage")
                                    if isinstance(usage, dict):
                                        try:
                                            prompt_tokens += int(usage.get("prompt_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                                        try:
                                            completion_tokens += int(usage.get("completion_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                                        try:
                                            total_tokens += int(usage.get("total_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                    except Exception:
                        pass

        CYAN = "\033[1;36m"
        GREEN = "\033[1;32m"
        YELLOW = "\033[1;33m"
        GRAY = "\033[90m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        print(f"{BOLD}Session closed successfully.{RESET}")
        
        if has_usage:
            print(f" {GRAY}›{RESET} {BOLD}Token Usage Summary:{RESET}")
            print(f"   {GREEN}•{RESET} Prompt Tokens:     {YELLOW}{prompt_tokens}{RESET}")
            print(f"   {GREEN}•{RESET} Completion Tokens: {YELLOW}{completion_tokens}{RESET}")
            print(f"   {GREEN}•{RESET} Total Tokens:      {YELLOW}{total_tokens}{RESET}")
        
        if session is not None:
            print(f" {GRAY}›{RESET} {BOLD}To resume this conversation, run:{RESET}")
            print(f"   {CYAN}testcode --resume {session.session_id}{RESET}")
        print(f" {GRAY}›{RESET} {BOLD}To resume the most recent conversation, run:{RESET}")
        print(f"   {CYAN}testcode --last{RESET}")
        print()

    def _attach_last_run_id(self, session: StoredSession) -> None:
        run_id = getattr(self.logger, "last_run_id", None)
        if isinstance(run_id, str) and run_id and run_id not in session.run_ids:
            session.run_ids.append(run_id)
