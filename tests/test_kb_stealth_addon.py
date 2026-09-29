"""kb_stealth_addon hooks, driven through mitmproxy's own test utilities.

Run with a python that can import mitmproxy (for example the pool bridge venv);
the plain python3 suite skips these tests.
"""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from mitmproxy import websocket
    from mitmproxy.http import Headers, Request
    from mitmproxy.test import taddons, tflow
    from wsproto.frame_protocol import Opcode
except ImportError:  # pragma: no cover - depends on the interpreter
    websocket = None

import kb_remote_proxy as proxy

SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
DEV = {"role": "developer", "content": "rules"}
USER = {"role": "user", "content": "question"}
ITEMS = [{"type": "compaction", "encrypted_content": "KB-one"}]
BLOCK = {"type": "text", "text": "KB知識"}


def request(host, method, path, body=b"", headers=()):
    return Request(host, 443, method.encode(), b"https", host.encode(), path.encode(), b"HTTP/1.1",
                   Headers([(k.encode(), v.encode()) for k, v in (("host", host), *headers)]),
                   body, Headers(), 0, 0)


@unittest.skipIf(websocket is None, "mitmproxy is not importable; run with the bridge venv python")
class AddonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.bindings = self.dir / "bindings"
        self.original = proxy.BINDINGS
        self.addCleanup(setattr, proxy, "BINDINGS", self.original)
        import kb_stealth_addon
        self.module = kb_stealth_addon

    def addon(self, payload):
        path = self.dir / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        addon = self.module.StealthKB()
        tctx = taddons.context(addon)
        tctx.__enter__()
        self.addCleanup(tctx.__exit__, None, None, None)
        tctx.configure(addon, kb_payload=str(path), kb_bindings=str(self.bindings), kb_repo=str(ROOT))
        return addon

    def codex(self, record=None):
        record = record or {"name": "octane", "store": "pepabo", "file": "latest.json"}
        return self.addon({"client": "codex", "record": record, "items": ITEMS})

    def claude(self):
        return self.addon({"client": "claude", "record": {"name": "octane", "store": "s", "file": "latest.json"},
                           "block": BLOCK})

    def http_flow(self, addon, req):
        flow = tflow.tflow(req=req)
        addon.requestheaders(flow)
        addon.request(flow)
        return flow

    # --- Codex HTTP ------------------------------------------------------

    def test_codex_http_post_is_injected_and_bound(self):
        addon = self.codex()
        body = json.dumps({"input": [DEV, USER], "client_metadata": {"session_id": SID}}).encode()
        flow = self.http_flow(addon, request(
            "chatgpt.com", "POST", "/backend-api/codex/responses", gzip.compress(body),
            (("content-encoding", "gzip"), ("content-type", "application/json"))))
        self.assertFalse(flow.request.stream)
        self.assertEqual(flow.request.headers["content-encoding"], "gzip")
        self.assertEqual(json.loads(flow.request.content)["input"], [DEV, *ITEMS, USER])
        self.assertEqual(proxy.read_binding(SID, "codex")["name"], "octane")
        self.assertIn("codex binding", (self.bindings / "proxy.log").read_text())

    def test_codex_http_compaction_and_other_paths_untouched(self):
        addon = self.codex()
        for req in (
            request("chatgpt.com", "POST", "/backend-api/codex/responses",
                    json.dumps({"input": [DEV, {"type": "compaction_trigger"}]}).encode()),
            request("chatgpt.com", "POST", "/backend-api/codex/responses/compact",
                    json.dumps({"input": [DEV, USER]}).encode()),
            request("chatgpt.com", "GET", "/backend-api/codex/models", b""),
            request("api.anthropic.com", "POST", "/v1/messages", b'{"system":"s"}'),
            request("example.com", "POST", "/backend-api/codex/responses",
                    json.dumps({"input": [DEV, USER]}).encode()),
        ):
            original = req.content
            flow = self.http_flow(addon, req)
            self.assertEqual(flow.request.content, original, req.url)

    def test_codex_http_previous_response_id_uses_sse_tracking(self):
        addon = self.codex()
        flow = self.http_flow(addon, request("chatgpt.com", "POST", "/backend-api/codex/responses",
                                             json.dumps({"input": [DEV, USER]}).encode()))
        flow.response = tflow.tresp(headers=Headers(content_type="text/event-stream"), content=b"")
        addon.responseheaders(flow)
        self.assertTrue(callable(flow.response.stream))
        chunk = b'data: {"type":"response.completed","response":{"id":"r1","output":[]}}\n\n'
        self.assertEqual(flow.response.stream(chunk), chunk)
        delta = json.dumps({"previous_response_id": "r1", "input": [{"role": "user", "content": "two"}]}).encode()
        second = tflow.tflow(client_conn=flow.client_conn,
                             req=request("chatgpt.com", "POST", "/backend-api/codex/responses", delta))
        addon.requestheaders(second)
        addon.request(second)
        self.assertEqual(second.request.content, delta)

    def test_non_target_responses_stream(self):
        addon = self.codex()
        flow = self.http_flow(addon, request("chatgpt.com", "GET", "/backend-api/codex/models"))
        self.assertTrue(flow.request.stream)
        flow.response = tflow.tresp()
        addon.responseheaders(flow)
        self.assertIs(flow.response.stream, True)

    def test_hook_error_forwards_original(self):
        addon = self.codex()
        flow = self.http_flow(addon, request("chatgpt.com", "POST", "/backend-api/codex/responses", b"{not json"))
        self.assertEqual(flow.request.content, b"{not json")
        self.assertIn("forwarded unchanged", (self.bindings / "proxy.log").read_text())

    # --- Codex WebSocket -------------------------------------------------

    def ws_flow(self, path="/backend-api/codex/responses", host="chatgpt.com", headers=()):
        flow = tflow.twebsocketflow(messages=False)
        flow.request = request(host, "GET", path, headers=headers)
        return flow

    def send(self, addon, flow, text, from_client=True):
        flow.websocket.messages.append(websocket.WebSocketMessage(Opcode.TEXT, from_client, text.encode()))
        addon.websocket_message(flow)
        return flow.websocket.messages[-1].text

    def test_websocket_response_create_injected_then_delta_passes(self):
        addon = self.codex()
        flow = self.ws_flow(headers=(("session-id", SID),))
        first = self.send(addon, flow, json.dumps({"type": "response.create", "input": [DEV, USER]}))
        self.assertEqual(json.loads(first)["input"], [DEV, *ITEMS, USER])
        self.assertEqual(proxy.read_binding(SID, "codex")["name"], "octane")
        server = '{"type":"response.completed","response":{"id":"r1","output":[]}}'
        self.assertEqual(self.send(addon, flow, server, from_client=False), server)
        delta = '{"type":"response.create","previous_response_id":"r1","input":[{"role":"user"}]}'
        self.assertEqual(self.send(addon, flow, delta), delta)
        other = '{"type":"session.update","input":[]}'
        self.assertEqual(self.send(addon, flow, other), other)
        flow.websocket.messages.append(websocket.WebSocketMessage(Opcode.BINARY, True, b"\x00\x01"))
        addon.websocket_message(flow)
        self.assertEqual(flow.websocket.messages[-1].content, b"\x00\x01")

    def test_websocket_other_path_untouched(self):
        addon = self.codex()
        flow = self.ws_flow(path="/backend-api/other")
        text = json.dumps({"type": "response.create", "input": [DEV, USER]})
        self.assertEqual(self.send(addon, flow, text), text)

    def test_connections_track_separately(self):
        addon = self.codex()
        one, two = self.ws_flow(), self.ws_flow()
        self.send(addon, one, json.dumps({"type": "response.create", "input": [USER]}))
        self.send(addon, one, '{"type":"response.completed","response":{"id":"r1","output":[]}}', False)
        delta = '{"type":"response.create","previous_response_id":"r1","input":[]}'
        self.assertEqual(self.send(addon, two, delta), delta)
        addon.websocket_end(one)
        self.assertNotIn(("ws", one.id), addon.conversations)

    # --- Claude ----------------------------------------------------------

    def test_claude_messages_injected_and_session_bound(self):
        addon = self.claude()
        flow = self.http_flow(addon, request("api.anthropic.com", "POST", "/v1/messages?beta=true",
                                             b'{"system":"s"}', (("x-claude-code-session-id", SID),)))
        system = json.loads(flow.request.content)["system"]
        self.assertEqual(system, [BLOCK, {"type": "text", "text": "s"}])
        self.assertEqual(proxy.read_binding(SID, "claude")["name"], "octane")

    def test_claude_session_leaves_other_requests_alone(self):
        addon = self.claude()
        for req in (request("api.anthropic.com", "POST", "/v1/messages/count_tokens", b'{"system":"s"}'),
                    request("chatgpt.com", "POST", "/backend-api/codex/responses", b'{"input":[]}')):
            original = req.content
            self.assertEqual(self.http_flow(addon, req).request.content, original)

    # --- TLS -------------------------------------------------------------

    def test_tls_failure_logs_ca_setup_hint(self):
        addon = self.codex()
        data = type("Data", (), {"context": type("C", (), {"client": type("Cl", (), {"sni": "chatgpt.com"})()})(),
                                 "conn": type("Conn", (), {"error": "unknown ca"})()})()
        addon.tls_failed_client(data)
        line = (self.bindings / "proxy.log").read_text()
        self.assertIn("chatgpt.com", line)
        self.assertIn("kb ca-setup", line)


if __name__ == "__main__":
    unittest.main()
