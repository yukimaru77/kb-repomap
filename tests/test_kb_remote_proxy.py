from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_remote_proxy as proxy


ITEMS = [{"type": "compaction", "encrypted_content": "KB"},
         {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "charter"}]}]
DEV = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "d"}]}
SYS = {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "s"}]}
USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}


class Upstream:
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.requests.append({"method": self.command, "path": self.path,
                                       "headers": self.headers, "body": body})
                data = b'data: {"type":"response.completed"}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/backend-api/codex"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CodexInjectorTests(unittest.TestCase):
    def test_inserts_after_leading_system_and_developer(self):
        payload = {"input": [SYS, DEV, USER, DEV]}
        out = json.loads(proxy.inject_codex(payload, ITEMS))
        self.assertEqual(out["input"], [SYS, DEV, *ITEMS, USER, DEV])

    def test_inserts_at_start_and_end(self):
        self.assertEqual(json.loads(proxy.inject_codex({"input": [USER]}, ITEMS))["input"], [*ITEMS, USER])
        self.assertEqual(json.loads(proxy.inject_codex({"input": [DEV]}, ITEMS))["input"], [DEV, *ITEMS])
        self.assertEqual(json.loads(proxy.inject_codex({"input": []}, ITEMS))["input"], ITEMS)

    def test_identity_mirrors_pool_fields(self):
        headers = {"Session-Id": "s-header", "Thread-Id": "t-header"}
        payload = {"client_metadata": {"thread_id": "t-body", "x-codex-parent-thread-id": "p",
                                        "x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}}
        found = proxy.codex_identity(headers, payload)
        self.assertEqual((found["session"], found["thread"], found["parent"], found["kind"]),
                         ("s-header", "t-body", "p", "compaction"))
        self.assertEqual(found["sources"]["thread"], "client_metadata.thread_id")


class CodexProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        self.seen = []
        self.running = proxy.start(proxy.codex_injector(ITEMS), self.upstream.url,
                                   on_request=lambda *a: self.seen.append(a[1]),
                                   set_headers={"Authorization": "Bearer POOLKEY"})
        self.addCleanup(self.running.close)

    def post(self, path, body):
        request = urllib.request.Request(self.running.url + path, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer dummy", "Session-Id": "sid"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()

    def test_responses_injected_and_auth_swapped(self):
        status, data = self.post("/responses", json.dumps({"model": "m", "input": [DEV, USER]}).encode())
        self.assertEqual((status, data), (200, b'data: {"type":"response.completed"}\n\n'))
        request = self.upstream.requests[0]
        self.assertEqual(request["path"], "/backend-api/codex/responses")
        self.assertEqual(request["headers"].get_all("Authorization"), ["Bearer POOLKEY"])
        self.assertEqual(request["headers"]["Session-Id"], "sid")
        self.assertEqual(int(request["headers"]["Content-Length"]), len(request["body"]))
        self.assertEqual(json.loads(request["body"])["input"], [DEV, *ITEMS, USER])
        self.assertEqual(self.seen, ["/responses"])

    def test_compaction_passes_through_byte_for_byte(self):
        for body in (b'{"input": [{"type": "compaction_trigger"}] }',
                     b'{"request_kind":"compaction", "input": []}'):
            self.post("/responses", body)
            self.assertEqual(self.upstream.requests[-1]["body"], body)

    def test_previous_response_id_is_rejected_locally(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/responses", b'{"previous_response_id": "r1", "input": []}')
        caught.exception.close()
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_other_paths_pass_through(self):
        body = b'{ "input" : [ ] }'
        self.post("/responses/compact", body)
        self.assertEqual(self.upstream.requests[0]["body"], body)
        self.assertEqual(self.upstream.requests[0]["headers"]["Authorization"], "Bearer POOLKEY")
        with urllib.request.urlopen(self.running.url + "/models?client_version=1", timeout=10) as response:
            response.read()
        self.assertEqual(self.upstream.requests[1]["path"], "/backend-api/codex/models?client_version=1")


class BindingTests(unittest.TestCase):
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(proxy, "BINDINGS", Path(temporary)):
            path = proxy.write_binding("abc-123", "codex", "octane", "research", "latest.json",
                                       source="Session-Id header")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            record = proxy.read_binding("abc-123")
            self.assertEqual((record["client"], record["name"], record["store"], record["file"]),
                             ("codex", "octane", "research", "latest.json"))
            self.assertIn("created", record)
            self.assertIsNone(proxy.read_binding("abc-123", client="claude"))
            self.assertIsNone(proxy.read_binding("missing"))
            proxy.write_binding("abc-123", "codex", "other", None, "v2.json", overwrite=False)
            self.assertEqual(proxy.read_binding("abc-123")["name"], "octane")
            with self.assertRaises(ValueError):
                proxy.binding_path("../x")


class TruncatingUpstream:
    """Upstream that starts a chunked stream and then drops the connection."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"6\r\ndata: \r\n")
                self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/backend-api/codex"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class QuietDisconnectTests(unittest.TestCase):
    """Dropped connections must never print to the client's terminal."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch.object(proxy, "BINDINGS", Path(temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = Path(temporary.name) / "proxy.log"
        self.stderr = io.StringIO()
        self.stdout = io.StringIO()
        for name, stream in (("stderr", self.stderr), ("stdout", self.stdout)):
            patcher = mock.patch.object(sys, name, stream)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, upstream_url):
        running = proxy.start(proxy.codex_injector(ITEMS), upstream_url)
        self.addCleanup(running.close)
        return running

    def log_lines(self, deadline=5.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if self.log.exists() and self.log.read_text(encoding="utf-8").strip():
                time.sleep(0.2)  # let any further (unwanted) lines land
                break
            time.sleep(0.02)
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def assert_quiet(self, lines):
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("Traceback", lines[0])
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d proxy: .*127\.0\.0\.1:\d+")

    def test_client_closing_mid_request_is_logged_quietly(self):
        upstream = Upstream()
        self.addCleanup(upstream.close)
        running = self.start(upstream.url)
        client = socket.create_connection(("127.0.0.1", running.port), timeout=5)
        client.sendall(b"POST /responses HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       b"Content-Length: 1000\r\n\r\n{\"input\": [")
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        lines = self.log_lines()
        self.assert_quiet(lines)
        self.assertIn("request read dropped", lines[0])
        self.assertEqual(upstream.requests, [])

    def test_upstream_closing_mid_stream_is_logged_quietly(self):
        upstream = TruncatingUpstream()
        self.addCleanup(upstream.close)
        running = self.start(upstream.url)
        request = urllib.request.Request(running.url + "/responses", method="POST",
                                         data=json.dumps({"input": [USER]}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except (OSError, proxy.http.client.HTTPException):
            pass
        lines = self.log_lines()
        self.assert_quiet(lines)
        self.assertIn("response stream dropped", lines[0])

    def test_handle_error_writes_one_line_not_a_traceback(self):
        running = self.start("http://127.0.0.1:9")
        try:
            raise ConnectionResetError(54, "Connection reset by peer")
        except ConnectionResetError:
            running.server.handle_error(None, ("127.0.0.1", 4242))
        lines = self.log_lines()
        self.assert_quiet(lines)
        self.assertIn("127.0.0.1:4242", lines[0])
        self.assertIn("ConnectionResetError", lines[0])


if __name__ == "__main__":
    unittest.main()
