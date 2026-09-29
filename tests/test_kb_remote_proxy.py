from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
