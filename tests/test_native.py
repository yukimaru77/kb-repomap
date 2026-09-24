import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_cli
import kb_codex
import kb_native
import kb_native_local
from kb_remote import RemoteKB


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
                self.assertEqual(len(injected), 3)
                self.assertEqual(injected[-1]["role"], "developer")
                self.assertEqual(injected[-1]["content"][0]["text"], "SOURCE DIFF")
                execute.assert_called_once_with("codex", [
                    "codex", "exec", "--json", "resume", "seed", prompt])
                self.assertEqual(stdin.tell(), 0)

    def test_codex_boundary_preserves_all_native_arguments(self):
        tail = ["exec", "--json", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="low"',
                "--output-schema", "schema.json", "-o", "answer.txt", "--", "--remote"]
        args = kb_native.parse(["example", "--remote", "--store", "research", "--file", "v2", "codex", *tail])
        self.assertEqual(args.native_args, tail)
        self.assertEqual((args.name, args.remote, args.store, args.file), ("example", True, "research", "v2.json"))
        self.assertEqual(args.rebuild, "never")

    def test_remote_rr_registers_before_native_execution_without_bootstrap_turn(self):
        args = kb_native.parse(["example", "--remote", "codex", "exec", "--json", "-"])
        loaded = {"store": {"name": "research"}, "jsonl": Path("snapshot.json"),
                  "info": {"repository_url": "repo", "source_commit": "a" * 40, "branch": "main"}}
        calls = mock.Mock()
        remote = mock.Mock(origin="http://127.0.0.1:18473", key="SECRET", items=[])
        calls.attach_mock(remote, "remote")
        overrides = ['model_provider="pool_rr"',
                     'model_providers.pool_rr={name="Pool",base_url="http://127.0.0.1:18473/_pool/rr"}']
        output, diagnostics = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"KB_CODEX_CONFIG_OVERRIDES": json.dumps(overrides)}), \
             mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
             mock.patch.object(kb_cli.store, "source_update", return_value=("a" * 40, "")), \
             mock.patch.object(kb_native, "RemoteKB", return_value=remote), \
             mock.patch.object(kb_native.uuid, "uuid4", return_value="kb-root"), \
             mock.patch.object(kb_native.os, "execvpe") as execute, \
             mock.patch.object(kb_codex, "CodexAppServer") as server, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            calls.attach_mock(execute, "execute")
            kb_cli.launch(args, {})
        server.assert_not_called()
        self.assertEqual(calls.mock_calls[0], mock.call.remote.bind("kb-root"))
        command = execute.call_args.args[1]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[-2:], ["--json", "-"])
        self.assertIn('model_providers.pool_rr.http_headers.X-Codex-Parent-Thread-Id="kb-root"', command)
        self.assertNotIn("SECRET", " ".join(command))
        self.assertEqual(output.getvalue(), "")
        self.assertIn("KB: example", diagnostics.getvalue())

    def test_normal_remote_uses_fill_first_without_persistent_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "models_cache.json"
            cache.write_text('{"models": []}')
            remote = mock.Mock(origin="http://127.0.0.1:18473", key="SECRET")
            with mock.patch.dict(os.environ, {"CODEX_HOME": temporary, "KB_CODEX_CONFIG_OVERRIDES": "[]"}):
                command, env = kb_native.remote_command(["review", "--uncommitted"], remote, "root")
            self.assertEqual(command[:2], ["codex", "review"])
            self.assertEqual(command[-1], "--uncommitted")
            self.assertIn("/backend-api/codex", " ".join(command))
            self.assertNotIn("/_pool/rr", " ".join(command))
            self.assertEqual(env["KB_NATIVE_POOL_KEY"], "SECRET")
            self.assertNotIn("SECRET", " ".join(command))
            self.assertEqual(list(Path(temporary).iterdir()), [cache])

    def test_wrapper_pool_environment_wins_over_saved_build_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "key"
            key.write_text("new-key")
            snapshot = root / "kb.json"
            snapshot.write_text('[{"type":"compaction","encrypted_content":"blob"}]')
            with mock.patch.dict(os.environ, {"KB_POOL_ORIGIN": "http://127.0.0.1:9001",
                                             "KB_POOL_KEY_FILE": str(key), "KB_POOL_PRIVATE_HTTP": "0"}):
                remote = RemoteKB({"build_args": ["--origin", "http://other.invalid:9000", "--key-file", "/missing", "--private-http"]}, snapshot)
            self.assertEqual(remote.origin, "http://127.0.0.1:9001")
            self.assertEqual(remote.key, "new-key")

    def test_native_help_does_not_load_kb_or_start_session(self):
        with mock.patch.object(kb_cli.store, "read_config") as read, \
             mock.patch.object(kb_cli.os, "execvp") as execute:
            kb_cli.main(["anything", "--remote", "codex", "exec", "--help"])
        read.assert_not_called()
        execute.assert_called_once_with("codex", ["codex", "exec", "--help"])


if __name__ == "__main__":
    unittest.main()
