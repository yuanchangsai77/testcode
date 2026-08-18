from testcode.model.streaming import NaturalLanguageStreamProjector


def test_projector_streams_only_top_level_natural_language_fields():
    projector = NaturalLanguageStreamProjector()
    chunks = [
        '{"message":"Hello\\n<th',
        'ink>checking \\u4f60',
        '\\u597d</think>world",',
        '"done":false,"actions":[{"name":"shell_exec","arguments":',
        '{"message":"must-not-leak","command":"secret"}}],',
        '"thinking":"explicit note"}',
    ]

    events = [event for chunk in chunks for event in projector.feed(chunk)]
    events.extend(projector.finish())
    by_channel = {
        channel: "".join(event.text for event in events if event.channel == channel)
        for channel in {event.channel for event in events}
    }

    assert by_channel["message"] == "Hello\nworld"
    assert by_channel["thinking"] == "checking 你好explicit note"
    assert "must-not-leak" not in "".join(by_channel.values())
    assert "secret" not in "".join(by_channel.values())


def test_projector_decodes_split_surrogate_pair_without_emitting_json_syntax():
    projector = NaturalLanguageStreamProjector()

    events = projector.feed('{"message":"ok \\uD83D')
    events += projector.feed('\\uDE00","actions":[]}')
    events += projector.finish()

    assert "".join(event.text for event in events) == "ok 😀"


def test_projector_suppresses_legacy_tool_protocol_blocks_inside_message():
    projector = NaturalLanguageStreamProjector()
    raw = (
        '{"message":"before<minimax:tool_call tool=\\"shell_exec\\">'
        '<parameter name=\\"command\\">secret command</parameter>'
        '</minimax:tool_call>after","done":false,"actions":[]}'
    )

    events = projector.feed(raw) + projector.finish()
    visible = "".join(event.text for event in events)

    assert visible == "beforeafter"
    assert "secret command" not in visible


def test_projector_streams_plain_assistant_content():
    projector = NaturalLanguageStreamProjector()

    events = projector.feed("  您好！有什么")
    events += projector.feed("可以帮您的吗？")
    events += projector.finish()

    assert projector.input_mode == "plain"
    assert "".join(event.text for event in events) == "  您好！有什么可以帮您的吗？"
    assert all(event.channel == "message" for event in events)


def test_projector_suppresses_tool_protocol_in_plain_content():
    projector = NaturalLanguageStreamProjector()

    events = projector.feed("before<tool_call>")
    events += projector.feed("secret</tool_call>after")
    events += projector.finish()

    assert "".join(event.text for event in events) == "beforeafter"


def test_projector_stops_plain_projection_before_embedded_json_actions():
    projector = NaturalLanguageStreamProjector()

    events = projector.feed('Checking now. {"message":"safe",')
    events += projector.feed(
        '"actions":[{"name":"shell_exec","arguments":{"command":"secret"}}]}'
    )
    events += projector.finish()
    visible = "".join(event.text for event in events)

    assert visible == "Checking now. safe"
    assert "shell_exec" not in visible
    assert "secret" not in visible
