from __future__ import annotations

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
            self.presenter.show_session_state(session, resumed=resumed)

        prompt = initial_prompt

        while True:
            if prompt is None:
                try:
                    prompt = input("codexcli> ").strip()
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

            request = UserRequest(
                prompt=prompt,
                cwd=cwd,
                metadata={
                    "conversation": list(conversation),
                    "session_id": session.session_id if session is not None else None,
                },
            )
            summary = self._run_once(request)
            conversation.append({"role": "user", "content": prompt})
            conversation.append({"role": "assistant", "content": summary.final_message})
            if session is not None:
                session.messages = list(conversation)
                session.status = "active"
                self.session_store.save(session)
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
                print("[codexcli] enter a session number")
                continue

            index = int(raw)
            if 1 <= index <= len(sessions):
                return self.load_session(sessions[index - 1].session_id)

            print(f"[codexcli] choose a number between 1 and {len(sessions)}")

    def _run_once(self, request: UserRequest) -> ExecutionSummary:
        if self.logger is not None:
            self.logger.start_run(request)
        self.presenter.show_start(request)
        try:
            summary = self.engine.execute(request)
        except RuntimeError as error:
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
        if session is None or self.session_store is None:
            return
        session.status = "closed"
        session.messages = list(conversation)
        self.session_store.save(session)
