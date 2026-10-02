import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_claude_remote
import test_kb_claude


BLOCK = {"type": "text", "text": "KB知識"}
SSE = b"event: message_start\ndata: {\"a\":1}\n\nevent: message_stop\ndata: {}\n\n"


class FakeUpstream:
    """Record every request and answer messages calls with an SSE stream."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.requests.append({"path": self.path, "headers": self.headers, "body": body})
                if self.path.startswith("/v1/messages?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for part in SSE.split(b"\n\n")[:-1]:
                        part += b"\n\n"
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    data = b'{"input_tokens":3}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.addCleanup(self.upstream.close)
        self.proxy = kb_claude_remote.make_server(BLOCK, upstream=self.upstream.url)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        self.base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.logs = tempfile.TemporaryDirectory()
        self.addCleanup(self.logs.cleanup)
        patcher = mock.patch.object(kb_claude_remote.proxy, "BINDINGS", Path(self.logs.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        kb_claude_remote.proxy._CLAUDE_LOGGED.clear()

    def post(self, path, body, headers=None):
        request = urllib.request.Request(self.base + path, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer sk-ant-oat-test",
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20", **(headers or {}),
        })
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()

    def sent(self):
        self.assertEqual(len(self.upstream.requests), 1)
        request = self.upstream.requests[0]
        self.assertEqual(int(request["headers"]["Content-Length"]), len(request["body"]))
        return request, json.loads(request["body"])

    def test_kb_goes_into_first_user_message_and_system_is_untouched(self):
        system = [{"type": "text", "text": "billing"},
                  {"type": "text", "text": "main", "cache_control": {"type": "ephemeral"}}]
        reminder = {"type": "text", "text": "<system-reminder>r</system-reminder>"}
        messages = [{"role": "user", "content": [reminder, {"type": "text", "text": "q"}]}]
        self.post("/v1/messages?beta=true",
                  json.dumps({"system": system, "messages": messages, "stream": True}).encode())
        request, payload = self.sent()
        self.assertEqual(payload["system"], system)
        self.assertEqual(payload["messages"][0]["content"], [reminder, BLOCK, {"type": "text", "text": "q"}])
        self.assertEqual(request["path"], "/v1/messages?beta=true")

    def test_string_content_is_normalised(self):
        self.post("/v1/messages", json.dumps({"system": "hello", "messages": [
            {"role": "user", "content": "hi"}]}).encode())
        _request, payload = self.sent()
        self.assertEqual(payload["system"], "hello")
        self.assertEqual(payload["messages"][0]["content"], [BLOCK, {"type": "text", "text": "hi"}])

    def test_request_without_user_message_is_forwarded_byte_for_byte(self):
        body = b'{ "system" : "x",\n "messages":[] }'
        self.post("/v1/messages", body)
        self.assertEqual(self.upstream.requests[0]["body"], body)

    def test_compaction_request_is_forwarded_byte_for_byte(self):
        body = json.dumps({"messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "a"},
            {"role": "user", "content": kb_claude_remote.proxy.CLAUDE_COMPACTION_ANCHOR + "."}]}).encode()
        self.post("/v1/messages?beta=true", body)
        self.assertEqual(self.upstream.requests[0]["body"], body)
        self.assertIn("compaction request, KB not injected", (Path(self.logs.name) / "proxy.log").read_text())

    def test_auth_headers_reach_upstream_unchanged(self):
        self.post("/v1/messages", b"{}")
        request, _payload = self.sent()
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-ant-oat-test")
        self.assertEqual(request["headers"]["anthropic-beta"], "claude-code-20250219,oauth-2025-04-20")
        self.assertEqual(request["headers"]["Host"], self.upstream.url.split("//", 1)[1])

    def test_count_tokens_passes_through_byte_for_byte(self):
        body = b'{ "system" : "x",\n "messages":[] }'
        status, data = self.post("/v1/messages/count_tokens?beta=true", body)
        self.assertEqual((status, data), (200, b'{"input_tokens":3}'))
        self.assertEqual(self.upstream.requests[0]["body"], body)

    def test_sse_response_is_streamed_unchanged(self):
        status, data = self.post("/v1/messages?beta=true", b'{"stream": true}')
        self.assertEqual((status, data), (200, SSE))

    def test_gzip_request_is_decoded_and_edited(self):
        body = gzip.compress(json.dumps({"messages": [{"role": "user", "content": "a"}]}).encode())
        self.post("/v1/messages", body, {"Content-Encoding": "gzip"})
        request, payload = self.sent()
        self.assertNotIn("Content-Encoding", request["headers"])
        self.assertEqual(payload["messages"][0]["content"], [BLOCK, {"type": "text", "text": "a"}])

    def test_invalid_json_is_rejected_locally(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/v1/messages", b"not json")
        caught.exception.close()
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/v1/messages", b'{"messages": [{"role": "user", "content": 3}]}')
        caught.exception.close()
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.upstream.requests, [])


class StartRemoteTests(unittest.TestCase):
    setUp = test_kb_claude.ClaudeContextTests.setUp

    def test_start_remote_runs_claude_through_the_proxy(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.close)
        bindings = tempfile.TemporaryDirectory()
        self.addCleanup(bindings.cleanup)
        patcher = mock.patch.object(kb_claude_remote.proxy, "BINDINGS", Path(bindings.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = mock.patch.dict(kb_claude_remote.os.environ, {
            "KB_CLAUDE_UPSTREAM": upstream.url, "ANTHROPIC_BASE_URL": "https://example.invalid",
            "KB_REMOTE_MODE": "provider",
        })
        environment.start()
        self.addCleanup(environment.stop)
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["-p", "hi"],
        })()
        seen = {}

        def runner(command, env, cwd):
            seen.update(command=command, env=env, cwd=cwd)
            request = urllib.request.Request(env["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true",
                                              data=b'{"system": "s", "messages": [{"role": "user", "content": "q"}]}',
                                              method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            return type("Done", (), {"returncode": 7})()

        with mock.patch("sys.stderr") as stderr:
            code = kb_claude_remote.start_remote(args, {"stores": [self.loaded["store"]]}, runner=runner)
        self.assertEqual(code, 7)
        self.assertEqual(seen["command"][0:2], ["claude", "--session-id"])
        self.assertEqual(seen["command"][3:], ["-p", "hi"])
        self.assertNotIn("--append-system-prompt", seen["command"])
        self.assertRegex(seen["env"]["ANTHROPIC_BASE_URL"], r"^http://127\.0\.0\.1:\d+$")
        # Behind a non-Anthropic base URL Claude Code inlines every MCP tool
        # schema unless tool search is explicitly enabled.
        self.assertEqual(seen["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(seen["cwd"], str(self.root.resolve()))
        sent = json.loads(upstream.requests[0]["body"])
        self.assertEqual(sent["system"], "s")
        injected = sent["messages"][0]["content"][0]["text"]
        self.assertTrue(injected.startswith(kb_claude_remote.proxy.CLAUDE_KB_FRAMING))
        self.assertIn("復号された知識", injected)
        self.assertIn("記録の場所", injected)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("上書き", output)
        self.assertIn("Remote KB: proxy 127.0.0.1:", output)
        record = kb_claude_remote.proxy.read_binding(seen["command"][2], "claude")
        self.assertEqual((record["name"], record["file"]), ("example", "latest.json"))

    def test_start_remote_fork_binds_the_new_session_from_its_first_request(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.close)
        bindings = tempfile.TemporaryDirectory()
        self.addCleanup(bindings.cleanup)
        for patcher in (mock.patch.object(kb_claude_remote.proxy, "BINDINGS", Path(bindings.name)),
                        mock.patch.dict(kb_claude_remote.os.environ, {
                            "KB_CLAUDE_UPSTREAM": upstream.url, "KB_REMOTE_MODE": "provider"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        source, forked = "11111111-2222-4333-8444-555555555555", "11111111-2222-4333-8444-666666666666"
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--resume", source, "--fork-session", "-p", "hi"],
        })()
        seen = {}

        def runner(command, env, cwd):
            seen.update(command=command)
            request = urllib.request.Request(env["ANTHROPIC_BASE_URL"] + "/v1/messages",
                                              data=b'{"messages": [{"role": "user", "content": "q"}]}',
                                              headers={"X-Claude-Code-Session-Id": forked}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            return type("Done", (), {"returncode": 0})()

        with mock.patch("sys.stderr") as stderr:
            kb_claude_remote.start_remote(args, {"stores": [self.loaded["store"]]}, runner=runner)
        self.assertEqual(seen["command"], ["claude", "--resume", source, "--fork-session", "-p", "hi"])
        self.assertIn("復号された知識", json.loads(upstream.requests[0]["body"])["messages"][0]["content"][0]["text"])
        record = kb_claude_remote.proxy.read_binding(forked, "claude")
        self.assertEqual((record["name"], record["file"], record["id_source"]),
                         ("example", "latest.json", "X-Claude-Code-Session-Id header"))
        self.assertIsNone(kb_claude_remote.proxy.read_binding(source))
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("新しいセッションを作成します", output)

    def test_start_remote_rejects_session_id(self):
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--session-id=x"],
        })()
        with self.assertRaisesRegex(ValueError, "--session-id"):
            kb_claude_remote.start_remote(args, {"stores": [self.loaded["store"]]}, runner=None)


if __name__ == "__main__":
    unittest.main()
