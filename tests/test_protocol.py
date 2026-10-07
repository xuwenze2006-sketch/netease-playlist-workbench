"""Behavioral tests for the stdio JSON-RPC boundary."""

import io
import json
import unittest

from netease_bridge.protocol import McpServer


class FakeService:
    """A small service contract with observable, JSON-safe results."""

    def list_tools(self):
        return [
            {
                "name": "echo_tracks",
                "description": "Return a small Unicode track page.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"offset": {"type": "integer"}},
                },
            }
        ]

    def call_tool(self, name, arguments):
        if name == "explode":
            raise RuntimeError("private-secret-request-token")
        if name == "tool_error":
            return {
                "content": [{"type": "text", "text": "Cached playlist is unavailable"}],
                "isError": True,
            }
        if name == "unserializable":
            return {
                "content": [{"type": "text", "text": object()}],
                "isError": False,
            }
        if name != "echo_tracks":
            raise KeyError(name)
        if arguments != {"offset": 0}:
            raise ValueError("offset must be zero in this fixture")
        return {
            "content": [{"type": "text", "text": "歌曲：平凡之路"}],
            "structuredContent": {"tracks": [{"id": 1, "name": "平凡之路"}]},
            "isError": False,
        }


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialize_message(version="2025-11-25", request_id=1):
    return request(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "protocol-test", "version": "1.0"},
        },
        request_id,
    )


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeService())

    def ready(self):
        self.server.handle(initialize_message())
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def assert_error(self, response, code, request_id=1):
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], request_id)
        self.assertEqual(response["error"]["code"], code)
        self.assertIsInstance(response["error"]["message"], str)
        self.assertNotIn("result", response)

    def test_initialize_negotiates_supported_and_unknown_versions(self):
        for version, expected in (
            ("2025-11-25", "2025-11-25"),
            ("2025-06-18", "2025-06-18"),
            ("2025-03-26", "2025-03-26"),
            ("2024-11-05", "2024-11-05"),
            ("future-version", "2025-11-25"),
        ):
            with self.subTest(version=version):
                response = McpServer(FakeService()).handle(initialize_message(version))
                self.assertEqual(response["jsonrpc"], "2.0")
                self.assertEqual(response["id"], 1)
                self.assertEqual(response["result"]["protocolVersion"], expected)
                self.assertIn("tools", response["result"]["capabilities"])
                self.assertIsInstance(response["result"]["serverInfo"]["name"], str)
                self.assertIsInstance(response["result"]["serverInfo"]["version"], str)

    def test_tools_require_initialize_response_and_initialized_notification(self):
        for method in ("tools/list", "tools/call"):
            params = {"name": "echo_tracks", "arguments": {"offset": 0}}
            self.assert_error(self.server.handle(request(method, params)), -32000)
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assert_error(self.server.handle(request("tools/list")), -32000)
        self.server.handle(initialize_message())
        self.assert_error(self.server.handle(request("tools/list")), -32000)
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.server.handle(request("tools/list"))["result"]["tools"][0]["name"], "echo_tracks")

    def test_ping_succeeds_before_and_after_initialization(self):
        self.assertEqual(self.server.handle(request("ping", request_id="before")), {"jsonrpc": "2.0", "id": "before", "result": {}})
        self.ready()
        self.assertEqual(self.server.handle(request("ping", request_id=None)), {"jsonrpc": "2.0", "id": None, "result": {}})

    def test_tools_call_preserves_mcp_content_and_structured_unicode_result(self):
        self.ready()
        response = self.server.handle(request("tools/call", {"name": "echo_tracks", "arguments": {"offset": 0}}, "tool-1"))
        self.assertEqual(response["id"], "tool-1")
        self.assertEqual(response["result"], {
            "content": [{"type": "text", "text": "歌曲：平凡之路"}],
            "structuredContent": {"tracks": [{"id": 1, "name": "平凡之路"}]},
            "isError": False,
        })

    def test_tool_application_error_remains_a_tool_result(self):
        self.ready()
        response = self.server.handle(request("tools/call", {"name": "tool_error", "arguments": {}}))
        self.assertNotIn("error", response)
        self.assertEqual(response["result"], {
            "content": [{"type": "text", "text": "Cached playlist is unavailable"}],
            "isError": True,
        })

    def test_duplicate_initialize_is_rejected_without_resetting_ready_session(self):
        self.ready()
        self.assert_error(self.server.handle(initialize_message(request_id=2)), -32600, 2)
        self.assertEqual(self.server.handle(request("tools/list"))["result"]["tools"][0]["name"], "echo_tracks")

    def test_malformed_initialized_notification_does_not_complete_handshake(self):
        self.server.handle(initialize_message())
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": []}))
        self.assert_error(self.server.handle(request("tools/list")), -32000)

    def test_valid_notifications_never_reply_or_dispatch_tool_requests(self):
        self.ready()
        for method, params in (
            ("ping", {}),
            ("unknown/private", {}),
            ("tools/call", {"name": "explode", "arguments": {}}),
            ("tools/list", []),
        ):
            with self.subTest(method=method):
                self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": method, "params": params}))

    def test_invalid_envelopes_and_ids_return_safe_invalid_request(self):
        invalid = [
            [], None, "secret-payload", {},
            {"jsonrpc": "1.0", "method": "ping", "id": 1},
            {"jsonrpc": "2.0", "method": 1, "id": 1},
            {"jsonrpc": "2.0", "method": "ping", "id": True},
            {"jsonrpc": "2.0", "method": "ping", "id": 1.5},
            {"jsonrpc": "2.0", "method": "ping", "id": {"secret-payload": 1}},
        ]
        for message in invalid:
            with self.subTest(message=message):
                response = self.server.handle(message)
                self.assert_error(response, -32600, None)
                self.assertNotIn("secret-payload", json.dumps(response))

    def test_unknown_method_returns_method_not_found_without_echoing_method(self):
        response = self.server.handle(request("private-secret-request-token"))
        self.assert_error(response, -32601)
        self.assertNotIn("private-secret-request-token", json.dumps(response))

    def test_nonobject_params_are_invalid_and_do_not_initialize(self):
        for params in ([], None, "private-secret-request-token", True, 4):
            with self.subTest(params=params):
                message = initialize_message()
                message["params"] = params
                response = self.server.handle(message)
                self.assert_error(response, -32602)
                self.assertNotIn("private-secret-request-token", json.dumps(response))
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assert_error(self.server.handle(request("tools/list")), -32000)

    def test_missing_or_wrong_initialize_fields_are_invalid(self):
        for params in (
            {},
            {"protocolVersion": 3, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            {"protocolVersion": "2025-11-25", "capabilities": [], "clientInfo": {"name": "test", "version": "1"}},
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}},
        ):
            with self.subTest(params=params):
                self.assert_error(self.server.handle(request("initialize", params)), -32602)

    def test_initialize_notification_cannot_enable_tools(self):
        message = initialize_message()
        del message["id"]
        self.assertIsNone(self.server.handle(message))
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assert_error(self.server.handle(request("tools/list")), -32000)

    def test_tool_parameters_and_service_errors_use_safe_protocol_errors(self):
        self.ready()
        for params in ({}, {"name": 1}, {"name": "echo_tracks", "arguments": []}, {"name": "echo_tracks", "arguments": None}):
            with self.subTest(params=params):
                self.assert_error(self.server.handle(request("tools/call", params)), -32602)
        unknown = self.server.handle(request("tools/call", {"name": "private-secret-request-token", "arguments": {}}))
        self.assert_error(unknown, -32602)
        self.assertNotIn("private-secret-request-token", json.dumps(unknown))
        self.assert_error(self.server.handle(request("tools/call", {"name": "echo_tracks", "arguments": {"offset": 2}})), -32602)
        unexpected = self.server.handle(request("tools/call", {"name": "explode", "arguments": {}}))
        self.assert_error(unexpected, -32603)
        self.assertNotIn("private-secret-request-token", json.dumps(unexpected))

    def test_serve_handles_bad_json_nonobjects_and_recovers_until_eof(self):
        inputs = [
            '{"private-secret-request-token":',
            json.dumps([]),
            json.dumps(initialize_message()),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(request("tools/call", {"name": "echo_tracks", "arguments": {"offset": 0}}, "中文请求")),
            json.dumps(request("ping", request_id=3)),
        ]
        output = io.StringIO()
        self.server.serve(io.StringIO("\n".join(inputs)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        self.assert_error(responses[0], -32700, None)
        self.assert_error(responses[1], -32600, None)
        self.assertEqual(responses[2]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[3]["id"], "中文请求")
        self.assertEqual(responses[3]["result"]["structuredContent"]["tracks"][0]["name"], "平凡之路")
        self.assertEqual(responses[4], {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertNotIn("private-secret-request-token", output.getvalue())

    def test_serve_rejects_nonfinite_json_numbers(self):
        output = io.StringIO()
        self.server.serve(io.StringIO('{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'), output)
        self.assert_error(json.loads(output.getvalue()), -32700, None)

    def test_serve_discards_one_oversized_line_then_recovers(self):
        oversized = 'private-secret-request-token' * 50000
        valid = json.dumps(request("ping", request_id="after-long"))
        output = io.StringIO()
        self.server.serve(io.StringIO(oversized + "\n" + valid + "\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assert_error(responses[0], -32600, None)
        self.assertNotIn("private-secret-request-token", json.dumps(responses[0]))
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": "after-long", "result": {}})

    def test_serve_applies_size_limit_to_utf8_bytes_and_accepts_exact_limit(self):
        valid = json.dumps(request("ping", request_id="boundary"))
        at_limit = valid + " " * (1024 * 1024 - len(valid.encode("utf-8")))
        too_large = json.dumps(request("ping", {"payload": "中" * 350000}), ensure_ascii=False)
        output = io.StringIO()
        self.server.serve(io.StringIO(at_limit + "\r\n" + too_large + "\n" + valid), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["id"], "boundary")
        self.assert_error(responses[1], -32600, None)
        self.assertEqual(responses[2]["id"], "boundary")

    def test_serve_empty_input_emits_nothing(self):
        output = io.StringIO()
        self.server.serve(io.StringIO(), output)
        self.assertEqual(output.getvalue(), "")

    def test_serve_oversized_eof_emits_one_error(self):
        output = io.StringIO()
        self.server.serve(io.StringIO("x" * (2 * 1024 * 1024)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 1)
        self.assert_error(responses[0], -32600, None)

    def test_serve_unserializable_service_result_returns_safe_error_then_recovers(self):
        self.ready()
        lines = [
            json.dumps(request("tools/call", {"name": "unserializable", "arguments": {}})),
            json.dumps(request("ping", request_id="after-error")),
        ]
        output = io.StringIO()
        try:
            self.server.serve(io.StringIO("\n".join(lines)), output)
        except TypeError:
            self.fail("A malformed service result interrupted the protocol session")
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assert_error(responses[0], -32603)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": "after-error", "result": {}})
        self.assertNotIn("object", output.getvalue())


if __name__ == "__main__":
    unittest.main()
