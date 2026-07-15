from __future__ import annotations

import http.client
import json
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

from .config import MCPServerConfig


MAX_MCP_MESSAGE_BYTES = 10 * 1024 * 1024
MAX_PENDING_MCP_RESPONSES = 8
MAX_MCP_STDERR_LINE_BYTES = 16 * 1024


class MCPTransport(Protocol):
    def connect(self) -> None:
        """Open the underlying transport connection."""

    def close(self) -> None:
        """Close the underlying transport connection."""

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one protocol request and return the decoded response."""

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send one protocol notification."""


class MCPTransportError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.metadata = metadata or {}


class UnsupportedMCPTransport(MCPTransportError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="mcp_transport_unsupported")


class MCPTransportConnectFailed(MCPTransportError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="mcp_transport_connect_failed", metadata=metadata)


class MCPTransportTimeout(MCPTransportError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="mcp_transport_timeout", metadata=metadata)


class MCPTransportReadTimeout(MCPTransportError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="mcp_transport_read_timeout", metadata=metadata)


class MCPTransportClosed(MCPTransportError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="mcp_transport_closed", metadata=metadata)


class MCPHTTPError(MCPTransportError):
    def __init__(self, message: str, *, status: int, body: str = "") -> None:
        super().__init__(
            message,
            error_code="mcp_http_error",
            metadata={"http_status": status, "body": body},
        )


class MCPProtocolError(MCPTransportError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="mcp_protocol_error", metadata=metadata)


def _message_too_large(config: MCPServerConfig, size: int) -> MCPProtocolError:
    return MCPProtocolError(
        f"MCP message exceeded {MAX_MCP_MESSAGE_BYTES} bytes for server '{config.name}'",
        metadata={
            "server_name": config.name,
            "transport": config.transport,
            "message_bytes": size,
            "max_message_bytes": MAX_MCP_MESSAGE_BYTES,
        },
    )


def _read_bounded_body(response: http.client.HTTPResponse, config: MCPServerConfig) -> bytes:
    raw_content_length = response.getheader("Content-Length")
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except (TypeError, ValueError):
            content_length = -1
        if content_length > MAX_MCP_MESSAGE_BYTES:
            raise _message_too_large(config, content_length)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, MAX_MCP_MESSAGE_BYTES - total + 1))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MCP_MESSAGE_BYTES:
            raise _message_too_large(config, total)


@dataclass(slots=True)
class StreamableHttpTransport:
    config: MCPServerConfig
    _parsed_url: SplitResult = field(init=False)
    _connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = field(default=None, init=False)
    _request_ids: count = field(default_factory=lambda: count(1), init=False)
    session_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._parsed_url = urlsplit(self.config.url)
        if self._parsed_url.scheme not in {"http", "https"}:
            raise UnsupportedMCPTransport(
                f"Unsupported MCP streamable_http URL scheme '{self._parsed_url.scheme}' for server '{self.config.name}'"
            )
        if not self._parsed_url.netloc:
            raise UnsupportedMCPTransport(f"Missing MCP endpoint host for server '{self.config.name}'")

    def connect(self) -> None:
        if self._connection is not None:
            return

        connection_cls: type[http.client.HTTPConnection] | type[http.client.HTTPSConnection]
        if self._parsed_url.scheme == "https":
            connection_cls = http.client.HTTPSConnection
        else:
            connection_cls = http.client.HTTPConnection

        try:
            connection = connection_cls(
                self._parsed_url.hostname,
                self._parsed_url.port,
                timeout=self.config.timeout,
            )
            connection.connect()
        except socket.timeout as exc:
            raise MCPTransportTimeout(
                f"MCP connection timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except OSError as exc:
            raise MCPTransportConnectFailed(
                f"MCP connection failed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc

        self._connection = connection

    def close(self) -> None:
        self._drop_connection()
        self.session_id = None

    def _drop_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._request_ids),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        return self._send(payload, expect_response=True)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._send(payload, expect_response=False)

    def _send(self, payload: dict[str, Any], *, expect_response: bool) -> dict[str, Any]:
        self.connect()
        if self._connection is None:
            raise MCPTransportClosed(f"MCP connection is unavailable for server '{self.config.name}'")

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        try:
            self._connection.request("POST", self._target_path, body=body, headers=headers)
            response = self._connection.getresponse()
        except socket.timeout as exc:
            self.close()
            raise MCPTransportTimeout(
                f"MCP request timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except http.client.HTTPException as exc:
            self.close()
            raise MCPTransportClosed(
                f"MCP connection closed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except OSError as exc:
            self.close()
            raise MCPTransportConnectFailed(
                f"MCP request failed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc

        self.session_id = response.getheader("Mcp-Session-Id") or self.session_id
        content_type = response.getheader("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if response.status < 400 and expect_response and media_type == "text/event-stream":
            request_id = payload.get("id")
            return self._read_sse_response(response, request_id=request_id)

        raw_body = self._read_response_body(response)
        decoded_body = raw_body.decode("utf-8", errors="replace").strip()

        if response.status >= 400:
            raise MCPHTTPError(
                f"MCP HTTP error for server '{self.config.name}': {response.status}",
                status=response.status,
                body=decoded_body,
            )
        if not expect_response:
            return {}
        if response.status == 202 or not decoded_body:
            return {}
        try:
            parsed = json.loads(decoded_body)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError(
                f"MCP response was not valid JSON for server '{self.config.name}'",
                metadata={"body": decoded_body},
            ) from exc
        return _select_jsonrpc_response(parsed, request_id=payload.get("id"))

    @property
    def _target_path(self) -> str:
        path = self._parsed_url.path or "/"
        if self._parsed_url.query:
            return f"{path}?{self._parsed_url.query}"
        return path

    def _read_response_body(self, response: http.client.HTTPResponse) -> bytes:
        sock = getattr(self._connection, "sock", None)
        previous_timeout = None
        if sock is not None:
            previous_timeout = sock.gettimeout()
            sock.settimeout(self.config.read_timeout)
        try:
            return _read_bounded_body(response, self.config)
        except socket.timeout as exc:
            self.close()
            raise MCPTransportReadTimeout(
                f"MCP response read timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except MCPProtocolError:
            self.close()
            raise
        finally:
            if sock is not None and previous_timeout is not None:
                try:
                    sock.settimeout(previous_timeout)
                except OSError:
                    self.close()

    def _read_sse_response(self, response: http.client.HTTPResponse, *, request_id: object) -> dict[str, Any]:
        sock = getattr(self._connection, "sock", None)
        previous_timeout = None
        if sock is not None:
            previous_timeout = sock.gettimeout()
            sock.settimeout(self.config.read_timeout)

        data_lines: list[str] = []
        event_bytes = 0
        matched_response: dict[str, Any] | None = None
        protocol_error: MCPProtocolError | None = None
        try:
            while True:
                raw_line = response.readline(MAX_MCP_MESSAGE_BYTES + 1)
                if not raw_line:
                    break
                raw_size = (
                    len(raw_line)
                    if isinstance(raw_line, bytes)
                    else len(raw_line.encode("utf-8"))
                )
                if raw_size > MAX_MCP_MESSAGE_BYTES:
                    raise _message_too_large(self.config, raw_size)
                line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                stripped = line.strip()
                if not stripped:
                    if data_lines:
                        candidates = _parse_sse_event(data_lines)
                        data_lines = []
                        event_bytes = 0
                        for candidate in candidates:
                            if candidate.get("id") == request_id:
                                matched_response = candidate
                                break
                        if matched_response is not None:
                            break
                    continue
                if stripped.startswith(":"):
                    continue
                if stripped.startswith("data:"):
                    event_bytes += raw_size
                    if event_bytes > MAX_MCP_MESSAGE_BYTES:
                        raise _message_too_large(self.config, event_bytes)
                    data_lines.append(stripped[5:].lstrip())

            if data_lines:
                for candidate in _parse_sse_event(data_lines):
                    if candidate.get("id") == request_id:
                        matched_response = candidate
                        break
        except socket.timeout as exc:
            self.close()
            raise MCPTransportReadTimeout(
                f"MCP response read timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except MCPProtocolError as exc:
            protocol_error = exc
        finally:
            if sock is not None and previous_timeout is not None:
                try:
                    sock.settimeout(previous_timeout)
                except OSError:
                    self.close()

        if protocol_error is not None:
            self._drop_connection()
            raise protocol_error

        if matched_response is not None:
            # A Streamable HTTP SSE response may remain open after the matching
            # JSON-RPC response arrives. It cannot be reused for another POST,
            # so discard only the HTTP connection while preserving the MCP
            # session id for the next connection.
            self._drop_connection()
            return matched_response

        self._drop_connection()
        raise MCPProtocolError(
            f"MCP SSE response did not contain the response for request '{request_id}'",
            metadata={"request_id": request_id},
        )


def _parse_sse_jsonrpc(body: str) -> dict[str, Any]:
    json_objects: list[dict[str, Any]] = []
    data_lines: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if data_lines:
                json_objects.extend(_parse_sse_event(data_lines))
                data_lines = []
            continue
        if stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            data_lines.append(stripped[5:].lstrip())

    if data_lines:
        json_objects.extend(_parse_sse_event(data_lines))

    if not json_objects:
        raise MCPProtocolError("MCP SSE response did not contain a JSON-RPC payload", metadata={"body": body})
    return json_objects[-1]


def _parse_sse_event(data_lines: list[str]) -> list[dict[str, Any]]:
    payload = "\n".join(data_lines).strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MCPProtocolError("MCP SSE event was not valid JSON", metadata={"body": payload}) from exc
    return _decode_jsonrpc_messages(parsed)


def _decode_jsonrpc_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        messages = [payload]
    elif isinstance(payload, list) and payload and all(isinstance(item, dict) for item in payload):
        messages = list(payload)
    else:
        raise MCPProtocolError("MCP message was not a JSON-RPC object or batch", metadata={"body": payload})

    for message in messages:
        if message.get("jsonrpc") != "2.0":
            raise MCPProtocolError(
                "MCP message did not declare JSON-RPC 2.0",
                metadata={"jsonrpc": message.get("jsonrpc")},
            )
    return messages


def _queue_jsonrpc_response(
    responses: "queue.Queue[dict[str, Any]]",
    message: dict[str, Any],
) -> None:
    if "id" not in message or not ("result" in message or "error" in message):
        return
    try:
        responses.put_nowait(message)
    except queue.Full:
        try:
            responses.get_nowait()
        except queue.Empty:
            pass
        try:
            responses.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"message": "MCP response queue exceeded its limit"},
                }
            )
        except queue.Full:
            pass


def _select_jsonrpc_response(payload: Any, *, request_id: object) -> dict[str, Any]:
    for message in _decode_jsonrpc_messages(payload):
        if message.get("id") == request_id:
            return message
    raise MCPProtocolError(
        f"MCP response did not contain the response for request '{request_id}'",
        metadata={"request_id": request_id},
    )


@dataclass(slots=True)
class StdioTransport:
    config: MCPServerConfig
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _request_ids: count = field(default_factory=lambda: count(1), init=False)
    _responses: "queue.Queue[dict[str, Any]]" = field(
        default_factory=lambda: queue.Queue(maxsize=MAX_PENDING_MCP_RESPONSES),
        init=False,
    )
    _reader_thread: threading.Thread | None = field(default=None, init=False)
    _stderr_thread: threading.Thread | None = field(default=None, init=False)
    _stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20), init=False)
    _closing: bool = field(default=False, init=False)

    def connect(self) -> None:
        if self._process is not None:
            return
        self._closing = False
        if not self.config.command:
            raise UnsupportedMCPTransport(f"Missing stdio command for server '{self.config.name}'")

        merged_env = None
        if self.config.env:
            merged_env = {**os.environ, **self.config.env}

        try:
            process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
            )
        except FileNotFoundError as exc:
            raise MCPTransportConnectFailed(
                f"MCP stdio command not found for server '{self.config.name}': {self.config.command}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except OSError as exc:
            raise MCPTransportConnectFailed(
                f"MCP stdio launch failed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc

        self._process = process
        self._reader_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()

    def close(self) -> None:
        self._closing = True
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._request_ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write_message(payload)
        return self._wait_for_response(request_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write_message(payload)

    def _write_message(self, payload: dict[str, Any]) -> None:
        self.connect()
        if self._process is None or self._process.stdin is None:
            raise MCPTransportClosed(
                f"MCP stdio transport is unavailable for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            )

        message = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._process.stdin.write(message)
            self._process.stdin.flush()
        except BrokenPipeError as exc:
            raise MCPTransportClosed(
                self._closed_message("stdin pipe closed"),
                metadata={"server_name": self.config.name, "transport": self.config.transport, "stderr": list(self._stderr_tail)},
            ) from exc

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.read_timeout
        pending: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTransportReadTimeout(
                    f"MCP stdio response read timed out for server '{self.config.name}'",
                    metadata={"server_name": self.config.name, "transport": self.config.transport},
                )
            if self._process is not None and self._process.poll() is not None:
                raise MCPTransportClosed(
                    self._closed_message(f"process exited with code {self._process.returncode}"),
                    metadata={"server_name": self.config.name, "transport": self.config.transport, "stderr": list(self._stderr_tail)},
                )
            try:
                message = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise MCPTransportReadTimeout(
                    f"MCP stdio response read timed out for server '{self.config.name}'",
                    metadata={"server_name": self.config.name, "transport": self.config.transport},
                ) from exc
            if message.get("id") == request_id:
                for item in pending:
                    _queue_jsonrpc_response(self._responses, item)
                return message
            if message.get("id") is None and message.get("error"):
                raise MCPTransportClosed(
                    self._closed_message(str(message["error"].get("message", "stdio reader failed"))),
                    metadata={"server_name": self.config.name, "transport": self.config.transport, "stderr": list(self._stderr_tail)},
                )
            if "id" in message:
                pending.append(message)

    def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        stdout = process.stdout
        try:
            while True:
                first_line = stdout.readline(MAX_MCP_MESSAGE_BYTES + 1)
                if not first_line:
                    self._signal_reader_closed("stdout reached EOF")
                    return
                if len(first_line) > MAX_MCP_MESSAGE_BYTES:
                    raise _message_too_large(self.config, len(first_line))
                if first_line in {b"\n", b"\r\n"}:
                    continue
                stripped = first_line.strip()
                if stripped.startswith(b"{"):
                    message = json.loads(stripped.decode("utf-8"))
                    for item in _decode_jsonrpc_messages(message):
                        _queue_jsonrpc_response(self._responses, item)
                    continue

                if stripped.startswith(b"["):
                    message = json.loads(stripped.decode("utf-8"))
                    for item in _decode_jsonrpc_messages(message):
                        _queue_jsonrpc_response(self._responses, item)
                    continue

                headers: dict[str, str] = {}
                line = first_line
                header_bytes = 0
                while line and line not in {b"\n", b"\r\n"}:
                    header_bytes += len(line)
                    if header_bytes > MAX_MCP_MESSAGE_BYTES:
                        raise _message_too_large(self.config, header_bytes)
                    name, _, value = line.decode("utf-8", errors="replace").partition(":")
                    headers[name.strip().lower()] = value.strip()
                    line = stdout.readline(MAX_MCP_MESSAGE_BYTES - header_bytes + 1)
                content_length = int(headers.get("content-length", "0"))
                if content_length <= 0:
                    continue
                if content_length > MAX_MCP_MESSAGE_BYTES:
                    raise _message_too_large(self.config, content_length)
                body = stdout.read(content_length)
                if not body:
                    self._signal_reader_closed("stdout reached EOF")
                    return
                message = json.loads(body.decode("utf-8"))
                for item in _decode_jsonrpc_messages(message):
                    _queue_jsonrpc_response(self._responses, item)
        except Exception as exc:
            self._signal_reader_closed(f"stdio reader failed: {exc}")

    def _read_stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw_line = process.stderr.readline(MAX_MCP_STDERR_LINE_BYTES + 1)
            if not raw_line:
                return
            truncated = len(raw_line) > MAX_MCP_STDERR_LINE_BYTES or not raw_line.endswith((b"\n", b"\r"))
            line = raw_line[:MAX_MCP_STDERR_LINE_BYTES].decode("utf-8", errors="replace").rstrip()
            if truncated:
                line += " [truncated]"
            if line:
                self._stderr_tail.append(line)

    def _closed_message(self, reason: str) -> str:
        if self._stderr_tail:
            return f"MCP stdio transport closed for server '{self.config.name}': {reason}. stderr: {' | '.join(self._stderr_tail)}"
        return f"MCP stdio transport closed for server '{self.config.name}': {reason}"

    def _signal_reader_closed(self, reason: str) -> None:
        if self._closing:
            return
        _queue_jsonrpc_response(
            self._responses,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"message": reason},
            }
        )


@dataclass(slots=True)
class SSETransport:
    config: MCPServerConfig
    _parsed_url: SplitResult = field(init=False)
    _stream_connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = field(default=None, init=False)
    _stream_response: http.client.HTTPResponse | Any | None = field(default=None, init=False)
    _reader_thread: threading.Thread | None = field(default=None, init=False)
    _request_ids: count = field(default_factory=lambda: count(1), init=False)
    _responses: "queue.Queue[dict[str, Any]]" = field(
        default_factory=lambda: queue.Queue(maxsize=MAX_PENDING_MCP_RESPONSES),
        init=False,
    )
    _endpoint_ready: threading.Event = field(default_factory=threading.Event, init=False)
    _message_endpoint: str | None = field(default=None, init=False)
    _closing: bool = field(default=False, init=False)
    _connect_error: MCPTransportError | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._parsed_url = urlsplit(self.config.url)
        if self._parsed_url.scheme not in {"http", "https"}:
            raise UnsupportedMCPTransport(
                f"Unsupported MCP SSE URL scheme '{self._parsed_url.scheme}' for server '{self.config.name}'"
            )
        if not self._parsed_url.netloc:
            raise UnsupportedMCPTransport(f"Missing MCP SSE endpoint host for server '{self.config.name}'")

    def connect(self) -> None:
        if self._stream_connection is not None:
            return
        self._closing = False
        self._connect_error = None
        connection = self._build_connection(self._parsed_url, timeout=self.config.timeout)
        try:
            connection.connect()
            connection.request("GET", self._target_path(self._parsed_url), headers={"Accept": "text/event-stream", **self.config.headers})
            response = connection.getresponse()
        except socket.timeout as exc:
            raise MCPTransportTimeout(
                f"MCP SSE connect timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except OSError as exc:
            raise MCPTransportConnectFailed(
                f"MCP SSE connect failed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc

        if response.status >= 400:
            try:
                body = _read_bounded_body(response, self.config).decode("utf-8", errors="replace")
            finally:
                connection.close()
            raise MCPHTTPError(
                f"MCP SSE HTTP error for server '{self.config.name}': {response.status}",
                status=response.status,
                body=body,
            )

        self._stream_connection = connection
        self._stream_response = response
        stream_socket = getattr(connection, "sock", None)
        if stream_socket is not None:
            stream_socket.settimeout(self.config.read_timeout)
        self._reader_thread = threading.Thread(target=self._read_sse_loop, daemon=True)
        self._reader_thread.start()
        if not self._endpoint_ready.wait(timeout=self.config.timeout):
            self.close()
            raise MCPTransportTimeout(
                f"MCP SSE endpoint negotiation timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            )
        if self._connect_error is not None:
            error = self._connect_error
            self.close()
            raise error

    def close(self) -> None:
        self._closing = True
        if self._stream_connection is not None:
            self._stream_connection.close()
        self._stream_connection = None
        self._stream_response = None
        self._message_endpoint = None
        self._connect_error = None
        self._endpoint_ready.clear()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._request_ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._post_message(payload)
        return self._wait_for_response(request_id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post_message(payload)

    def _post_message(self, payload: dict[str, Any]) -> None:
        self.connect()
        if not self._message_endpoint:
            raise MCPTransportClosed(
                f"MCP SSE message endpoint is unavailable for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            )
        endpoint = urlsplit(self._message_endpoint)
        connection = self._build_connection(endpoint, timeout=self.config.timeout)
        body = json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", **self.config.headers}
        try:
            connection.connect()
            connection.request("POST", self._target_path(endpoint), body=body, headers=headers)
            response = connection.getresponse()
            response_body = _read_bounded_body(response, self.config).decode("utf-8", errors="replace").strip()
        except socket.timeout as exc:
            connection.close()
            raise MCPTransportTimeout(
                f"MCP SSE POST timed out for server '{self.config.name}'",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        except OSError as exc:
            connection.close()
            raise MCPTransportConnectFailed(
                f"MCP SSE POST failed for server '{self.config.name}': {exc}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            ) from exc
        finally:
            connection.close()

        if response.status >= 400:
            raise MCPHTTPError(
                f"MCP SSE HTTP error for server '{self.config.name}': {response.status}",
                status=response.status,
                body=response_body,
            )
        if response_body:
            try:
                parsed = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise MCPProtocolError(
                    f"MCP SSE POST response was not valid JSON for server '{self.config.name}'",
                    metadata={"body": response_body},
                ) from exc
            for item in _decode_jsonrpc_messages(parsed):
                _queue_jsonrpc_response(self._responses, item)

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.read_timeout
        pending: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTransportReadTimeout(
                    f"MCP SSE response read timed out for server '{self.config.name}'",
                    metadata={"server_name": self.config.name, "transport": self.config.transport},
                )
            try:
                message = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise MCPTransportReadTimeout(
                    f"MCP SSE response read timed out for server '{self.config.name}'",
                    metadata={"server_name": self.config.name, "transport": self.config.transport},
                ) from exc
            if message.get("id") == request_id:
                for item in pending:
                    _queue_jsonrpc_response(self._responses, item)
                return message
            if message.get("id") is None and message.get("error"):
                raise MCPTransportClosed(
                    f"MCP SSE transport closed for server '{self.config.name}': {message['error'].get('message', 'reader failed')}",
                    metadata={"server_name": self.config.name, "transport": self.config.transport},
                )
            if "id" in message:
                pending.append(message)

    def _read_sse_loop(self) -> None:
        response = self._stream_response
        if response is None:
            return
        event_name = "message"
        data_lines: list[str] = []
        event_bytes = 0
        try:
            while True:
                # Read through HTTPResponse so http.client can remove chunked
                # transfer framing before the SSE parser sees event lines.
                line = response.readline(MAX_MCP_MESSAGE_BYTES + 1)
                if not line:
                    self._signal_reader_closed("SSE stream reached EOF")
                    return
                raw_size = len(line) if isinstance(line, bytes) else len(line.encode("utf-8"))
                if raw_size > MAX_MCP_MESSAGE_BYTES:
                    raise _message_too_large(self.config, raw_size)
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                stripped = line.strip()
                if not stripped:
                    self._handle_sse_event(event_name, data_lines)
                    event_name = "message"
                    data_lines = []
                    event_bytes = 0
                    continue
                if stripped.startswith(":"):
                    continue
                if stripped.startswith("event:"):
                    event_name = stripped[6:].strip() or "message"
                    continue
                if stripped.startswith("data:"):
                    event_bytes += raw_size
                    if event_bytes > MAX_MCP_MESSAGE_BYTES:
                        raise _message_too_large(self.config, event_bytes)
                    data_lines.append(stripped[5:].lstrip())
        except Exception as exc:
            if not self._endpoint_ready.is_set():
                self._connect_error = (
                    exc
                    if isinstance(exc, MCPTransportError)
                    else MCPProtocolError(
                        f"MCP SSE reader failed for server '{self.config.name}': {exc}"
                    )
                )
                self._endpoint_ready.set()
            else:
                self._signal_reader_closed(f"sse reader failed: {exc}")

    def _handle_sse_event(self, event_name: str, data_lines: list[str]) -> None:
        payload = "\n".join(data_lines).strip()
        if not payload:
            return
        if event_name == "endpoint":
            self._message_endpoint = self._resolve_endpoint(payload)
            self._endpoint_ready.set()
            return
        parsed = json.loads(payload)
        for item in _decode_jsonrpc_messages(parsed):
            _queue_jsonrpc_response(self._responses, item)

    def _resolve_endpoint(self, endpoint: str) -> str:
        resolved = urlsplit(urljoin(self.config.url, endpoint))
        if not self._same_origin(self._parsed_url, resolved):
            raise MCPProtocolError(
                f"MCP SSE endpoint for server '{self.config.name}' must remain on the configured origin",
                metadata={"endpoint": endpoint},
            )
        return resolved.geturl()

    def _same_origin(self, left: SplitResult, right: SplitResult) -> bool:
        def effective_port(value: SplitResult) -> int | None:
            if value.port is not None:
                return value.port
            return 443 if value.scheme == "https" else 80 if value.scheme == "http" else None

        return (
            left.scheme == right.scheme
            and left.hostname == right.hostname
            and effective_port(left) == effective_port(right)
        )

    def _signal_reader_closed(self, reason: str) -> None:
        if self._closing:
            return
        if not self._endpoint_ready.is_set():
            self._connect_error = MCPTransportClosed(
                f"MCP SSE transport closed for server '{self.config.name}': {reason}",
                metadata={"server_name": self.config.name, "transport": self.config.transport},
            )
            self._endpoint_ready.set()
            return
        _queue_jsonrpc_response(
            self._responses,
            {"jsonrpc": "2.0", "id": None, "error": {"message": reason}},
        )

    def _build_connection(self, parsed_url: SplitResult, *, timeout: float):
        if parsed_url.scheme == "https":
            return http.client.HTTPSConnection(parsed_url.hostname, parsed_url.port, timeout=timeout)
        return http.client.HTTPConnection(parsed_url.hostname, parsed_url.port, timeout=timeout)

    def _target_path(self, parsed_url: SplitResult) -> str:
        path = parsed_url.path or "/"
        if parsed_url.query:
            return f"{path}?{parsed_url.query}"
        return path
