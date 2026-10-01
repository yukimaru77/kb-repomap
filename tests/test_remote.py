import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import kb_cli
import kb_codex
from kb_remote import RemoteKB
from kb_fork_mint import CHARTER
from kb_items import developer_item


class RemoteKBTests(unittest.TestCase):
    def test_loads_portable_items_without_environment_or_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "v1.00.jsonl"
            items = [{"type": "compaction_summary", "encrypted_content": f"blob-{i}", "future": True}
                     for i in range(35)] + [{"type": "message", "role": "user", "content": CHARTER}]
            records = [{"type": "session_meta", "payload": {"id": "old"}}]
            records += [{"type": "response_item", "payload": item} for item in items]
            records += [{"type": "event_msg", "payload": {"never": "send"}}]
            source.write_text("\n".join(json.dumps(record) for record in records))
            original = source.read_bytes()
            before = dict(os.environ)
            clean = {k: v for k, v in os.environ.items() if not k.startswith(("KB_POOL", "KB_RR"))}
            with mock.patch.dict(os.environ, clean, clear=True):
                remote = RemoteKB({"stores": []}, source)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(source.read_bytes(), original)
            expected_items = items[:-1]
            self.assertEqual(remote.items, expected_items)
            portable = root / "latest.json"
            portable.write_text(json.dumps(items))
            self.assertEqual(RemoteKB({}, portable).items, expected_items)
            self.assertEqual(RemoteKB({}, portable, developer_text="notes\n").items,
                             [*items[:-1], developer_item("notes\n")])

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
                "items": [developer_item(kb_codex.REMOTE_SEED_TEXT)],
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

    def test_no_prompt_binds_and_persists_diff_before_closing_without_inference(self):
        calls = mock.Mock()
        remote = mock.Mock()
        with mock.patch.object(kb_codex, "CodexAppServer") as constructor:
            server = constructor.return_value.__enter__.return_value
            server.start.return_value = "new-id"
            calls.attach_mock(server, "server")
            calls.attach_mock(remote, "remote")
            result = kb_codex.start_session(Path("snapshot.json"), Path("/work"), "example",
                                           "SOURCE DIFF", full_access=False, remote=remote)
        self.assertEqual(result, "new-id")
        self.assertEqual(calls.method_calls, [
            mock.call.server.start(Path("/work"), False),
            mock.call.remote.bind("new-id"),
            mock.call.server.request("thread/inject_items", {"threadId": "new-id", "items": [
                {"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "SOURCE DIFF"}]}]}),
            mock.call.server.request("thread/name/set", {
                "threadId": "new-id", "name": "example Remote KBを活用する"}),
        ])
        constructor.assert_called_once()
        constructor.return_value.__exit__.assert_called_once()

    def test_start_and_resume_do_not_override_model_provider_or_environment(self):
        server = kb_codex.CodexAppServer.__new__(kb_codex.CodexAppServer)
        server.request = mock.Mock(return_value={"thread": {"id": "new-id"}})
        self.assertEqual(server.start(Path("/work"), False), "new-id")
        server.request.assert_called_with("thread/start", {"cwd": "/work", "ephemeral": False})
        self.assertEqual(server.resume("new-id", Path("/work"), True), "new-id")
        server.request.assert_called_with("thread/resume", {"threadId": "new-id", "cwd": "/work",
                                                          "sandbox": "danger-full-access", "approvalPolicy": "never"})

    def legacy_remote(self, argv):
        import kb_native
        import kb_stealth
        loaded = {"store": {"name": "chosen"}, "jsonl": Path("v1.00.jsonl"),
                  "info": {"repository_url": "repo", "source_commit": "a" * 40, "branch": "main"}}
        config = {"build_args": ["--workers", "12"]}
        remote = mock.Mock(items=["kb", "dev"])
        session = mock.Mock(port=5)
        session.client_env.return_value = {"HTTPS_PROXY": "http://127.0.0.1:5"}
        opened = mock.MagicMock()
        opened.return_value.__enter__.return_value = session
        with mock.patch.object(kb_cli.store, "read_config", return_value=config), \
             mock.patch.object(kb_cli.store, "find_kb", return_value=loaded) as find, \
             mock.patch.object(kb_cli.store, "source_update", return_value=("b" * 40, "SOURCE DIFF")), \
             mock.patch.object(kb_native, "RemoteKB", return_value=remote) as constructor, \
             mock.patch.object(kb_stealth, "stealth_session", opened), \
             mock.patch.object(kb_stealth, "run_client", return_value=0) as run_client, \
             mock.patch.object(kb_native.proxy, "start") as provider_proxy, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.suppress(SystemExit):
            kb_cli.main(["codex", "example", "--remote", "--store", "chosen", "--file", "v1.00",
                         "--rebuild", "never", *argv])
        find.assert_called_once_with(config, "example", "chosen", filename="v1.00.json")
        constructor.assert_called_once_with(config, loaded["jsonl"], developer_text=None)
        provider_proxy.assert_not_called()
        return SimpleNamespace(opened=opened, session=session, start=start, run_client=run_client,
                               out=out.getvalue(), err=err.getvalue())

    def test_legacy_remote_seeds_behind_stealth_and_keeps_session_only_hint(self):
        result = self.legacy_remote(["--session-only", "--prompt", "Use it"])
        client, payload = result.opened.call_args.args
        self.assertEqual(client, "codex")
        # The seeded thread persists no KB item; the stealth proxy inserts all of them.
        self.assertEqual(payload["items"], ["kb", "dev"])
        self.assertEqual(payload["record"], {"name": "example", "store": "chosen", "file": "v1.00.json"})
        start = result.start
        self.assertEqual(start.call_args.args[3], "SOURCE DIFF")
        self.assertEqual(start.call_args.kwargs["prompt"], "Use it")
        self.assertEqual(start.call_args.kwargs["env"], {"HTTPS_PROXY": "http://127.0.0.1:5"})
        self.assertNotIn("overrides", start.call_args.kwargs)
        with tempfile.TemporaryDirectory() as bindings, \
             mock.patch("kb_native.proxy.BINDINGS", Path(bindings)):
            start.call_args.kwargs["remote"].bind("new-id")
            import kb_native
            record = kb_native.proxy.read_binding("new-id", "codex")
        self.assertEqual((record["name"], record["store"], record["file"]), ("example", "chosen", "v1.00.json"))
        self.assertNotIn("local_guidance", record)
        self.assertIn("/ stealth", result.out)
        self.assertIn("kb --remote codex resume new-id", result.err)
        result.run_client.assert_not_called()

    def test_legacy_remote_resumes_the_seeded_thread_through_stealth(self):
        result = self.legacy_remote(["--no-yolo"])
        session, client, command, workspace = result.run_client.call_args.args[:4]
        self.assertIs(session, result.session)
        self.assertEqual(client, "codex")
        self.assertEqual(command, ["codex", "resume", "-C", str(workspace), "new-id"])
        self.assertFalse(any(part == "-c" for part in command))

    def test_empty_snapshot_is_rejected_before_creating_codex_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "key").write_text("test-key")
            (root / "latest.jsonl").write_text('{"type":"session_meta","payload":{}}\n')
            with self.assertRaises(ValueError):
                RemoteKB({}, root / "latest.jsonl")


if __name__ == "__main__":
    unittest.main()
