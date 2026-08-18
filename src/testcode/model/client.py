from __future__ import annotations

import http.client
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from codecs import getincrementaldecoder
from uuid import uuid4

from ..orchestration.session import SessionContext
from ..context.packager import ContextPackager
from ..types import ModelReply, ToolAction
from . import types as model_types
from .parser import ModelReplyParser
from .prompt import ModelPromptBuilder
from .streaming import NaturalLanguageDelta, NaturalLanguageStreamProjector


class _ResponseReadTimeout(TimeoutError):
    def __init__(self, kind: str, timeout: float) -> None:
        super().__init__(f"{kind} timeout exceeded after {timeout:g} seconds")
        self.kind = kind
        self.timeout = timeout


class StubModelClient:
    """
    Minimal placeholder model client.

    The first turn asks for a workspace summary tool. The second turn returns
    a final answer informed by the observed tool output.
    """

    def respond(self, session: SessionContext) -> ModelReply:
        if not session.tool_results:
            return ModelReply(
                message="I need workspace context before answering.",
                actions=[ToolAction(name="list_dir", arguments={"path": "."})],
                done=False,
            )

        summary = session.tool_results[-1].output
        return ModelReply(
            message=(
                "testcode architecture scaffold is ready. "
                f"Initial workspace inspection: {summary}"
            ),
            done=True,
        )


class OpenAICompatibleModelClient:
    """OpenAI-compatible chat client backed by the configured model proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str = "gpt-5.4",
        timeout: float = 60.0,
        stream_max_seconds: float = 900.0,
        logger=None,
        config: model_types.ModelClientConfig | None = None,
        prompt_builder: ModelPromptBuilder | None = None,
        parser: ModelReplyParser | None = None,
        capability_profile: model_types.ModelCapabilityProfile | None = None,
        context_budget_chars: int = 120_000,
        stream: bool = False,
        on_stream_delta: Callable[[str], None] | None = None,
        on_natural_language_delta: Callable[[NaturalLanguageDelta], None] | None = None,
    ) -> None:
        if config is not None:
            base_url = config.base_url
            model = config.model
            timeout = config.timeout
            stream_max_seconds = config.stream_max_seconds
            stream = config.stream
        if base_url is None:
            raise ValueError("base_url is required")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.stream_max_seconds = max(timeout, stream_max_seconds)
        self.stream = stream
        self.on_stream_delta = on_stream_delta
        self.on_natural_language_delta = on_natural_language_delta
        self.logger = logger
        self.capability_profile = capability_profile or model_types.ModelCapabilityProfile(
            model=model,
            context_budget_chars=context_budget_chars,
        )
        self.prompt_builder = prompt_builder or ModelPromptBuilder(
            ContextPackager(self.capability_profile.context_budget_chars)
        )
        self.parser = parser or ModelReplyParser(logger=logger)
        self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def respond(self, session: SessionContext) -> ModelReply:
        session.request.metadata.setdefault(
            "model_capability_profile",
            {
                "structured_output_mode": self.capability_profile.structured_output_mode,
                "native_tool_calls": self.capability_profile.native_tool_calls,
                "parallel_tool_calls": self.capability_profile.parallel_tool_calls,
                "context_budget_chars": self.capability_profile.context_budget_chars,
                "provenance": self.capability_profile.provenance,
                "verified": self.capability_profile.verified,
            },
        )
        messages = self.prompt_builder.build_messages(session)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": self.stream,
        }
        tools = self.prompt_builder.build_tools(session.available_tools)
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/v1/chat/completions"
        request_id = uuid4().hex
        if self.logger is not None:
            package_stats = getattr(
                getattr(self.prompt_builder, "context_packager", None),
                "last_stats",
                None,
            )
            self.logger.record(
                "model.request",
                {
                    "url": url,
                    "request_id": request_id,
                    "client_timeout_ms": round(
                        (self.stream_max_seconds if self.stream else self.timeout) * 1_000
                    ),
                    "client_idle_timeout_ms": (
                        round(self.timeout * 1_000) if self.stream else 0
                    ),
                    "stream": self.stream,
                    "model": self.model,
                    "messages": messages,
                    "tools": [tool["function"]["name"] for tool in tools],
                    "context_package": {
                        "budget_chars": getattr(package_stats, "budget_chars", 0),
                        "included_chars": getattr(package_stats, "included_chars", 0),
                        "omitted_messages": getattr(package_stats, "omitted_messages", 0),
                    },
                },
            )
        data = self._post_json(url, payload, request_id=request_id)
        if self.logger is not None:
            self.logger.record("model.response", {**data, "_transport_request_id": request_id})
        reply = self.parser.parse_response(
            data,
            allowed_tool_names={tool.name for tool in session.available_tools},
        )
        if self.logger is not None:
            self.logger.record(
                "model.parsed_reply",
                {
                    "request_id": request_id,
                    "message": reply.message,
                    "done": reply.done,
                    "metadata": reply.metadata,
                    "actions": [
                        {"name": action.name, "arguments": action.arguments}
                        for action in reply.actions
                    ],
                },
            )
        return reply

    def _post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        request_id = request_id or uuid4().hex
        is_stream_request = payload.get("stream") is True
        total_timeout = self.stream_max_seconds if is_stream_request else self.timeout
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") is True else "application/json",
                "X-Client-Request-ID": request_id,
                "X-Client-Timeout-Ms": str(round(total_timeout * 1_000)),
                **(
                    {"X-Client-Idle-Timeout-Ms": str(round(self.timeout * 1_000))}
                    if is_stream_request
                    else {}
                ),
            },
            method="POST",
        )

        deadline = time.monotonic() + total_timeout
        data: dict[str, object] | None = None
        body = ""
        try:
            with self._open_url(request, timeout=self.timeout) as response:
                response_headers = getattr(response, "headers", None)
                echoed_request_id = (
                    response_headers.get("X-Client-Request-ID")
                    if response_headers is not None and hasattr(response_headers, "get")
                    else None
                )
                if echoed_request_id and echoed_request_id != request_id:
                    if self.logger is not None:
                        self.logger.record(
                            "model.request_id_mismatch",
                            {"request_id": request_id, "gateway_request_id": echoed_request_id},
                        )
                    raise model_types.ModelConnectionError(
                        "Model endpoint returned a mismatched X-Client-Request-ID"
                    )
                content_type = (
                    response_headers.get("Content-Type", "")
                    if response_headers is not None and hasattr(response_headers, "get")
                    else ""
                )
                if "text/event-stream" in str(content_type).lower():
                    stream_stats: dict[str, object] = {
                        "event_count": 0,
                        "content_chars": 0,
                        "tool_call_count": 0,
                        "visible_message_chars": 0,
                        "visible_thinking_chars": 0,
                        "natural_language_format": "disabled",
                        "first_event_after_ms": None,
                        "last_event_after_ms": None,
                    }
                    if self.logger is not None:
                        self.logger.record(
                            "model.stream_started",
                            {
                                "url": url,
                                "request_id": request_id,
                                "idle_timeout": self.timeout,
                                "max_duration": self.stream_max_seconds,
                            },
                        )
                    try:
                        data, stream_stats = self._read_chat_sse_response(
                            response,
                            deadline,
                            idle_timeout=self.timeout,
                            stats=stream_stats,
                            request_id=request_id,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                        if self.logger is not None:
                            self.logger.record(
                                "model.stream_aborted",
                                {
                                    "url": url,
                                    "request_id": request_id,
                                    **stream_stats,
                                    "reason": str(error),
                                },
                            )
                            self.logger.record(
                                "model.invalid_stream",
                                {"url": url, "request_id": request_id, "reason": str(error)},
                            )
                        raise RuntimeError(f"Model SSE response was invalid: {error}") from error
                    except Exception as error:
                        if self.logger is not None:
                            self.logger.record(
                                "model.stream_aborted",
                                {
                                    "url": url,
                                    "request_id": request_id,
                                    **stream_stats,
                                    "reason": str(error),
                                },
                            )
                        raise
                    if self.logger is not None:
                        self.logger.record(
                            "model.stream_complete",
                            {"url": url, "request_id": request_id, **stream_stats},
                        )
                else:
                    body = self._read_response(response, deadline).decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if self.logger is not None:
                self.logger.record(
                    "model.http_error",
                    {"url": url, "request_id": request_id, "status": error.code, "body": body},
                )
            message = f"Model request failed with HTTP {error.code}: {body}"
            if error.code in {408, 429} or 500 <= error.code <= 599:
                raise model_types.ModelServiceError(message) from error
            raise RuntimeError(message) from error
        except TimeoutError as error:
            timeout_kind = getattr(error, "kind", "connection_or_response")
            timeout_value = float(getattr(error, "timeout", self.timeout))
            if self.logger is not None:
                self.logger.record(
                    "model.timeout",
                    {
                        "url": url,
                        "request_id": request_id,
                        "timeout": timeout_value,
                        "timeout_kind": timeout_kind,
                    },
                )
            raise model_types.ModelTimeoutError(
                f"Model request timed out after {timeout_value:g} seconds ({timeout_kind})"
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                if self.logger is not None:
                    self.logger.record(
                        "model.timeout",
                        {"url": url, "request_id": request_id, "timeout": self.timeout},
                    )
                raise model_types.ModelTimeoutError(
                    f"Model request timed out after {self.timeout:g} seconds"
                ) from error
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "request_id": request_id, "reason": str(error.reason)},
                )
            raise model_types.ModelConnectionError(
                f"Model request failed: {error.reason}"
            ) from error
        except http.client.RemoteDisconnected as error:
            if self.logger is not None:
                self.logger.record(
                    "model.network_error",
                    {"url": url, "request_id": request_id, "reason": str(error)},
                )
            raise model_types.ModelConnectionError(f"Model request failed: {error}") from error

        if data is None:
            try:
                data = json.loads(body)
            except json.JSONDecodeError as error:
                if self.logger is not None:
                    self.logger.record(
                        "model.invalid_json",
                        {"url": url, "request_id": request_id, "body": body},
                    )
                raise RuntimeError(f"Model response was not valid JSON: {body}") from error

        if "choices" not in data:
            if self.logger is not None:
                self.logger.record(
                    "model.invalid_shape",
                    {"url": url, "request_id": request_id, "body": data},
                )
            raise RuntimeError(f"Model response missing choices: {data}")

        return data

    def _open_url(self, request: urllib.request.Request, *, timeout: float):
        hostname = urllib.parse.urlsplit(request.full_url).hostname or ""
        if self._is_loopback_host(hostname):
            return self._direct_opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def _read_response(self, response, deadline: float) -> bytes:
        return b"".join(self._iter_response_chunks(response, deadline))

    def _iter_response_chunks(
        self,
        response,
        deadline: float,
        *,
        idle_timeout: float | None = None,
    ) -> Iterator[bytes]:
        read1 = getattr(response, "read1", None)
        if not callable(read1):
            if time.monotonic() >= deadline:
                kind = "stream_total" if idle_timeout is not None else "response_total"
                limit = self.stream_max_seconds if idle_timeout is not None else self.timeout
                raise _ResponseReadTimeout(kind, limit)
            chunk = response.read()
            if chunk:
                yield chunk
            return

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kind = "stream_total" if idle_timeout is not None else "response_total"
                limit = self.stream_max_seconds if idle_timeout is not None else self.timeout
                raise _ResponseReadTimeout(kind, limit)
            read_timeout = min(remaining, idle_timeout) if idle_timeout is not None else remaining
            self._set_response_timeout(response, read_timeout)
            try:
                chunk = read1(65_536)
            except TimeoutError as error:
                if idle_timeout is None:
                    kind = "response_total"
                    limit = self.timeout
                elif idle_timeout <= remaining:
                    kind = "stream_idle"
                    limit = idle_timeout
                else:
                    kind = "stream_total"
                    limit = self.stream_max_seconds
                raise _ResponseReadTimeout(kind, float(limit)) from error
            if not chunk:
                return
            yield chunk

    def _read_chat_sse_response(
        self,
        response,
        deadline: float,
        *,
        idle_timeout: float,
        stats: dict[str, object],
        request_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        response_id = ""
        response_model = self.model
        role = "assistant"
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, object]] = {}
        finish_reason: str | None = None
        usage: dict[str, object] = {}
        event_count = 0
        first_event_recorded = False
        stream_read_started = time.monotonic()
        projector = (
            NaturalLanguageStreamProjector()
            if self.on_natural_language_delta is not None
            else None
        )

        for event in self._iter_sse_json_events(
            response,
            deadline,
            idle_timeout=idle_timeout,
        ):
            event_count += 1
            stats["event_count"] = event_count
            event_after_ms = max(0, round((time.monotonic() - stream_read_started) * 1_000))
            stats["last_event_after_ms"] = event_after_ms
            if not first_event_recorded:
                first_event_recorded = True
                stats["first_event_after_ms"] = event_after_ms
                if self.logger is not None:
                    self.logger.record("model.stream_first_event", {"request_id": request_id})
            if isinstance(event.get("error"), dict):
                raise ValueError(str(event["error"].get("message", "stream failed")))
            response_id = str(event.get("id") or response_id)
            response_model = str(event.get("model") or response_model)
            if isinstance(event.get("usage"), dict):
                usage = dict(event["usage"])
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(delta.get("role"), str):
                role = delta["role"]
            delta_text = self._stream_delta_text(delta.get("content"))
            if delta_text:
                content_parts.append(delta_text)
                stats["content_chars"] = int(stats["content_chars"]) + len(delta_text)
                self._emit_stream_delta(delta_text)
                if projector is not None:
                    natural_deltas = projector.feed(delta_text)
                    for natural_delta in natural_deltas:
                        key = f"visible_{natural_delta.channel}_chars"
                        stats[key] = int(stats.get(key, 0)) + len(natural_delta.text)
                    self._emit_natural_language_deltas(natural_deltas)
            pieces = delta.get("tool_calls")
            if isinstance(pieces, list):
                for position, piece in enumerate(pieces):
                    if not isinstance(piece, dict):
                        continue
                    index = piece.get("index", position)
                    if not isinstance(index, int):
                        index = position
                    target = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if piece.get("id"):
                        target["id"] = str(piece["id"])
                    if piece.get("type"):
                        target["type"] = str(piece["type"])
                    function = piece.get("function")
                    if isinstance(function, dict):
                        target_function = target["function"]
                        if isinstance(target_function, dict):
                            target_function["name"] = str(target_function["name"]) + str(function.get("name") or "")
                            target_function["arguments"] = str(target_function["arguments"]) + str(function.get("arguments") or "")
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])

        if projector is not None:
            final_deltas = projector.finish()
            for natural_delta in final_deltas:
                key = f"visible_{natural_delta.channel}_chars"
                stats[key] = int(stats.get(key, 0)) + len(natural_delta.text)
            self._emit_natural_language_deltas(final_deltas)
            stats["natural_language_format"] = projector.input_mode

        message: dict[str, object] = {"role": role, "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        stats["tool_call_count"] = len(tool_calls)
        result: dict[str, object] = {
            "id": response_id,
            "object": "chat.completion",
            "model": response_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }
            ],
        }
        if usage:
            result["usage"] = usage
        return result, stats

    def _iter_sse_json_events(
        self,
        response,
        deadline: float,
        *,
        idle_timeout: float,
    ) -> Iterator[dict[str, object]]:
        decoder = getincrementaldecoder("utf-8")()
        buffer = ""
        data_lines: list[str] = []

        def decode_event() -> tuple[bool, dict[str, object] | None]:
            if not data_lines:
                return False, None
            raw = "\n".join(data_lines)
            data_lines.clear()
            if raw == "[DONE]":
                return True, None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("SSE data must be a JSON object")
            return False, value

        for chunk in self._iter_response_chunks(
            response,
            deadline,
            idle_timeout=idle_timeout,
        ):
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    done, event = decode_event()
                    if done:
                        return
                    if event is not None:
                        yield event
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

        buffer += decoder.decode(b"", final=True)
        if buffer:
            line = buffer.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        _done, event = decode_event()
        if event is not None:
            yield event

    def _stream_delta_text(self, content: object) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    def _emit_stream_delta(self, delta: str) -> None:
        if self.on_stream_delta is None:
            return
        try:
            self.on_stream_delta(delta)
        except Exception as error:
            if self.logger is not None:
                self.logger.record("model.stream_observer_error", {"reason": str(error)})

    def _emit_natural_language_deltas(
        self,
        deltas: list[NaturalLanguageDelta],
    ) -> None:
        if self.on_natural_language_delta is None:
            return
        for delta in deltas:
            try:
                self.on_natural_language_delta(delta)
            except Exception as error:
                if self.logger is not None:
                    self.logger.record(
                        "model.stream_observer_error",
                        {"channel": delta.channel, "reason": str(error)},
                    )

    def set_natural_language_stream_observer(
        self,
        observer: Callable[[NaturalLanguageDelta], None] | None,
    ) -> None:
        self.on_natural_language_delta = observer

    def _set_response_timeout(self, response, timeout: float) -> None:
        candidates = [
            getattr(response, "fp", None),
            getattr(getattr(response, "fp", None), "raw", None),
        ]
        for candidate in candidates:
            sock = getattr(candidate, "_sock", None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(timeout)
                return

    def _is_loopback_host(self, hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

__all__ = [
    "OpenAICompatibleModelClient",
    "StubModelClient",
]
