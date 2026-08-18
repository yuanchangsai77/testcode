from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NaturalLanguageDelta:
    channel: str
    text: str


class NaturalLanguageStreamProjector:
    """Project user-visible strings from streamed JSON or plain content.

    Strict JSON exposes only top-level ``message`` and ``thinking`` strings.
    Plain assistant content is treated as a message while legacy tool protocol
    blocks remain suppressed. Native tool calls never enter this projector.
    """

    _OPEN_THINK = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
    _CLOSE_THINK = re.compile(r"</think\s*>", re.IGNORECASE)
    _OPEN_TOOL_PROTOCOL = re.compile(
        r"<(?:[\w.-]+:)?(?:tool_call|invoke|parameter)\b[^>]*>",
        re.IGNORECASE,
    )
    _CLOSE_TOOL_PROTOCOL = re.compile(
        r"</(?:[\w.-]+:)?(?:tool_call|invoke|parameter)\s*>",
        re.IGNORECASE,
    )
    _SIMPLE_ESCAPES = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def __init__(self) -> None:
        self._input_mode = "undetermined"
        self._prefix_buffer = ""
        self._depth = 0
        self._expecting_key = False
        self._pending_key = ""
        self._awaiting_channel: str | None = None
        self._in_string = False
        self._string_role = ""
        self._string_channel = ""
        self._string_buffer: list[str] = []
        self._escaped = False
        self._unicode_digits: str | None = None
        self._pending_high_surrogate: int | None = None
        self._message_channel = "message"
        self._tag_buffer = ""
        self._suppressed_protocol_depth = 0

    def feed(self, raw: str) -> list[NaturalLanguageDelta]:
        emitted: list[NaturalLanguageDelta] = []
        for character in raw:
            if self._input_mode == "undetermined":
                if character.isspace():
                    self._prefix_buffer += character
                    continue
                if character == "{":
                    self._input_mode = "json"
                    self._prefix_buffer = ""
                    self._consume_structure_character(character)
                    continue
                self._input_mode = "plain"
                for prefix_character in self._prefix_buffer:
                    self._route_message_character(prefix_character, emitted)
                self._prefix_buffer = ""
                self._route_message_character(character, emitted)
            elif self._input_mode == "plain":
                if character == "{":
                    # The reply parser accepts a JSON object after a textual
                    # preamble. Stop treating the remainder as plain content so
                    # action fields can never leak into the preview.
                    self._input_mode = "json"
                    self._consume_structure_character(character)
                else:
                    self._route_message_character(character, emitted)
            elif self._in_string:
                self._consume_string_character(character, emitted)
            else:
                self._consume_structure_character(character)
        return self._coalesce(emitted)

    def finish(self) -> list[NaturalLanguageDelta]:
        emitted: list[NaturalLanguageDelta] = []
        if self._input_mode == "undetermined" and self._prefix_buffer:
            self._input_mode = "plain"
            for character in self._prefix_buffer:
                self._route_message_character(character, emitted)
            self._prefix_buffer = ""
        self._flush_pending_surrogate(emitted)
        if self._tag_buffer:
            if self._suppressed_protocol_depth == 0:
                self._append(emitted, self._message_channel, self._tag_buffer)
            self._tag_buffer = ""
        return self._coalesce(emitted)

    @property
    def input_mode(self) -> str:
        return self._input_mode

    def _consume_structure_character(self, character: str) -> None:
        if character == "{":
            self._depth += 1
            if self._depth == 1:
                self._expecting_key = True
            elif self._depth == 2 and self._awaiting_channel is not None:
                self._awaiting_channel = None
            return
        if character == "[":
            self._depth += 1
            if self._depth == 2 and self._awaiting_channel is not None:
                self._awaiting_channel = None
            return
        if character in "}]":
            self._depth = max(0, self._depth - 1)
            return
        if character == ',' and self._depth == 1:
            self._expecting_key = True
            self._pending_key = ""
            self._awaiting_channel = None
            return
        if character == ':' and self._depth == 1 and self._pending_key:
            self._awaiting_channel = (
                self._pending_key if self._pending_key in {"message", "thinking"} else None
            )
            self._pending_key = ""
            return
        if character == '"':
            self._in_string = True
            self._escaped = False
            self._unicode_digits = None
            self._pending_high_surrogate = None
            self._string_buffer = []
            if self._depth == 1 and self._expecting_key:
                self._string_role = "key"
                self._string_channel = ""
            elif self._depth == 1 and self._awaiting_channel is not None:
                self._string_role = "value"
                self._string_channel = self._awaiting_channel
            else:
                self._string_role = "other"
                self._string_channel = ""
            return
        if not character.isspace() and self._awaiting_channel is not None:
            self._awaiting_channel = None

    def _consume_string_character(
        self,
        character: str,
        emitted: list[NaturalLanguageDelta],
    ) -> None:
        if self._unicode_digits is not None:
            if character.lower() not in "0123456789abcdef":
                self._unicode_digits = None
                self._accept_decoded_character("�", emitted)
                self._accept_decoded_character(character, emitted)
                return
            self._unicode_digits += character
            if len(self._unicode_digits) == 4:
                codepoint = int(self._unicode_digits, 16)
                self._unicode_digits = None
                if 0xD800 <= codepoint <= 0xDBFF:
                    self._pending_high_surrogate = codepoint
                elif 0xDC00 <= codepoint <= 0xDFFF and self._pending_high_surrogate is not None:
                    high = self._pending_high_surrogate
                    self._pending_high_surrogate = None
                    combined = 0x10000 + ((high - 0xD800) << 10) + (codepoint - 0xDC00)
                    self._accept_decoded_character(chr(combined), emitted)
                else:
                    self._flush_pending_surrogate(emitted)
                    self._accept_decoded_character(chr(codepoint), emitted)
            return

        if self._escaped:
            self._escaped = False
            if character == "u":
                self._unicode_digits = ""
                return
            self._flush_pending_surrogate(emitted)
            self._accept_decoded_character(self._SIMPLE_ESCAPES.get(character, character), emitted)
            return

        if character == "\\":
            self._escaped = True
            return
        if character == '"':
            self._flush_pending_surrogate(emitted)
            self._in_string = False
            if self._string_role == "key":
                self._pending_key = "".join(self._string_buffer)
                self._expecting_key = False
            elif self._string_role == "value":
                self._awaiting_channel = None
                if self._string_channel == "message" and self._tag_buffer:
                    self._append(emitted, self._message_channel, self._tag_buffer)
                    self._tag_buffer = ""
            self._string_role = ""
            self._string_channel = ""
            return
        self._flush_pending_surrogate(emitted)
        self._accept_decoded_character(character, emitted)

    def _accept_decoded_character(
        self,
        character: str,
        emitted: list[NaturalLanguageDelta],
    ) -> None:
        if self._string_role == "key":
            self._string_buffer.append(character)
        elif self._string_role == "value":
            if self._string_channel == "thinking":
                self._append(emitted, "thinking", character)
            elif self._string_channel == "message":
                self._route_message_character(character, emitted)

    def _route_message_character(
        self,
        character: str,
        emitted: list[NaturalLanguageDelta],
    ) -> None:
        if self._tag_buffer:
            self._tag_buffer += character
            if character != ">" and len(self._tag_buffer) < 128:
                return
            tag = self._tag_buffer
            self._tag_buffer = ""
            if self._OPEN_TOOL_PROTOCOL.fullmatch(tag):
                self._suppressed_protocol_depth += 1
            elif self._CLOSE_TOOL_PROTOCOL.fullmatch(tag):
                self._suppressed_protocol_depth = max(0, self._suppressed_protocol_depth - 1)
            elif self._suppressed_protocol_depth:
                return
            elif self._OPEN_THINK.fullmatch(tag):
                self._message_channel = "thinking"
            elif self._CLOSE_THINK.fullmatch(tag):
                self._message_channel = "message"
            return
        if character == "<":
            self._tag_buffer = character
            return
        if self._suppressed_protocol_depth == 0:
            self._append(emitted, self._message_channel, character)

    def _flush_pending_surrogate(self, emitted: list[NaturalLanguageDelta]) -> None:
        if self._pending_high_surrogate is not None:
            self._pending_high_surrogate = None
            self._accept_decoded_character("�", emitted)

    def _append(
        self,
        emitted: list[NaturalLanguageDelta],
        channel: str,
        text: str,
    ) -> None:
        if text:
            emitted.append(NaturalLanguageDelta(channel=channel, text=text))

    def _coalesce(self, emitted: list[NaturalLanguageDelta]) -> list[NaturalLanguageDelta]:
        result: list[NaturalLanguageDelta] = []
        for item in emitted:
            if result and result[-1].channel == item.channel:
                previous = result[-1]
                result[-1] = NaturalLanguageDelta(previous.channel, previous.text + item.text)
            else:
                result.append(item)
        return result
