import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_claude
import kb_store


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(kb_store, "CACHE", self.root / "cache").start()
        repo = self.root / "store"
        kb_store.run(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "example").mkdir()
        (repo / "example" / "info.json").write_text(json.dumps({"source_commit": "a"}))
        (repo / "example" / "dev.txt").write_text("記録の場所")
        (repo / "example" / "latest.json").write_text(json.dumps([{
            "type": "compaction", "id": "blob", "encrypted_content": "opaque"
        }]))
        raw = repo / "example" / "decrypt" / "latest" / "01-blob" / "raw.txt"
        raw.parent.mkdir(parents=True)
        raw.write_text("復号された知識")
        kb_store.git(repo, "add", ".")
        kb_store.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
        bare = self.root / "store.git"
        kb_store.run(["git", "clone", "--bare", str(repo), str(bare)])
        self.loaded = {"store": {"name": "test", "url": str(bare)}, "developer_text": "記録の場所"}

    def test_build_context_uses_dev_and_selected_raw_text(self):
        text = kb_claude.build_context(self.loaded, "example", "latest.json")
        self.assertIn("記録の場所", text)
        self.assertIn("復号された知識", text)

    def test_dev_notes_follow_all_decrypted_material(self):
        text = kb_claude.build_context(self.loaded, "example", "latest.json")
        material = text.index("## KB section 1/1\n> " + kb_claude.SCOPE_NOTE + "\n\n復号された知識")
        notes = text.index("## KB developer notes\n\n記録の場所")
        self.assertLess(material, notes)
        self.assertLess(text.index("復号された知識"), notes)
        self.assertTrue(text.endswith("記録の場所\n"))

    def test_no_dev_means_no_notes_section(self):
        for developer in (None, "", " \n"):
            with self.subTest(developer=developer):
                text = kb_claude.build_context({**self.loaded, "developer_text": developer},
                                               "example", "latest.json")
                self.assertNotIn("## KB developer notes", text)
                self.assertIn("復号された知識", text)

    def test_missing_decrypt_is_rejected(self):
        empty_repo = self.root / "empty"
        kb_store.run(["git", "init", "-q", "-b", "main", str(empty_repo)])
        (empty_repo / "example").mkdir()
        (empty_repo / "example" / "info.json").write_text(json.dumps({"source_commit": "a"}))
        kb_store.git(empty_repo, "add", ".")
        kb_store.git(empty_repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
        empty_bare = self.root / "empty.git"
        kb_store.run(["git", "clone", "--bare", str(empty_repo), str(empty_bare)])
        self.loaded["store"] = {"name": "empty", "url": str(empty_bare)}
        with self.assertRaisesRegex(ValueError, "kb decrypt example"):
            kb_claude.build_context(self.loaded, "example", "latest.json")

    def test_start_executes_claude_with_a_new_session_and_context(self):
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--model", "sonnet"],
        })()
        with mock.patch.object(kb_claude.os, "execvp", side_effect=SystemExit) as execvp, \
             mock.patch.object(kb_claude.os, "chdir") as chdir, \
             self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        chdir.assert_called_once_with(self.root.resolve())
        command = execvp.call_args.args[1]
        self.assertEqual(command[0:2], ["claude", "--session-id"])
        self.assertIn("--append-system-prompt", command)
        self.assertIn("復号された知識", " ".join(command))
        self.assertIn("--model", command)

    def test_large_context_uses_private_prompt_file(self):
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--model", "sonnet"],
        })()
        context = "x" * (kb_claude.MAX_INLINE_CONTEXT + 1)
        observed = {}

        def run(command, context_path):
            observed["command"] = command
            observed["text"] = context_path.read_text()
            observed["mode"] = context_path.stat().st_mode & 0o777
            observed["parent_mode"] = context_path.parent.stat().st_mode & 0o777
            return 0

        with mock.patch.object(kb_claude, "build_context", return_value=context), \
             mock.patch.object(kb_claude, "_supports_system_prompt_file_option", return_value=True), \
             mock.patch.object(kb_claude, "_run_with_context_file", side_effect=run), \
             mock.patch.object(kb_claude.os, "chdir"), \
             self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        command = observed["command"]
        option_index = command.index(kb_claude.SYSTEM_PROMPT_FILE_OPTION)
        self.assertEqual(observed["text"], kb_claude.SYSTEM_PROMPT_PREAMBLE + context)
        self.assertEqual(observed["mode"], 0o600)
        self.assertEqual(observed["parent_mode"], 0o700)
        self.assertNotIn("--append-system-prompt", command)

    def test_large_context_fails_when_prompt_file_option_is_unsupported(self):
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--model", "sonnet"],
        })()
        with mock.patch.object(kb_claude, "build_context", return_value="x" * (kb_claude.MAX_INLINE_CONTEXT + 1)), \
             mock.patch.object(kb_claude, "_supports_system_prompt_file_option", return_value=False), \
             mock.patch.object(kb_claude.os, "execvp") as execvp:
            with self.assertRaisesRegex(RuntimeError, "append-system-prompt-file"):
                kb_claude.start(args, {"stores": [self.loaded["store"]]})
        execvp.assert_not_called()

    def test_large_context_resume_uses_file_and_disables_snapshot(self):
        sid = "11111111-2222-4333-8444-555555555555"
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root),
            "claude_args": ["--resume", sid, "--fork-session", "-p", "hi"],
        })()
        with mock.patch.object(kb_claude, "build_context", return_value="x" * (kb_claude.MAX_INLINE_CONTEXT + 1)), \
             mock.patch.object(kb_claude, "_supports_system_prompt_file_option", return_value=True), \
             mock.patch.object(kb_claude, "_supports_snapshot_option", return_value=True), \
             mock.patch.object(kb_claude, "_run_with_context_file", return_value=0) as run, \
             mock.patch.object(kb_claude.os, "chdir"), \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        command = run.call_args.args[0]
        self.assertNotIn("--session-id", command)
        self.assertEqual(command[1], kb_claude.SYSTEM_PROMPT_FILE_OPTION)
        self.assertEqual(command[3:], ["--system-prompt-snapshot", "off", "--resume", sid,
                                       "--fork-session", "-p", "hi"])

    def test_context_parent_symlink_is_rejected(self):
        cache = kb_store.CACHE
        cache.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside"
        target.mkdir()
        cache.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            kb_claude._make_private_parent(cache / "example" / "latest")

    def test_prompt_file_probe_requires_real_parser_rejection(self):
        accepted = subprocess.CompletedProcess([], 0, "help", "")
        rejected = subprocess.CompletedProcess([], 1, "", "invalid permission mode __kb_invalid_permission_probe__")
        with mock.patch.object(kb_claude.subprocess, "run", side_effect=[accepted, rejected]) as run:
            kb_claude._supports_system_prompt_file_option.cache_clear()
            self.assertTrue(kb_claude._supports_system_prompt_file_option())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-1], "__kb_invalid_permission_probe__")

    def test_context_file_is_removed_after_child_exit(self):
        context_path = self.root / "context.md"
        context_path.write_text("private")
        child = mock.Mock()
        child.wait.return_value = 17
        with mock.patch.object(kb_claude.subprocess, "Popen", return_value=child):
            self.assertEqual(kb_claude._run_with_context_file(["claude"], context_path), 17)
        self.assertFalse(context_path.exists())

        context_path.write_text("private")
        with mock.patch.object(kb_claude.subprocess, "Popen", side_effect=OSError("missing claude")):
            with self.assertRaises(OSError):
                kb_claude._run_with_context_file(["claude"], context_path)
        self.assertFalse(context_path.exists())

    def test_resume_reattaches_the_kb_without_a_new_session_id(self):
        for resume in (["--resume", "11111111-2222-4333-8444-555555555555"], ["-r", "x"], ["--continue"], ["-c"]):
            args = type("Args", (), {
                "name": "example", "file": "latest.json", "store": "test",
                "workspace": str(self.root), "claude_args": [*resume, "-p", "hi"],
            })()
            with mock.patch.object(kb_claude.os, "execvp", side_effect=SystemExit) as execvp, \
                 mock.patch.object(kb_claude.os, "chdir"), \
                 mock.patch.object(kb_claude, "_supports_snapshot_option", return_value=False), \
                 self.assertRaises(SystemExit):
                kb_claude.start(args, {"stores": [self.loaded["store"]]})
            command = execvp.call_args.args[1]
            self.assertNotIn("--session-id", command)  # Claude Code rejects it with --resume/--continue
            self.assertEqual(command[1], "--append-system-prompt")
            self.assertEqual(command[3:], [*resume, "-p", "hi"])

    def test_an_explicit_snapshot_choice_is_left_to_the_user(self):
        with mock.patch.object(kb_claude, "_supports_snapshot_option", return_value=True):
            self.assertEqual(kb_claude.fresh_system_prompt(["-c"]), ["--system-prompt-snapshot", "off"])
            self.assertEqual(kb_claude.fresh_system_prompt(["-c", "--system-prompt-snapshot=on"]), [])
        with mock.patch.object(kb_claude, "_supports_snapshot_option", return_value=False):
            self.assertEqual(kb_claude.fresh_system_prompt(["-c"]), [])

    def test_fork_reattaches_the_kb_and_lets_claude_create_the_new_session(self):
        sid = "11111111-2222-4333-8444-555555555555"
        for fork in (["--resume", sid, "--fork-session"], ["--continue", "--fork-session"]):
            args = type("Args", (), {
                "name": "example", "file": "latest.json", "store": "test",
                "workspace": str(self.root), "claude_args": [*fork, "-p", "hi"],
            })()
            with mock.patch.object(kb_claude.os, "execvp", side_effect=SystemExit) as execvp, \
                 mock.patch.object(kb_claude.os, "chdir"), \
                 mock.patch.object(kb_claude, "_supports_snapshot_option", return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout, \
                 self.assertRaises(SystemExit):
                kb_claude.start(args, {"stores": [self.loaded["store"]]})
            command = execvp.call_args.args[1]
            self.assertNotIn("--session-id", command)
            self.assertEqual(command[1], "--append-system-prompt")
            # The recorded system prompt of the source would otherwise hide the KB.
            self.assertEqual(command[3:], ["--system-prompt-snapshot", "off", *fork, "-p", "hi"])
            self.assertIn("新しいセッションを作成します (--fork-session)", stdout.getvalue())
