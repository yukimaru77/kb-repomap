import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import kb_cli
import kb_codex
from kb_remote import RemoteKB
from kb_fork_mint import CHARTER


class RemoteKBTests(unittest.TestCase):
    def test_registers_all_items_using_existing_config_without_environment_changes(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"snapshot_id":"fixed","item_count":36}')

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                key = root / "client.key"
                key.write_text("test-key\n")
                source = root / "v1.00.jsonl"
                items = [{"type": "compaction_summary", "encrypted_content": f"blob-{i}", "future": True}
                         for i in range(35)] + [{"type": "message", "role": "user", "content": CHARTER}]
                records = [{"type": "session_meta", "payload": {"id": "old"}}]
                records += [{"type": "response_item", "payload": item} for item in items]
                records += [{"type": "event_msg", "payload": {"never": "send"}}]
                source.write_text("\n".join(json.dumps(record) for record in records))
                original = source.read_bytes()
                before = dict(os.environ)
                config = {"build_args": ["--origin", f"http://127.0.0.1:{server.server_port}",
                                         "--workers", "12", "--key-file", str(key)]}
                with contextlib.redirect_stdout(io.StringIO()):
                    remote = RemoteKB(config, source)
                    remote.bind("new-session")
                self.assertEqual(dict(os.environ), before)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(seen, [("/_pool/kb/bind", "Bearer test-key", {"session_id": "new-session", "items": items})])
                portable = root / "latest.json"
                portable.write_text(json.dumps(items))
                with contextlib.redirect_stdout(io.StringIO()):
                    RemoteKB(config, portable).bind("portable-session")
                self.assertEqual(seen[-1], ("/_pool/kb/bind", "Bearer test-key", {
                    "session_id": "portable-session", "items": items}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_registers_before_inference_and_reopens_after_startup_prewarm(self):
        calls = mock.Mock()
        bootstrap, active, remote = mock.Mock(), mock.Mock(), mock.Mock()
        calls.attach_mock(bootstrap, "bootstrap")
        calls.attach_mock(active, "active")
        calls.attach_mock(remote, "remote")
        bootstrap.start.return_value = "new-id"
        contexts = [mock.MagicMock(), mock.MagicMock()]
        contexts[0].__enter__.return_value = bootstrap
        contexts[1].__enter__.return_value = active
        with mock.patch.object(kb_codex, "CodexAppServer", side_effect=contexts):
            result = kb_codex.start_session(Path("v1.00.jsonl"), Path("/work"), "example", "EXACT DIFF",
                                           prompt="Use the KB", full_access=False, remote=remote)
        self.assertEqual(result, "new-id")
        self.assertEqual(calls.method_calls[:6], [
            mock.call.bootstrap.start(Path("/work"), False),
            mock.call.remote.bind("new-id"),
            mock.call.bootstrap.request("thread/inject_items", {
                "threadId": "new-id",
                "items": [{"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "Remote KB is provided by the account pool for this session."}
                ]}],
            }),
            mock.call.bootstrap.request("thread/name/set", {"threadId": "new-id", "name": "example Remote KBを活用する"}),
            mock.call.active.resume("new-id", Path("/work"), False),
            mock.call.active.run_turn("new-id", "EXACT DIFF\n\nUse the KB", Path("/work")),
        ])
        contexts[0].__exit__.assert_called_once()
        bootstrap.fork.assert_not_called()
        active.fork.assert_not_called()

    def test_registration_failure_never_sends_first_turn(self):
        with mock.patch.object(kb_codex, "CodexAppServer") as constructor:
            server = constructor.return_value.__enter__.return_value
            server.start.return_value = "new-id"
            remote = mock.Mock()
            remote.bind.side_effect = RuntimeError("registration failed")
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                kb_codex.start_session(Path("latest.jsonl"), Path("/work"), "example", "", remote=remote)
            server.run_turn.assert_not_called()
            constructor.assert_called_once()

    def test_start_and_resume_do_not_override_model_provider_or_environment(self):
        server = kb_codex.CodexAppServer.__new__(kb_codex.CodexAppServer)
        server.request = mock.Mock(return_value={"thread": {"id": "new-id"}})
        self.assertEqual(server.start(Path("/work"), False), "new-id")
        server.request.assert_called_with("thread/start", {"cwd": "/work", "ephemeral": False})
        self.assertEqual(server.resume("new-id", Path("/work"), True), "new-id")
        server.request.assert_called_with("thread/resume", {"threadId": "new-id", "cwd": "/work",
                                                          "sandbox": "danger-full-access", "approvalPolicy": "never"})

    def test_cli_remote_keeps_named_file_store_and_update_flow(self):
        loaded = {"store": {"name": "chosen"}, "jsonl": Path("v1.00.jsonl"),
                  "info": {"repository_url": "repo", "source_commit": "a" * 40, "branch": "main"}}
        config = {"build_args": ["--workers", "12"]}
        with mock.patch.object(kb_cli.store, "read_config", return_value=config), \
             mock.patch.object(kb_cli.store, "find_kb", return_value=loaded) as find, \
             mock.patch.object(kb_cli.store, "source_update", return_value=("b" * 40, "SOURCE DIFF")), \
             mock.patch("kb_remote.RemoteKB") as constructor, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["codex", "example", "--remote", "--store", "chosen", "--file", "v1.00",
                         "--session-only", "--rebuild", "never", "--prompt", "Use it"])
        find.assert_called_once_with(config, "example", "chosen", filename="v1.00.json")
        constructor.assert_called_once_with(config, loaded["jsonl"])
        self.assertEqual(start.call_args.args[3], "SOURCE DIFF")
        self.assertEqual(start.call_args.kwargs["remote"], constructor.return_value)

    def test_empty_snapshot_is_rejected_before_creating_codex_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "key").write_text("test-key")
            (root / "latest.jsonl").write_text('{"type":"session_meta","payload":{}}\n')
            with self.assertRaises(ValueError):
                RemoteKB({"build_args": ["--origin", "http://127.0.0.1:8080", "--key-file", str(root / "key")]}, root / "latest.jsonl")


if __name__ == "__main__":
    unittest.main()
