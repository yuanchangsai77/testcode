from __future__ import annotations

import sys
from typing import TYPE_CHECKING



if TYPE_CHECKING:
    from ...types import StoredSession
    from ..cli import CLI


def handle_resume(cli: CLI, args: list[str], session: StoredSession | None = None, conversation: list[dict[str, str]] | None = None, **kwargs) -> bool:
    target_id = args[0] if args else None
    cli.handle_resume_command(target_id=target_id, current_session=session, conversation=conversation)
    return False


def handle_reset(cli: CLI, args: list[str], session: StoredSession | None = None, conversation: list[dict[str, str]] | None = None, **kwargs) -> bool:
    if conversation is not None:
        conversation.clear()

    if session is not None and cli.session_store is not None:
        old_id = session.session_id
        new_session = cli.session_store.create(cwd=session.cwd, messages=[])
        new_session.active_skills = list(getattr(session, "active_skills", []))
        new_session.active_capability_ids = list(getattr(session, "active_capability_ids", []))
        new_session.trace = list(getattr(session, "trace", []))
        new_session.run_ids = list(getattr(session, "run_ids", []))
        cli.session_store.save(new_session)

        # Update pointers
        session.cwd = new_session.cwd
        session.session_id = new_session.session_id
        session.messages = list(new_session.messages)
        cli.active_session = new_session

        if cli.presenter and hasattr(cli.presenter, "_print"):
            CYAN = "\033[1;36m"
            YELLOW = "\033[1;33m"
            RESET = "\033[0m"
            cli.presenter._print(
                f"   {CYAN}Started new session:{RESET} {YELLOW}{new_session.session_id}{RESET} "
                f"(inherited environment from {old_id})"
            )
    elif session is not None:
        session.messages.clear()

    if cli.presenter and hasattr(cli.presenter, "show_context_reset"):
        cli.presenter.show_context_reset()
    return False



def handle_compact(cli: CLI, args: list[str], session: StoredSession | None = None, conversation: list[dict[str, str]] | None = None, **kwargs) -> bool:
    if conversation is None or not conversation:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print("\nConversation context is empty. Nothing to compact.\n")
        return False

    old_count = len(conversation)
    if old_count <= 2:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print("\nConversation context is short (<= 2 messages). No compaction needed.\n")
        return False

    older_messages = conversation[:-2]
    recent_messages = conversation[-2:]

    handle = None
    presenter = cli.presenter
    if presenter and hasattr(presenter, "show_status_bar") and hasattr(presenter, "clear_running_status_bar"):
        presenter.show_status_bar(engine=cli.engine, is_running=True)
        if hasattr(presenter, "model_started"):
            handle = presenter.model_started(message="Compacting conversation context...")
    elif presenter and hasattr(presenter, "model_started"):
        handle = presenter.model_started(message="Compacting conversation context...")


    try:
        summary_text = _generate_ai_summary(cli, older_messages)
    finally:
        if handle is not None and presenter and hasattr(presenter, "model_finished"):
            presenter.model_finished(handle)
        if presenter and hasattr(presenter, "clear_running_status_bar"):
            presenter.clear_running_status_bar(0)




    compacted_messages = [{"role": "system", "content": summary_text}, *recent_messages]
    conversation.clear()
    conversation.extend(compacted_messages)

    if cli.session_store is not None and session is not None:
        old_id = session.session_id
        new_session = cli.session_store.create(cwd=session.cwd, messages=list(conversation))
        new_session.active_skills = list(getattr(session, "active_skills", []))
        new_session.active_capability_ids = list(getattr(session, "active_capability_ids", []))
        new_session.trace = list(getattr(session, "trace", []))
        new_session.run_ids = list(getattr(session, "run_ids", []))
        cli.session_store.save(new_session)

        # Update active session pointer
        session.cwd = new_session.cwd
        session.session_id = new_session.session_id
        session.messages = list(new_session.messages)
        cli.active_session = new_session


        if cli.presenter and hasattr(cli.presenter, "_print"):
            CYAN = "\033[1;36m"
            YELLOW = "\033[1;33m"
            RESET = "\033[0m"
            cli.presenter._print(
                f"   {CYAN}Forked new session:{RESET} {YELLOW}{new_session.session_id}{RESET} "
                f"(inherited environment from {old_id})"
            )

    new_count = len(conversation)
    if cli.presenter and hasattr(cli.presenter, "show_context_compacted"):
        cli.presenter.show_context_compacted(old_count=old_count, new_count=new_count)
    return False


def _generate_ai_summary(cli: CLI, older_messages: list[dict[str, str]]) -> str:
    summary_text = None
    engine = getattr(cli, "engine", None)
    model = getattr(engine, "model", None)

    if model and hasattr(model, "base_url") and hasattr(model, "_post_json"):
        try:
            raw_transcript = []
            for msg in older_messages:
                role = msg.get("role", "user")
                content = msg.get("content", "").strip()
                if content:
                    raw_transcript.append(f"{role.upper()}: {content}")
            transcript_str = "\n\n".join(raw_transcript)

            url = f"{model.base_url.rstrip('/')}/v1/chat/completions"
            prompt_content = (
                "You are an AI coding session summarizer. Summarize the preceding conversation "
                "into a dense, structured executive summary preserving:\n"
                "1. PRIMARY GOAL & USER INTENT\n"
                "2. TECHNICAL DECISIONS & ARCHITECTURE\n"
                "3. CODE & FILE CHANGES COMPLETED\n"
                "4. PENDING ISSUES & NEXT STEPS\n"
                "Keep it concise, technical, and accurate."
            )
            payload = {
                "model": model.model,
                "messages": [
                    {"role": "system", "content": prompt_content},
                    {"role": "user", "content": f"Conversation Transcript:\n\n{transcript_str}"},
                ],
                "stream": False,
            }
            res_data = model._post_json(url, payload)
            choices = res_data.get("choices", [])
            if choices and isinstance(choices, list):
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    msg_obj = first_choice.get("message", {})
                    if isinstance(msg_obj, dict):
                        summary_text = str(msg_obj.get("content", "")).strip()
        except Exception:
            summary_text = None

    if not summary_text:
        summaries: list[str] = []
        for msg in older_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "").strip()
            if content:
                first_line = content.splitlines()[0]
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                summaries.append(f"{role}: {first_line}")
        return "[Local Executive Summary]\n" + "\n".join(summaries)

    return "[AI Executive Summary]\n" + summary_text
