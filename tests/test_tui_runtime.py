from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
import os
import re
import signal
from types import SimpleNamespace
import threading
import time

import pytest

from testcode.interaction.tui import (
    ComposerState,
    InlineTerminalSurface,
    TUIConsolePresenter,
    TUIController,
    TUIRenderer,
    _display_width,
)
from testcode.interaction.tui_events import TUIEvent, TUIEventKind, TUIEventQueue
from testcode.interaction.tui_state import RunStatus, ToolStatus
from testcode.types import ExecutionSummary


def _plain(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def test_event_queue_coalesces_refresh_events_but_keeps_lifecycle_events():
    events = TUIEventQueue(max_events=8)
    events.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 40}))
    events.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 120}))
    for index in range(7):
        events.publish(
            TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=str(index), payload={"name": "tool"})
        )

    drained = events.drain()

    resize_events = [event for event in drained if event.kind == TUIEventKind.RESIZED]
    assert [event.payload["width"] for event in resize_events] == [120]
    assert len([event for event in drained if event.kind == TUIEventKind.TOOL_STARTED]) == 7


def test_event_queue_applies_backpressure_instead_of_dropping_outcomes():
    events = TUIEventQueue(max_events=8)
    for index in range(8):
        events.publish(TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=str(index)))
    published = threading.Event()
    producer = threading.Thread(
        target=lambda: (
            events.publish(TUIEvent(TUIEventKind.RUN_FINISHED)),
            published.set(),
        )
    )
    producer.start()
    time.sleep(0.02)
    assert published.is_set() is False

    first_batch = events.drain()
    producer.join(timeout=1)

    assert len(first_batch) == 8
    assert published.is_set() is True
    assert [event.kind for event in events.drain()] == [TUIEventKind.RUN_FINISHED]


def test_controller_reduces_runtime_events_by_stable_tool_id():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"prompt": "检查项目", "model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="tool-1", payload={"name": "read_file"})
    )
    controller.publish(
        TUIEvent(
            TUIEventKind.TOOL_FINISHED,
            entity_id="tool-1",
            payload={"success": True, "summary": "README.md"},
        )
    )

    state = controller.drain()

    assert state.run_status == RunStatus.WORKING
    assert state.request_summary == "检查项目"
    assert state.model_name == "gpt-5"
    assert state.cwd == "/repo"
    assert state.tools[0].status == ToolStatus.SUCCEEDED


@pytest.mark.parametrize("width", [20, 40, 80, 120])
def test_renderer_respects_terminal_width_for_chinese_content(width):
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"prompt": "分析问题", "model_name": "gpt-5", "cwd": "/很长的路径"},
        )
    )
    controller.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": width, "height": 20}))

    frame = TUIRenderer().render(controller.drain(), now=time.monotonic() + 2)

    assert frame
    assert all(_display_width(line) < width or width == 1 for line in frame.splitlines())


def test_renderer_keeps_model_metadata_separate_from_activity():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    state = controller.drain()
    renderer = TUIRenderer()

    activity = renderer.render_rows(state, now=0, include_runtime=False)

    assert "gpt-5" not in "".join(text for _style, text in activity)
    assert renderer.runtime_row(state) == ("class:runtime", "  gpt-5 · /repo")


def test_renderer_shows_complete_pending_model_stream_instead_of_last_three_lines():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(TUIEvent(TUIEventKind.MODEL_STARTED, entity_id="model-1"))
    controller.publish(
        TUIEvent(
            TUIEventKind.MODEL_STREAM_DELTA,
            entity_id="model-1",
            payload={
                "thinking": "checking",
                "message": "first\nsecond\nthird\nfourth",
            },
        )
    )

    frame = _plain(TUIRenderer().render(controller.drain()))

    assert "thinking:" in frame
    assert "checking" in frame
    assert "   first" in frame
    assert "   second" in frame
    assert "   third" in frame
    assert "   fourth" in frame
    assert "Receiving model stream" in frame


def test_renderer_uses_terminal_height_for_sliding_stream_preview():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 80, "height": 14}))
    controller.publish(
        TUIEvent(
            TUIEventKind.MODEL_STREAM_DELTA,
            entity_id="model-1",
            payload={"message": "\n".join(f"line-{index}" for index in range(10))},
        )
    )

    frame = _plain(TUIRenderer().render(controller.drain()))

    assert "line-0" not in frame
    assert "line-9" in frame
    assert "   …" in frame


def test_tui_stream_previews_incrementally_then_defers_final_answer_to_renderer(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    committed: list[str] = []
    monkeypatch.setattr(presenter._surface, "commit", lambda rows: committed.extend(rows))

    handle = presenter.model_started()
    presenter.model_stream_delta(handle, "message", "您好！\n有什么可以")

    state = presenter.controller.drain()
    assert committed == []
    assert state.model_stream_message == "您好！\n有什么可以"
    assert "   您好！" in _plain(TUIRenderer().render(state))
    assert "   有什么可以" in _plain(TUIRenderer().render(state))

    presenter.model_stream_delta(handle, "message", "帮您的吗？")
    presenter.model_response_ready(
        handle,
        "您好！\n有什么可以帮您的吗？",
        True,
    )
    presenter.model_finished(handle)

    assert committed == []
    assert presenter._last_streamed_message == ""
    presenter.show_summary(
        ExecutionSummary(
            final_message="## 问候\n\n**您好！** 有什么可以帮您的吗？",
            tool_results=[],
        )
    )
    rendered = _plain(output.getvalue())
    assert "问候" in rendered
    assert "您好！" in rendered
    assert "**您好！**" not in rendered


def test_tui_renders_complete_tool_round_markdown_but_not_protocol_placeholder(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[list[str]] = []
    monkeypatch.setattr(presenter._surface, "commit", lambda rows: committed.append(rows))

    handle = presenter.model_started()
    presenter.model_stream_delta(handle, "message", "## 读取计划")
    presenter.model_response_ready(handle, "## 读取计划\n\n- 架构文档", False)
    presenter.model_finished(handle)
    placeholder = presenter.model_started()
    presenter.model_response_ready(placeholder, "Model requested tool calls.", False)
    presenter.model_finished(placeholder)

    assert len(committed) == 1
    rendered = _plain("\n".join(committed[0]))
    assert committed[0][0].strip() == ""
    assert "读取计划" in rendered
    assert "架构文档" in rendered
    assert "##" not in rendered
    assert "Model requested tool calls." not in rendered


def test_tui_commits_model_turns_and_tools_in_causal_order(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[str] = []
    monkeypatch.setattr(
        presenter._surface,
        "commit",
        lambda rows: committed.append(_plain("\n".join(rows))),
    )

    first_model = presenter.model_started()
    presenter.model_response_ready(first_model, "**第一轮**", False)
    presenter.model_finished(first_model)
    first_tool = presenter.tool_started("read_file")
    presenter.tool_finished(
        first_tool,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="first.py", error_code=None),
    )
    second_model = presenter.model_started()
    presenter.model_response_ready(second_model, "**第二轮**", False)
    presenter.model_finished(second_model)
    second_tool = presenter.tool_started("search_text")
    presenter.tool_finished(
        second_tool,
        SimpleNamespace(name="search_text"),
        SimpleNamespace(success=True, output="4 matches", error_code=None),
    )

    assert len(committed) == 4
    assert committed[0].splitlines()[0].strip() == ""
    assert "第一轮" in committed[0]
    assert "read_file → first.py" in committed[1]
    assert "第二轮" in committed[2]
    assert "search_text → 4 matches" in committed[3]


def test_final_markdown_has_a_blank_line_after_the_last_tool():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    tool = presenter.tool_started("read_file")
    presenter.tool_finished(
        tool,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="README.md", error_code=None),
    )

    presenter.show_summary(
        ExecutionSummary(final_message="项目架构整理如下：", tool_results=[])
    )

    lines = _plain(output.getvalue()).splitlines()
    tool_line = next(index for index, line in enumerate(lines) if "read_file → README.md" in line)
    answer_line = next(index for index, line in enumerate(lines) if "项目架构整理如下：" in line)
    assert answer_line >= tool_line + 2
    assert all(not line.strip() for line in lines[tool_line + 1 : answer_line])


def test_first_answer_reuses_prompt_separator_without_adding_a_second_blank_line():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter.show_start(SimpleNamespace(prompt="您好", cwd="/repo"))

    presenter.show_summary(
        ExecutionSummary(final_message="您好！请问有什么可以帮您的吗？", tool_results=[])
    )

    lines = _plain(output.getvalue()).splitlines()
    prompt_line = next(index for index, line in enumerate(lines) if "testcode> 您好" in line)
    answer_line = next(
        index for index, line in enumerate(lines) if "您好！请问有什么可以帮您的吗？" in line
    )
    # One blank row belongs to the input box itself; exactly one fixed blank
    # row remains between the bottom of that box and the answer.
    assert answer_line == prompt_line + 3
    assert all(not line.strip() for line in lines[prompt_line + 1 : answer_line])


def test_stream_preview_adds_missing_separator_immediately_after_a_tool(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    monkeypatch.setattr(presenter._surface, "commit", lambda _rows: None)
    tool = presenter.tool_started("list_dir")
    presenter.tool_finished(
        tool,
        SimpleNamespace(name="list_dir"),
        SimpleNamespace(success=True, output="listed /repo", error_code=None),
    )
    model = presenter.model_started()
    presenter.model_stream_delta(model, "message", "正在分析")

    rows = presenter.renderer.render_rows(presenter.controller.drain())
    message_index = next(index for index, (_style, text) in enumerate(rows) if "正在分析" in text)
    assert message_index > 0
    assert rows[message_index - 1][1] == ""


def test_stream_preview_reuses_fixed_prompt_separator_without_adding_another():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.show_start(SimpleNamespace(prompt="看看", cwd="/repo"))
    model = presenter.model_started()
    presenter.model_stream_delta(model, "message", "正在查看")

    state = presenter.controller.drain()
    rows = presenter.renderer.render_rows(state)
    message_index = next(index for index, (_style, text) in enumerate(rows) if "正在查看" in text)
    assert state.model_stream_needs_separator is False
    assert message_index == 0


def test_failed_tool_transcript_is_committed_before_rendered_final_answer():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    tool_handle = presenter.tool_started("toolbox_open")
    presenter.tool_finished(
        tool_handle,
        SimpleNamespace(name="toolbox_open"),
        SimpleNamespace(
            name="toolbox_open",
            success=False,
            output="unknown toolbox",
            error_code="capability_toolbox_unavailable",
        ),
    )
    presenter.controller.drain()

    final_handle = presenter.model_started()
    presenter.model_stream_delta(final_handle, "message", "## 最终架构")
    presenter.model_response_ready(final_handle, "## 最终架构", True)
    presenter.model_finished(final_handle)
    presenter.controller.drain()
    presenter._flush_tool_transcript()
    presenter.show_summary(
        ExecutionSummary(final_message="## 最终架构", tool_results=[])
    )

    rendered = _plain(output.getvalue())
    assert rendered.index("toolbox_open") < rendered.index("最终架构")
    assert "## 最终架构" not in rendered


def test_renderer_keeps_only_running_tools_in_transient_rows():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="running", payload={"name": "read"})
    )
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="done", payload={"name": "test"})
    )
    controller.publish(
        TUIEvent(
            TUIEventKind.TOOL_FINISHED,
            entity_id="done",
            payload={"success": True, "summary": "passed"},
        )
    )

    rows = TUIRenderer().render_rows(controller.drain())

    assert rows[0][0] == "class:tool.running"
    assert rows[0][1].startswith(" • read →")
    assert all("test → passed" not in text for _style, text in rows)


def test_tui_commits_each_terminal_tool_state_immediately(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[list[str]] = []
    monkeypatch.setattr(
        presenter._surface,
        "commit",
        lambda rows: committed.append([_plain(row) for row in rows]),
    )

    finished = presenter.tool_started("list_dir")
    presenter.tool_finished(
        finished,
        SimpleNamespace(name="list_dir"),
        SimpleNamespace(success=True, output="listed /repo", error_code=None),
    )
    aborted = presenter.tool_started("read_file")
    presenter.tool_aborted(aborted)
    presenter.tool_skipped(SimpleNamespace(name="write_file"), "not approved")

    assert committed == [
        [" • list_dir → listed /repo"],
        [" • read_file → aborted"],
        [" • write_file → not approved"],
    ]
    state = presenter.controller.drain()
    assert all(tool.status != ToolStatus.RUNNING for tool in state.tools)
    assert all(tool.name not in _plain(TUIRenderer().render(state)) for tool in state.tools)

    presenter._flush_tool_transcript()
    assert len(committed) == 3


def test_composer_supports_unicode_multiline_cursor_and_history():
    composer = ComposerState()
    composer.edit("你好", [])
    composer.edit("\x1b\r", [])
    composer.edit("world", [])
    composer.edit("\x1b[D", [])
    composer.edit("!", [])

    assert composer.value == "你好\nworl!d"
    assert composer.edit("\r", []) == "submit"

    composer.set_value("draft")
    composer.edit("\x1b[A", ["first", "second"])
    assert composer.value == "second"
    composer.edit("\x1b[B", ["first", "second"])
    assert composer.value == "draft"


def test_inline_surface_redraws_only_transient_tail_without_alternate_screen_or_mouse():
    output = StringIO()
    surface = InlineTerminalSurface(output)

    surface.render(["working", "testcode> hello", "model"], cursor_row=1, cursor_column=12)
    surface.render(["done", "testcode> hello", "model"], cursor_row=1, cursor_column=12)
    surface.clear()

    rendered = output.getvalue()
    assert "working" in rendered and "done" in rendered
    assert "\x1b[?1049h" not in rendered
    assert "\x1b[?1000h" not in rendered
    assert "\x1b[J" in rendered


def test_inline_surface_accounts_for_old_frame_reflow_when_clearing(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(
        "testcode.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((10, 18)),
    )
    surface = InlineTerminalSurface(output)
    surface._active = True
    surface._rows = ["123456789012345", "input", "model"]
    surface._cursor_row = 1
    surface._cursor_column = 3

    surface.clear()

    assert "\r\033[2A\r\033[J" in output.getvalue()


def test_prompt_input_reads_utf8_without_prompt_toolkit():
    read_fd, write_fd = os.pipe()
    output = StringIO()
    try:
        os.write(write_fd, "你好，testcode\r".encode())
        presenter = TUIConsolePresenter(input=read_fd, output=output)

        value = presenter.prompt_input()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert value == "你好，testcode"
    assert "\x1b[?1049h" not in output.getvalue()


def test_committed_output_is_written_once_to_native_scrollback():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)

    presenter.show_start(SimpleNamespace(prompt="inspect", cwd="/repo"))
    presenter._print("answer")

    plain = _plain(output.getvalue())
    assert plain.count("testcode> inspect") == 1
    assert plain.count("answer") == 1


def test_worked_separator_has_a_blank_line_below(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter._pending_worked_seconds = 220
    monkeypatch.setattr(
        "testcode.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((60, 20)),
    )

    presenter._show_worked_separator()

    plain = _plain(output.getvalue())
    assert "Worked for 3m 40s" in plain
    assert plain.endswith("\n\n")


def test_runtime_frame_places_model_below_composer(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter.controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    monkeypatch.setattr(
        "testcode.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((80, 24)),
    )

    presenter._render_runtime()

    plain = _plain(output.getvalue())
    assert plain.index("testcode>") < plain.index("gpt-5 · /repo")


def test_runtime_thinking_has_plain_blank_rows_above_and_below():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    state = presenter.controller.drain()

    rows, _cursor_row, _cursor_column = presenter._runtime_frame(state, 80)
    plain_rows = [_plain(row) for row in rows]
    thinking_index = next(
        index for index, row in enumerate(plain_rows) if "Working" in row
    )
    assert thinking_index == 0
    assert plain_rows[thinking_index + 1] == ""


def test_prompt_text_reapplies_gray_background_after_colored_label():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("hello")

    rows, _cursor_row, _cursor_column = presenter._composer_rows(80)

    assert "testcode> \033[0m\033[48;5;236mhello" in rows[0]
    assert "hello\033[K\033[0m" in rows[0]


def test_gray_input_rows_do_not_use_reflowable_space_padding():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("hello")
    rows, _cursor_row, _cursor_column = presenter._composer_rows(100)
    blank = "\033[48;5;236m\033[K\033[0m"

    assert " " * 20 not in rows[0]
    assert InlineTerminalSurface._visual_height([blank, rows[0]], 20) == 2


def test_runtime_submission_interrupts_and_queues_next_prompt(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    state = presenter.controller.drain()
    presenter._composer.set_value("change direction")
    signals = []
    monkeypatch.setattr(
        "testcode.interaction.tui.os.kill",
        lambda process_id, sent_signal: signals.append((process_id, sent_signal)),
    )

    submitted = presenter._submit_runtime_prompt(state)

    assert submitted is True
    assert presenter._queued_prompt == "change direction"
    assert presenter.controller.drain().run_status == RunStatus.CANCELLING
    assert signals[0][1] == signal.SIGINT


def test_runtime_lifecycle_commits_stable_tool_result_without_fullscreen():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    engine = SimpleNamespace(model=SimpleNamespace(model="gpt-5"))
    presenter.show_start(SimpleNamespace(prompt="inspect", cwd="/repo"))

    presenter.show_status_bar(engine=engine, is_running=True)
    handle = presenter.tool_started("read_file")
    presenter.tool_finished(
        handle,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="ok", error_code=None, name="read_file"),
    )
    presenter.clear_running_status_bar(1)

    state = presenter.controller.snapshot()
    plain = _plain(output.getvalue())
    assert presenter._runtime_active is False
    assert state.run_status == RunStatus.IDLE
    assert state.tools[0].status == ToolStatus.SUCCEEDED
    assert plain.count("read_file → ok") == 1
    assert "\x1b[?1049h" not in output.getvalue()


def test_approval_waits_for_runtime_resolution():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    decision: list[bool] = []
    worker = threading.Thread(
        target=lambda: decision.append(
            presenter.confirm_tool_action(
                SimpleNamespace(name="patch", arguments={"path": "README.md"}),
                "modify a file",
            )
        )
    )
    worker.start()

    deadline = time.monotonic() + 1
    approval = None
    while approval is None and time.monotonic() < deadline:
        approval = presenter.controller.drain().approval
        time.sleep(0.01)
    assert approval is not None

    presenter._resolve_approval(approval.approval_id, True)
    worker.join(timeout=1)

    assert decision == [True]


def test_renderer_formats_approval_choices_vertically():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.APPROVAL_REQUESTED,
            entity_id="app-1",
            payload={
                "action_name": "shell_exec",
                "reason": "tool 'shell_exec' has risk 'execute' and requires explicit approval",
                "arguments": '{"command": "echo \\"hallo\\""}',
            },
        )
    )
    state = controller.drain()

    renderer = TUIRenderer()
    rows = renderer.render_rows(state)
    assert rows[3] == ("class:approval.choice", " › Yes")
    assert rows[4] == ("class:approval.choice", "   No")
    assert rows[5] == ("class:approval.hint", " ↑/↓ to select · enter to confirm · esc to deny")

    rendered = renderer.render(state)
    lines = rendered.splitlines()
    assert " Permission required: shell_exec" in lines[0]
    assert " › Yes" in lines[3]
    assert "   No" in lines[4]
    assert " ↑/↓ to select · enter to confirm · esc to deny" in lines[5]


def test_composer_rows_scrolled_multiline_does_not_repeat_prompt_prefix():
    presenter = TUIConsolePresenter(output=StringIO())
    long_input = "d" * 600
    presenter._composer.set_value(long_input)

    rows, _cursor_row, _cursor_column = presenter._composer_rows(80)
    plain_rows = [_plain(row) for row in rows]

    assert len(plain_rows) == 6
    assert plain_rows[0].startswith("  ")
    assert "testcode>" not in plain_rows[0]
    assert all(len(row) < 80 for row in plain_rows)


def test_composer_slash_command_autocomplete_navigation_and_tab_fill():
    composer = ComposerState()
    composer.edit("/", [])

    matches = composer.get_completion_matches()
    cmd_names = [cmd for cmd, _ in matches]
    assert "/clear" in cmd_names and "/status" in cmd_names and "/resume" in cmd_names
    assert composer.completion_index == 0

    # Down arrow moves selection
    composer.edit("\x1b[B", [])
    assert composer.completion_index == 1

    # Filtering by prefix /sk
    composer.set_value("/sk")
    matches_sk = composer.get_completion_matches()
    assert [cmd for cmd, _ in matches_sk] == ["/skill", "/skills"]

    # Enter confirms the selected /skill command
    result = composer.edit("\r", [])
    assert result == "changed"
    assert composer.value == "/skill"

    # Subsequent enter submits
    result_submit = composer.edit("\r", [])
    assert result_submit == "submit"


def test_composer_rows_renders_slash_command_completion_menu():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("/")

    matches = presenter._composer.get_completion_matches()
    rows = presenter._completion_rows(matches)
    plain_rows = [_plain(row) for row in rows]

    assert any("Commands (" in row for row in plain_rows)
    assert any("/clear" in row for row in plain_rows)
    assert any("/compact" in row for row in plain_rows)
    assert any("▼ (9 more below)" in row for row in plain_rows)

    # Scroll selection down to /status
    status_idx = next(i for i, (cmd, _) in enumerate(matches) if cmd == "/status")
    presenter._composer.completion_index = status_idx
    scrolled_rows = presenter._completion_rows(matches)
    scrolled_plain = [_plain(row) for row in scrolled_rows]
    assert any("▲ (" in row for row in scrolled_plain)
    assert any("› /status" in row for row in scrolled_plain)


@pytest.mark.parametrize("columns", [50, 80, 120])
def test_completion_rows_never_wrap_long_capability_descriptions(monkeypatch, columns):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("/capabilities activate ")
    monkeypatch.setattr(
        "testcode.interaction.tui._terminal_size",
        lambda _output=None: os.terminal_size((columns, 24)),
    )
    matches = [
        (
            "local:subagents:subagent_spawn",
            "Create a new independent child session for a delegated task. "
            "Do not use this for feedback or repairs owned by another child.",
        ),
        (
            "local:subagents:subagent_run_ready",
            "Execute every ready child session concurrently with isolated model runtimes.",
        ),
    ]

    rows = presenter._completion_rows(matches)
    plain_rows = [_plain(row) for row in rows]

    assert all(_display_width(row) < columns for row in plain_rows)
    assert any(row.endswith("…") for row in plain_rows)


def test_composer_offers_dynamic_second_level_command_options(tmp_path, monkeypatch):
    from testcode.app import create_app

    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    app = create_app(workspace_root=tmp_path)
    first_session = app.session_store.create(cwd=str(tmp_path), messages=[{"role": "user", "content": "first task"}])
    composer = ComposerState(
        command_registry=app.command_registry,
        command_context=app,
    )

    composer.set_value("/mode ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/mode readonly",
        "/mode confirm",
        "/mode auto",
    ]
    composer.set_value("/mo")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/mode "
    assert len(composer.get_completion_matches()) == 3
    composer.set_value("/mode")
    assert composer.edit("\t", []) == "changed"
    assert composer.value == "/mode "
    assert composer.edit("\x1b[B", []) == "changed"
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/mode confirm"
    assert composer.edit("\r", []) == "submit"

    composer.set_value("/skill ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/skill git-helper",
        "/skill pytest-helper",
    ]

    composer.set_value("/resume ")
    assert any(
        value == f"/resume {first_session.session_id}"
        for value, _ in composer.get_completion_matches()
    )
    composer.set_value("/resu")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/resume "
    assert composer.get_completion_matches()

    composer.set_value("/capabilities ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/capabilities list",
        "/capabilities open",
        "/capabilities status",
        "/capabilities activate",
        "/capabilities release",
    ]

    composer.set_value("/capabilities open ")
    open_matches = [value for value, _ in composer.get_completion_matches()]
    assert "/capabilities open local:subagents" in open_matches
    assert app.engine.capability_warehouse.status()["opened"] == []

    composer.set_value("/capabilities activate --scope=run")
    unopened_capability_matches = [
        value for value, _ in composer.get_completion_matches()
    ]
    assert (
        "/capabilities activate --scope=run local:subagents"
        in unopened_capability_matches
    )
    assert app.engine.capability_warehouse.status()["opened"] == []

    composer.set_value("/capabilities o")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/capabilities open "
    assert composer.get_completion_matches()

    app.engine.capability_warehouse.open_toolbox("local:subagents")
    composer.set_value("/capabilities activate sub")
    activate_matches = [value for value, _ in composer.get_completion_matches()]
    assert activate_matches == []

    composer.set_value("/capabilities activate --scope=turn")
    capability_matches = [value for value, _ in composer.get_completion_matches()]
    assert capability_matches == [
        "/capabilities activate --scope=turn skill:git-helper",
        "/capabilities activate --scope=turn skill:pytest-helper",
        "/capabilities activate --scope=turn local:subagents",
    ]
    assert all("--scope=" not in value.rsplit(" ", 1)[-1] for value in capability_matches)
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/capabilities activate --scope=turn skill:git-helper"

    composer.set_value("/capabilities activate local:subagents ")
    scope_matches = [value for value, _ in composer.get_completion_matches()]
    assert "/capabilities activate local:subagents --scope=session" in scope_matches
    composer.set_value(
        "/capabilities activate local:subagents --scope=turn"
    )
    assert composer.edit("\r", []) == "submit"

    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer = composer
    option_rows = presenter._completion_rows(composer.get_completion_matches())
    plain_option_rows = [_plain(row) for row in option_rows]
    assert any("Options (" in row for row in plain_option_rows)
    assert any("--scope=turn" in row for row in plain_option_rows)
    assert all("/capabilities activate" not in row for row in plain_option_rows)
