from __future__ import annotations

try:
    import readline
except ImportError:
    pass

from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest
from .presenter import ConsolePresenter


class CLI:
    """Thin interaction shell that delegates work to the execution engine."""

    def __init__(self, engine, presenter: ConsolePresenter, logger=None, session_store=None) -> None:
        self.engine = engine
        self.presenter = presenter
        self.logger = logger
        self.session_store = session_store

    def run(self, request: UserRequest) -> ExecutionSummary:
        return self._run_once(request)

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
            self.presenter.show_session_state(session, resumed=resumed, engine=self.engine)

        prompt = initial_prompt

        while True:
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
                cmd_parts = prompt.split()
                cmd = cmd_parts[0].lower()
                if cmd in {"/help", "?", "？"}:
                    self.presenter.show_help()
                    prompt = None
                    continue
                elif cmd == "/tasks":
                    self.presenter.show_tasks()
                    prompt = None
                    continue
                elif cmd == "/skills":
                    self.presenter.show_skills(self.engine)
                    prompt = None
                    continue
                elif cmd == "/mode":
                    mode_arg = cmd_parts[1].lower() if len(cmd_parts) > 1 else None
                    self.presenter.show_or_change_mode(self.engine, mode_arg)
                    prompt = None
                    continue

            active_skills = []
            if session is not None:
                active_skills = getattr(session, "active_skills", [])

            request = UserRequest(
                prompt=prompt,
                cwd=cwd,
                metadata={
                    "conversation": list(conversation),
                    "session_id": session.session_id if session is not None else None,
                    "active_skills": list(active_skills),
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
                    if hasattr(summary, "active_skills"):
                        session.active_skills = [s.metadata.name for s in summary.active_skills]
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
        if hasattr(summary, "active_skills"):
            session.active_skills = [skill.metadata.name for skill in summary.active_skills]
        run_summary = getattr(self.logger, "last_run_summary", None)
        if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
            session.trace.append(run_summary)
        self._attach_last_run_id(session)
        try:
            self.session_store.save(session)
        finally:
            if close_runtime:
                tools = getattr(self.engine, "tools", None)
                reset_state = getattr(tools, "reset_state", None)
                if callable(reset_state):
                    reset_state()

    def choose_session(self, prompt: str = "Select a session number to resume") -> StoredSession | None:
        sessions = self.list_sessions()
        self.presenter.show_session_list(sessions)
        if not sessions:
            return None

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
            self.presenter.clear_running_status_bar(tools_count)
            self.presenter.show_interrupted()
            interrupted_summary = ExecutionSummary(final_message="Interrupted", tool_results=interrupted_results)
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
                    f"{error}. You can keep this session open and try again."
                ),
                tool_results=[],
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
        finally:
            tools = getattr(self.engine, "tools", None)
            reset_state = getattr(tools, "reset_state", None)
            if callable(reset_state):
                reset_state()

    def _attach_last_run_id(self, session: StoredSession) -> None:
        run_id = getattr(self.logger, "last_run_id", None)
        if isinstance(run_id, str) and run_id and run_id not in session.run_ids:
            session.run_ids.append(run_id)
