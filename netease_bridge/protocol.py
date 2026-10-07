"""A bounded, synchronous MCP JSON-RPC transport over text stdio.

The service owns tool schemas and results.  This module only implements the
protocol envelope, initialization state, and one-JSON-message-per-line framing.
"""

from __future__ import annotations

import json
from typing import Any, TextIO


SUPPORTED_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
MAX_REQUEST_BYTES = 1024 * 1024


def _error(request_id: str | int | None, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(request_id: str | int | None, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _reject_nonfinite(value: str) -> None:
    # Python otherwise accepts NaN and Infinity, which are not JSON numbers.
    raise ValueError("Invalid JSON number")


class McpServer:
    """Expose a service through a single MCP stdio session."""

    def __init__(self, service: Any):
        self.service = service
        self._initialization_started = False
        self._ready = False

    def handle(self, message: dict) -> dict | None:
        """Validate and dispatch one decoded JSON-RPC message.

        Valid notifications never receive responses.  Invalid envelopes have
        no usable request identity, so their error responses use a null ID.
        """
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return _error(None, -32600, "Invalid request")

        request_id = message.get("id")
        if "id" in message and not (
            request_id is None or type(request_id) in (str, int)
        ):
            return _error(None, -32600, "Invalid request")

        method = message["method"]
        params = message.get("params", {})
        if "id" not in message:
            if (
                method == "notifications/initialized"
                and isinstance(params, dict)
                and self._initialization_started
            ):
                self._ready = True
            return None

        if not isinstance(params, dict):
            return _error(request_id, -32602, "Parameters must be an object")

        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "ping":
            return _result(request_id, {})
        if method not in ("tools/list", "tools/call"):
            return _error(request_id, -32601, "Method not found")
        if not self._ready:
            return _error(request_id, -32000, "Initialization is not complete")

        try:
            if method == "tools/list":
                return _result(request_id, {"tools": self.service.list_tools()})
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                return _error(request_id, -32602, "Invalid tool parameters")
            return _result(request_id, self.service.call_tool(name, arguments))
        except KeyError:
            return _error(request_id, -32602, "Unknown tool")
        except ValueError as exc:
            # The service contract guarantees public-safe validation messages.
            return _error(request_id, -32602, str(exc))
        except Exception:
            return _error(request_id, -32603, "Internal error")

    def _initialize(self, request_id: str | int | None, params: dict) -> dict:
        client = params.get("clientInfo")
        if (
            not isinstance(params.get("protocolVersion"), str)
            or not isinstance(params.get("capabilities"), dict)
            or not isinstance(client, dict)
            or not isinstance(client.get("name"), str)
            or not isinstance(client.get("version"), str)
        ):
            return _error(request_id, -32602, "Invalid initialization parameters")
        if self._initialization_started:
            return _error(request_id, -32600, "Initialization already started")
        requested_version = params["protocolVersion"]
        version = (
            requested_version
            if requested_version in SUPPORTED_VERSIONS
            else SUPPORTED_VERSIONS[0]
        )
        self._initialization_started = True
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "netease-desktop-bridge", "version": "0.1.0"},
            },
        )

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        """Serve JSONL requests until EOF, writing only protocol responses.

        The limit counts UTF-8 bytes of the message, excluding its line ending.
        Reads are bounded even when a sender never supplies a newline.  An
        oversized message is drained through its newline before processing the
        next message, and produces one safe error response.
        """
        while True:
            line = input_stream.readline(MAX_REQUEST_BYTES + 2)
            if not line:
                return
            body = line[:-1] if line.endswith("\n") else line
            if line.endswith("\n") and body.endswith("\r"):
                body = body[:-1]

            try:
                oversized = len(body.encode("utf-8")) > MAX_REQUEST_BYTES
            except UnicodeEncodeError:
                # A text stream should be UTF-8; reject malformed Unicode
                # without exposing the offending input.
                self._discard_line_remainder(input_stream, line)
                response = _error(None, -32700, "Parse error")
            else:
                if oversized:
                    self._discard_line_remainder(input_stream, line)
                    response = _error(None, -32600, "Request exceeds maximum size")
                else:
                    try:
                        message = json.loads(body, parse_constant=_reject_nonfinite)
                    except (ValueError, RecursionError):
                        response = _error(None, -32700, "Parse error")
                    else:
                        response = self.handle(message)

            if response is not None:
                # Escaping non-ASCII characters also makes arbitrary string
                # IDs safe to write through a UTF-8 stream, including escaped
                # surrogate code units accepted by JSON parsers.
                try:
                    encoded = json.dumps(response, ensure_ascii=True, allow_nan=False)
                except (TypeError, ValueError, RecursionError):
                    encoded = json.dumps(
                        _error(response["id"], -32603, "Internal error"),
                        ensure_ascii=True,
                        allow_nan=False,
                    )
                output_stream.write(encoded + "\n")
                output_stream.flush()

    @staticmethod
    def _discard_line_remainder(input_stream: TextIO, line: str) -> None:
        while line and not line.endswith("\n"):
            line = input_stream.readline(MAX_REQUEST_BYTES + 2)
