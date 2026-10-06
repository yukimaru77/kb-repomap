import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kb_cli
import kb_claude
import kb_claude_remote
import kb_codex
import kb_developer
import kb_native
from kb_items import load_session_items


class DeveloperAdditionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.notes = self.root / "decisions with spaces.txt"
        self.notes.write_text("決定と理由\n原文を保持", encoding="utf-8")
        self.snapshot = self.root / "latest.json"
        self.snapshot.write_text('[{"type":"compaction","encrypted_content":"opaque"}]')
        self.loaded = {"developer_text": "既存の案内", "jsonl": self.snapshot,
                       "store": {"name": "test"},
                       "info": {"repository_url": "repo", "source_commit": "abc", "branch": "main"}}

    def test_text_and_files_append_in_command_order_without_replacing_store_notes(self):
        args = kb_native.parse(["example", "--append-dev", "最初", "--append-dev-file", str(self.notes),
                                "--append-dev", "最後", "codex", "exec", "--json", "依頼"])

        with mock.patch.object(kb_cli.store, "find_kb", return_value=self.loaded), \
             mock.patch.object(kb_cli.store, "source_update", return_value=("abc", "")), \
             mock.patch.object(kb_native, "run") as launch, contextlib.redirect_stderr(io.StringIO()):
            kb_cli.launch(args, {})

        developer = launch.call_args.kwargs["developer_text"]
        self.assertEqual(developer, "既存の案内\n\n最初\n\n決定と理由\n原文を保持\n\n最後")
        items = load_session_items(self.snapshot, developer_text=developer)
        self.assertEqual(items[0]["type"], "compaction")
        self.assertEqual(items[1]["role"], "developer")
        self.assertEqual(items[1]["content"][0]["text"], developer)
        self.assertEqual(self.loaded["developer_text"], "既存の案内")
        self.assertEqual(self.notes.read_text(), "決定と理由\n原文を保持")
        self.assertEqual(args.native_args, ["exec", "--json", "依頼"])

    def test_without_store_dev_and_without_additions(self):
        for existing, options, expected in [
                (None, ["--append-dev", "追加"], "追加"),
                ("既存", [], "既存"),
                ("既存", ["--append-dev", " \n"], "既存"),
                (None, [], None)]:
            with self.subTest(existing=existing, options=options):
                args = kb_native.parse(["example", *options, "codex"])
                result = kb_developer.append_text(existing, kb_developer.read_additions(args))
                self.assertEqual(result, expected)

    def test_client_names_in_added_text_do_not_select_the_client(self):
        args = kb_native.parse(["example", "--append-dev", "codex", "codex", "exec", "claude"])
        self.assertEqual(kb_developer.read_additions(args), "codex")
        self.assertEqual(args.native_args, ["exec", "claude"])
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
             mock.patch.object(kb_claude, "start") as start:
            kb_cli.main(["example", "--append-dev", "codex", "claude", "-p", "codex"])
        received = start.call_args.args[0]
        self.assertEqual(received.claude_args, ["-p", "codex"])
        self.assertEqual(kb_developer.read_additions(received), "codex")

    def test_unreadable_file_fails_before_kb_lookup_or_client_launch(self):
        invalid_utf8 = self.root / "invalid.txt"
        invalid_utf8.write_bytes(b"\xff")
        for path in (self.root / "missing.txt", invalid_utf8):
            with self.subTest(path=path), mock.patch.object(kb_cli.store, "find_kb") as lookup:
                args = kb_native.parse(["example", "--append-dev-file", str(path), "codex"])
                with self.assertRaisesRegex(ValueError, "--append-dev-fileを読めません"):
                    kb_cli.launch(args, {})
                lookup.assert_not_called()

    def test_relative_file_is_read_from_launch_directory_not_workspace(self):
        args = kb_native.parse(["example", "--workspace", "/other/worktree", "--append-dev-file",
                                "decisions.txt", "codex"])
        with mock.patch.object(Path, "read_text", autospec=True, return_value="追加") as read:
            self.assertEqual(kb_developer.read_additions(args), "追加")
        self.assertEqual(read.call_args.args[0], Path("decisions.txt"))

    def test_remote_codex_injects_additions_with_kb(self):
        args = kb_native.parse(["example", "--remote", "--append-dev-file", str(self.notes), "codex"])
        with mock.patch.object(kb_cli.store, "find_kb", return_value=self.loaded), \
             mock.patch.object(kb_cli.store, "source_update", return_value=("abc", "")), \
             mock.patch.object(kb_native, "launch_remote") as launch, \
             contextlib.redirect_stderr(io.StringIO()):
            kb_cli.launch(args, {})

        binder = launch.call_args.args[1]
        self.assertEqual(binder.cached[-1]["content"][0]["text"], "既存の案内\n\n決定と理由\n原文を保持")
        self.assertEqual(binder.cached[0]["type"], "compaction")

    def test_legacy_codex_session_creation_receives_additions(self):
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
             mock.patch.object(kb_cli.store, "find_kb", return_value=self.loaded), \
             mock.patch.object(kb_cli.store, "source_update", return_value=("abc", "")), \
             mock.patch.object(kb_codex, "start_session", return_value="session") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["codex", "example", "--session-only", "--append-dev", "追加"])
        self.assertEqual(start.call_args.kwargs["developer_text"], "既存の案内\n\n追加")

    def test_claude_local_and_remote_receive_same_notes(self):
        for remote in (False, True):
            with self.subTest(remote=remote), \
                 mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                 mock.patch.object(kb_claude.store, "find_kb", return_value=self.loaded), \
                 mock.patch.object(kb_claude, "_decrypt_files", return_value=[("raw.txt", "知識")]), \
                 mock.patch.object(kb_claude.os, "chdir"), \
                 mock.patch.object(kb_claude.os, "execvp", side_effect=SystemExit(0)) as execute, \
                 mock.patch.object(kb_claude_remote.proxy, "write_binding"), \
                 mock.patch.object(kb_claude_remote.kb_resume, "run_claude", return_value=0) as run, \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                kb_cli.main(["example", *(["--remote"] if remote else []), "--append-dev", "最初",
                             "--append-dev-file", str(self.notes), "claude", "--model", "opus"])
            if remote:
                context = run.call_args.args[1].block["text"]
            else:
                command = execute.call_args.args[1]
                context = command[command.index("--append-system-prompt") + 1]
            self.assertIn("知識", context)
            self.assertIn("既存の案内\n\n最初\n\n決定と理由\n原文を保持", context)
            self.assertEqual(self.loaded["developer_text"], "既存の案内")


if __name__ == "__main__":
    unittest.main()
