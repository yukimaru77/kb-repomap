import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_cli
import kb_codex
import kb_native
import kb_native_local


class NativeTests(unittest.TestCase):
    def test_local_seed_keeps_diff_once_and_leaves_prompt_to_native_command(self):
        for prompt in ("The actual user request", "-"):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                snapshot = root / "kb.json"
                snapshot.write_text('[{"type":"compaction","encrypted_content":"opaque"}]')
                args = kb_native.parse(["example", "codex", "exec", "--json", prompt])
                stdin = io.StringIO("piped user request")
                def native_command(tail, sid):
                    self.assertEqual(tail, args.native_args)
                    return ["codex", "exec", "--json", "resume", sid, prompt]
                with mock.patch.object(kb_native_local, "command", side_effect=native_command), \
                     mock.patch.object(kb_native_local, "seed_overrides", return_value=[]), \
                     mock.patch.object(kb_codex, "CodexAppServer") as constructor, \
                     mock.patch.object(kb_codex, "config_flags", return_value=[]), \
                     mock.patch.object(kb_native.os, "chdir"), \
                     mock.patch.object(kb_native.os, "execvp") as execute, \
                     mock.patch("sys.stdin", stdin):
                    server = constructor.return_value.__enter__.return_value
                    server.start.return_value = "seed"
                    kb_native.run(args, {}, snapshot, root, "SOURCE DIFF")
                constructor.assert_called_once()
                server.run_turn.assert_not_called()
                injected = server.request.call_args_list[0].args[1]["items"]
                self.assertEqual(len(injected), 2)  # blob, then update context (no fixed guidance)
                self.assertEqual(injected[-1]["role"], "developer")
                self.assertEqual(injected[-1]["content"][0]["text"], "SOURCE DIFF")
                execute.assert_called_once_with("codex", [
                    "codex", "exec", "--json", "resume", "seed", prompt])
                self.assertEqual(stdin.tell(), 0)

    def test_local_resume_converts_the_thread_and_resumes_the_copy(self):
        import kb_codex_resume
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "kb.json"
            snapshot.write_text('[{"type":"compaction","encrypted_content":"opaque"}]')
            args = kb_native.parse(["example", "codex", "exec", "resume", "old-id", "question"])
            request = {"thread": "old-id", "last": False, "all": False, "exec": True}
            with mock.patch.object(kb_native_local, "resume_request", return_value=request), \
                 mock.patch.object(kb_native_local, "resume_command",
                                   return_value=["codex", "exec", "resume", "new-id", "question"]), \
                 mock.patch.object(kb_native_local, "seed_overrides", return_value=[]), \
                 mock.patch.object(kb_codex, "CodexAppServer"), \
                 mock.patch.object(kb_codex_resume, "resume_session",
                                   return_value=("old-id", "new-id")) as convert, \
                 mock.patch.object(kb_native.os, "chdir"), \
                 mock.patch.object(kb_native.os, "execvp") as execute, \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                kb_native.run(args, {}, snapshot, root, "SOURCE DIFF", developer_text="GUIDE")
        kb_items = convert.call_args.args[1]
        self.assertEqual([item.get("type") for item in kb_items], ["compaction", "message", "message"])
        self.assertEqual([item["content"][0]["text"] for item in kb_items[1:]], ["GUIDE", "SOURCE DIFF"])
        self.assertEqual(convert.call_args.args[3], request)
        execute.assert_called_once_with("codex", ["codex", "exec", "resume", "new-id", "question"])
        self.assertIn("old-id", stderr.getvalue())
        self.assertIn("new-id", stderr.getvalue())

    def test_codex_boundary_preserves_all_native_arguments(self):
        tail = ["exec", "--json", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="low"',
                "--output-schema", "schema.json", "-o", "answer.txt", "--", "--remote"]
        args = kb_native.parse(["example", "--remote", "--store", "research", "--file", "v2", "codex", *tail])
        self.assertEqual(args.native_args, tail)
        self.assertEqual((args.name, args.remote, args.store, args.file), ("example", True, "research", "v2.json"))
        self.assertEqual(args.rebuild, "never")

    def remote_launch(self, environ=None):
        import kb_stealth
        args = kb_native.parse(["example", "--remote", "codex", "exec", "--json", "-"])
        loaded = {"store": {"name": "research"}, "jsonl": Path("snapshot.json"),
                  "info": {"repository_url": "repo", "source_commit": "a" * 40, "branch": "main"}}
        remote = mock.Mock(items=[{"type": "compaction"}])
        output, diagnostics = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ or {}), \
             mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
             mock.patch.object(kb_cli.store, "source_update", return_value=("a" * 40, "")), \
             mock.patch.object(kb_native, "RemoteKB", return_value=remote), \
             mock.patch.object(kb_stealth, "find_mitmdump", return_value="/bin/mitmdump"), \
             mock.patch.object(kb_stealth, "Session") as session, \
             mock.patch.object(kb_native.proxy, "start") as provider_proxy, \
             mock.patch.object(kb_codex, "CodexAppServer") as server, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            session.return_value.__enter__.return_value = session.return_value
            session.return_value.port = 7
            session.return_value.client_env.return_value = {"HTTPS_PROXY": "http://127.0.0.1:7"}
            session.return_value.tls_failed.return_value = False
            with mock.patch.object(kb_stealth.proxy, "run_client", return_value=3) as run_client:
                code = kb_cli.launch(args, {})
        server.assert_not_called()
        provider_proxy.assert_not_called()
        return code, session, run_client, output.getvalue(), diagnostics.getvalue()

    def test_remote_runs_native_codex_unchanged_behind_stealth(self):
        code, session, run_client, output, diagnostics = self.remote_launch()
        self.assertEqual(code, 3)
        mitmdump, payload, _env = session.call_args.args
        self.assertEqual(mitmdump, "/bin/mitmdump")
        self.assertEqual(payload["client"], "codex")
        self.assertEqual(payload["items"], [{"type": "compaction"}])
        self.assertEqual(payload["record"]["name"], "example")
        command, env = run_client.call_args.args[:2]
        # Codex keeps its own provider and login: no -c overrides, only the proxy env.
        self.assertEqual(command, ["codex", "exec", "--json", "-"])
        self.assertEqual(env, {"HTTPS_PROXY": "http://127.0.0.1:7"})
        self.assertEqual(output, "")
        self.assertIn("KB: example", diagnostics)
        self.assertIn("/ stealth", diagnostics)

    def test_codex_rejects_provider_mode(self):
        with self.assertRaisesRegex(ValueError, "Codex の --remote はステルス方式のみ"):
            self.remote_launch({"KB_REMOTE_MODE": "provider"})

    def test_remote_exit_code_propagates_from_cli(self):
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
             mock.patch.object(kb_cli, "launch", return_value=5), self.assertRaises(SystemExit) as caught:
            kb_cli.main(["example", "--remote", "codex", "exec", "hi"])
        self.assertEqual(caught.exception.code, 5)

    def test_native_help_does_not_load_kb_or_start_session(self):
        with mock.patch.object(kb_cli.store, "read_config") as read, \
             mock.patch.object(kb_cli.os, "execvp") as execute:
            kb_cli.main(["anything", "--remote", "codex", "exec", "--help"])
        read.assert_not_called()
        execute.assert_called_once_with("codex", ["codex", "exec", "--help"])


if __name__ == "__main__":
    unittest.main()
